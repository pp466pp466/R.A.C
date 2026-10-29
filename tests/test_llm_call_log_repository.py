"""llm-structured-calls 規格：用量帳本的寫入與斷路查詢。"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import LlmCallLog
from app.repositories.llm_call_log_repository import ERROR_DETAIL_LIMIT, LlmCallLogRepository

NOW = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)
PRIMARY, FALLBACK = "gemini-3.8-flash", "gemini-3.5-flash-lite"


@pytest.fixture()
def ledger(member_session):
    return LlmCallLogRepository(member_session)


def record(ledger, *, model=PRIMARY, status="ok", at=NOW, **extra):
    values = dict(
        user_id=None, conversation_id=None, kind="chat", provider="gemini", model=model, attempt=1, status=status,
        counted=False, latency_ms=100, created_at=at,
    )
    values.update(extra)
    return ledger.add(**values)


def test_a_row_keeps_every_documented_field(ledger, member_session):
    row = record(
        ledger, status="ok", counted=True, prompt_tokens=120, completion_tokens=300, thinking_tokens=None,
        finish_reason="stop", latency_ms=2700, attempt=2, model=FALLBACK, conversation_id=7, kind="summary",
    )
    member_session.commit()
    stored = member_session.scalars(select(LlmCallLog)).one()
    assert stored.id == row.id
    assert (stored.kind, stored.provider, stored.model, stored.attempt, stored.status) == ("summary", "gemini", FALLBACK, 2, "ok")
    assert (stored.counted, stored.prompt_tokens, stored.completion_tokens) == (True, 120, 300)
    assert stored.thinking_tokens is None                                       # 供應商沒給：空值，不是 0
    assert (stored.finish_reason, stored.latency_ms, stored.conversation_id) == ("stop", 2700, 7)
    assert stored.created_at == NOW


@pytest.mark.parametrize(("length", "kept"), [(1999, 1999), (2000, 2000), (2001, 2000), (5000, 2000)])
def test_error_detail_is_cut_at_2000_characters_when_written(ledger, member_session, length, kept):
    record(ledger, status="validation_failed", error_detail="錯" * length)
    member_session.commit()
    assert len(member_session.scalars(select(LlmCallLog)).one().error_detail) == kept == min(length, ERROR_DETAIL_LIMIT)


def test_a_failure_without_text_stores_no_detail(ledger, member_session):
    record(ledger, status="timeout")
    member_session.commit()
    assert member_session.scalars(select(LlmCallLog)).one().error_detail is None


def test_chinese_detail_round_trips(ledger, member_session):
    record(ledger, status="validation_failed", error_detail="這不是 JSON：{ 缺了右括號 😀")
    member_session.commit()
    member_session.expire_all()
    assert member_session.scalars(select(LlmCallLog)).one().error_detail == "這不是 JSON：{ 缺了右括號 😀"


# ---- 斷路查詢 ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["timeout", "server_error"])
def test_a_recent_timeout_or_server_error_marks_the_model_unhealthy(ledger, status):
    record(ledger, status=status, at=NOW - timedelta(seconds=10))
    assert ledger.model_unhealthy_since(PRIMARY, NOW - timedelta(seconds=60)) is True


@pytest.mark.parametrize("status", ["ok", "rate_limited", "validation_failed", "blocked", "config_error", "network"])
def test_other_outcomes_never_mark_the_model_unhealthy(ledger, status):
    """速率限制回得快、沒有人在等；驗證失敗與被阻擋是內容問題，不是模型壞了。"""
    record(ledger, status=status, at=NOW - timedelta(seconds=1))
    assert ledger.model_unhealthy_since(PRIMARY, NOW - timedelta(seconds=60)) is False


def test_the_window_boundary_is_inclusive_at_the_start(ledger):
    record(ledger, status="timeout", at=NOW - timedelta(seconds=59))
    assert ledger.model_unhealthy_since(PRIMARY, NOW - timedelta(seconds=60)) is True
    assert ledger.model_unhealthy_since(PRIMARY, NOW - timedelta(seconds=58)) is False       # 59 秒前的失敗在 58 秒窗口外


def test_a_failure_older_than_the_window_is_ignored(ledger):
    record(ledger, status="timeout", at=NOW - timedelta(seconds=61))
    assert ledger.model_unhealthy_since(PRIMARY, NOW - timedelta(seconds=60)) is False


def test_models_do_not_affect_each_other(ledger):
    record(ledger, model=PRIMARY, status="timeout", at=NOW - timedelta(seconds=5))
    assert ledger.model_unhealthy_since(FALLBACK, NOW - timedelta(seconds=60)) is False
    assert ledger.model_unhealthy_since(PRIMARY, NOW - timedelta(seconds=60)) is True


def test_an_empty_ledger_means_healthy(ledger):
    assert ledger.model_unhealthy_since(PRIMARY, NOW - timedelta(seconds=60)) is False


def test_the_answer_comes_from_the_database_so_another_session_sees_it(db_engine, member_session):
    """多個 worker 各有自己的連線與 Session；斷路狀態必須只看資料庫，而不是任何行程內的狀態。"""
    from sqlalchemy.orm import Session

    record(LlmCallLogRepository(member_session), status="timeout", at=NOW - timedelta(seconds=3))
    member_session.commit()
    with Session(db_engine) as other:
        assert LlmCallLogRepository(other).model_unhealthy_since(PRIMARY, NOW - timedelta(seconds=60)) is True
