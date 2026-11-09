"""llm-streaming 規格：`StreamRunner` 的期限、備援時機、用量與帳本。

全部以假時鐘與假供應商（`tests/support/llm.py`）驅動，不真的等待。每個情境對應規格的一個 Scenario；
最後有用真實帳本 Repository（SQLite）的整合案例：帳本與日誌裡找不到任何劇情、行動或系統指令的文字。
"""
import logging
import re
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.config import LlmSettings
from app.llm.types import (
    FINISH_SAFETY, LLMBlockedError, LLMConfigError, LLMContent, LLMTransientError, StreamText, StreamTick,
)
from app.models import LlmCallLog
from app.models.llm_call_log import KIND_RPG_OPENING, KIND_RPG_STREAM
from app.repositories.llm_call_log_repository import LlmCallLogRepository
from app.services.rpg_stream import (
    ABORT_DISCONNECTED, ABORT_OUTPUT_BLOCKED, FAILURE_BLOCKED, FAILURE_CONFIG, FAILURE_TIMEOUT, FAILURE_TRANSIENT,
    StreamRequest, StreamRunner, ledger_breaker, ledger_recorder,
)
from tests.support.llm import Chunk, Fail, FakeLLMProvider, FakeMonotonic, Finish, Idle

PRIMARY, FALLBACK = "vendor/primary", "vendor/fallback"
SETTINGS = LlmSettings(primary_model=PRIMARY, fallback_model=FALLBACK)
SECRET_SYSTEM, SECRET_ACTION, SECRET_STORY = "SECRET-SYSTEM", "SECRET-ACTION", "SECRET-STORY"
REQUEST = StreamRequest(
    model=PRIMARY,
    system_instruction=f"你是敘事者。{SECRET_SYSTEM}",
    contents=(LLMContent("user", (f"玩家行動：{SECRET_ACTION}", "第二段")),),
    max_output_tokens=1800,
)
CONTEXT_CHARS = len(REQUEST.system_instruction) + len(f"玩家行動：{SECRET_ACTION}") + len("第二段")


def kit(*, breaker: bool = False, kind: str = KIND_RPG_STREAM, **runner_kwargs):
    clock = FakeMonotonic()
    provider = FakeLLMProvider(clock)
    entries: list = []
    runner = StreamRunner(
        provider=provider, settings=SETTINGS, kind=kind, record=entries.append, breaker_open=lambda model: breaker,
        user_id=7, conversation_id=3, monotonic=clock, **runner_kwargs,
    )
    return clock, provider, entries, runner


def texts(events) -> list[str]:
    return [event.text for event in events if isinstance(event, StreamText)]


def transient(kind: str, status: int | None = None) -> LLMTransientError:
    return LLMTransientError(kind, http_status=status)


# ---- 正常串流與閒置 --------------------------------------------------------------------------

def test_a_normal_stream_yields_the_chunks_then_succeeds_with_one_ledger_entry():
    clock, provider, entries, runner = kit()
    provider.script_stream(Chunk("清晨的", 0.4), Chunk("海風。", 0.2), Finish(prompt_tokens=120, completion_tokens=340, thinking_tokens=7, seconds=0.1))
    run = runner.start(REQUEST)
    events = list(run)
    assert texts(events) == ["清晨的", "海風。"] and run.outcome.ok and run.outcome.attempts == 1 and run.outcome.model == PRIMARY
    assert (run.outcome.text_started, run.outcome.chars, run.outcome.failure, run.outcome.aborted) == (True, 6, None, None)
    (entry,) = entries
    assert (entry.kind, entry.provider, entry.model, entry.attempt, entry.status) == (KIND_RPG_STREAM, "fake", PRIMARY, 1, "ok")
    assert (entry.user_id, entry.conversation_id) == (7, 3)
    assert (entry.prompt_tokens, entry.completion_tokens, entry.thinking_tokens, entry.finish_reason) == (120, 340, 7, "stop")
    assert entry.first_chunk_ms == 400 and entry.latency_ms == 700 and entry.error_detail is None
    assert provider.handles[0].closed                                      # 結束後上游一定被關閉
    request = provider.calls[0]
    assert (request.model, request.max_output_tokens, request.response_schema) == (PRIMARY, 1800, None)
    assert request.system_instruction == REQUEST.system_instruction and request.contents == REQUEST.contents


def test_the_opening_kind_is_recorded_as_given():
    clock, provider, entries, runner = kit(kind=KIND_RPG_OPENING)
    provider.script_stream(Chunk("開場"), Finish())
    list(runner.start(REQUEST))
    assert entries[0].kind == KIND_RPG_OPENING


