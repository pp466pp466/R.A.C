"""llm-structured-calls 規格：重試、備援切換、斷路、預算與帳本（`LlmService.run_structured`）。

以假供應商與假的單調時鐘驅動：逾時、預算都不必真的等待。資料庫部分用 `member_session`，同時在 SQLite 與
SQL Server 上執行；斷路的跨 worker 測試另用 `concurrent_engine`（獨立連線）。
"""
import itertools
import json
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import LlmSettings
from app.llm.types import LLMBlockedError, LLMConfigError, LLMContent, LLMTransientError
from app.models import LlmCallLog
from app.repositories.llm_call_log_repository import LlmCallLogRepository
from app.services.llm_output import SemanticRule
from app.services.llm_service import (
    DEGRADED_BLOCKED, DEGRADED_BUDGET, DEGRADED_CONFIG, DEGRADED_TRANSIENT, DEGRADED_VALIDATION, LlmService,
)
from tests.support.auth import add_user
from tests.support.llm import FakeLLMProvider, FakeMonotonic, Step

NOW = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)
PRIMARY, FALLBACK, SUMMARY = "gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-summary-x"
SETTINGS = LlmSettings(primary_model=PRIMARY, fallback_model=FALLBACK, summary_model=SUMMARY)
SCHEMA = {"type": "object", "required": ["reply"], "properties": {"reply": {"type": "string"}}}
RULES = (SemanticRule("reply_not_empty", lambda v: v["reply"] != ""),)
GOOD = json.dumps({"reply": "您好，有什麼可以幫忙的嗎？"}, ensure_ascii=False)
CONTENTS = (LLMContent("user", ("資料區塊", "使用者本次訊息")),)


class Wall:
    """可手動調整的牆上時鐘（帳本時間與斷路窗口用）。"""

    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def build(session, provider, *, wall=None, settings=SETTINGS):
    """假供應商自帶假的單調時鐘（它消耗的時間就是預算消耗的時間）；真實轉接器則用系統的單調時鐘。"""
    return LlmService(
        provider=provider, ledger=LlmCallLogRepository(session), settings=settings, clock=wall or Wall(),
        release=session.commit, monotonic=getattr(provider, "clock", time.monotonic),
    )


def run(service, *, kind="chat", user_id=None, conversation_id=None, **extra):
    return service.run_structured(
        kind=kind, user_id=user_id, conversation_id=conversation_id, system_instruction="系統指令",
        contents=CONTENTS, schema=SCHEMA, rules=RULES, **extra,
    )


def rows(session):
    session.expire_all()
    return session.scalars(select(LlmCallLog).order_by(LlmCallLog.id)).all()


def summary_of(session):
    return [(r.model, r.attempt, r.status, r.counted) for r in rows(session)]


def seed(session, *, model=PRIMARY, status="timeout", seconds_ago=10):
    LlmCallLogRepository(session).add(
        user_id=None, conversation_id=None, kind="chat", provider="fake", model=model, attempt=1, status=status,
        counted=False, latency_ms=100, created_at=NOW - timedelta(seconds=seconds_ago),
    )
    session.commit()


@pytest.fixture()
def provider():
    return FakeLLMProvider()


# ---- 一次成功 ------------------------------------------------------------------------


def test_a_first_call_success_returns_the_parsed_value_and_counts_toward_the_quota(member_session, provider):
    provider.script(Step(GOOD, seconds=2.5, prompt_tokens=1200, completion_tokens=420, thinking_tokens=None))
    outcome = run(build(member_session, provider))
    assert outcome.ok and outcome.value == {"reply": "您好，有什麼可以幫忙的嗎？"}
    assert (outcome.model, outcome.attempts, outcome.degraded, outcome.blocked_phase) == (PRIMARY, 1, None, None)
    [row] = rows(member_session)
    assert (row.model, row.attempt, row.status, row.counted, row.kind, row.provider) == (PRIMARY, 1, "ok", True, "chat", "fake")
    assert (row.prompt_tokens, row.completion_tokens, row.thinking_tokens) == (1200, 420, None)      # 供應商沒給思考 token：空值
    assert (row.finish_reason, row.latency_ms, row.error_detail) == ("stop", 2500, None)
    assert row.created_at == NOW


