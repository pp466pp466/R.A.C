"""service-observability 規格：模型呼叫的結構化日誌——只有分類與數字，斷路與設定錯誤可被察覺。"""
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from app.llm.gemini import GeminiProvider
from app.llm.types import LLMBlockedError, LLMConfigError, LLMContent, LLMTransientError
from app.observability.json_logging import ALLOWED_FIELDS
from app.observability.llm_events import EVENT_BREAKER_OPENED, EVENT_CALL
from app.repositories.llm_call_log_repository import LlmCallLogRepository
from app.services.llm_output import SemanticRule
from app.services.llm_service import LlmService
from tests.support.auth import add_user
from tests.support.config_env import FAKE_GEMINI_KEY
from tests.support.llm import FakeLLMProvider, Step
from tests.test_llm_service import FALLBACK, PRIMARY, SETTINGS, Wall, build, run, seed

SECRET_PROMPT = "使用者私密內容ABC123"
SECRET_REPLY = "模型回覆私密內容XYZ789"
BASE_FIELDS = {"time", "level", "logger", "message"}
GOOD_REPLY = json.dumps({"reply": SECRET_REPLY}, ensure_ascii=False)


def calls(lines):
    return [line for line in lines if line.get("event") == EVENT_CALL]


def breaker_events(lines):
    return [line for line in lines if line.get("event") == EVENT_BREAKER_OPENED]


def run_secret(service, **extra):
    return service.run_structured(
        kind="chat", user_id=extra.pop("user_id", None), conversation_id=extra.pop("conversation_id", None),
        system_instruction=SECRET_PROMPT, contents=(LLMContent("user", (SECRET_PROMPT,)),),
        schema={"type": "object", "required": ["reply"], "properties": {"reply": {"type": "string"}}},
        rules=(SemanticRule("reply_not_empty", lambda v: v["reply"] != ""),),
    )


# ---- 每次呼叫一筆，欄位限於白名單 -----------------------------------------------------------


def test_a_successful_call_logs_one_line_with_the_documented_fields(member_session, log_lines):
    user = add_user(member_session, 1).id
    provider = FakeLLMProvider().script(Step(GOOD_REPLY, seconds=2.5, prompt_tokens=1200, completion_tokens=420, thinking_tokens=80))
    run_secret(build(member_session, provider), user_id=user, conversation_id=9)
    [line] = calls(log_lines())
    assert line["level"] == "INFO" and line["message"] == "模型呼叫"
    assert {k: line[k] for k in line if k not in BASE_FIELDS} == {
        "event": "llm.call", "user_id": user, "conversation_id": 9, "kind": "chat", "provider": "fake", "model": PRIMARY,
        "attempt": 1, "outcome": "ok", "latency_ms": 2500, "prompt_tokens": 1200, "completion_tokens": 420,
        "thinking_tokens": 80, "finish_reason": "stop",
    }


def test_every_key_of_every_line_is_in_the_whitelist(member_session, log_lines):
    provider = FakeLLMProvider().script(Step("壞", seconds=25), Step(LLMTransientError("server_error", http_status=503)))
    run_secret(build(member_session, provider))
    for line in log_lines():
        assert set(line) <= BASE_FIELDS | set(ALLOWED_FIELDS), set(line) - BASE_FIELDS - set(ALLOWED_FIELDS)


def test_a_missing_thinking_token_count_is_left_out_not_logged_as_zero(member_session, log_lines):
    run_secret(build(member_session, FakeLLMProvider().script(GOOD_REPLY)))
    assert "thinking_tokens" not in calls(log_lines())[0]


def test_each_call_of_a_retry_sequence_gets_its_own_line_with_its_attempt_and_model(member_session, log_lines):
    provider = FakeLLMProvider().script(Step(LLMTransientError("rate_limited", http_status=429), seconds=0.3), GOOD_REPLY)
    run_secret(build(member_session, provider))
    assert [(c["attempt"], c["model"], c["outcome"]) for c in calls(log_lines())] == [
        (1, PRIMARY, "rate_limited"), (2, FALLBACK, "ok"),
    ]


def test_a_validation_failure_logs_only_the_classification_never_the_text(member_session, log_lines):
    provider = FakeLLMProvider().script(SECRET_REPLY + " 這不是 JSON", '{"reply": ""}')
    run_secret(build(member_session, provider))
    first, second = calls(log_lines())
    assert (first["level"], first["outcome"], first["reason"], first["error_type"]) == ("WARNING", "validation_failed", "not_json", "LLMValidationError")
    assert second["reason"] == "semantic:reply_not_empty"
    assert SECRET_REPLY not in log_lines.raw() and SECRET_PROMPT not in log_lines.raw()


def test_a_provider_error_logs_the_error_type_and_status_code_but_no_body(member_session, log_lines):
    provider = FakeLLMProvider().script(Step(LLMTransientError("server_error", http_status=503)), Step(LLMTransientError("timeout")))
    run_secret(build(member_session, provider))
    first, second = calls(log_lines())
    assert (first["level"], first["outcome"], first["provider_status"], first["error_type"]) == ("WARNING", "server_error", 503, "LLMTransientError")
    assert "provider_status" not in second and second["outcome"] == "timeout"


def test_a_blocked_call_is_informational_and_names_the_phase_through_the_finish_reason(member_session, log_lines):
    run_secret(build(member_session, FakeLLMProvider().script(Step(LLMBlockedError("output")))))
    [line] = calls(log_lines())
    assert (line["level"], line["outcome"], line["finish_reason"], line["error_type"]) == ("INFO", "blocked", "safety", "LLMBlockedError")


