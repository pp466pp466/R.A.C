"""llm-openai-compatible 規格：OpenAI 相容端點轉接器的請求、回應解析、錯誤對應、結構化輸出、零保存偏好與金鑰處理。

全部用假傳輸（不連網）。真實的傳輸與連線行為由 `tests/test_gemini_provider.py` 對 `UrllibTransport` 的測試涵蓋（兩個轉接器共用它）。
"""
import copy
import json
import logging

import pytest

from app.llm.openai_compatible import AUTH_HEADER, DEFAULT_BASE_URL, PROVIDER_NAME, OpenAICompatibleProvider
from app.llm.transport import STREAM_EOF, StreamHead
from app.llm.types import (
    BLOCKED_OUTPUT, FINISH_LENGTH, FINISH_OTHER, FINISH_STOP, LLMBlockedError, LLMConfigError, LLMContent, LLMRequest,
    LLMTransientError, StreamEnd, StreamText, StreamTick,
)

KEY = "sk-or-v1-fake-key-for-tests-0123456789"
ECHO = "回聲：使用者的私人內容 SECRET-ECHO"
SCHEMA = {
    "type": "object",
    "properties": {"intent": {"type": "string", "enum": ["greeting", "unknown"]}, "confidence": {"type": "number"}},
    "required": ["intent", "confidence"],
}