def test_a_summary_call_never_counts_toward_the_quota(member_session, provider):
    provider.script(GOOD)
    assert run(build(member_session, provider), kind="summary").ok
    assert [(r.kind, r.counted) for r in rows(member_session)] == [("summary", False)]


def test_the_request_carries_the_prompt_the_schema_and_the_configured_limits(member_session, provider):
    provider.script(GOOD)
    settings = replace(SETTINGS, max_output_tokens=3000, thinking_budget=0)
    run(build(member_session, provider, settings=settings))
    [request] = provider.calls
    assert (request.model, request.system_instruction, request.contents, request.response_schema) == (PRIMARY, "系統指令", CONTENTS, SCHEMA)
    assert (request.max_output_tokens, request.thinking_budget, request.timeout_seconds) == (3000, 0, 20.0)


def test_the_user_and_conversation_are_recorded_on_every_row(member_session, provider):
    user = add_user(member_session, 1).id
    provider.script(Step(LLMTransientError("rate_limited"), seconds=0.1), GOOD)
    run(build(member_session, provider), user_id=user, conversation_id=42)
    assert [(r.user_id, r.conversation_id) for r in rows(member_session)] == [(user, 42), (user, 42)]


def test_a_specified_model_is_the_first_choice_and_falls_back_like_the_primary(member_session, provider):
    provider.script(Step(LLMTransientError("server_error", http_status=503)), GOOD)
    outcome = run(build(member_session, provider), kind="summary", model=SUMMARY)
    assert [c.model for c in provider.calls] == [SUMMARY, FALLBACK] and outcome.model == FALLBACK


# ---- 驗證失敗：同一模型重試一次 ----------------------------------------------------------


def test_a_validation_failure_retries_the_same_model_and_only_the_second_row_counts(member_session, provider):
    provider.script("不是 JSON", GOOD)
    outcome = run(build(member_session, provider))
    assert outcome.ok and outcome.attempts == 2
    assert [c.model for c in provider.calls] == [PRIMARY, PRIMARY]
    assert summary_of(member_session) == [(PRIMARY, 1, "validation_failed", False), (PRIMARY, 2, "ok", True)]


def test_a_semantic_failure_is_a_validation_failure_too(member_session, provider):
    provider.script('{"reply": ""}', GOOD)
    assert run(build(member_session, provider)).ok
    assert [r.status for r in rows(member_session)] == ["validation_failed", "ok"]


def test_two_validation_failures_degrade_after_exactly_two_calls_and_nothing_counts(member_session, provider):
    provider.script("第一次壞掉", '{"reply": 5}')
    outcome = run(build(member_session, provider))
    assert (outcome.ok, outcome.degraded, outcome.value, outcome.attempts) == (False, DEGRADED_VALIDATION, None, 2)
    assert len(provider.calls) == 2
    assert summary_of(member_session) == [(PRIMARY, 1, "validation_failed", False), (PRIMARY, 2, "validation_failed", False)]


def test_the_failed_output_is_kept_in_the_ledger_cut_at_2000_characters(member_session, provider):
    long_text = "壞" * 5000
    provider.script(long_text, "短的壞輸出")
    run(build(member_session, provider))
    first, second = rows(member_session)
    assert first.error_detail == "壞" * 2000 and second.error_detail == "短的壞輸出"


def test_a_truncated_reply_is_recorded_with_its_finish_reason(member_session, provider):
    provider.script(Step('{"reply": "寫到一半', finish_reason="length"), GOOD)
    run(build(member_session, provider))
    assert [r.finish_reason for r in rows(member_session)] == ["length", "stop"]


# ---- 暫時性錯誤：切換備援（算作那次重試） -----------------------------------------------


@pytest.mark.parametrize("error, status", [
    (LLMTransientError("rate_limited", http_status=429), "rate_limited"),
    (LLMTransientError("server_error", http_status=503), "server_error"),
    (LLMTransientError("network"), "network"),
])
def test_a_transient_error_switches_to_the_fallback_immediately(member_session, provider, error, status):
    provider.script(Step(error, seconds=0.3), GOOD)
    outcome = run(build(member_session, provider))
    assert outcome.ok and (outcome.model, outcome.attempts) == (FALLBACK, 2)
    assert [c.model for c in provider.calls] == [PRIMARY, FALLBACK]
    assert summary_of(member_session) == [(PRIMARY, 1, status, False), (FALLBACK, 2, "ok", True)]


