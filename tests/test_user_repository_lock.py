"""google-first-login：UserRepository 的新方法——逾時未補填的帳號、補填時的抹除、玩家帳號鎖定與解除、鎖定計數。

以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行。
"""
from datetime import timedelta

from sqlalchemy import func, select

from app.models import User
from app.repositories.user_repository import UserRepository
from tests.support.auth import START, add_user

NOW = START + timedelta(days=3)


def unconfirmed(session, n: int, *, deadline, **overrides) -> User:
    return add_user(session, n, age=None, age_deadline_at=deadline, **overrides)


# ---- 逾時未補填 -----------------------------------------------------------------------------------


def test_only_players_without_a_birthday_whose_deadline_has_passed_are_overdue(member_session):
    s = member_session
    overdue = unconfirmed(s, 1, deadline=NOW - timedelta(hours=1))
    not_yet = unconfirmed(s, 2, deadline=NOW + timedelta(hours=1))
    exactly = unconfirmed(s, 3, deadline=NOW)
    legacy_without_deadline = add_user(s, 4, age=None)                                           # 舊帳號：沒有期限記錄
    admin = unconfirmed(s, 5, deadline=NOW - timedelta(days=5), role="admin")
    confirmed = add_user(s, 6, age=30, age_deadline_at=NOW - timedelta(days=5))        # 已完成（期限殘留）
    users = UserRepository(s)
    assert set(users.find_overdue_unconfirmed_ids(NOW)) == {overdue.id, exactly.id}
    assert users.count_overdue_unconfirmed(NOW) == 2
    assert not {not_yet.id, legacy_without_deadline.id, admin.id, confirmed.id} & set(users.find_overdue_unconfirmed_ids(NOW))


def test_the_overdue_query_respects_the_batch_limit_and_returns_only_ids(member_session):
    s = member_session
    for n in range(1, 6):
        unconfirmed(s, n, deadline=NOW - timedelta(hours=n))
    ids = UserRepository(s).find_overdue_unconfirmed_ids(NOW, limit=3)
    assert len(ids) == 3 and all(isinstance(i, int) for i in ids) and ids == sorted(ids)


def test_the_overdue_delete_rechecks_the_condition_at_delete_time(member_session):
    s = member_session
    user = unconfirmed(s, 1, deadline=NOW - timedelta(hours=1))
    users = UserRepository(s)
    assert users.find_overdue_unconfirmed_ids(NOW) == [user.id]
    # 選出之後、刪除之前，使用者剛好完成年齡確認
    assert users.complete_age_verification_if_unset(
        user, birth_date=NOW.date().replace(year=NOW.year - 30), age_range="25-34", is_adult=True, confirmed_at=NOW, policy_version="v-test",
    ) is True
    s.commit()
    assert users.delete_overdue_unconfirmed(user.id, NOW) is False
    assert s.scalar(select(func.count(User.id))) == 1


def test_completing_the_age_confirmation_clears_the_deadline(member_session):
    s = member_session
    user = unconfirmed(s, 1, deadline=NOW + timedelta(hours=5))
    users = UserRepository(s)
    users.complete_age_verification_if_unset(
        user, birth_date=NOW.date().replace(year=NOW.year - 30), age_range="25-34", is_adult=True, confirmed_at=NOW, policy_version="v-test",
    )
    s.commit()
    s.refresh(user)
    assert user.age_deadline_at is None and user.birth_date is not None


def test_the_overdue_delete_removes_the_account_when_still_unconfirmed(member_session):
    s = member_session
    user = unconfirmed(s, 1, deadline=NOW - timedelta(minutes=1))
    assert UserRepository(s).delete_overdue_unconfirmed(user.id, NOW) is True
    s.commit()
    assert s.scalar(select(func.count(User.id))) == 0


# ---- 補填時未滿 13 歲：立即抹除 -----------------------------------------------------------------------


def test_the_immediate_erasure_only_deletes_non_admin_accounts_without_a_birthday(member_session):
    s = member_session
    player = add_user(s, 1, age=None)
    confirmed = add_user(s, 2, age=30)
    admin = add_user(s, 3, role="admin")
    users = UserRepository(s)
    assert users.delete_unconfirmed_player(confirmed.id) is False
    assert users.delete_unconfirmed_player(admin.id) is False
    assert users.delete_unconfirmed_player(player.id) is True
    s.commit()
    assert s.scalar(select(func.count(User.id))) == 2


# ---- 玩家帳號鎖定 ---------------------------------------------------------------------------------


def test_locking_is_conditional_so_a_second_attempt_does_not_lock_again(member_session):
    s = member_session
    user = add_user(s, 1)
    users = UserRepository(s)
    assert users.lock_player(user, reason="identity_rejected", now=NOW) is True
    s.commit()
    s.refresh(user)
    assert user.locked_at is not None and user.lock_reason == "identity_rejected"
    assert users.lock_player(user, reason="rate_limited", now=NOW + timedelta(minutes=1)) is False       # 並行的第二個請求
    s.refresh(user)
    assert user.lock_reason == "identity_rejected"                                                        # 第一個原因保留


def test_an_admin_is_never_locked_by_the_player_lock(member_session):
    s = member_session
    admin = add_user(s, 1, role="admin")
    assert UserRepository(s).lock_player(admin, reason="rate_limited", now=NOW) is False
    s.refresh(admin)
    assert admin.locked_at is None


def test_unlocking_clears_the_lock_and_reports_whether_anything_changed(member_session):
    s = member_session
    user = add_user(s, 1, locked_at=NOW, lock_reason="rate_limited")
    users = UserRepository(s)
    assert users.unlock_player(user.id) is True
    s.commit()
    s.refresh(user)
    assert user.locked_at is None and user.lock_reason is None
    assert users.unlock_player(user.id) is False


def test_the_locked_count_combines_password_locks_and_player_locks_one_per_account(member_session):
    s = member_session
    add_user(s, 1, locked_until=NOW + timedelta(minutes=10))                                   # 密碼鎖定中
    add_user(s, 2, locked_until=NOW - timedelta(minutes=1))                                    # 密碼鎖定已到期
    add_user(s, 3, locked_at=NOW - timedelta(days=2), lock_reason="identity_rejected")         # 玩家鎖定（不會自動到期）
    add_user(s, 4, locked_until=NOW + timedelta(minutes=10), locked_at=NOW, lock_reason="rate_limited")   # 兩種同時，只算一個
    add_user(s, 5)
    assert UserRepository(s).count_locked(NOW) == 3