class FakeTransport:
    def __init__(self, status=200, payload=None, error: Exception | None = None):
        self.status, self.payload, self.error = status, payload, error
        self.calls: list[dict] = []

    def post_json(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": dict(headers), "body": copy.deepcopy(body), "timeout": timeout})
        if self.error:
            raise self.error
        return self.status, self.payload


def ok_payload(text="{}", *, finish="stop", usage=None, extra=None):
    payload = {
        "id": "gen-1", "model": "vendor/some-model",
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": finish}],
        "usage": usage if usage is not None else {"prompt_tokens": 11, "completion_tokens": 22},
    }
    payload.update(extra or {})
    return payload


def make_request(**overrides) -> LLMRequest:
    values = dict(
        model="vendor/some-model:free", system_instruction="你是敘事者。",
        contents=(LLMContent("user", ("資料", "問題")), LLMContent("model", ("上一句回覆",)), LLMContent("user", ("再來一句",))),
        response_schema=None, timeout_seconds=20.0, max_output_tokens=2048,
    )
    values.update(overrides)
    return LLMRequest(**values)


def provider(transport, **kwargs):
    ticks = iter(x * 0.25 for x in range(1, 100))            # 每次讀取時鐘前進 0.25 秒 → 呼叫耗時 250 毫秒
    return OpenAICompatibleProvider(KEY, transport=transport, monotonic=lambda: next(ticks), **kwargs)


# ---- 1.2 請求 ------------------------------------------------------------------------

def test_the_request_goes_to_chat_completions_with_the_key_in_the_authorization_header_and_never_in_the_url():
    transport = FakeTransport(payload=ok_payload())
    provider(transport).generate(make_request())
    call = transport.calls[0]
    assert call["url"] == f"{DEFAULT_BASE_URL}/chat/completions"
    assert call["headers"][AUTH_HEADER] == f"Bearer {KEY}"
    assert KEY not in call["url"] and "key=" not in call["url"]
    assert call["timeout"] == 20.0


def test_the_body_has_a_system_message_then_user_and_assistant_messages_with_the_callers_model_and_limit():
    transport = FakeTransport(payload=ok_payload())
    provider(transport).generate(make_request())
    body = transport.calls[0]["body"]
    assert body["model"] == "vendor/some-model:free" and body["max_tokens"] == 2048 and body["stream"] is False
    assert body["messages"] == [
        {"role": "system", "content": "你是敘事者。"},
        {"role": "user", "content": "資料\n\n問題"},           # 同一個內容區塊的多個片段依序合併
        {"role": "assistant", "content": "上一句回覆"},         # 中立的 model 角色 → assistant
        {"role": "user", "content": "再來一句"},
    ]


def test_a_different_base_url_is_used_as_is_with_a_trailing_slash_ignored():
    transport = FakeTransport(payload=ok_payload())
    provider(transport, base_url="http://localhost:11434/v1/").generate(make_request())
    assert transport.calls[0]["url"] == "http://localhost:11434/v1/chat/completions"
    assert transport.calls[0]["body"]["messages"][0]["role"] == "system"          # 其餘格式相同


def test_the_provider_name_fits_the_ledger_column_and_the_key_is_never_shown_in_its_string_forms():
    from app.models.llm_call_log import LlmCallLog

    assert len(PROVIDER_NAME) <= LlmCallLog.provider.type.length
    adapter = provider(FakeTransport(payload=ok_payload()))
    assert KEY not in repr(adapter) and KEY not in str(adapter) and "***" in repr(adapter)


def test_an_empty_key_is_rejected_at_construction():
    with pytest.raises(ValueError):
        OpenAICompatibleProvider("")


# ---- 1.3 回應與錯誤 ------------------------------------------------------------------

def test_a_normal_answer_reports_text_the_requested_model_tokens_latency_and_a_normalised_finish_reason():
    response = provider(FakeTransport(payload=ok_payload("好的。"))).generate(make_request())
    assert response.text == "好的。"
    assert response.model == "vendor/some-model:free"             # 回報呼叫端要求的名稱：帳本與斷路都以它為鍵
    assert response.finish_reason == FINISH_STOP
    assert (response.prompt_tokens, response.completion_tokens, response.latency_ms) == (11, 22, 250)
    assert response.thinking_tokens is None                        # 供應商沒有提供時是空值，不是 0


def test_reasoning_tokens_are_reported_when_the_provider_gives_them():
    usage = {"prompt_tokens": 5, "completion_tokens": 9, "completion_tokens_details": {"reasoning_tokens": 4}}
    assert provider(FakeTransport(payload=ok_payload(usage=usage))).generate(make_request()).thinking_tokens == 4


@pytest.mark.parametrize("usage", [{}, None, "x", {"prompt_tokens": "11", "completion_tokens": True}])
def test_missing_or_malformed_usage_gives_empty_token_counts_and_does_not_fail_the_call(usage):
    payload = ok_payload()
    if usage is None:
        del payload["usage"]
    else:
        payload["usage"] = usage
    response = provider(FakeTransport(payload=payload)).generate(make_request())
    assert response.prompt_tokens is None and response.completion_tokens is None and response.thinking_tokens is None


@pytest.mark.parametrize(
    "finish, expected",
    [("stop", FINISH_STOP), ("length", FINISH_LENGTH), ("tool_calls", FINISH_OTHER), ("", FINISH_OTHER), (None, FINISH_OTHER)],
)
def test_finish_reasons_are_normalised(finish, expected):
    assert provider(FakeTransport(payload=ok_payload(finish=finish))).generate(make_request()).finish_reason == expected


def test_a_null_or_missing_content_is_an_empty_answer_not_a_crash():
    payload = ok_payload()
    payload["choices"][0]["message"]["content"] = None
    assert provider(FakeTransport(payload=payload)).generate(make_request()).text == ""
    payload["choices"][0]["message"] = None
    assert provider(FakeTransport(payload=payload)).generate(make_request()).text == ""


def test_a_content_filtered_ending_is_an_output_block_even_when_there_is_text():
    with pytest.raises(LLMBlockedError) as caught:
        provider(FakeTransport(payload=ok_payload("部分文字", finish="content_filter"))).generate(make_request())
    assert caught.value.phase == BLOCKED_OUTPUT and caught.value.latency_ms == 250


@pytest.mark.parametrize("status, kind", [(429, "rate_limited"), (408, "timeout"), (500, "server_error"), (502, "server_error"), (503, "server_error")])
def test_transient_statuses_map_to_transient_errors(status, kind):
    with pytest.raises(LLMTransientError) as caught:
        provider(FakeTransport(status=status, payload={"error": {"message": ECHO}})).generate(make_request())
    assert (caught.value.kind, caught.value.http_status, caught.value.latency_ms) == (kind, status, 250)


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404])
def test_request_and_account_problems_are_configuration_errors_to_fix_by_hand(status):
    """402 是餘額不足、404 是模型下架或沒有可用端點：切換備援也不會好，所以不是暫時性錯誤。"""
    with pytest.raises(LLMConfigError) as caught:
        provider(FakeTransport(status=status, payload={"error": {"message": ECHO}})).generate(make_request())
    assert caught.value.http_status == status and caught.value.latency_ms == 250