def test_a_call_timeout_switches_to_the_fallback_which_gets_only_the_remaining_budget(member_session, provider):
    provider.script(Step("太慢了", seconds=25), GOOD)
    outcome = run(build(member_session, provider))
    assert outcome.ok and outcome.model == FALLBACK
    assert [c.timeout_seconds for c in provider.calls] == [20.0, 10.0]               # 20 秒逾時，剩下 10 秒給備援
    assert summary_of(member_session) == [(PRIMARY, 1, "timeout", False), (FALLBACK, 2, "ok", True)]
    assert rows(member_session)[0].latency_ms == 20000


def test_the_fallback_uses_the_whole_remaining_budget_not_the_single_call_limit(member_session, provider):
    provider.script(Step(LLMTransientError("rate_limited"), seconds=0.3), GOOD)
    run(build(member_session, provider))
    assert [c.timeout_seconds for c in provider.calls] == [20.0, pytest.approx(29.7)]


def test_the_failure_row_of_a_transient_error_holds_only_a_category_and_status_code(member_session, provider):
    provider.script(Step(LLMTransientError("server_error", http_status=503)), Step(LLMTransientError("timeout")))
    run(build(member_session, provider))
    assert [r.error_detail for r in rows(member_session)] == ["server_error 503", "timeout"]


def test_when_the_fallback_fails_too_the_reply_degrades_after_two_calls(member_session, provider):
    provider.script(Step("太慢", seconds=25), Step(LLMTransientError("server_error", http_status=500)))
    outcome = run(build(member_session, provider))
    assert (outcome.degraded, outcome.attempts, outcome.model) == (DEGRADED_TRANSIENT, 2, FALLBACK)
    assert summary_of(member_session) == [(PRIMARY, 1, "timeout", False), (FALLBACK, 2, "server_error", False)]


def test_an_invalid_fallback_output_is_not_retried_because_two_calls_are_used(member_session, provider):
    provider.script(Step(LLMTransientError("rate_limited")), "壞輸出")
    outcome = run(build(member_session, provider))
    assert (outcome.degraded, outcome.attempts) == (DEGRADED_VALIDATION, 2) and len(provider.calls) == 2
    assert summary_of(member_session) == [(PRIMARY, 1, "rate_limited", False), (FALLBACK, 2, "validation_failed", False)]


# ---- 被阻擋與設定錯誤：不重試、不切換 ----------------------------------------------------


@pytest.mark.parametrize("phase, finish", [("input", "prompt_blocked"), ("output", "safety")])
def test_a_blocked_call_degrades_at_once_without_retry_or_fallback(member_session, provider, phase, finish):
    provider.script(Step(LLMBlockedError(phase), seconds=1.2))
    outcome = run(build(member_session, provider))
    assert (outcome.degraded, outcome.blocked_phase, outcome.attempts) == (DEGRADED_BLOCKED, phase, 1)
    assert len(provider.calls) == 1
    [row] = rows(member_session)
    assert (row.status, row.finish_reason, row.counted, row.latency_ms, row.error_detail) == ("blocked", finish, False, 1200, f"blocked {phase}")


@pytest.mark.parametrize("code", [400, 403])
def test_a_configuration_error_degrades_at_once_without_retry_or_fallback(member_session, provider, code):
    provider.script(Step(LLMConfigError(http_status=code), seconds=0.4))
    outcome = run(build(member_session, provider))
    assert (outcome.degraded, outcome.attempts, outcome.blocked_phase) == (DEGRADED_CONFIG, 1, None)
    assert len(provider.calls) == 1
    [row] = rows(member_session)
    assert (row.status, row.counted, row.error_detail) == ("config_error", False, f"config_error {code}")


# ---- 總時間預算 ----------------------------------------------------------------------


