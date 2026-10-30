"""account-deletion 規格：申請與撤銷刪除的條件式 UPDATE（前置條件、寬限期邊界、並行只成立一次）。

一般測試以 `member_session` 同時在 SQLite 與真實 SQL Server 上執行；並行測試用 `concurrent_engine` 與真實的執行緒。
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy.orm import Session

from app.models import User
from app.models.user import (
    DELETION_GRACE_PERIOD, STATUS_ACTIVE, STATUS_DELETION_PENDING, STATUS_PENDING_VERIFICATION, STATUS_SUSPENDED,
)
from app.repositories.user_repository import UserRepository
from tests.support.auth import START, add_user

NOW = START


def reload(session, user: User) -> User:
    session.expire_all()
    return session.get(User, user.id)


# ---- 申請 -------------------------------------------------------------------------

@pytest.mark.parametrize("status", [STATUS_ACTIVE, STATUS_PENDING_VERIFICATION])
def test_an_active_or_unverified_member_can_request_deletion_and_everything_is_recorded_at_once(member_session, status):
    user = add_user(member_session, 1, status=status)
    assert UserRepository(member_session).request_deletion(user, now=NOW) is True
    member_session.commit()
    fresh = reload(member_session, user)
    assert fresh.status == STATUS_DELETION_PENDING and fresh.deletion_requested_at == NOW
    assert fresh.status_before_deletion == status          # 申請前的狀態是更新前的值，不是新的 deletion_pending
    assert fresh.token_version == 2                          # 所有裝置的登入狀態失效


@pytest.mark.parametrize("status", [STATUS_SUSPENDED, STATUS_DELETION_PENDING])
def test_a_suspended_or_already_pending_member_cannot_request_deletion(member_session, status):
    user = add_user(member_session, 1, status=status, deletion_requested_at=NOW - timedelta(days=1), status_before_deletion=STATUS_ACTIVE)
    assert UserRepository(member_session).request_deletion(user, now=NOW) is False
    fresh = reload(member_session, user)
    assert fresh.status == status and fresh.token_version == 1
    assert fresh.deletion_requested_at == NOW - timedelta(days=1)       # 已在待刪除的申請時間不被延後或覆蓋


def test_an_admin_cannot_request_deletion(member_session):
    admin = add_user(member_session, 1, role="admin")
    assert UserRepository(member_session).request_deletion(admin, now=NOW) is False
    fresh = reload(member_session, admin)
    assert fresh.status == STATUS_ACTIVE and fresh.deletion_requested_at is None and fresh.token_version == 1


def test_requesting_deletion_keeps_unsaved_changes_to_other_columns(member_session):
    """`_run_update` 先 flush：還沒寫入的修改不會因為條件式 UPDATE 而遺失。"""
    user = add_user(member_session, 1)
    user.display_name = "新名字"
    assert UserRepository(member_session).request_deletion(user, now=NOW)
    member_session.commit()
    assert reload(member_session, user).display_name == "新名字"


# ---- 撤銷 -------------------------------------------------------------------------

def requested(session, n: int = 1, *, at=NOW, before=STATUS_ACTIVE, **overrides) -> User:
    return add_user(session, n, status=STATUS_DELETION_PENDING, deletion_requested_at=at, status_before_deletion=before, **overrides)


def test_cancelling_restores_the_previous_status_clears_the_columns_and_keeps_the_session_valid(member_session):
    user = requested(member_session, at=NOW - timedelta(days=3))
    assert UserRepository(member_session).cancel_deletion(user, now=NOW) is True
    fresh = reload(member_session, user)
    assert fresh.status == STATUS_ACTIVE and fresh.deletion_requested_at is None and fresh.status_before_deletion is None
    assert fresh.token_version == 1                           # 不改變 token_version


def test_the_grace_period_boundary_is_seven_days_exactly(member_session):
    just_inside = requested(member_session, 1, at=NOW - DELETION_GRACE_PERIOD + timedelta(seconds=1))
    exactly = requested(member_session, 2, at=NOW - DELETION_GRACE_PERIOD)
    users = UserRepository(member_session)
    assert users.cancel_deletion(just_inside, now=NOW) is True
    assert users.cancel_deletion(exactly, now=NOW) is False        # 剛好 7 天：寬限期已結束，交給排程刪除
    assert reload(member_session, exactly).status == STATUS_DELETION_PENDING


def test_a_pending_verification_account_stays_unverified_after_cancelling(member_session):
    user = requested(member_session, before=STATUS_PENDING_VERIFICATION)
    assert UserRepository(member_session).cancel_deletion(user, now=NOW)
    assert reload(member_session, user).status == STATUS_PENDING_VERIFICATION


def test_an_account_verified_while_pending_becomes_active_on_cancel(member_session):
    """待刪除期間以 Google 登入完成了信箱驗證：撤銷後是 active，而不是又回到待驗證。"""
    user = requested(member_session, before=STATUS_PENDING_VERIFICATION, email_verified_at=NOW - timedelta(hours=1))
    assert UserRepository(member_session).cancel_deletion(user, now=NOW)
    assert reload(member_session, user).status == STATUS_ACTIVE


def test_an_empty_previous_status_falls_back_on_whether_the_email_is_verified(member_session):
    verified = requested(member_session, 1, before=None, email_verified_at=NOW)
    unverified = requested(member_session, 2, before=None)
    users = UserRepository(member_session)
    assert users.cancel_deletion(verified, now=NOW) and users.cancel_deletion(unverified, now=NOW)
    assert reload(member_session, verified).status == STATUS_ACTIVE
    assert reload(member_session, unverified).status == STATUS_PENDING_VERIFICATION


def test_cancelling_requires_a_pending_account_with_a_request_time(member_session):
    active = add_user(member_session, 1)
    no_time = add_user(member_session, 2, status=STATUS_DELETION_PENDING)
    users = UserRepository(member_session)
    assert users.cancel_deletion(active, now=NOW) is False and users.cancel_deletion(no_time, now=NOW) is False
    assert reload(member_session, no_time).status == STATUS_DELETION_PENDING


# ---- 並行 -------------------------------------------------------------------------

def run_in_threads(engine, worker, count: int = 6):
    barrier = Barrier(count)

    def task(index: int):
        with Session(engine) as s:
            barrier.wait(timeout=30)
            result = worker(s, index)
            s.commit()
            return result

    with ThreadPoolExecutor(max_workers=count) as pool:
        return [f.result(timeout=60) for f in [pool.submit(task, i) for i in range(count)]]


def test_parallel_requests_succeed_exactly_once_and_bump_the_token_version_once(concurrent_engine):
    with Session(concurrent_engine) as s:
        user_id = add_user(s, 1).id

    def worker(s: Session, _index: int) -> bool:
        stale = s.get(User, user_id)                           # 每個執行緒都先讀到 active
        return UserRepository(s).request_deletion(stale, now=NOW)

    results = run_in_threads(concurrent_engine, worker)
    assert sorted(results) == [False] * 5 + [True]
    with Session(concurrent_engine) as s:
        fresh = s.get(User, user_id)
        assert (fresh.status, fresh.status_before_deletion, fresh.token_version) == (STATUS_DELETION_PENDING, STATUS_ACTIVE, 2)


def test_parallel_cancellations_succeed_exactly_once(concurrent_engine):
    with Session(concurrent_engine) as s:
        user_id = requested(s, at=NOW - timedelta(days=1)).id

    def worker(s: Session, _index: int) -> bool:
        return UserRepository(s).cancel_deletion(s.get(User, user_id), now=NOW)

    results = run_in_threads(concurrent_engine, worker)
    assert sorted(results) == [False] * 5 + [True]