@pytest.mark.parametrize("exc, kind", [(TimeoutError(), "timeout"), (OSError("boom"), "network"), (ConnectionResetError(), "network")])
def test_timeouts_and_connection_failures_are_transient(exc, kind):
    with pytest.raises(LLMTransientError) as caught:
        provider(FakeTransport(error=exc)).generate(make_request())
    assert caught.value.kind == kind and caught.value.latency_ms == 250


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"error": {"code": 429, "message": ECHO}}, ("transient", "rate_limited")),
        ({"error": {"code": 404, "message": ECHO}}, ("config", None)),
        ({"error": {"code": 502, "message": ECHO}}, ("transient", "server_error")),
        ({"error": {"code": "weird", "message": ECHO}}, ("transient", "server_error")),     # 代碼不是整數：無法判斷
        ({"error": ECHO}, ("transient", "server_error")),
        ({}, ("transient", "server_error")),                                                   # 沒有結果也沒有錯誤
        ({"choices": []}, ("transient", "server_error")),
        (None, ("transient", "server_error")),                                                 # 本文不是 JSON
        ("not a dict", ("transient", "server_error")),
    ],
)
def test_a_200_without_a_result_is_classified_by_its_error_code_or_treated_as_transient(payload, expected):
    with pytest.raises((LLMTransientError, LLMConfigError)) as caught:
        provider(FakeTransport(status=200, payload=payload)).generate(make_request())
    family, kind = expected
    assert isinstance(caught.value, LLMConfigError if family == "config" else LLMTransientError)
    if kind:
        assert caught.value.kind == kind


def test_a_choice_that_ended_with_an_error_is_transient():
    with pytest.raises(LLMTransientError) as caught:
        provider(FakeTransport(payload=ok_payload("", finish="error"))).generate(make_request())
    assert caught.value.kind == "server_error"


def test_failures_carry_no_provider_body_and_no_key_anywhere(caplog):
    """供應商的錯誤本文可能回聲使用者的內容：它不能出現在例外、日誌或任何屬性裡。"""
    caplog.set_level(logging.DEBUG)
    for status in (429, 500, 400, 402):
        with pytest.raises((LLMTransientError, LLMConfigError)) as caught:
            provider(FakeTransport(status=status, payload={"error": {"message": ECHO}, "echo": ECHO})).generate(make_request())
        error = caught.value
        dumped = f"{error!s} {error!r} {vars(error)!r} {error.__cause__!r} {error.__context__!r}"
        assert "SECRET-ECHO" not in dumped and KEY not in dumped
    assert "SECRET-ECHO" not in caplog.text and KEY not in caplog.text


# ---- 1.3a 結構化輸出的退化 -------------------------------------------------------------

def test_by_default_no_structured_output_parameter_is_sent_and_the_schema_goes_into_the_system_instruction():
    transport = FakeTransport(payload=ok_payload())
    provider(transport).generate(make_request(response_schema=SCHEMA))
    body = transport.calls[0]["body"]
    assert "response_format" not in body
    system = body["messages"][0]["content"]
    assert system.startswith("你是敘事者。") and "只輸出" in system and "JSON" in system
    assert json.dumps(SCHEMA, ensure_ascii=False) in system            # 結構描述原樣附上
    assert body["messages"][1]["role"] == "user"                       # 其餘訊息不受影響


def test_when_the_model_is_declared_to_support_schemas_the_parameter_is_sent_and_the_instruction_is_untouched():
    transport = FakeTransport(payload=ok_payload())
    provider(transport, supports_json_schema=True).generate(make_request(response_schema=SCHEMA))
    body = transport.calls[0]["body"]
    assert body["response_format"] == {"type": "json_schema", "json_schema": {"name": "response", "schema": SCHEMA}}
    assert body["messages"][0]["content"] == "你是敘事者。"