def test_no_second_call_is_made_when_less_than_two_seconds_of_budget_remain(member_session, provider):
    settings = replace(SETTINGS, call_timeout_seconds=30)
    provider.script(Step("壞輸出", seconds=29.5))
    outcome = run(build(member_session, provider, settings=settings))
    assert (outcome.degraded, outcome.attempts) == (DEGRADED_BUDGET, 1) and len(provider.calls) == 1
    assert summary_of(member_session) == [(PRIMARY, 1, "validation_failed", False)]


def test_a_timeout_that_leaves_no_budget_does_not_switch_to_the_fallback(member_session, provider):
    settings = replace(SETTINGS, call_timeout_seconds=20, request_budget_seconds=21)
    provider.script(Step("太慢", seconds=25))
    outcome = run(build(member_session, provider, settings=settings))
    assert (outcome.degraded, outcome.attempts) == (DEGRADED_BUDGET, 1) and len(provider.calls) == 1


def test_exactly_two_seconds_of_budget_is_still_enough_for_a_call(member_session, provider):
    settings = replace(SETTINGS, call_timeout_seconds=30)
    provider.script(Step("壞輸出", seconds=28), GOOD)
    outcome = run(build(member_session, provider, settings=settings))
    assert outcome.ok and provider.calls[1].timeout_seconds == 2.0


def test_the_first_call_is_limited_by_the_single_call_timeout_not_the_whole_budget(member_session, provider):
    provider.script(GOOD)
    run(build(member_session, provider))
    assert provider.calls[0].timeout_seconds == 20.0


def test_the_budget_follows_the_monotonic_clock_not_the_wall_clock(member_session, provider):
    """牆上時鐘被往前撥一小時（例如校時）不應讓預算判斷出錯。"""
    wall = Wall()
    provider.script(Step(LLMTransientError("rate_limited"), seconds=0.3), GOOD)
    provider.hooks.append(lambda request: setattr(wall, "now", wall.now + timedelta(hours=1)))
    assert run(build(member_session, provider, wall=wall)).ok
    assert provider.calls[1].timeout_seconds == pytest.approx(29.7)


# ---- 斷路：主要模型近期逾時或伺服器錯誤就暫時跳過 -------------------------------------------


@pytest.mark.parametrize("status", ["timeout", "server_error"])
def test_a_recent_primary_failure_sends_the_next_request_straight_to_the_fallback(member_session, provider, status):
    seed(member_session, status=status, seconds_ago=10)
    provider.script(GOOD)
    outcome = run(build(member_session, provider))
    assert outcome.ok and outcome.model == FALLBACK and [c.model for c in provider.calls] == [FALLBACK]


@pytest.mark.parametrize("seconds_ago, expected_first", [(59, FALLBACK), (60, FALLBACK), (61, PRIMARY)])
def test_the_breaker_window_is_sixty_seconds(member_session, provider, seconds_ago, expected_first):
    seed(member_session, seconds_ago=seconds_ago)
    provider.script(GOOD)
    run(build(member_session, provider))
    assert provider.calls[0].model == expected_first


@pytest.mark.parametrize("status", ["rate_limited", "network", "validation_failed", "blocked", "config_error", "ok"])
def test_other_recent_outcomes_do_not_open_the_breaker(member_session, provider, status):
    seed(member_session, status=status, seconds_ago=1)
    provider.script(GOOD)
    run(build(member_session, provider))
    assert provider.calls[0].model == PRIMARY


def test_a_failure_of_another_model_does_not_open_the_breaker(member_session, provider):
    seed(member_session, model=FALLBACK, status="timeout", seconds_ago=1)
    provider.script(GOOD)
    run(build(member_session, provider))
    assert provider.calls[0].model == PRIMARY


def test_the_window_length_comes_from_the_settings(member_session, provider):
    seed(member_session, seconds_ago=20)
    provider.script(GOOD)
    run(build(member_session, provider, settings=replace(SETTINGS, breaker_seconds=15)))
    assert provider.calls[0].model == PRIMARY


def test_a_failing_fallback_that_was_chosen_by_the_breaker_is_not_retried(member_session, provider):
    seed(member_session)
    provider.script(Step(LLMTransientError("server_error", http_status=502)))
    outcome = run(build(member_session, provider))
    assert (outcome.degraded, outcome.attempts) == (DEGRADED_TRANSIENT, 1) and len(provider.calls) == 1


