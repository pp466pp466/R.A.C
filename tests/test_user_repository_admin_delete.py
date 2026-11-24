"""admin-delete-account 的 Repository 層：管理員在後台刪除帳號的條件式刪除（`delete_player_by_admin`）。

以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行。
"""
import pytest
from sqlalchemy import func, select

from app.models import User
from app.models.user import STATUS_ACTIVE, STATUS_DELETION_PENDING, STATUS_SUSPENDED
from app.repositories.user_repository import UserRepository
from tests.support.auth import START, add_user

NOW = START


def count_users(session) -> int:
    return session.scalar(select(func.count(User.id)))


@pytest.mark.parametrize(
    "overrides",
    [
        {},                                                                                   # 使用中
        {"status": STATUS_SUSPENDED},                                                         # 停權
        {"locked_at": NOW, "lock_reason": "rate_limited"},                                    # 被鎖定
        {"status": STATUS_DELETION_PENDING, "deletion_requested_at": NOW, "status_before_deletion": STATUS_ACTIVE},   # 待刪除：不必等寬限期
        {"age": None},                                                                        # 還沒填生日：也刪得掉
    ],
    ids=["active", "suspended", "locked", "deletion_pending", "no_birthday"],
)
def test_any_player_account_can_be_deleted_whatever_its_state(member_session, overrides):
    s = member_session
    player = add_user(s, 1, **overrides)
    other = add_user(s, 2)
    assert UserRepository(s).delete_player_by_admin(player.id) is True
    s.commit()
    assert count_users(s) == 1
    assert s.scalar(select(User.id)) == other.id                                              # 只有指定的那一個被刪


def test_an_admin_account_is_never_deleted(member_session):
    s = member_session
    admin = add_user(s, 1, role="admin")
    player = add_user(s, 2)
    users = UserRepository(s)
    assert users.delete_player_by_admin(admin.id) is False
    s.commit()
    assert count_users(s) == 2
    assert s.get(User, admin.id) is not None
    assert users.delete_player_by_admin(player.id) is True


def test_a_missing_id_deletes_nothing_and_reports_false(member_session):
    s = member_session
    add_user(s, 1)
    assert UserRepository(s).delete_player_by_admin(987654) is False
    s.commit()
    assert count_users(s) == 1


def test_the_condition_is_rechecked_at_delete_time(member_session):
    """檢查之後、刪除之前變成管理員的帳號不會被刪（回傳 False、資料不變）。"""
    s = member_session
    user = add_user(s, 1)
    s.commit()
    user.role = "admin"
    s.flush()
    assert UserRepository(s).delete_player_by_admin(user.id) is False
    s.commit()
    assert count_users(s) == 1


def test_deleting_twice_only_succeeds_once(member_session):
    s = member_session
    player_id = add_user(s, 1).id
    users = UserRepository(s)
    assert users.delete_player_by_admin(player_id) is True
    s.commit()
    assert users.delete_player_by_admin(player_id) is False
