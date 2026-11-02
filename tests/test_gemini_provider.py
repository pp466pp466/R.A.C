"""llm-provider 規格：Gemini 轉接器的請求、回應解析、錯誤對應與金鑰處理。

用假傳輸驗證轉接器對請求與各種回應的處理（不連網）；真實的 `UrllibTransport` 則對本機的 HTTP 伺服器驗證
逾時與連線失敗的行為。
"""
import copy
import json
import socket
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.llm.gemini import DEFAULT_BASE_URL, KEY_HEADER, GeminiProvider, UrllibTransport, to_gemini_schema
from app.llm.types import (
    BLOCKED_INPUT, BLOCKED_OUTPUT, FINISH_LENGTH, FINISH_OTHER, FINISH_STOP, LLMBlockedError, LLMConfigError, LLMContent,
    LLMRequest, LLMTransientError,
)

KEY = "AIzaSy-fake-key-for-tests-0123456789"
SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["greeting", "unknown"]},
        "confidence": {"type": "number"},
        "entities": {
            "type": "object",
            "properties": {"topic": {"type": "string", "nullable": True, "maxLength": 50}},
            "required": ["topic"],
            "additionalProperties": False,
        },
        "suggested_actions": {
            "type": "array", "maxItems": 4, "minItems": 0,
            "items": {"type": "object", "properties": {"label": {"type": "string", "maxLength": 12}}, "required": ["label"]},
        },
    },
    "required": ["intent", "confidence", "entities", "suggested_actions"],
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


def ok_payload(text="{}", *, finish="STOP", usage=None, parts=None):
    return {
        "candidates": [{"content": {"parts": parts if parts is not None else [{"text": text}]}, "finishReason": finish}],
        "usageMetadata": usage if usage is not None else {"promptTokenCount": 11, "candidatesTokenCount": 22},
    }


def make_request(**overrides) -> LLMRequest:
    values = dict(
        model="gemini-test", system_instruction="你是客服。", contents=(LLMContent("user", ("資料", "問題")),),
        response_schema=SCHEMA, timeout_seconds=20.0, max_output_tokens=4096,
    )
    values.update(overrides)
    return LLMRequest(**values)


def provider(transport, **kwargs):
    ticks = iter(x * 0.25 for x in range(1, 100))            # 每次讀取時鐘前進 0.25 秒 → 呼叫耗時 250 毫秒
    return GeminiProvider(KEY, transport=transport, monotonic=lambda: next(ticks), **kwargs)


# ---- 請求 ----------------------------------------------------------------------------

def test_the_request_goes_to_the_model_endpoint_with_the_key_in_a_header_and_never_in_the_url():
    transport = FakeTransport(payload=ok_payload())
    provider(transport).generate(make_request(model="gemini-3.5-flash-lite"))
    call = transport.calls[0]
    assert call["url"] == f"{DEFAULT_BASE_URL}/models/gemini-3.5-flash-lite:generateContent"
    assert call["headers"][KEY_HEADER] == KEY
    assert KEY not in call["url"] and "key=" not in call["url"]
    assert call["timeout"] == 20.0


def test_the_body_carries_the_system_instruction_contents_and_the_json_output_settings():
    transport = FakeTransport(payload=ok_payload())
    provider(transport).generate(make_request())
    body = transport.calls[0]["body"]
    assert body["system_instruction"] == {"parts": [{"text": "你是客服。"}]}
    assert body["contents"] == [{"role": "user", "parts": [{"text": "資料"}, {"text": "問題"}]}]
    config = body["generationConfig"]
    assert config["responseMimeType"] == "application/json" and config["maxOutputTokens"] == 4096
    assert config["responseSchema"]["type"] == "OBJECT"
    assert "thinkingConfig" not in config                                       # 沒設定就不送，使用供應商預設
    assert "safetySettings" not in body                                         # 使用供應商預設，不自建過濾


