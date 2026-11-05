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


# ---- 用量彙總（usage-limits：每日輪數、全站用量、安全過濾暫停）------------------------------

DAY_START = datetime(2026, 10, 28, 16, 0, tzinfo=timezone.utc)         # 台灣 10/29 00:00
DAY_END = datetime(2026, 10, 29, 16, 0, tzinfo=timezone.utc)
DURING = DAY_START + timedelta(hours=5)


@pytest.fixture()
def two_users(member_session):
    from tests.support.auth import add_user

    return add_user(member_session, 1).id, add_user(member_session, 2).id


def test_the_daily_turn_count_only_counts_counted_chat_calls_of_that_user_in_the_period(ledger, two_users):
    alice, bob = two_users
    record(ledger, user_id=alice, counted=True, at=DURING)
    record(ledger, user_id=alice, counted=True, at=DURING + timedelta(minutes=1))
    record(ledger, user_id=alice, counted=False, at=DURING)                              # 降級或失敗的嘗試
    record(ledger, user_id=alice, counted=True, kind="summary", at=DURING)               # 摘要不計
    record(ledger, user_id=bob, counted=True, at=DURING)                                 # 別人的不算
    assert ledger.count_counted_chat_calls(alice, start=DAY_START, end=DAY_END) == 2
    assert ledger.count_counted_chat_calls(bob, start=DAY_START, end=DAY_END) == 1


def test_the_period_includes_its_start_and_excludes_its_end(ledger, two_users):
    alice, _ = two_users
    record(ledger, user_id=alice, counted=True, at=DAY_START)                            # 台灣 00:00 整點屬於今天
    record(ledger, user_id=alice, counted=True, at=DAY_END)                              # 明天 00:00 整點屬於明天
    record(ledger, user_id=alice, counted=True, at=DAY_START - timedelta(seconds=1))     # 昨天最後一秒
    assert ledger.count_counted_chat_calls(alice, start=DAY_START, end=DAY_END) == 1


def test_a_user_without_any_call_has_zero_turns_and_no_blocks(ledger, two_users):
    alice, _ = two_users
    assert ledger.count_counted_chat_calls(alice, start=DAY_START, end=DAY_END) == 0
    assert ledger.blocked_chat_calls(alice, start=DAY_START, end=DAY_END) == (0, None)


def test_blocked_calls_count_only_chat_calls_with_the_blocked_status_and_report_the_latest_time(ledger, two_users):
    alice, bob = two_users
    record(ledger, user_id=alice, status="blocked", at=DURING)
    record(ledger, user_id=alice, status="blocked", at=DURING + timedelta(minutes=7))
    record(ledger, user_id=alice, status="blocked", kind="summary", at=DURING + timedelta(minutes=30))   # 摘要被擋不算
    record(ledger, user_id=alice, status="ok", at=DURING + timedelta(minutes=40))
    record(ledger, user_id=alice, status="validation_failed", at=DURING + timedelta(minutes=41))
    record(ledger, user_id=bob, status="blocked", at=DURING + timedelta(minutes=50))
    count, latest = ledger.blocked_chat_calls(alice, start=DAY_START, end=DAY_END)
    assert count == 2
    assert latest == DURING + timedelta(minutes=7) and latest.tzinfo is not None


def test_blocked_calls_of_another_day_are_not_counted(ledger, two_users):
    alice, _ = two_users
    record(ledger, user_id=alice, status="blocked", at=DAY_START - timedelta(hours=1))
    record(ledger, user_id=alice, status="blocked", at=DAY_END + timedelta(hours=1))
    assert ledger.blocked_chat_calls(alice, start=DAY_START, end=DAY_END) == (0, None)


def test_the_site_totals_add_all_three_token_kinds_and_every_row_of_every_kind(ledger, two_users):
    alice, bob = two_users
    record(ledger, user_id=alice, prompt_tokens=1000, completion_tokens=300, thinking_tokens=50, at=DURING)
    record(ledger, user_id=bob, kind="summary", prompt_tokens=400, completion_tokens=100, at=DURING)       # 摘要也算
    record(ledger, user_id=alice, status="timeout", prompt_tokens=200, at=DURING)                           # 失敗的嘗試也算
    assert ledger.usage_totals(start=DAY_START, end=DAY_END) == (2050, 3)


