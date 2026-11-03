"""account-lockout 規格：鎖定計次與自動鎖定的 Service（門檻、視窗、類別、管理員、並行只鎖一次、稽核與日誌）。

以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行；並行測試用 `concurrent_engine`。
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AdminAuditLog, LockStrike, User
from app.models.lock_strike import STRIKE_IDENTITY_REJECTED, STRIKE_RATE_LIMITED
from app.repositories.user_repository import UserRepository
from app.services.current_user import CurrentUser
from app.services.registry import build_account_lock_service
from tests.support.auth import START, FakeClock, add_user


def make_service(session, clock=None, persist=None):
    return build_account_lock_service(session, clock=clock or FakeClock(), persist=persist)


def strikes(session, user_id: int | None = None) -> int:
    session.expire_all()
    query = select(func.count()).select_from(LockStrike)
    if user_id is not None:
        query = query.where(LockStrike.user_id == user_id)
    return session.scalar(query)


def lock_rows(session) -> list[AdminAuditLog]:
    session.expire_all()
    return list(session.scalars(select(AdminAuditLog).where(AdminAuditLog.action == "account.lock")))


# ---- 門檻 ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("kind", "limit"), [(STRIKE_IDENTITY_REJECTED, 5), (STRIKE_RATE_LIMITED, 10)])
def test_the_account_locks_exactly_on_the_threshold_strike_and_not_before(member_session, kind, limit):
    s = member_session
    user = add_user(s, 1)
    service = make_service(s)
    results = [service.register_strike(user.id, kind) for _ in range(limit)]
    assert results == [False] * (limit - 1) + [True]
    s.expire_all()
    stored = s.get(User, user.id)
    assert stored.locked_at == START and stored.lock_reason == kind


def test_the_two_kinds_are_counted_separately(member_session):
    s = member_session
    user = add_user(s, 1)
    service = make_service(s)
    for _ in range(4):
        assert service.register_strike(user.id, STRIKE_IDENTITY_REJECTED) is False
    for _ in range(9):
        assert service.register_strike(user.id, STRIKE_RATE_LIMITED) is False
    s.expire_all()
    assert s.get(User, user.id).locked_at is None and strikes(s, user.id) == 13


def test_the_window_is_fifteen_minutes_sliding_and_strikes_outside_it_do_not_count(member_session):
    s = member_session
    user = add_user(s, 1)
    clock = FakeClock()
    service = make_service(s, clock)
    for _ in range(4):
        service.register_strike(user.id, STRIKE_IDENTITY_REJECTED)
    clock.advance(minutes=15)                                          # 前四次恰好滿 15 分鐘：視窗是嚴格大於，不計入
    assert service.register_strike(user.id, STRIKE_IDENTITY_REJECTED) is False
    s.expire_all()
    assert s.get(User, user.id).locked_at is None


def test_four_strikes_just_inside_the_window_plus_a_new_one_lock(member_session):
    s = member_session
    user = add_user(s, 1)
    clock = FakeClock()
    service = make_service(s, clock)
    for _ in range(4):
        service.register_strike(user.id, STRIKE_IDENTITY_REJECTED)
    clock.advance(minutes=14, seconds=59)
    assert service.register_strike(user.id, STRIKE_IDENTITY_REJECTED) is True


def test_a_lock_is_never_released_by_time(member_session):
    s = member_session
    user = add_user(s, 1)
    service = make_service(s)
    for _ in range(5):
        service.register_strike(user.id, STRIKE_IDENTITY_REJECTED)
    s.expire_all()
    stored = s.get(User, user.id)
    assert CurrentUser.from_model(stored).restriction == "locked"
    # 48 小時後（計次早已過期）仍然鎖定：解除只有管理員能做
    future = START + timedelta(hours=48)
    assert stored.locked_at is not None and stored.locked_at < future
    assert UserRepository(s).count_locked(future) == 1


# ---- 不計次的對象 -------------------------------------------------------------------------------------

def test_an_admin_is_never_counted_or_locked(member_session):
    s = member_session
    admin = add_user(s, 1, role="admin")
    service = make_service(s)
    for _ in range(12):
        assert service.register_strike(admin.id, STRIKE_RATE_LIMITED) is False
    assert strikes(s) == 0 and s.get(User, admin.id).locked_at is None


def test_a_locked_account_collects_no_more_strikes(member_session):
    s = member_session
    user = add_user(s, 1, locked_at=START, lock_reason=STRIKE_RATE_LIMITED)
    assert make_service(s).register_strike(user.id, STRIKE_IDENTITY_REJECTED) is False
    assert strikes(s) == 0


def test_an_unknown_account_is_ignored_and_an_unknown_kind_is_refused(member_session):
    service = make_service(member_session)
    assert service.register_strike(999999, STRIKE_RATE_LIMITED) is False and strikes(member_session) == 0
    user = add_user(member_session, 1)
    with pytest.raises(ValueError):
        service.register_strike(user.id, "something_else")
    assert strikes(member_session) == 0


# ---- 稽核、日誌與保存 ---------------------------------------------------------------------------------

def test_locking_writes_one_audit_row_without_an_admin_and_one_log_event_with_only_the_internal_id(member_session, log_lines):
    s = member_session
    user = add_user(s, 1, email="private.person@example.com", email_canonical="private.person@example.com", display_name="私人姓名")
    service = make_service(s)
    for _ in range(10):
        service.register_strike(user.id, STRIKE_RATE_LIMITED)
    (audit,) = lock_rows(s)
    assert audit.admin_user_id is None and audit.target_user_public_id == user.public_id
    assert audit.detail_json == {"reason": STRIKE_RATE_LIMITED} and audit.reason is None and audit.created_at == START
    events = [line for line in log_lines() if line.get("event") == "account.player_locked"]
    assert len(events) == 1 and events[0]["user_id"] == user.id and events[0]["level"] == "WARNING"
    for secret in ("private.person@example.com", "私人姓名"):
        assert secret not in log_lines.raw() and secret not in repr(audit.detail_json)


def test_the_strike_is_persisted_through_the_given_hook_only_when_it_was_recorded(member_session):
    s = member_session
    calls: list[int] = []
    service = make_service(s, persist=lambda: calls.append(1))
    user = add_user(s, 1)
    admin = add_user(s, 2, role="admin")
    service.register_strike(admin.id, STRIKE_RATE_LIMITED)             # 管理員不計次：不需要保留
    service.register_strike(999999, STRIKE_RATE_LIMITED)
    assert calls == []
    service.register_strike(user.id, STRIKE_RATE_LIMITED)
    service.register_strike(user.id, STRIKE_IDENTITY_REJECTED)
    assert calls == [1, 1]


# ---- 並行 ---------------------------------------------------------------------------------------------

def test_eight_parallel_threshold_strikes_lock_the_account_once_and_write_one_audit_row(concurrent_engine):
    with Session(concurrent_engine) as setup:
        user = add_user(setup, 1)
        for _ in range(9):                                              # 已有 9 次限流：再來的每一個請求自己的那一筆都是第 10 次
            setup.add(LockStrike(user_id=user.id, kind=STRIKE_RATE_LIMITED, created_at=START - timedelta(minutes=1)))
        setup.commit()
        user_id = user.id

    barrier = Barrier(8)

    def task(_index: int) -> bool:
        with Session(concurrent_engine) as session:
            service = make_service(session)
            barrier.wait(timeout=30)
            locked = service.register_strike(user_id, STRIKE_RATE_LIMITED)
            session.commit()
            return locked

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [f.result(timeout=60) for f in [pool.submit(task, i) for i in range(8)]]

    assert sorted(results) == [False] * 7 + [True]
    with Session(concurrent_engine) as check:
        assert check.get(User, user_id).locked_at is not None
        assert len(lock_rows(check)) == 1