def test_a_thinking_budget_of_zero_is_sent_and_is_not_confused_with_unset():
    transport = FakeTransport(payload=ok_payload())
    provider(transport).generate(make_request(thinking_budget=0))
    assert transport.calls[0]["body"]["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}


def test_without_a_schema_the_output_is_not_forced_to_json():
    transport = FakeTransport(payload=ok_payload("好"))
    response = provider(transport).generate(make_request(response_schema=None))
    config = transport.calls[0]["body"]["generationConfig"]
    assert "responseMimeType" not in config and "responseSchema" not in config
    assert response.text == "好"


def test_chinese_text_is_sent_as_is():
    transport = FakeTransport(payload=ok_payload())
    provider(transport).generate(make_request(contents=(LLMContent("user", ("忘記密碼怎麼辦？😀",)),)))
    assert transport.calls[0]["body"]["contents"][0]["parts"][0]["text"] == "忘記密碼怎麼辦？😀"


# ---- 結構描述轉換 -------------------------------------------------------------------------

def test_the_schema_is_converted_to_gemini_types_and_keeps_only_supported_keys():
    converted = to_gemini_schema(SCHEMA)
    assert converted["type"] == "OBJECT" and converted["required"][0] == "intent"
    assert converted["properties"]["intent"] == {"type": "STRING", "enum": ["greeting", "unknown"]}
    assert converted["properties"]["confidence"] == {"type": "NUMBER"}
    topic = converted["properties"]["entities"]["properties"]["topic"]
    assert topic == {"type": "STRING", "nullable": True}                        # maxLength 被丟掉，nullable 保留
    assert "additionalProperties" not in converted["properties"]["entities"]
    actions = converted["properties"]["suggested_actions"]
    assert (actions["type"], actions["maxItems"], actions["minItems"]) == ("ARRAY", 4, 0)
    assert actions["items"]["properties"]["label"] == {"type": "STRING"}


def test_a_type_list_with_null_becomes_a_nullable_type_and_the_input_is_not_modified():
    schema = {"type": ["string", "null"], "enum": ["a"]}
    original = copy.deepcopy(schema)
    assert to_gemini_schema(schema) == {"type": "STRING", "nullable": True, "enum": ["a"]}
    assert schema == original


# ---- 回應 ----------------------------------------------------------------------------

def test_a_successful_response_reports_the_used_model_usage_finish_reason_and_latency():
    usage = {"promptTokenCount": 182, "candidatesTokenCount": 531, "thoughtsTokenCount": 40}
    response = provider(FakeTransport(payload=ok_payload('{"a": 1}', usage=usage))).generate(make_request(model="m-fallback"))
    assert response.text == '{"a": 1}' and response.model == "m-fallback"
    assert (response.prompt_tokens, response.completion_tokens, response.thinking_tokens) == (182, 531, 40)
    assert response.finish_reason == FINISH_STOP and response.latency_ms == 250


def test_missing_thinking_tokens_are_none_not_zero():
    response = provider(FakeTransport(payload=ok_payload())).generate(make_request())
    assert response.thinking_tokens is None
    assert (response.prompt_tokens, response.completion_tokens) == (11, 22)


def test_missing_usage_metadata_gives_empty_counts():
    payload = {"candidates": [{"content": {"parts": [{"text": "x"}]}, "finishReason": "STOP"}]}
    response = provider(FakeTransport(payload=payload)).generate(make_request())
    assert (response.prompt_tokens, response.completion_tokens, response.thinking_tokens) == (None, None, None)


@pytest.mark.parametrize(("raw", "normalised"), [("STOP", FINISH_STOP), ("MAX_TOKENS", FINISH_LENGTH), ("RECITATION", FINISH_OTHER), ("", FINISH_OTHER)])
def test_finish_reasons_are_normalised(raw, normalised):
    response = provider(FakeTransport(payload=ok_payload("{", finish=raw))).generate(make_request())
    assert response.finish_reason == normalised


def test_thought_summaries_are_not_mixed_into_the_answer():
    parts = [{"text": "我在想……", "thought": True}, {"text": '{"ok": '}, {"text": "true}"}]
    assert provider(FakeTransport(payload=ok_payload(parts=parts))).generate(make_request()).text == '{"ok": true}'


def test_a_200_without_any_text_returns_an_empty_string_for_the_validator_to_reject():
    for payload in ({"candidates": []}, {}, {"candidates": [{"finishReason": "STOP"}]}, None):
        response = provider(FakeTransport(payload=payload)).generate(make_request())
        assert response.text == ""


def test_a_blocked_prompt_is_reported_as_an_input_block():
    payload = {"promptFeedback": {"blockReason": "SAFETY"}, "usageMetadata": {"promptTokenCount": 5}}
    with pytest.raises(LLMBlockedError) as raised:
        provider(FakeTransport(payload=payload)).generate(make_request())
    assert raised.value.phase == BLOCKED_INPUT and raised.value.latency_ms == 250


@pytest.mark.parametrize("finish", ["SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"])
def test_a_blocked_output_is_reported_as_an_output_block_even_if_some_text_came_back(finish):
    with pytest.raises(LLMBlockedError) as raised:
        provider(FakeTransport(payload=ok_payload("不能用的半截文字", finish=finish))).generate(make_request())
    assert raised.value.phase == BLOCKED_OUTPUT


# ---- 錯誤對應 ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("status", "kind"),
    [(429, "rate_limited"), (500, "server_error"), (502, "server_error"), (503, "server_error"), (504, "server_error"),
     (408, "timeout")],
)
def test_transient_statuses_map_to_transient_errors_with_the_latency(status, kind):
    with pytest.raises(LLMTransientError) as raised:
        provider(FakeTransport(status=status, payload={"error": {"message": "x"}})).generate(make_request())
    assert (raised.value.kind, raised.value.http_status, raised.value.latency_ms) == (kind, status, 250)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413])
