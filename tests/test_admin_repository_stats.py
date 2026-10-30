"""admin-console 規格「總覽只有彙總資料」與 admin-content-review 規格「被標記清單」的計數：後台 Repository 的彙總查詢。"""
from datetime import datetime, timedelta, timezone

import pytest

from app.models.user import MEMBER_TIERS, STATUS_ACTIVE, STATUS_SUSPENDED, USER_STATUSES
from app.repositories.admin_repository import AdminRepository, DayUsage
from app.repositories.llm_call_log_repository import LlmCallLogRepository
from tests.support.auth import add_user

START = datetime(2026, 10, 28, 16, 0, tzinfo=timezone.utc)            # 台灣 10/29 00:00
END = datetime(2026, 10, 29, 16, 0, tzinfo=timezone.utc)
DURING = START + timedelta(hours=5)


@pytest.fixture()
def repo(member_session):
    return AdminRepository(member_session)


@pytest.fixture()
def ledger(member_session):
    return LlmCallLogRepository(member_session)


def call(ledger, *, user_id=None, kind="chat", status="ok", counted=False, at=DURING, **tokens):
    return ledger.add(
        user_id=user_id, conversation_id=None, kind=kind, provider="fake", model="m", attempt=1, status=status,
        counted=counted, latency_ms=10, created_at=at, **tokens,
    )


# ---- 使用者人數 -----------------------------------------------------------------------------

def test_users_are_counted_by_status_and_by_tier(repo, member_session):
    add_user(member_session, 1)
    add_user(member_session, 2, member_tier="plus")
    add_user(member_session, 3, member_tier="plus", status=STATUS_SUSPENDED)
    add_user(member_session, 4, member_tier="vip", role="admin")
    assert repo.count_users_by_status() == {STATUS_ACTIVE: 3, STATUS_SUSPENDED: 1}
    assert repo.count_users_by_tier() == {"free": 1, "plus": 2, "vip": 1}


def test_counting_users_with_nobody_returns_empty_mappings(repo):
    assert repo.count_users_by_status() == {} and repo.count_users_by_tier() == {}


def test_the_counts_never_include_any_user_identity(repo, member_session):
    add_user(member_session, 1)
    for mapping in (repo.count_users_by_status(), repo.count_users_by_tier()):
        assert all(isinstance(key, str) and isinstance(value, int) for key, value in mapping.items())
        assert set(mapping) <= set(USER_STATUSES) | set(MEMBER_TIERS)


# ---- 某個台灣日的用量 -----------------------------------------------------------------------

def test_an_empty_day_is_all_zeros(repo):
    assert repo.usage_for_day(start=START, end=END) == DayUsage(tokens=0, calls=0, turns=0, blocked_calls=0)


def test_the_day_sums_tokens_counts_calls_turns_and_blocked_calls(repo, ledger, member_session):
    user = add_user(member_session, 1)
    call(ledger, user_id=user.id, counted=True, prompt_tokens=1000, completion_tokens=300, thinking_tokens=50)
    call(ledger, user_id=user.id, counted=True, prompt_tokens=800, completion_tokens=200)
    call(ledger, user_id=user.id, status="blocked", prompt_tokens=500)
    call(ledger, user_id=user.id, kind="summary", prompt_tokens=400, completion_tokens=100)
    call(ledger, user_id=user.id, status="timeout")                                   # 失敗的嘗試：算一次呼叫、token 為空值
    assert repo.usage_for_day(start=START, end=END) == DayUsage(tokens=3350, calls=5, turns=2, blocked_calls=1)


def test_a_summary_call_is_neither_a_turn_nor_a_blocked_call(repo, ledger):
    call(ledger, kind="summary", status="blocked", counted=True, prompt_tokens=10)
    assert repo.usage_for_day(start=START, end=END) == DayUsage(tokens=10, calls=1, turns=0, blocked_calls=0)


def test_rows_without_a_user_are_part_of_the_day(repo, ledger):
    call(ledger, user_id=None, counted=True, prompt_tokens=7)
    assert repo.usage_for_day(start=START, end=END) == DayUsage(tokens=7, calls=1, turns=1, blocked_calls=0)


def test_the_period_includes_its_start_and_excludes_its_end(repo, ledger):
    call(ledger, at=START - timedelta(seconds=1), prompt_tokens=100)
    call(ledger, at=START, prompt_tokens=1)
    call(ledger, at=END - timedelta(microseconds=1), prompt_tokens=10)
    call(ledger, at=END, prompt_tokens=1000)
    assert repo.usage_for_day(start=START, end=END) == DayUsage(tokens=11, calls=2, turns=0, blocked_calls=0)


def test_consecutive_days_do_not_overlap(repo, ledger):
    call(ledger, at=END, prompt_tokens=5)
    assert repo.usage_for_day(start=START, end=END).calls == 0
    assert repo.usage_for_day(start=END, end=END + timedelta(days=1)).calls == 1


# ---- 今日被擋的使用者 -----------------------------------------------------------------------

def test_blocked_users_are_grouped_with_their_count_and_latest_time(repo, ledger, member_session):
    alice, bob = add_user(member_session, 1), add_user(member_session, 2)
    call(ledger, user_id=alice.id, status="blocked", at=DURING)
    call(ledger, user_id=alice.id, status="blocked", at=DURING + timedelta(minutes=9))
    call(ledger, user_id=alice.id, status="blocked", at=DURING + timedelta(minutes=3))
    call(ledger, user_id=bob.id, status="blocked", at=DURING + timedelta(minutes=1))
    result = repo.blocked_by_user(start=START, end=END)
    assert result == {alice.id: (3, DURING + timedelta(minutes=9)), bob.id: (1, DURING + timedelta(minutes=1))}
    assert all(latest.tzinfo is not None for _, latest in result.values())


def test_only_chat_calls_with_the_blocked_status_count(repo, ledger, member_session):
    user = add_user(member_session, 1)
    call(ledger, user_id=user.id, status="blocked", kind="summary")
    call(ledger, user_id=user.id, status="ok")
    call(ledger, user_id=user.id, status="validation_failed")
    assert repo.blocked_by_user(start=START, end=END) == {}


def test_rows_of_other_days_and_of_deleted_users_are_not_counted(repo, ledger, member_session):
    user = add_user(member_session, 1)
    call(ledger, user_id=user.id, status="blocked", at=START - timedelta(seconds=1))
    call(ledger, user_id=user.id, status="blocked", at=END)
    call(ledger, user_id=None, status="blocked")
    assert repo.blocked_by_user(start=START, end=END) == {}


def test_the_page_filter_limits_the_users_looked_at(repo, ledger, member_session):
    alice, bob = add_user(member_session, 1), add_user(member_session, 2)
    call(ledger, user_id=alice.id, status="blocked")
    call(ledger, user_id=bob.id, status="blocked")
    assert set(repo.blocked_by_user(start=START, end=END, user_ids=[bob.id])) == {bob.id}
    assert repo.blocked_by_user(start=START, end=END, user_ids=[]) == {}
    assert repo.blocked_by_user(start=START, end=END, user_ids=[999999]) == {}