def test_a_plain_text_request_gets_neither_the_parameter_nor_the_added_instruction():
    for supports in (False, True):
        transport = FakeTransport(payload=ok_payload())
        provider(transport, supports_json_schema=supports).generate(make_request(response_schema=None))
        body = transport.calls[0]["body"]
        assert "response_format" not in body and body["messages"][0]["content"] == "你是敘事者。"


def test_the_adapter_never_assumes_the_output_matches_the_schema():
    """轉接器只回傳文字：模型不聽話、輸出不是 JSON 時它不會自己驗證或修補（驗證由呼叫端的 `parse_llm_json` 負責）。"""
    response = provider(FakeTransport(payload=ok_payload("好的，這是答案：{oops"))).generate(make_request(response_schema=SCHEMA))
    assert response.text == "好的，這是答案：{oops"


# ---- 1.4 資料保存偏好 ------------------------------------------------------------------

def test_zero_retention_adds_the_routing_preference_to_every_request_including_the_fallback_model():
    transport = FakeTransport(payload=ok_payload())
    adapter = provider(transport, zero_retention=True)
    adapter.generate(make_request(model="vendor/primary"))
    adapter.generate(make_request(model="vendor/fallback"))
    assert [c["body"]["provider"] for c in transport.calls] == [{"zdr": True, "data_collection": "deny"}] * 2
    assert [c["body"]["model"] for c in transport.calls] == ["vendor/primary", "vendor/fallback"]


def test_without_the_setting_no_routing_preference_is_sent():
    transport = FakeTransport(payload=ok_payload())
    provider(transport).generate(make_request())
    assert "provider" not in transport.calls[0]["body"]


@pytest.mark.parametrize("status", [404, 400, 403])
def test_when_no_endpoint_matches_the_preference_the_call_fails_as_a_config_error_and_is_never_resent_without_it(status):
    transport = FakeTransport(status=status, payload={"error": {"message": "No endpoints found matching your data policy"}})
    with pytest.raises(LLMConfigError):
        provider(transport, zero_retention=True).generate(make_request())
    assert len(transport.calls) == 1                               # 沒有第二次請求
    assert transport.calls[0]["body"]["provider"]["zdr"] is True


# ---- 1.5 免費與付費只差設定 -----------------------------------------------------------

def test_a_free_and_a_paid_model_name_produce_identical_requests_apart_from_the_model_string():
    free, paid = FakeTransport(payload=ok_payload()), FakeTransport(payload=ok_payload())
    provider(free).generate(make_request(model="vendor/some-model:free", response_schema=SCHEMA))
    provider(paid).generate(make_request(model="vendor/some-model", response_schema=SCHEMA))
    free_call, paid_call = free.calls[0], paid.calls[0]
    assert free_call["body"].pop("model") == "vendor/some-model:free" and paid_call["body"].pop("model") == "vendor/some-model"
    assert free_call == paid_call


def test_error_handling_does_not_depend_on_the_free_marker_either():
    for model in ("vendor/some-model:free", "vendor/some-model"):
        with pytest.raises(LLMTransientError) as caught:
            provider(FakeTransport(status=429, payload={})).generate(make_request(model=model))
        assert caught.value.kind == "rate_limited"


# ---- 2.2 串流 ------------------------------------------------------------------------------

class FakeRaw:
    """腳本化的上游：項目是 `StreamHead`、bytes 行、None（沉默一個 tick）、`STREAM_EOF` 或要拋出的例外。"""

    def __init__(self, items):
        self._items = list(items)
        self.closed = False
        self.polls = 0

    def poll(self, timeout):
        if self.closed:
            return STREAM_EOF
        self.polls += 1
        if not self._items:
            return STREAM_EOF
        item = self._items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        self.closed = True