def test_request_and_permission_errors_map_to_a_config_error(status):
    with pytest.raises(LLMConfigError) as raised:
        provider(FakeTransport(status=status, payload={"error": {"message": "API key not valid"}})).generate(make_request())
    assert raised.value.http_status == status


def test_an_unexpected_non_error_status_is_treated_as_a_server_problem():
    with pytest.raises(LLMTransientError) as raised:
        provider(FakeTransport(status=302, payload=None)).generate(make_request())
    assert raised.value.kind == "server_error"


def test_a_transport_timeout_and_a_connection_failure_map_to_timeout_and_network():
    with pytest.raises(LLMTransientError) as timeout:
        provider(FakeTransport(error=TimeoutError())).generate(make_request())
    assert timeout.value.kind == "timeout" and timeout.value.latency_ms == 250
    with pytest.raises(LLMTransientError) as network:
        provider(FakeTransport(error=ConnectionRefusedError())).generate(make_request())
    assert network.value.kind == "network"


# ---- 金鑰與供應商本文不外洩 ------------------------------------------------------------------------

def test_the_key_never_appears_in_the_repr_or_in_any_raised_error_even_if_the_provider_echoes_it_back():
    echo = {"error": {"message": f"Invalid key {KEY} in request", "details": [{"key": KEY}]}}
    candidates = [
        FakeTransport(status=400, payload=echo), FakeTransport(status=429, payload=echo), FakeTransport(status=500, payload=echo),
        FakeTransport(error=TimeoutError(KEY)), FakeTransport(error=OSError(KEY)),
    ]
    assert KEY not in repr(provider(FakeTransport()))
    for transport in candidates:
        with pytest.raises(Exception) as raised:
            provider(transport).generate(make_request())
        text = f"{raised.value!s} {raised.value!r} {raised.value.args}"
        assert KEY not in text and "Invalid key" not in text, type(raised.value).__name__


def test_the_provider_requires_a_key():
    with pytest.raises(ValueError):
        GeminiProvider("")


# ---- 真實傳輸（本機 HTTP 伺服器）-----------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):                       # 不要把請求印到測試輸出
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))   # HTTP 標頭不分大小寫
        mode = self.server.mode
        if mode == "slow":
            threading.Event().wait(1.5)
        status, payload = {"ok": (200, b'{"hello": "\xe4\xbd\xa0\xe5\xa5\xbd"}'), "503": (503, b'{"error": {"status": "UNAVAILABLE"}}'),
                           "html": (429, b"<html>too many</html>"), "slow": (200, b"{}")}[mode]
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except OSError:
            pass


