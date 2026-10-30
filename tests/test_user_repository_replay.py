"""deletion-replay 規格：備份還原後重新抹除用的 Repository 方法（建立時間比對、不抹除管理員、刪除紀錄的讀取）。

以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行。
"""
from datetime import timedelta

from sqlalchemy import func, select

from app.models import User
from app.repositories.deletion_tombstone_repository import DeletionTombstoneRepository
from app.repositories.user_repository import (
    REPLAY_ADMIN, REPLAY_ELIGIBLE, REPLAY_MISSING, REPLAY_REISSUED, UserRepository,
)
from tests.support.auth import START, add_user

DELETED_AT = START


def created(session, n: int, created_at, **overrides) -> User:
    return add_user(session, n, created_at=created_at, **overrides)


def test_a_user_created_before_the_deletion_time_is_eligible_and_gets_deleted(member_session):
    s = member_session
    user = created(s, 1, DELETED_AT - timedelta(days=30))
    users = UserRepository(s)
    assert users.replay_status(user.id, DELETED_AT) == REPLAY_ELIGIBLE
    assert users.delete_if_created_not_after(user.id, DELETED_AT) is True
    s.commit()
    assert s.scalar(select(func.count(User.id))) == 0


def test_a_user_created_exactly_at_the_deletion_time_is_eligible(member_session):
    s = member_session
    user = created(s, 1, DELETED_AT)
    assert UserRepository(s).replay_status(user.id, DELETED_AT) == REPLAY_ELIGIBLE
    assert UserRepository(s).delete_if_created_not_after(user.id, DELETED_AT) is True


def test_a_user_created_after_the_deletion_time_reuses_the_id_and_is_never_deleted(member_session):
    s = member_session
    newcomer = created(s, 1, DELETED_AT + timedelta(seconds=1))
    users = UserRepository(s)
    assert users.replay_status(newcomer.id, DELETED_AT) == REPLAY_REISSUED
    assert users.delete_if_created_not_after(newcomer.id, DELETED_AT) is False
    s.commit()
    assert s.scalar(select(func.count(User.id))) == 1


def test_an_admin_is_never_deleted_whatever_the_dates_say(member_session):
    s = member_session
    admin = created(s, 1, DELETED_AT - timedelta(days=400), role="admin")
    users = UserRepository(s)
    assert users.replay_status(admin.id, DELETED_AT) == REPLAY_ADMIN
    assert users.delete_if_created_not_after(admin.id, DELETED_AT) is False
    s.commit()
    assert s.scalar(select(func.count(User.id))) == 1


def test_a_missing_user_is_reported_as_missing_and_nothing_is_deleted(member_session):
    s = member_session
    users = UserRepository(s)
    assert users.replay_status(999, DELETED_AT) == REPLAY_MISSING
    assert users.delete_if_created_not_after(999, DELETED_AT) is False


def test_only_the_named_user_is_deleted(member_session):
    s = member_session
    target = created(s, 1, DELETED_AT - timedelta(days=5))
    other = created(s, 2, DELETED_AT - timedelta(days=5))
    UserRepository(s).delete_if_created_not_after(target.id, DELETED_AT)
    s.commit()
    assert s.scalars(select(User.id)).all() == [other.id]


# ---- 刪除紀錄的讀取 ---------------------------------------------------------------------


def test_tombstone_records_are_listed_with_only_the_id_and_the_time(member_session):
    s = member_session
    tombstones = DeletionTombstoneRepository(s)
    tombstones.record(user_id=9, reason="user_requested", deleted_at=DELETED_AT)
    tombstones.record(user_id=3, reason="inactive_12_months", deleted_at=DELETED_AT - timedelta(days=1))
    s.commit()
    assert tombstones.records() == [(3, DELETED_AT - timedelta(days=1)), (9, DELETED_AT)]


def test_a_newer_or_equal_record_for_the_same_id_is_found_and_an_older_one_is_not(member_session):
    s = member_session
    tombstones = DeletionTombstoneRepository(s)
    tombstones.record(user_id=5, reason="user_requested", deleted_at=DELETED_AT)
    s.commit()
    assert tombstones.has_record_not_before(5, DELETED_AT) is True
    assert tombstones.has_record_not_before(5, DELETED_AT - timedelta(days=1)) is True
    assert tombstones.has_record_not_before(5, DELETED_AT + timedelta(seconds=1)) is False
    assert tombstones.has_record_not_before(6, DELETED_AT) is False