def test_idle_ticks_are_passed_to_the_caller_so_it_can_send_heartbeats():
    clock, provider, entries, runner = kit()
    provider.script_stream(Idle(), Idle(), Chunk("好"), Idle(), Finish())
    events = list(runner.start(REQUEST))
    assert events == [StreamTick(), StreamTick(), StreamText("好"), StreamTick()]


def test_an_empty_text_chunk_is_not_the_first_chunk():
    clock, provider, entries, runner = kit()
    provider.script_stream(Chunk("", 0.5), Chunk("好", 0.5), Finish())
    run = runner.start(REQUEST)
    events = list(run)
    assert events == [StreamTick(), StreamText("好")] and entries[0].first_chunk_ms == 1000


# ---- 首字與總時間的期限 -----------------------------------------------------------------------

def test_only_reasoning_fragments_for_20_seconds_is_a_first_chunk_timeout_and_the_upstream_is_closed():
    clock, provider, entries, runner = kit()
    provider.script_stream(*[Idle() for _ in range(25)]).script_stream(Chunk("備援的劇情"), Finish())
    run = runner.start(REQUEST)
    assert texts(list(run)) == ["備援的劇情"]
    assert provider.handles[0].closed and provider.handles[0].served == 20          # 第 20 秒檢查到逾時，不再讀取
    first, second = entries
    assert (first.model, first.status, first.error_detail, first.latency_ms) == (PRIMARY, "timeout", "timeout first_chunk", 20000)
    assert (second.model, second.status, second.attempt) == (FALLBACK, "ok", 2)
    assert run.outcome.ok and run.outcome.attempts == 2 and run.outcome.model == FALLBACK


def test_a_first_chunk_that_arrives_after_the_deadline_is_too_late():
    clock, provider, entries, runner = kit()
    provider.script_stream(Chunk("太晚了", 21.0)).script_stream(Chunk("備援"), Finish())
    run = runner.start(REQUEST)
    assert texts(list(run)) == ["備援"] and entries[0].error_detail == "timeout first_chunk"


def test_when_the_fallback_also_never_produces_text_the_run_fails_after_exactly_two_attempts():
    clock, provider, entries, runner = kit()
    for _ in range(3):
        provider.script_stream(*[Idle() for _ in range(30)])
    run = runner.start(REQUEST)
    assert list(run) == [StreamTick()] * 38                                         # 19 + 19 個 tick，之後都逾時
    assert run.outcome.failure == FAILURE_TIMEOUT and run.outcome.attempts == 2 and not run.outcome.text_started
    assert [e.model for e in entries] == [PRIMARY, FALLBACK] and provider.remaining_streams == 1       # 沒有第三次
    assert clock.now == 1040.0


def test_the_stream_is_cut_at_the_60_second_total_even_after_text_started_and_it_is_not_switched():
    clock, provider, entries, runner = kit()
    provider.script_stream(Chunk("開頭"), *[Idle() for _ in range(80)]).script_stream(Chunk("不該用到"), Finish())
    run = runner.start(REQUEST)
    assert texts(list(run)) == ["開頭"]
    assert run.outcome.failure == FAILURE_TIMEOUT and run.outcome.text_started and run.outcome.attempts == 1
    assert provider.handles[0].closed and provider.remaining_streams == 1
    (entry,) = entries
    assert (entry.status, entry.error_detail) == ("timeout", "timeout total") and clock.now == 1060.2      # 首字花 0.2 秒，之後每個 tick 1 秒
    assert (entry.prompt_tokens, entry.completion_tokens) == (CONTEXT_CHARS, 2)    # 沒有用量：以字數估算


def test_a_complete_stream_whose_end_event_lands_just_past_the_budget_is_still_accepted():
    clock, provider, entries, runner = kit()
    provider.script_stream(Chunk("好", 0.5), Idle(59.0), Finish(seconds=1.0))
    run = runner.start(REQUEST)
    list(run)
    assert run.outcome.ok and entries[0].status == "ok"


def test_with_less_than_two_seconds_left_no_second_attempt_is_made():
    clock, provider, entries, runner = kit(total_budget_seconds=21.0)
    provider.script_stream(*[Idle() for _ in range(30)]).script_stream(Chunk("不該用到"), Finish())
    run = runner.start(REQUEST)
    list(run)
    assert run.outcome.failure == FAILURE_TIMEOUT and run.outcome.attempts == 1 and provider.remaining_streams == 1