class FakeStreamTransport:
    def __init__(self, items=(), error: Exception | None = None):
        self.items, self.error = list(items), error
        self.calls: list[dict] = []
        self.raws: list[FakeRaw] = []

    def open_stream(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": dict(headers), "body": copy.deepcopy(body), "timeout": timeout})
        if self.error:
            raise self.error
        raw = FakeRaw(self.items)
        self.raws.append(raw)
        return raw


def data(obj) -> bytes:
    return b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8")


def delta(content=None, *, reasoning=None, finish=None) -> bytes:
    inner = {}
    if content is not None:
        inner["content"] = content
    if reasoning is not None:
        inner["reasoning"] = reasoning
    return data({"choices": [{"delta": inner, "finish_reason": finish}]})


USAGE_EVENT = data({"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 340, "completion_tokens_details": {"reasoning_tokens": 7}}})


def texts(events) -> list[str]:
    return [event.text for event in events if isinstance(event, StreamText)]


def normal_stream(*parts) -> list:
    return [StreamHead(200), *[delta(part) for part in parts], delta(finish="stop"), USAGE_EVENT, b"data: [DONE]", STREAM_EOF]


def test_a_stream_yields_text_events_in_order_then_one_end_event_with_usage_finish_reason_and_model():
    events = list(provider(FakeStreamTransport(normal_stream("清晨的", "海風。"))).stream(make_request()))
    assert texts(events) == ["清晨的", "海風。"]
    assert isinstance(events[-1], StreamEnd) and sum(isinstance(e, StreamEnd) for e in events) == 1
    end = events[-1]
    assert (end.finish_reason, end.model) == (FINISH_STOP, "vendor/some-model:free")
    assert (end.prompt_tokens, end.completion_tokens, end.thinking_tokens) == (120, 340, 7)


def test_the_stream_request_asks_for_sse_with_usage_and_keeps_the_key_in_the_header_only():
    transport = FakeStreamTransport(normal_stream("好"))
    list(provider(transport).stream(make_request(timeout_seconds=33.0)))
    call = transport.calls[0]
    assert call["url"] == f"{DEFAULT_BASE_URL}/chat/completions" and call["timeout"] == 33.0
    assert call["headers"][AUTH_HEADER] == f"Bearer {KEY}" and call["headers"]["Accept"] == "text/event-stream"
    assert KEY not in call["url"]
    body = call["body"]
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True} and body["max_tokens"] == 2048
    assert [m["role"] for m in body["messages"]] == ["system", "user", "assistant", "user"]


def test_a_keepalive_comment_is_ignored_and_produces_no_text_event():
    transport = FakeStreamTransport([StreamHead(200), b": OPENROUTER PROCESSING", delta("好"), delta(finish="stop"), b"data: [DONE]", STREAM_EOF])
    events = list(provider(transport).stream(make_request()))
    assert texts(events) == ["好"] and StreamTick() in events           # 忽略，但喚醒外層檢查期限


def test_reasoning_fragments_and_empty_fragments_are_never_text_only_ticks():
    transport = FakeStreamTransport([
        StreamHead(200), delta(reasoning="先想一想……"), delta(None, reasoning="再想"), delta(""), delta("答案"), delta(finish="stop"),
        b"data: [DONE]", STREAM_EOF,
    ])
    events = list(provider(transport).stream(make_request()))
    assert texts(events) == ["答案"]
    first_text = next(i for i, e in enumerate(events) if isinstance(e, StreamText))
    assert first_text >= 3 and all(isinstance(e, StreamTick) for e in events[:first_text])      # 首字之前只有 tick


def test_silence_from_the_upstream_becomes_idle_ticks_and_the_connection_stays_open():
    transport = FakeStreamTransport([StreamHead(200), None, None, delta("好"), delta(finish="stop"), b"data: [DONE]", STREAM_EOF])
    events = list(provider(transport).stream(make_request()))
    assert events[:2] == [StreamTick(), StreamTick()] and texts(events) == ["好"]


def test_closing_midstream_closes_the_upstream_and_nothing_is_read_afterwards():
    transport = FakeStreamTransport(normal_stream("一", "二", "三"))
    handle = provider(transport).stream(make_request())
    iterator = iter(handle)
    assert next(iterator) == StreamText("一")
    raw = transport.raws[0]
    polls = raw.polls
    handle.close()
    handle.close()                                                       # 可重複呼叫
    assert raw.closed and list(iterator) == [] and raw.polls == polls


def test_closing_before_iterating_closes_the_upstream_too():
    transport = FakeStreamTransport(normal_stream("一"))
    handle = provider(transport).stream(make_request())
    handle.close()
    assert transport.raws[0].closed and list(handle) == []


@pytest.mark.parametrize("status, kind", [(429, "rate_limited"), (500, "server_error"), (503, "server_error"), (408, "timeout")])
def test_an_http_error_before_the_first_event_maps_like_the_non_streaming_call(status, kind):
    transport = FakeStreamTransport([StreamHead(status), STREAM_EOF])
    with pytest.raises(LLMTransientError) as caught:
        list(provider(transport).stream(make_request()))
    assert (caught.value.kind, caught.value.http_status) == (kind, status) and caught.value.latency_ms >= 0


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404])
def test_account_and_request_problems_are_configuration_errors_in_streaming_too(status):
    with pytest.raises(LLMConfigError) as caught:
        list(provider(FakeStreamTransport([StreamHead(status), STREAM_EOF])).stream(make_request()))
    assert caught.value.http_status == status


