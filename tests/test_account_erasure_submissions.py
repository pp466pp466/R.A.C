"""privacy-lifecycle「刪除帳號時送審紀錄一併消失」：抹除一位玩家時，他的劇本送審紀錄不存在，別人的紀錄不受影響。

- 紀錄只有結果、類別與時間（沒有劇本文字），隨帳號刪除（外鍵串聯刪除），不看「匿名保留」偏好，也不留下任何可回溯的關聯。
- 條件不成立（使用者剛好登入）時整個交易回復，紀錄一併復原。
- 抹除不受暫停發佈旗標影響（被暫停的帳號照樣可以被刪除）。

以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行。
"""
from datetime import timedelta

from sqlalchemy import func, select

from app.models import DeletionTombstone, ScriptSubmission, User
from app.repositories.deletion_tombstone_repository import DeletionTombstoneRepository
from app.repositories.script_submission_repository import ScriptSubmissionRepository
from app.repositories.user_repository import UserRepository
from app.services.account_erasure_service import AccountErasureService
from tests.support.auth import START, add_user

CUTOFF = START + timedelta(days=1)


def erase_inactive(session, user_id: int) -> bool:
    service = AccountErasureService(
        users=UserRepository(session), tombstones=DeletionTombstoneRepository(session), commit=session.commit, rollback=session.rollback,
    )
    return service.erase_inactive(user_id, cutoff=CUTOFF, now=START)


def old_user(session, n: int, **overrides) -> User:
    return add_user(session, n, last_login_at=START - timedelta(days=400), created_at=START - timedelta(days=500), **overrides)


def owners(session) -> list[int]:
    session.expire_all()
    return sorted(session.scalars(select(ScriptSubmission.user_id)))


def seed(session, user: User, others: User) -> None:
    repo = ScriptSubmissionRepository(session)
    repo.record(user.id, "auto_rejected", ("minor_sexual",), START)
    repo.record(user.id, "sent", (), START + timedelta(hours=1))
    repo.record(others.id, "sent", (), START)
    session.commit()


def test_erasing_a_player_removes_their_submission_records_and_only_theirs(member_session):
    s = member_session
    gone, kept = old_user(s, 1), add_user(s, 2)
    seed(s, gone, kept)
    gone_id, kept_id = gone.id, kept.id
    assert owners(s) == sorted([gone_id, gone_id, kept_id])
    assert erase_inactive(s, gone_id) is True
    s.expire_all()
    assert s.get(User, gone_id) is None
    assert owners(s) == [kept_id]
    assert s.scalar(select(func.count(DeletionTombstone.id))) == 1


def test_a_suspended_account_is_erased_like_any_other(member_session):
    s = member_session
    gone, kept = old_user(s, 1, publish_suspended_at=START - timedelta(days=30)), add_user(s, 2)
    seed(s, gone, kept)
    gone_id, kept_id = gone.id, kept.id
    assert erase_inactive(s, gone_id) is True
    assert owners(s) == [kept_id]


def test_when_the_erasure_condition_no_longer_holds_the_records_stay(member_session):
    s = member_session
    user, other = old_user(s, 1), add_user(s, 2)
    seed(s, user, other)
    user.last_login_at = START + timedelta(days=2)             # 剛好又登入了：條件在刪除當下再確認一次，不成立
    s.commit()
    user_id, other_id = user.id, other.id
    assert erase_inactive(s, user_id) is False
    assert owners(s) == sorted([user_id, user_id, other_id])
    s.expire_all()
    assert s.get(User, user_id) is not None