def test_the_fallback_gets_only_the_remaining_budget_as_its_deadline():
    clock, provider, entries, runner = kit(total_budget_seconds=25.0)
    provider.script_stream(*[Idle() for _ in range(30)]).script_stream(*[Idle() for _ in range(30)])
    run = runner.start(REQUEST)
    list(run)
    assert [r.timeout_seconds for r in provider.calls] == [25.0, 5.0]               # 第二次只剩 5 秒
    assert run.outcome.attempts == 2 and entries[1].error_detail == "timeout total" and clock.now == 1025.0


# ---- 備援只在第一個正文片段之前切換 ------------------------------------------------------------

def test_a_429_before_any_text_switches_to_the_fallback_and_records_both_attempts():
    clock, provider, entries, runner = kit()
    provider.script_stream(Fail(transient("rate_limited", 429))).script_stream(Chunk("備援"), Finish())
    run = runner.start(REQUEST)
    assert texts(list(run)) == ["備援"] and run.outcome.ok and run.outcome.model == FALLBACK
    assert [r.model for r in provider.calls] == [PRIMARY, FALLBACK]
    first, second = entries
    assert (first.status, first.error_detail, first.attempt, first.completion_tokens) == ("rate_limited", "rate_limited 429", 1, None)
    assert (second.status, second.attempt, second.model) == ("ok", 2, FALLBACK)


@pytest.mark.parametrize("kind, status", [("server_error", "server_error"), ("network", "network"), ("timeout", "timeout")])
def test_other_transient_failures_before_any_text_also_switch(kind, status):
    clock, provider, entries, runner = kit()
    provider.script_stream(Fail(transient(kind, 503 if kind == "server_error" else None))).script_stream(Chunk("備援"), Finish())
    run = runner.start(REQUEST)
    list(run)
    assert run.outcome.ok and entries[0].status == status and entries[1].model == FALLBACK


def test_a_failure_after_text_does_not_switch_and_ends_as_an_interruption():
    clock, provider, entries, runner = kit()
    provider.script_stream(Chunk("說到一半"), Fail(transient("network"))).script_stream(Chunk("不該用到"), Finish())
    run = runner.start(REQUEST)
    assert texts(list(run)) == ["說到一半"]
    assert run.outcome.failure == FAILURE_TRANSIENT and run.outcome.text_started and run.outcome.attempts == 1
    assert provider.remaining_streams == 1 and provider.handles[0].closed
    (entry,) = entries
    assert (entry.status, entry.completion_tokens, entry.prompt_tokens) == ("network", 4, CONTEXT_CHARS)


def test_a_configuration_error_does_not_switch():
    clock, provider, entries, runner = kit()
    provider.script_stream(Fail(LLMConfigError(http_status=402))).script_stream(Chunk("不該用到"), Finish())
    run = runner.start(REQUEST)
    assert list(run) == [] and run.outcome.failure == FAILURE_CONFIG and run.outcome.attempts == 1
    assert (entries[0].status, entries[0].error_detail) == ("config_error", "config_error 402") and provider.remaining_streams == 1


@pytest.mark.parametrize("phase, finish", [("output", FINISH_SAFETY), ("input", "prompt_blocked")])
def test_a_provider_safety_block_does_not_switch(phase, finish):
    clock, provider, entries, runner = kit()
    provider.script_stream(Fail(LLMBlockedError(phase))).script_stream(Chunk("不該用到"), Finish())
    run = runner.start(REQUEST)
    list(run)
    assert run.outcome.failure == FAILURE_BLOCKED and run.outcome.blocked_phase == phase and provider.remaining_streams == 1
    assert (entries[0].status, entries[0].finish_reason, entries[0].error_detail) == ("blocked", finish, f"blocked {phase}")


def test_when_the_fallback_fails_too_the_run_fails_and_nothing_more_is_tried():
    clock, provider, entries, runner = kit()
    provider.script_stream(Fail(transient("rate_limited", 429))).script_stream(Fail(transient("server_error", 503)))
    provider.script_stream(Chunk("不該用到"), Finish())
    run = runner.start(REQUEST)
    assert list(run) == [] and run.outcome.failure == FAILURE_TRANSIENT and run.outcome.attempts == 2
    assert [e.status for e in entries] == ["rate_limited", "server_error"] and provider.remaining_streams == 1


def test_an_open_circuit_starts_on_the_fallback_and_a_failure_there_has_no_second_attempt():
    clock, provider, entries, runner = kit(breaker=True)
    provider.script_stream(Fail(transient("server_error", 502))).script_stream(Chunk("不該用到"), Finish())
    run = runner.start(REQUEST)
    list(run)
    assert provider.calls[0].model == FALLBACK and run.outcome.failure == FAILURE_TRANSIENT and run.outcome.attempts == 1
    assert provider.remaining_streams == 1