def test_a_stream_that_stops_for_length_with_no_text_is_a_transient_failure_not_an_empty_success():
    transport = FakeStreamTransport([StreamHead(200), delta(reasoning="想"), delta(finish="length"), USAGE_EVENT, b"data: [DONE]", STREAM_EOF])
    with pytest.raises(LLMTransientError) as caught:
        list(provider(transport).stream(make_request()))
    assert caught.value.kind == "server_error"


def test_a_stream_that_stops_for_length_with_text_ends_normally_with_the_length_reason():
    transport = FakeStreamTransport([StreamHead(200), delta("被截斷的"), delta(finish="length"), USAGE_EVENT, b"data: [DONE]", STREAM_EOF])
    events = list(provider(transport).stream(make_request()))
    assert texts(events) == ["被截斷的"] and events[-1].finish_reason == FINISH_LENGTH


def test_a_content_filtered_ending_is_an_output_block_in_streaming_too():
    transport = FakeStreamTransport([StreamHead(200), delta("部分"), delta(finish="content_filter"), b"data: [DONE]", STREAM_EOF])
    with pytest.raises(LLMBlockedError) as caught:
        list(provider(transport).stream(make_request()))
    assert caught.value.phase == BLOCKED_OUTPUT


def test_a_stream_that_ends_without_done_or_a_finish_reason_is_a_dropped_connection():
    transport = FakeStreamTransport([StreamHead(200), delta("說到一半"), STREAM_EOF])
    with pytest.raises(LLMTransientError) as caught:
        list(provider(transport).stream(make_request()))
    assert caught.value.kind == "network"


def test_a_stream_with_a_finish_reason_but_no_done_marker_is_still_complete_and_missing_usage_is_empty_not_zero():
    transport = FakeStreamTransport([StreamHead(200), delta("好"), delta(finish="stop"), STREAM_EOF])
    events = list(provider(transport).stream(make_request()))
    assert texts(events) == ["好"] and isinstance(events[-1], StreamEnd)
    assert (events[-1].prompt_tokens, events[-1].completion_tokens, events[-1].thinking_tokens) == (None, None, None)


@pytest.mark.parametrize(
    "event, expected",
    [
        ({"error": {"code": 502, "message": ECHO}}, LLMTransientError),
        ({"error": {"code": 429, "message": ECHO}}, LLMTransientError),
        ({"error": {"code": 402, "message": ECHO}}, LLMConfigError),
        ({"error": ECHO}, LLMTransientError),                                   # 沒有代碼：無法判斷，當作暫時性
        ({"choices": [{"delta": {}, "finish_reason": "error"}]}, LLMTransientError),
    ],
)
def test_an_error_event_midstream_is_classified_without_keeping_the_provider_text(event, expected):
    transport = FakeStreamTransport([StreamHead(200), delta("開頭"), data(event), b"data: [DONE]", STREAM_EOF])
    with pytest.raises(expected) as caught:
        list(provider(transport).stream(make_request()))
    error = caught.value
    dumped = f"{error!s} {error!r} {vars(error)!r} {error.__cause__!r} {error.__context__!r}"
    assert "SECRET-ECHO" not in dumped and KEY not in dumped


