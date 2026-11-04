"""lockout-appeals 規格：申訴的 Repository（每小時計數、待處理、處理標記、只回自己的申訴、管理員只看被鎖定帳號的申訴、串聯刪除）。"""
from datetime import timedelta

from sqlalchemy import func, select

from app.models import LockAppeal
from app.repositories.admin_repository import AdminRepository
from app.repositories.lock_appeal_repository import LockAppealRepository
from tests.support.auth import START, add_user

NOW = START + timedelta(days=1)


def test_counts_pending_and_latest_are_per_user(member_session):
    s = member_session
    mine, other = add_user(s, 1), add_user(s, 2)
    appeals = LockAppealRepository(s)
    appeals.create(mine.id, "第一則", NOW - timedelta(minutes=90))
    appeals.create(mine.id, "第二則", NOW - timedelta(minutes=10))
    appeals.create(other.id, "別人的", NOW)
    assert appeals.count_since(mine.id, NOW - timedelta(hours=1)) == 1
    assert appeals.count_since(mine.id, NOW - timedelta(hours=2)) == 2
    assert appeals.has_pending(mine.id) is True
    assert appeals.latest_for_user(mine.id).content == "第二則"
    assert [a.content for a in appeals.list_for_user(mine.id)] == ["第一則", "第二則"]
    assert appeals.latest_for_user(add_user(s, 3).id) is None


def test_marking_handled_only_touches_pending_appeals_of_that_user(member_session):
    s = member_session
    mine, other = add_user(s, 1), add_user(s, 2)
    appeals = LockAppealRepository(s)
    appeals.create(mine.id, "待處理", NOW)
    appeals.create(other.id, "別人的", NOW)
    assert appeals.mark_pending_handled(mine.id, NOW + timedelta(minutes=5)) == 1
    s.commit()
    assert appeals.has_pending(mine.id) is False and appeals.has_pending(other.id) is True
    assert appeals.mark_pending_handled(mine.id, NOW + timedelta(minutes=6)) == 0          # 已處理的不再改動
    assert appeals.latest_for_user(mine.id).handled_at is not None


def test_the_admin_sees_appeals_only_while_the_account_is_locked(member_session):
    s = member_session
    locked = add_user(s, 1, locked_at=NOW, lock_reason="rate_limited")
    free = add_user(s, 2)
    appeals = LockAppealRepository(s)
    appeals.create(locked.id, "請解除鎖定", NOW)
    appeals.create(free.id, "我沒被鎖定", NOW)
    admin = AdminRepository(s)
    rows = admin.appeals_of_restricted_accounts(limit=10, offset=0)
    assert [r.content for r in rows] == ["請解除鎖定"] and rows[0].user_public_id == locked.public_id
    assert [r.user_public_id for r in admin.locked_accounts(limit=10, offset=0)] == [locked.public_id]
    # 解除鎖定之後，申訴不再出現在管理員的檢視中，但資料仍在
    from app.repositories.user_repository import UserRepository

    assert UserRepository(s).unlock_player(locked.id) is True
    s.commit()
    assert admin.appeals_of_restricted_accounts(limit=10, offset=0) == []
    assert s.scalar(select(func.count(LockAppeal.id))) == 2


def test_deleting_the_account_deletes_its_appeals(member_session):
    s = member_session
    user = add_user(s, 1)
    LockAppealRepository(s).create(user.id, "個資", NOW)
    s.commit()
    s.delete(user)
    s.commit()
    assert s.scalar(select(func.count(LockAppeal.id))) == 0