# ---- 設定錯誤：錯誤層級，不含金鑰 ----------------------------------------------------------


class EchoingTransport:
    """模擬供應商回 403 並在本文回聲請求內容與金鑰：轉接器與服務都不得讓它進入日誌或帳本。"""

    def post_json(self, url, headers, body, timeout):
        echo = json.dumps(body, ensure_ascii=False) + " " + " ".join(headers.values())
        return 403, {"error": {"message": f"API key not valid: {FAKE_GEMINI_KEY}. {echo}"}}


def test_a_configuration_error_is_logged_at_error_level_with_the_model_and_no_key(member_session, log_lines):
    provider = GeminiProvider(FAKE_GEMINI_KEY, transport=EchoingTransport())
    outcome = run_secret(build(member_session, provider))
    assert outcome.degraded == "config"
    [line] = calls(log_lines())
    assert (line["level"], line["outcome"], line["model"], line["provider_status"]) == ("ERROR", "config_error", PRIMARY, 403)
    assert FAKE_GEMINI_KEY not in log_lines.raw() and SECRET_PROMPT not in log_lines.raw()


def test_the_key_and_the_prompt_never_reach_the_ledger_either(member_session):
    from sqlalchemy import select

    from app.models import LlmCallLog

    run_secret(build(member_session, GeminiProvider(FAKE_GEMINI_KEY, transport=EchoingTransport())))
    member_session.commit()
    dumped = json.dumps(
        [{c.name: str(getattr(r, c.name)) for c in LlmCallLog.__table__.columns} for r in member_session.scalars(select(LlmCallLog))],
        ensure_ascii=False,
    )
    assert FAKE_GEMINI_KEY not in dumped and SECRET_PROMPT not in dumped


# ---- 主要模型異常：斷路開啟時警告一次 -------------------------------------------------------


@pytest.mark.parametrize("error", [
    Step("太慢", seconds=25),
    Step(LLMTransientError("server_error", http_status=500)),
])
def test_a_primary_timeout_or_server_error_logs_one_breaker_warning_with_only_the_model_and_class(member_session, log_lines, error):
    provider = FakeLLMProvider().script(error, GOOD_REPLY)
    run_secret(build(member_session, provider))
    [event] = breaker_events(log_lines())
    assert event["level"] == "WARNING" and event["model"] == PRIMARY and event["outcome"] in ("timeout", "server_error")
    assert {k for k in event if k not in BASE_FIELDS} == {"event", "model", "outcome"}


@pytest.mark.parametrize("error", [
    Step(LLMTransientError("rate_limited", http_status=429)), Step(LLMTransientError("network")),
    Step(LLMBlockedError("input")), Step(LLMConfigError(http_status=403)), Step("壞輸出"),
])
def test_other_failures_do_not_open_the_breaker_so_they_log_no_breaker_warning(member_session, log_lines, error):
    provider = FakeLLMProvider().script(error, GOOD_REPLY)
    run_secret(build(member_session, provider))
    assert breaker_events(log_lines()) == []


def test_a_timeout_of_the_fallback_itself_is_not_a_breaker_event(member_session, log_lines):
    provider = FakeLLMProvider().script(Step(LLMTransientError("rate_limited")), Step("太慢", seconds=60))
    run_secret(build(member_session, provider))
    assert breaker_events(log_lines()) == []


def test_requests_skipped_by_the_open_breaker_log_no_warning_each(member_session, log_lines):
    """窗口內 5 個請求直接用備援：沒有任何一個產生警告（也沒有重複的斷路事件）。"""
    seed(member_session)
    for _ in range(5):
        run_secret(build(member_session, FakeLLMProvider().script(GOOD_REPLY)))
    lines = log_lines()
    assert breaker_events(lines) == []
    assert [line for line in lines if line["level"] in ("WARNING", "ERROR")] == []
    assert [c["model"] for c in calls(lines)] == [FALLBACK] * 5


def test_one_failure_logs_the_warning_once_even_though_the_following_requests_are_skipped(member_session, log_lines):
    run_secret(build(member_session, FakeLLMProvider().script(Step("太慢", seconds=25), GOOD_REPLY)))
    for _ in range(3):
        run_secret(build(member_session, FakeLLMProvider().script(GOOD_REPLY)))
    assert len(breaker_events(log_lines())) == 1


# ---- 整段日誌掃描 ------------------------------------------------------------------------


@pytest.mark.parametrize("script", [
    [GOOD_REPLY],
    [SECRET_REPLY + " 不是 JSON", SECRET_REPLY + " 還是不是"],
    [Step(LLMTransientError("rate_limited", http_status=429)), Step("太慢", seconds=60)],
    [Step(LLMBlockedError("input"))],
    [Step(LLMConfigError(http_status=400))],
])
def test_nothing_the_user_or_the_model_wrote_appears_anywhere_in_the_log(member_session, log_lines, script):
    run_secret(build(member_session, FakeLLMProvider().script(*script)))
    raw = log_lines.raw()
    assert raw and SECRET_PROMPT not in raw and SECRET_REPLY not in raw


def test_the_log_message_is_always_a_fixed_string(member_session, log_lines):
    provider = FakeLLMProvider().script(Step("太慢", seconds=25), Step(LLMTransientError("server_error")))
    run_secret(build(member_session, provider))
    assert {line["message"] for line in log_lines()} == {"模型呼叫", "主要模型異常，暫時改用備援模型"}
