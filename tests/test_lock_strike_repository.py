"""account-lockout / data-retention 規格：鎖定計次的 Repository（視窗計數、類別、清除某帳號、期限清除）。"""
from datetime import timedelta

from sqlalchemy import func, select

from app.models import LockStrike, User
from app.models.lock_strike import STRIKE_IDENTITY_REJECTED, STRIKE_RATE_LIMITED
from app.repositories.lock_strike_repository import LockStrikeRepository
from tests.support.auth import START, add_user

NOW = START + timedelta(days=1)
WINDOW = timedelta(minutes=15)


def test_the_window_counts_only_recent_strikes_of_the_given_kinds_for_that_user(member_session):
    s = member_session
    mine, other = add_user(s, 1), add_user(s, 2)
    strikes = LockStrikeRepository(s)
    strikes.record(mine.id, STRIKE_IDENTITY_REJECTED, NOW - timedelta(minutes=16))      # 視窗外
    strikes.record(mine.id, STRIKE_IDENTITY_REJECTED, NOW - timedelta(minutes=14))
    strikes.record(mine.id, STRIKE_IDENTITY_REJECTED, NOW)
    strikes.record(mine.id, STRIKE_RATE_LIMITED, NOW)                                   # 另一類別
    strikes.record(other.id, STRIKE_IDENTITY_REJECTED, NOW)                             # 別人
    since = NOW - WINDOW
    assert strikes.count_since(mine.id, (STRIKE_IDENTITY_REJECTED,), since) == 2
    assert strikes.count_since(mine.id, (STRIKE_RATE_LIMITED,), since) == 1
    assert strikes.count_since(mine.id, (STRIKE_IDENTITY_REJECTED, STRIKE_RATE_LIMITED), since) == 3
    assert strikes.count_since(other.id, (STRIKE_IDENTITY_REJECTED,), since) == 1


def test_a_strike_exactly_at_the_window_edge_is_not_counted(member_session):
    s = member_session
    user = add_user(s, 1)
    strikes = LockStrikeRepository(s)
    strikes.record(user.id, STRIKE_RATE_LIMITED, NOW - WINDOW)
    assert strikes.count_since(user.id, (STRIKE_RATE_LIMITED,), NOW - WINDOW) == 0


def test_clearing_one_user_leaves_the_others(member_session):
    s = member_session
    first, second = add_user(s, 1), add_user(s, 2)
    strikes = LockStrikeRepository(s)
    strikes.record(first.id, STRIKE_RATE_LIMITED, NOW)
    strikes.record(first.id, STRIKE_IDENTITY_REJECTED, NOW)
    strikes.record(second.id, STRIKE_RATE_LIMITED, NOW)
    assert strikes.clear_for_user(first.id) == 2
    s.commit()
    assert s.scalar(select(func.count(LockStrike.id))) == 1


def test_deleting_the_account_cascades_to_its_strikes(member_session):
    s = member_session
    user = add_user(s, 1)
    LockStrikeRepository(s).record(user.id, STRIKE_RATE_LIMITED, NOW)
    s.commit()
    s.delete(user)
    s.commit()
    assert s.scalar(select(func.count(LockStrike.id))) == 0 and s.scalar(select(func.count(User.id))) == 0