def test_a_complete_stream_with_no_text_at_all_is_a_failure_and_switches():
    clock, provider, entries, runner = kit()
    provider.script_stream(Finish()).script_stream(Chunk("備援"), Finish())
    run = runner.start(REQUEST)
    assert texts(list(run)) == ["備援"] and entries[0].error_detail == "empty_response" and entries[0].status == "server_error"


def test_a_stream_that_just_stops_without_an_end_event_is_a_dropped_connection():
    clock, provider, entries, runner = kit()
    provider.script_stream(Chunk("說到"), Idle())
    run = runner.start(REQUEST)
    list(run)
    assert run.outcome.failure == FAILURE_TRANSIENT and entries[0].status == "network"


# ---- 用量 -------------------------------------------------------------------------------------

def test_missing_usage_is_estimated_from_the_context_and_received_characters():
    clock, provider, entries, runner = kit()
    provider.script_stream(Chunk("一二三"), Chunk("四五"), Finish(prompt_tokens=None, completion_tokens=None))
    run = runner.start(REQUEST)
    list(run)
    assert (entries[0].prompt_tokens, entries[0].completion_tokens, entries[0].thinking_tokens) == (CONTEXT_CHARS, 5, None)
    assert run.outcome.completion_tokens == 5 and run.outcome.finish_reason == "stop"


def test_usage_reported_by_the_provider_always_wins_over_the_estimate():
    clock, provider, entries, runner = kit()
    provider.script_stream(Chunk("一二三"), Finish(prompt_tokens=11, completion_tokens=22, thinking_tokens=3))
    list(runner.start(REQUEST))
    assert (entries[0].prompt_tokens, entries[0].completion_tokens, entries[0].thinking_tokens) == (11, 22, 3)


# ---- 斷線與外層中止 -----------------------------------------------------------------------------

def test_a_disconnect_records_estimated_tokens_and_the_disconnected_reason_and_stops_reading():
    clock, provider, entries, runner = kit()
    provider.script_stream(Chunk("你好", 0.2), Chunk("世界", 0.2), Idle(), Finish())
    run = runner.start(REQUEST)
    iterator = iter(run)
    assert next(iterator) == StreamText("你好")
    run.abort(ABORT_DISCONNECTED)
    run.abort(ABORT_DISCONNECTED)                                                   # 可重複呼叫，不會多記一筆
    assert provider.handles[0].closed and provider.handles[0].served == 1 and list(iterator) == []
    (entry,) = entries
    assert (entry.status, entry.finish_reason, entry.error_detail) == ("ok", "other", "disconnected")
    assert (entry.prompt_tokens, entry.completion_tokens, entry.first_chunk_ms) == (CONTEXT_CHARS, 2, 200)
    assert run.outcome.aborted == ABORT_DISCONNECTED and not run.outcome.ok and run.outcome.text_started


def test_our_own_output_block_is_recorded_with_the_category_only():
    clock, provider, entries, runner = kit()
    provider.script_stream(Chunk(f"一句話{SECRET_STORY}"), Chunk("第二句"), Finish())
    run = runner.start(REQUEST)
    iterator = iter(run)
    next(iterator)
    run.abort(ABORT_OUTPUT_BLOCKED, "minor")
    assert provider.handles[0].closed and list(iterator) == []
    (entry,) = entries
    assert (entry.status, entry.error_detail, entry.finish_reason) == ("blocked", "output_blocked minor", "other")
    assert run.outcome.aborted == ABORT_OUTPUT_BLOCKED and run.outcome.abort_category == "minor"


@pytest.mark.parametrize("category", [f"他的名字是 {SECRET_STORY}", "Minor-Sexual", "", "x" * 40, "有中文"])
def test_a_category_that_is_not_an_enumeration_style_identifier_is_never_stored(category):
    clock, provider, entries, runner = kit()
    provider.script_stream(Chunk("一"), Finish())
    run = runner.start(REQUEST)
    iterator = iter(run)                                                            # 持有迭代器：否則被回收時會先記成斷線
    next(iterator)
    run.abort(ABORT_OUTPUT_BLOCKED, category)
    assert entries[0].error_detail == "output_blocked unknown" and SECRET_STORY not in repr(entries)


def test_closing_or_dropping_the_iterator_counts_as_a_disconnect_so_no_exit_loses_the_ledger():
    clock, provider, entries, runner = kit()
    provider.script_stream(Chunk("一二"), Chunk("三"), Finish())
    run = runner.start(REQUEST)
    generator = iter(run)
    next(generator)
    generator.close()                                                              # 例如外層收到 GeneratorExit
    assert provider.handles[0].closed
    (entry,) = entries
    assert (entry.status, entry.error_detail, entry.completion_tokens) == ("ok", "disconnected", 2)
    assert run.outcome.aborted == ABORT_DISCONNECTED