@pytest.fixture()
def local_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.seen, server.mode = [], "ok"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_the_real_transport_posts_utf8_json_and_parses_the_reply(local_server):
    url = f"http://127.0.0.1:{local_server.server_port}/x"
    status, payload = UrllibTransport().post_json(url, {"Content-Type": "application/json", "x-test": "1"}, {"問": "你好"}, 5)
    assert (status, payload) == (200, {"hello": "你好"})
    path, headers, body = local_server.seen[0]
    assert json.loads(body.decode("utf-8")) == {"問": "你好"} and headers["x-test"] == "1"


def test_the_real_transport_returns_error_statuses_instead_of_raising(local_server):
    url = f"http://127.0.0.1:{local_server.server_port}/x"
    local_server.mode = "503"
    assert UrllibTransport().post_json(url, {}, {}, 5) == (503, {"error": {"status": "UNAVAILABLE"}})
    local_server.mode = "html"
    assert UrllibTransport().post_json(url, {}, {}, 5) == (429, None)           # 非 JSON 的本文不會讓它爆掉


def test_the_real_transport_raises_a_timeout_when_the_server_is_too_slow(local_server):
    local_server.mode = "slow"
    with pytest.raises(TimeoutError):
        UrllibTransport().post_json(f"http://127.0.0.1:{local_server.server_port}/x", {}, {}, 0.3)


@pytest.mark.parametrize(
    ("reason", "expected"),
    [(ConnectionRefusedError(), OSError), (socket.gaierror(), OSError), (socket.timeout(), TimeoutError), (TimeoutError(), TimeoutError)],
)
def test_the_real_transport_distinguishes_a_timeout_from_other_connection_failures(monkeypatch, reason, expected):
    """用假的 `urlopen` 讓失敗確定發生（Windows 對沒人聽的埠要約 2 秒才拒絕連線，真的去連會與逾時糾纏）。"""
    def failing_urlopen(*args, **kwargs):
        raise urllib.error.URLError(reason)

    monkeypatch.setattr(urllib.request, "urlopen", failing_urlopen)
    with pytest.raises(expected) as raised:
        UrllibTransport().post_json("https://example.invalid/x", {}, {}, 2)
    assert (isinstance(raised.value, TimeoutError)) == (expected is TimeoutError)
    assert "example.invalid" not in str(raised.value)                              # 不帶原始訊息（可能含網址與主機）


def test_the_provider_end_to_end_over_the_real_transport_maps_a_503(local_server):
    local_server.mode = "503"
    real = GeminiProvider(KEY, base_url=f"http://127.0.0.1:{local_server.server_port}/v1beta")
    with pytest.raises(LLMTransientError) as raised:
        real.generate(make_request(model="gemini-x"))
    assert raised.value.kind == "server_error"
    path, headers, _ = local_server.seen[0]
    assert path == "/v1beta/models/gemini-x:generateContent" and headers[KEY_HEADER.lower()] == KEY and KEY not in path


# ---- GEMINI_BASE_URL 讓 create_app 建立的轉接器改用其他端點（staging-deployment 3.1）-----------------------------


def test_create_app_points_the_gemini_adapter_at_the_configured_endpoint_and_calls_it():
    from app import create_app
    from app.config import load_config

    endpoint = "https://fake-llm.example.com/v1beta"
    app = create_app(load_config(environ={"APP_ENV": "testing", "GEMINI_API_KEY": KEY, "GEMINI_BASE_URL": endpoint + "/"}))
    provider = app.extensions["llm_provider"]
    provider._transport = FakeTransport(payload=ok_payload("好"))              # 假傳輸：不連網，只記下實際呼叫的網址
    provider.generate(make_request(model="gemini-x"))
    call = provider._transport.calls[0]
    assert call["url"] == f"{endpoint}/models/gemini-x:generateContent"
    assert call["headers"][KEY_HEADER] == KEY and KEY not in call["url"]


def test_create_app_without_the_override_uses_the_default_gemini_endpoint():
    from app import create_app
    from app.config import load_config

    app = create_app(load_config(environ={"APP_ENV": "testing", "GEMINI_API_KEY": KEY}))
    provider = app.extensions["llm_provider"]
    provider._transport = FakeTransport(payload=ok_payload("好"))
    provider.generate(make_request(model="gemini-x"))
    assert provider._transport.calls[0]["url"] == f"{DEFAULT_BASE_URL}/models/gemini-x:generateContent"
