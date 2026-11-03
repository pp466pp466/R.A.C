"""account-retention 規格：逾時未完成年齡確認帳號的清除服務——判斷在刪除當下再確認、批次、並行完成年齡確認不會被誤刪。

以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行。
"""
from datetime import timedelta

import pytest

from app.models import DeletionTombstone, User
from app.repositories.deletion_tombstone_repository import DeletionTombstoneRepository
from app.repositories.user_repository import UserRepository
from app.services.account_retention_service import AccountRetentionService
from tests.support.age import age_columns
from tests.support.auth import START, FakeClock, add_user

OVERDUE = START - timedelta(hours=1)


def make_service(session, users=None, **kwargs):
    return AccountRetentionService(
        users=users or UserRepository(session), tombstones=DeletionTombstoneRepository(session),
        clock=FakeClock(), commit=session.commit, rollback=session.rollback, **kwargs,
    )


def overdue(session, n: int, **overrides) -> User:
    return add_user(session, n, age=None, age_deadline_at=OVERDUE, **overrides)


def test_the_hours_around_the_deadline_decide_who_goes(member_session):
    s = member_session
    twenty_five = add_user(s, 1, age=None, age_deadline_at=START - timedelta(hours=1)).id        # 建立滿 25 小時
    twenty_three = add_user(s, 2, age=None, age_deadline_at=START + timedelta(hours=1)).id       # 建立 23 小時
    result = make_service(s).purge_unconfirmed()
    assert (result.matched, result.deleted, result.dry_run) == (1, 1, False)
    s.expire_all()
    assert s.get(User, twenty_five) is None and s.get(User, twenty_three) is not None


def test_a_player_who_finishes_the_age_step_during_the_purge_is_kept_and_leaves_no_record(member_session, log_lines):
    class FinishesDuringPurge(UserRepository):
        def delete_overdue_unconfirmed(self, user_id, now):
            row = self._session.get(User, user_id)
            for key, value in age_columns(30).items():          # 判斷之後、刪除之前剛好補填完成
                setattr(row, key, value)
            row.age_deadline_at = None
            self._session.flush()
            return super().delete_overdue_unconfirmed(user_id, now)

    user = overdue(member_session, 1)
    result = make_service(member_session, FinishesDuringPurge(member_session)).purge_unconfirmed()
    assert (result.matched, result.deleted) == (1, 0)
    member_session.expire_all()
    assert member_session.get(User, user.id) is not None
    assert member_session.query(DeletionTombstone).count() == 0
    assert [line for line in log_lines() if line.get("event") == "account.unconfirmed_purged"] == []


def test_many_accounts_are_handled_across_several_batches(member_session):
    for n in range(1, 8):
        overdue(member_session, n)
    result = make_service(member_session, batch_size=3).purge_unconfirmed()
    assert (result.matched, result.deleted) == (7, 7)
    assert member_session.query(User).count() == 0 and member_session.query(DeletionTombstone).count() == 7


def test_a_batch_where_nothing_could_be_deleted_ends_the_run_instead_of_looping(member_session):
    class NeverDeletes(UserRepository):
        def delete_overdue_unconfirmed(self, user_id, now):
            return False

    overdue(member_session, 1)
    result = make_service(member_session, NeverDeletes(member_session)).purge_unconfirmed()
    assert (result.matched, result.deleted) == (1, 0)


def test_each_deletion_is_committed_with_its_tombstone_so_a_failure_midway_keeps_what_was_done(member_session):
    class FailsOnSecond(UserRepository):
        calls = 0

        def delete_overdue_unconfirmed(self, user_id, now):
            FailsOnSecond.calls += 1
            if FailsOnSecond.calls == 2:
                raise RuntimeError("資料庫暫時失敗")
            return super().delete_overdue_unconfirmed(user_id, now)

    for n in (1, 2, 3):
        overdue(member_session, n)
    with pytest.raises(RuntimeError):
        make_service(member_session, FailsOnSecond(member_session)).purge_unconfirmed()
    member_session.rollback()
    assert member_session.query(User).count() == 2 and member_session.query(DeletionTombstone).count() == 1


def test_the_preview_counts_without_touching_anything(member_session):
    for n in (1, 2):
        overdue(member_session, n)
    result = make_service(member_session).purge_unconfirmed(dry_run=True)
    assert (result.matched, result.deleted, result.dry_run) == (2, 0, True)
    assert member_session.query(User).count() == 2 and member_session.query(DeletionTombstone).count() == 0


def test_the_inactive_purge_is_unaffected_by_unconfirmed_accounts(member_session):
    """兩個清除各自獨立：閒置清除不會刪到只是逾時未確認、但最近登入過的帳號（反之亦然）。"""
    overdue(member_session, 1, last_login_at=START)
    assert make_service(member_session).purge_inactive().deleted == 0
    assert make_service(member_session).purge_unconfirmed().deleted == 1