def test_aborting_before_the_stream_started_records_nothing_and_nothing_is_ever_requested():
    clock, provider, entries, runner = kit()
    provider.script_stream(Chunk("不該用到"), Finish())
    run = runner.start(REQUEST)
    run.abort(ABORT_DISCONNECTED)
    assert list(run) == [] and entries == [] and provider.calls == [] and run.outcome.aborted == ABORT_DISCONNECTED


def test_a_run_can_only_be_iterated_once():
    clock, provider, entries, runner = kit()
    provider.script_stream(Chunk("好"), Finish())
    run = runner.start(REQUEST)
    list(run)
    with pytest.raises(RuntimeError):
        iter(run)


# ---- 帳本與日誌沒有任何文字 ----------------------------------------------------------------------

NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)
ALLOWED_DETAIL = re.compile(
    r"(timeout (first_chunk|total)|(rate_limited|server_error|network|timeout)( \d{3})?|empty_response|config_error( \d{3})?|"
    r"blocked (input|output)|disconnected|output_blocked [a-z_]+)"
)


def test_the_real_ledger_gets_the_rows_and_no_column_contains_any_story_action_or_instruction_text(member_session, caplog):
    caplog.set_level(logging.DEBUG)
    ledger = LlmCallLogRepository(member_session)
    clock = FakeMonotonic()
    provider = FakeLLMProvider(clock)
    runner = StreamRunner(
        provider=provider, settings=SETTINGS, kind=KIND_RPG_STREAM, record=ledger_recorder(ledger, lambda: NOW),
        breaker_open=ledger_breaker(ledger, lambda: NOW, 60.0), monotonic=clock,
    )
    # 第一次：主要模型沒有首字（逾時）→ 備援成功；第二次：成功後使用者斷線；第三次：我方審查命中
    provider.script_stream(*[Idle() for _ in range(25)]).script_stream(Chunk(f"清晨的海風{SECRET_STORY}", 0.4), Finish(prompt_tokens=50, completion_tokens=60))
    run = runner.start(REQUEST)
    assert texts(list(run)) == [f"清晨的海風{SECRET_STORY}"]
    member_session.commit()

    provider.script_stream(Chunk(f"第二次{SECRET_STORY}"), Chunk("還有"), Finish())
    second = runner.start(REQUEST)                                                  # 主要模型剛逾時：斷路沿用帳本，直接用備援
    iterator = iter(second)
    next(iterator)
    second.abort(ABORT_OUTPUT_BLOCKED, "minor")
    member_session.commit()
    assert provider.calls[-1].model == FALLBACK

    rows = member_session.scalars(select(LlmCallLog).order_by(LlmCallLog.id)).all()
    assert [(r.model, r.status, r.attempt) for r in rows] == [(PRIMARY, "timeout", 1), (FALLBACK, "ok", 2), (FALLBACK, "blocked", 1)]
    assert rows[1].first_chunk_ms == 400 and rows[1].kind == KIND_RPG_STREAM and rows[1].counted is False
    assert (rows[1].prompt_tokens, rows[1].completion_tokens) == (50, 60)
    dumped = " ".join(str(getattr(row, column.name)) for row in rows for column in LlmCallLog.__table__.columns)
    for secret in (SECRET_STORY, SECRET_ACTION, SECRET_SYSTEM, "清晨", "玩家行動", "敘事者"):
        assert secret not in dumped, secret
    assert all(row.error_detail is None or ALLOWED_DETAIL.fullmatch(row.error_detail) for row in rows)
    logged = " ".join(str(record.__dict__) for record in caplog.records)
    for secret in (SECRET_STORY, SECRET_ACTION, SECRET_SYSTEM, "清晨", "玩家行動"):
        assert secret not in logged, secret


def test_the_ledger_circuit_is_the_same_query_the_structured_calls_use(member_session):
    ledger = LlmCallLogRepository(member_session)
    breaker = ledger_breaker(ledger, lambda: NOW, 60.0)
    assert breaker(PRIMARY) is False
    record = ledger_recorder(ledger, lambda: NOW)
    from app.services.rpg_stream import StreamLedgerEntry

    record(StreamLedgerEntry(None, None, KIND_RPG_STREAM, "fake", PRIMARY, 1, "timeout", 20000, None, None, None, None, None, "timeout first_chunk"))
    assert breaker(PRIMARY) is True and breaker(FALLBACK) is False
