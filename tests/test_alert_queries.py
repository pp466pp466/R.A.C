"""operational-alerts 規格：告警用的唯讀彙總查詢（窗口邊界、結果狀態分類、p95 與手算相同、鎖定到期不計）。

以 `member_session` 同時在 SQLite 與真實 SQL Server 上執行。
"""
from datetime import timedelta

import pytest

from app.models import LlmCallLog
from app.repositories.llm_call_log_repository import LlmCallLogRepository
from app.repositories.user_repository import UserRepository
from app.services.alert_rules import percentile_offset
from tests.support.auth import START, add_user

NOW = START
START_WINDOW = NOW - timedelta(minutes=5)


def call(session, status="ok", *, kind="chat", at=NOW - timedelta(minutes=1), latency=100, user_id=None) -> LlmCallLog:
    row = LlmCallLog(
        user_id=user_id, conversation_id=None, kind=kind, provider="gemini", model="m", attempt=1, status=status,
        counted=status == "ok", latency_ms=latency, created_at=at,
    )
    session.add(row)
    session.flush()
    return row


# ---- 各結果狀態的筆數 ----------------------------------------------------------------

def test_chat_status_counts_group_by_status_and_ignore_summary_calls(member_session):
    s = member_session
    for status in ("ok", "ok", "ok", "rate_limited", "timeout", "blocked"):
        call(s, status)
    call(s, "ok", kind="summary")                                  # 摘要不屬於「對話呼叫」
    counts = LlmCallLogRepository(s).chat_status_counts(start=START_WINDOW, end=NOW)
    assert counts == {"ok": 3, "rate_limited": 1, "timeout": 1, "blocked": 1}


def test_the_window_is_start_inclusive_and_end_exclusive(member_session):
    s = member_session
    call(s, "ok", at=START_WINDOW)                                 # 剛好在起點：算
    call(s, "ok", at=START_WINDOW - timedelta(seconds=1))          # 差一秒：不算
    call(s, "ok", at=NOW)                                          # 剛好在終點：不算
    call(s, "ok", at=NOW - timedelta(seconds=1))
    assert LlmCallLogRepository(s).chat_status_counts(start=START_WINDOW, end=NOW) == {"ok": 2}


def test_an_empty_window_gives_an_empty_mapping_and_zero(member_session):
    repo = LlmCallLogRepository(member_session)
    assert repo.chat_status_counts(start=START_WINDOW, end=NOW) == {}
    assert repo.status_count("config_error", start=START_WINDOW, end=NOW) == 0


def test_status_count_includes_every_kind_and_respects_the_window(member_session):
    s = member_session
    call(s, "config_error")
    call(s, "config_error", kind="summary")
    call(s, "config_error", at=START_WINDOW - timedelta(minutes=1))
    call(s, "ok")
    assert LlmCallLogRepository(s).status_count("config_error", start=START_WINDOW, end=NOW) == 2


# ---- 延遲 p95 ----------------------------------------------------------------------

def p95_of(session, window=timedelta(minutes=15)):
    repo = LlmCallLogRepository(session)
    start = NOW - window
    n = repo.completed_chat_call_count(start=start, end=NOW)
    return n, (repo.completed_chat_latency_at(percentile_offset(n), start=start, end=NOW) if n else None)


def test_only_completed_chat_calls_count_toward_latency(member_session):
    s = member_session
    call(s, "ok", latency=1_000)
    call(s, "validation_failed", latency=2_000)
    call(s, "timeout", latency=20_000)                             # 逾時算在錯誤率，不算延遲
    call(s, "rate_limited", latency=5)
    call(s, "blocked", latency=300)
    call(s, "ok", kind="summary", latency=9_999)
    assert p95_of(s) == (2, 2_000)


@pytest.mark.parametrize("slow_count", [3, 5, 6, 10])
def test_the_database_p95_equals_the_nearest_rank_computed_by_hand(member_session, slow_count):
    s = member_session
    latencies = [2_000] * (100 - slow_count) + [12_000] * slow_count
    for value in latencies:
        call(s, "ok", latency=value)
    n, p95 = p95_of(s)
    expected = sorted(latencies)[int(-(-95 * n // 100)) - 1]       # 第 ceil(0.95·n) 小
    assert (n, p95) == (100, expected)
    assert p95 == (12_000 if slow_count >= 6 else 2_000)           # 規格的兩個情境：6% 慢會推動 p95，3% 不會


def test_latency_percentile_ties_and_a_single_row(member_session):
    s = member_session
    call(s, "ok", latency=777)
    assert p95_of(s) == (1, 777)
    for _ in range(4):
        call(s, "ok", latency=777)
    assert p95_of(s) == (5, 777)


def test_no_completed_calls_gives_zero_and_none(member_session):
    assert p95_of(member_session) == (0, None)


def test_the_latency_window_is_fifteen_minutes_when_asked_for_it(member_session):
    s = member_session
    call(s, "ok", latency=100, at=NOW - timedelta(minutes=14))
    call(s, "ok", latency=999, at=NOW - timedelta(minutes=16))
    assert p95_of(s) == (1, 100)


# ---- 被鎖定的帳號 -------------------------------------------------------------------

def test_only_accounts_still_inside_the_lock_period_are_counted_once_each(member_session):
    s = member_session
    add_user(s, 1, locked_until=NOW + timedelta(minutes=10), failed_login_count=5)
    add_user(s, 2, locked_until=NOW + timedelta(seconds=1), failed_login_count=5)
    add_user(s, 3, locked_until=NOW, failed_login_count=5)                       # 剛好到期：不算
    add_user(s, 4, locked_until=NOW - timedelta(minutes=1), failed_login_count=5)
    add_user(s, 5, failed_login_count=3)                                         # 失敗次數多但沒被鎖
    add_user(s, 6)
    assert UserRepository(s).count_locked(NOW) == 2


def test_no_locked_accounts_gives_zero(member_session):
    assert UserRepository(member_session).count_locked(NOW) == 0