@pytest.mark.parametrize("failure, kind", [(TimeoutError(), "timeout"), (OSError("連線失敗"), "network")])
def test_a_transport_failure_during_the_stream_is_transient_and_closes_the_upstream(failure, kind):
    transport = FakeStreamTransport([StreamHead(200), delta("好"), failure])
    iterator = iter(provider(transport).stream(make_request()))
    assert next(iterator) == StreamText("好")
    with pytest.raises(LLMTransientError) as caught:
        list(iterator)
    assert caught.value.kind == kind and transport.raws[0].closed


@pytest.mark.parametrize("failure, kind", [(TimeoutError(), "timeout"), (OSError("x"), "network")])
def test_a_failure_to_open_the_stream_is_transient(failure, kind):
    with pytest.raises(LLMTransientError) as caught:
        provider(FakeStreamTransport(error=failure)).stream(make_request())
    assert caught.value.kind == kind


def test_malformed_or_unrelated_lines_are_skipped_without_ending_the_stream():
    transport = FakeStreamTransport([
        StreamHead(200), b"event: message", b"data: {oops", b"data: [1, 2]", b"", delta("好"), delta(finish="stop"), b"data: [DONE]", STREAM_EOF,
    ])
    assert texts(list(provider(transport).stream(make_request()))) == ["好"]


def test_stream_failures_carry_no_key_and_no_provider_body(caplog):
    caplog.set_level(logging.DEBUG)
    for status in (429, 500, 402):
        with pytest.raises((LLMTransientError, LLMConfigError)) as caught:
            list(provider(FakeStreamTransport([StreamHead(status), STREAM_EOF])).stream(make_request()))
        error = caught.value
        assert KEY not in f"{error!s} {error!r} {vars(error)!r}"
    assert KEY not in caplog.text and "SECRET-ECHO" not in caplog.text


# ---- 2.2 推理設定 ----------------------------------------------------------------------------

def test_by_default_every_request_asks_for_no_reasoning_for_both_models_streaming_or_not():
    transport = FakeTransport(payload=ok_payload())
    adapter = provider(transport)
    adapter.generate(make_request(model="vendor/primary"))
    adapter.generate(make_request(model="vendor/fallback"))
    stream_transport = FakeStreamTransport(normal_stream("好"))
    list(provider(stream_transport).stream(make_request(model="vendor/primary")))
    assert [c["body"]["reasoning"] for c in transport.calls] == [{"enabled": False}] * 2
    assert stream_transport.calls[0]["body"]["reasoning"] == {"enabled": False}


def test_when_reasoning_is_enabled_no_reasoning_parameter_is_sent_at_all():
    transport = FakeTransport(payload=ok_payload())
    provider(transport, reasoning=True).generate(make_request())
    stream_transport = FakeStreamTransport(normal_stream("好"))
    list(provider(stream_transport, reasoning=True).stream(make_request()))
    assert "reasoning" not in transport.calls[0]["body"] and "reasoning" not in stream_transport.calls[0]["body"]


def test_zero_retention_and_the_reasoning_parameter_are_sent_together_and_do_not_disturb_each_other():
    stream_transport = FakeStreamTransport(normal_stream("好"))
    list(provider(stream_transport, zero_retention=True).stream(make_request()))
    body = stream_transport.calls[0]["body"]
    assert body["provider"] == {"zdr": True, "data_collection": "deny"} and body["reasoning"] == {"enabled": False}


# ---- 2.2 長度且正文為空（非串流）---------------------------------------------------------------

def test_a_non_streaming_answer_cut_by_length_with_no_text_is_transient_so_the_fallback_model_can_run():
    for content in ("", None):
        payload = ok_payload("", finish="length")
        payload["choices"][0]["message"]["content"] = content
        with pytest.raises(LLMTransientError) as caught:
            provider(FakeTransport(payload=payload)).generate(make_request())
        assert caught.value.kind == "server_error" and caught.value.latency_ms == 250


def test_a_non_streaming_answer_cut_by_length_with_text_is_returned_with_the_length_reason():
    response = provider(FakeTransport(payload=ok_payload("半截的 JSON {", finish="length"))).generate(make_request())
    assert response.text == "半截的 JSON {" and response.finish_reason == FINISH_LENGTH