def test_a_validation_failure_while_the_breaker_is_open_retries_on_the_fallback(member_session, provider):
    seed(member_session)
    provider.script("壞輸出", GOOD)
    outcome = run(build(member_session, provider))
    assert outcome.ok and [c.model for c in provider.calls] == [FALLBACK, FALLBACK]


def test_when_the_primary_and_fallback_are_the_same_model_a_transient_error_is_not_retried(member_session, provider):
    settings = replace(SETTINGS, fallback_model=PRIMARY)
    provider.script(Step(LLMTransientError("rate_limited")))
    outcome = run(build(member_session, provider, settings=settings))
    assert (outcome.degraded, outcome.attempts) == (DEGRADED_TRANSIENT, 1)


def test_the_breaker_is_nothing_but_the_ledger_so_another_worker_sees_a_failure_while_the_first_is_still_running(concurrent_engine):
    """兩個獨立的 Session 與服務（等同兩個 worker）：A 記下主要模型逾時並尚在等備援時，B 就已經跳過主要模型。"""
    seen_by_b = {}
    with Session(concurrent_engine) as session_a, Session(concurrent_engine) as session_b:
        provider_a = FakeLLMProvider().script(Step("太慢", seconds=25), GOOD)

        def when_a_calls_the_fallback(request):
            if request.model == FALLBACK:
                provider_b = FakeLLMProvider().script(GOOD)
                run(build(session_b, provider_b))
                session_b.commit()                                   # B 的呼叫端提交最後一筆（否則它會握著寫入鎖）
                seen_by_b["models"] = [c.model for c in provider_b.calls]

        provider_a.hooks.append(when_a_calls_the_fallback)
        assert run(build(session_a, provider_a)).ok
    assert seen_by_b["models"] == [FALLBACK]


# ---- 呼叫期間沒有資料庫交易；最後一筆交給呼叫端提交 ---------------------------------------


@pytest.mark.parametrize("script", [
    [GOOD],
    ["壞輸出", GOOD],
    [Step(LLMTransientError("rate_limited")), GOOD],
    [Step("太慢", seconds=25), "壞輸出"],
    [Step(LLMBlockedError("input"))],
])
def test_no_database_transaction_is_open_while_the_model_is_being_called(member_session, provider, script):
    seed(member_session, model=FALLBACK, status="timeout", seconds_ago=500)            # 讓斷路查詢先讀一次資料庫
    open_during_calls = []
    provider.hooks.append(lambda request: open_during_calls.append(member_session.in_transaction()))
    provider.script(*script)
    run(build(member_session, provider))
    assert open_during_calls and not any(open_during_calls)


def test_the_last_row_is_left_for_the_caller_to_commit_and_a_rollback_drops_it(member_session, provider):
    provider.script(GOOD)
    run(build(member_session, provider))
    member_session.rollback()                                                           # 呼叫端的交易失敗：計入額度的那筆不應留下
    assert rows(member_session) == []


def test_earlier_failure_rows_are_already_committed_so_a_rollback_keeps_them(member_session, provider):
    provider.script(Step("太慢", seconds=25), GOOD)
    run(build(member_session, provider))
    member_session.rollback()
    assert summary_of(member_session) == [(PRIMARY, 1, "timeout", False)]


def test_the_committed_rows_survive_even_if_the_caller_never_commits(db_engine):
    from app.models import Base

    Base.metadata.drop_all(db_engine)
    Base.metadata.create_all(db_engine)
    with Session(db_engine) as session:
        provider = FakeLLMProvider().script(Step("太慢", seconds=25), Step(LLMTransientError("server_error")))
        run(build(session, provider))
        session.rollback()
        assert summary_of(session) == [(PRIMARY, 1, "timeout", False)]
    Base.metadata.drop_all(db_engine)


# ---- 帳本不含對話文字 ---------------------------------------------------------------------

SECRET_PROMPT = "使用者私密內容ABC123"
SECRET_REPLY = "模型回覆私密內容XYZ789"