def test_rows_without_token_counts_still_count_as_calls(ledger):
    record(ledger, at=DURING)                                                    # 全部是空值
    record(ledger, prompt_tokens=10, at=DURING)
    assert ledger.usage_totals(start=DAY_START, end=DAY_END) == (10, 2)


def test_an_empty_period_has_zero_totals(ledger):
    assert ledger.usage_totals(start=DAY_START, end=DAY_END) == (0, 0)


def test_rows_with_a_null_user_are_part_of_the_site_totals(ledger):
    """帳號被刪除後帳本的 user_id 變成空值；用量仍屬於全站。"""
    record(ledger, user_id=None, prompt_tokens=500, at=DURING)
    assert ledger.usage_totals(start=DAY_START, end=DAY_END) == (500, 1)


def test_the_site_totals_exclude_other_days(ledger):
    record(ledger, prompt_tokens=100, at=DAY_START - timedelta(seconds=1))
    record(ledger, prompt_tokens=100, at=DAY_END)
    record(ledger, prompt_tokens=7, at=DAY_START)
    assert ledger.usage_totals(start=DAY_START, end=DAY_END) == (7, 1)


# ---- 主角生成的每日計次（protagonist-suggestion：以第一次嘗試的筆數計）------------------------------------


def test_count_first_attempts_counts_requests_not_retries_and_respects_user_kind_and_period(ledger, member_session):
    from app.models import User

    users = []
    for n in (1, 2):
        user = User(email=f"p{n}@example.com", email_canonical=f"p{n}@example.com", display_name=f"使用者{n}", status="active")
        member_session.add(user)
        member_session.flush()
        users.append(user)
    mine, other = users
    start, end = NOW - timedelta(hours=12), NOW + timedelta(hours=12)

    record(ledger, user_id=mine.id, kind="protagonist_gen", attempt=1)                       # 一次請求
    record(ledger, user_id=mine.id, kind="protagonist_gen", attempt=2, model=FALLBACK)        # 同一次請求的重試：不重複計
    record(ledger, user_id=mine.id, kind="protagonist_gen", attempt=1, at=NOW + timedelta(minutes=5))   # 第二次請求
    record(ledger, user_id=mine.id, kind="protagonist_gen", attempt=1, at=NOW - timedelta(days=1))      # 昨天：不計
    record(ledger, user_id=mine.id, kind="protagonist_gen", attempt=1, at=end)                          # 訖不含
    record(ledger, user_id=mine.id, kind="protagonist_review", attempt=1)                    # 其他類別：不計
    record(ledger, user_id=mine.id, kind="chat", attempt=1)
    record(ledger, user_id=other.id, kind="protagonist_gen", attempt=1)                      # 別人：不計
    record(ledger, user_id=None, kind="protagonist_gen", attempt=1)                          # 已匿名化：不計
    member_session.commit()

    assert ledger.count_first_attempts(mine.id, "protagonist_gen", start=start, end=end) == 2
    assert ledger.count_first_attempts(mine.id, "protagonist_review", start=start, end=end) == 1
    assert ledger.count_first_attempts(other.id, "protagonist_gen", start=start, end=end) == 1
    assert ledger.count_first_attempts(mine.id, "protagonist_gen", start=NOW + timedelta(days=3), end=NOW + timedelta(days=4)) == 0


def test_the_new_call_kinds_fit_the_kind_column_and_are_registered():
    from app.models.llm_call_log import CALL_KINDS, KIND_PROTAGONIST_GEN, KIND_PROTAGONIST_REVIEW

    assert KIND_PROTAGONIST_GEN in CALL_KINDS and KIND_PROTAGONIST_REVIEW in CALL_KINDS
    assert LlmCallLog.kind.type.length >= max(len(kind) for kind in CALL_KINDS)          # SQL Server 會拒絕超長的字串，SQLite 不會