@pytest.mark.parametrize("script", [
    [json.dumps({"reply": SECRET_REPLY}, ensure_ascii=False)],
    [Step(LLMTransientError("rate_limited", http_status=429)), json.dumps({"reply": SECRET_REPLY}, ensure_ascii=False)],
    [Step(LLMTransientError("timeout")), Step(LLMTransientError("server_error", http_status=500))],
    [Step(LLMBlockedError("input"))],
    [Step(LLMConfigError(http_status=403))],
])
def test_no_ledger_column_holds_the_prompt_or_the_reply_text(member_session, provider, script):
    provider.script(*script)
    service = build(member_session, provider)
    service.run_structured(
        kind="chat", user_id=None, conversation_id=1, system_instruction=SECRET_PROMPT,
        contents=(LLMContent("user", (SECRET_PROMPT, SECRET_PROMPT)),), schema=SCHEMA, rules=RULES,
    )
    dumped = json.dumps([
        {column.name: str(getattr(row, column.name)) for column in LlmCallLog.__table__.columns} for row in rows(member_session)
    ], ensure_ascii=False)
    assert SECRET_PROMPT not in dumped and SECRET_REPLY not in dumped


def test_only_a_failed_output_keeps_text_and_never_the_prompt(member_session, provider):
    provider.script(SECRET_REPLY, SECRET_REPLY + "二")
    build(member_session, provider).run_structured(
        kind="chat", user_id=None, conversation_id=1, system_instruction=SECRET_PROMPT,
        contents=(LLMContent("user", (SECRET_PROMPT,)),), schema=SCHEMA, rules=RULES,
    )
    details = [r.error_detail for r in rows(member_session)]
    assert details == [SECRET_REPLY, SECRET_REPLY + "二"]
    assert all(SECRET_PROMPT not in d for d in details)


# ---- 任何組合都不超過兩次呼叫 -----------------------------------------------------------------

OUTCOMES = {
    "ok": lambda: Step(GOOD, seconds=1),
    "bad": lambda: Step("不是 JSON", seconds=1),
    "429": lambda: Step(LLMTransientError("rate_limited", http_status=429), seconds=0.3),
    "500": lambda: Step(LLMTransientError("server_error", http_status=500), seconds=0.5),
    "timeout": lambda: Step("太慢", seconds=60),
    "network": lambda: Step(LLMTransientError("network"), seconds=0.2),
    "blocked_in": lambda: Step(LLMBlockedError("input"), seconds=1),
    "blocked_out": lambda: Step(LLMBlockedError("output"), seconds=1),
    "config": lambda: Step(LLMConfigError(http_status=403), seconds=0.2),
}
TRANSIENT = {"429", "500", "timeout", "network"}


@pytest.mark.parametrize("breaker_open", [False, True])
@pytest.mark.parametrize("first, second", list(itertools.product(OUTCOMES, OUTCOMES)))
def test_no_combination_of_outcomes_ever_makes_more_than_two_calls(member_session, provider, first, second, breaker_open):
    if breaker_open:
        seed(member_session)
    provider.script(OUTCOMES[first](), OUTCOMES[second](), OUTCOMES["ok"]())          # 第三步是陷阱：不該被用到
    outcome = run(build(member_session, provider))

    assert len(provider.calls) <= 2 and provider.remaining >= 1
    stored = rows(member_session)
    stored = [r for r in stored if r.created_at > NOW - timedelta(seconds=1)]            # 排除斷路的種子資料
    assert len(stored) == len(provider.calls) == outcome.attempts
    counted = [r for r in stored if r.counted]
    assert len(counted) == (1 if outcome.ok else 0)
    if outcome.ok:
        assert counted[0] is stored[-1] and stored[-1].status == "ok"
    else:
        assert outcome.value is None and outcome.degraded in {
            DEGRADED_VALIDATION, DEGRADED_TRANSIENT, DEGRADED_BLOCKED, DEGRADED_CONFIG, DEGRADED_BUDGET,
        }
    if first in {"blocked_in", "blocked_out", "config", "ok"}:
        assert len(provider.calls) == 1                                                  # 這些結果都不會有第二次呼叫
    if first in TRANSIENT and not breaker_open and len(provider.calls) == 2:
        assert provider.calls[1].model == FALLBACK                                       # 暫時性錯誤：第二次一定是備援
    if first == "bad" and len(provider.calls) == 2:
        assert provider.calls[1].model == provider.calls[0].model                        # 驗證失敗：同一模型
