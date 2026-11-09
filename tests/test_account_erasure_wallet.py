"""account-deletion 規格「錢包與帳本隨帳號抹除」：四種抹除原因（自助刪除、連續 12 個月未登入、24 小時內未完成年齡確認、
未滿 13 歲）與備份還原後的重新抹除（`flask replay-deletions`），都把玩家的錢包與全部帳本一併移除（不是匿名化保留）；
有尚未結算的預扣也不會擋下抹除；別人的錢包不受影響。
"""
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base, DeletionTombstone, User, Wallet, WalletEntry
from app.repositories.deletion_tombstone_repository import DeletionTombstoneRepository
from app.repositories.user_repository import UserRepository
from app.services.account_erasure_service import AccountErasureService
from app.services.current_user import CurrentUser
from tests.support.auth import START, FakeClock, add_user
from tests.support.wallet import WalletKit

CUTOFF = START + timedelta(days=1)


def erasure(session) -> AccountErasureService:
    return AccountErasureService(
        users=UserRepository(session), tombstones=DeletionTombstoneRepository(session), commit=session.commit, rollback=session.rollback,
    )


def count(session, model) -> int:
    session.expire_all()
    return session.scalar(select(func.count()).select_from(model))


def with_wallet(session, user: User, *, open_hold: bool = True) -> None:
    """玩家有錢包、簽到、一輪已結算、一筆尚未結算的預扣與一筆管理員調整。"""
    kit = WalletKit(session)
    me = CurrentUser.from_model(user)
    kit.service.checkin(me)
    kit.service.hold(me, key=f"done:{user.id}")
    kit.service.settle(me, f"done:{user.id}")
    if open_hold:
        kit.service.hold(me, key=f"open:{user.id}")
    kit.service.adjust(user.id, bucket="paid", delta=50)
    session.commit()


def old_user(session, n: int, **overrides) -> User:
    return add_user(session, n, last_login_at=START - timedelta(days=400), created_at=START - timedelta(days=500), **overrides)


def bystander(session) -> None:
    other = add_user(session, 90)
    with_wallet(session, other, open_hold=False)


def assert_bystander_untouched(session) -> None:
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Wallet)) == 1
    assert session.scalar(select(func.count()).select_from(WalletEntry)) >= 4


# ---- 四種抹除原因 --------------------------------------------------------------------------------------


def test_erasing_an_inactive_account_removes_the_wallet_and_the_whole_ledger_even_with_an_open_hold(member_session):
    s = member_session
    user = old_user(s, 1)
    with_wallet(s, user)
    bystander(s)
    assert count(s, Wallet) == 2 and count(s, WalletEntry) >= 8
    user_id = user.id
    assert erasure(s).erase_inactive(user_id, cutoff=CUTOFF, now=START) is True
    s.expire_all()
    assert s.scalar(select(func.count()).select_from(Wallet).where(Wallet.user_id == user_id)) == 0
    assert s.scalar(select(func.count()).select_from(WalletEntry).where(WalletEntry.user_id == user_id)) == 0
    assert_bystander_untouched(s)


def test_the_erased_players_ledger_is_deleted_not_anonymised(member_session):
    s = member_session
    user = old_user(s, 1)
    with_wallet(s, user)
    user_id = user.id
    erasure(s).erase_inactive(user_id, cutoff=CUTOFF, now=START)
    assert count(s, WalletEntry) == 0 and count(s, Wallet) == 0
    assert s.scalar(select(func.count()).select_from(WalletEntry).where(WalletEntry.user_id.is_(None))) == 0


def test_erasing_an_unconfirmed_account_after_its_deadline_removes_the_wallet(member_session):
    s = member_session
    user = add_user(s, 1, age=None, password=None, age_deadline_at=START - timedelta(hours=1))
    WalletKit(s).service.adjust(user.id, bucket="paid", delta=10)                       # 管理員先調整過（建立了錢包）
    s.commit()
    assert erasure(s).erase_unconfirmed(user.id, now=START) is True
    assert count(s, Wallet) == 0 and count(s, WalletEntry) == 0


def test_erasing_a_player_found_to_be_under_13_removes_the_wallet(member_session):
    s = member_session
    user = add_user(s, 1, age=None, password=None)
    WalletKit(s).service.adjust(user.id, bucket="free", delta=5)
    s.commit()
    assert erasure(s).erase_underage(user.id, now=START) is True
    assert count(s, User) == 0 and count(s, Wallet) == 0 and count(s, WalletEntry) == 0


def test_erasing_after_the_deletion_grace_period_removes_the_wallet(member_session):
    s = member_session
    user = add_user(s, 1, status="deletion_pending", deletion_requested_at=START - timedelta(days=8))
    with_wallet(s, user)
    assert erasure(s).erase_pending_due(user.id, cutoff=START - timedelta(days=7), now=START) is True
    assert count(s, Wallet) == 0 and count(s, WalletEntry) == 0


def test_when_the_erasure_condition_no_longer_holds_the_wallet_is_untouched(member_session):
    """條件不成立（使用者剛好登入）時整個交易回復：錢包與帳本一列不少。"""
    s = member_session
    user = add_user(s, 1, last_login_at=START)                                          # 最近才登入
    with_wallet(s, user)
    before = (count(s, Wallet), count(s, WalletEntry))
    assert erasure(s).erase_inactive(user.id, cutoff=CUTOFF - timedelta(days=2000), now=START) is False
    assert (count(s, Wallet), count(s, WalletEntry)) == before


def test_a_tombstone_records_only_the_internal_id_and_reason_not_the_wallet(member_session):
    s = member_session
    user = old_user(s, 1)
    with_wallet(s, user)
    user_id = user.id
    erasure(s).erase_inactive(user_id, cutoff=CUTOFF, now=START)
    (row,) = s.scalars(select(DeletionTombstone))
    assert (row.user_id, row.reason) == (user_id, "inactive_12_months")
    assert not any(hasattr(row, name) for name in ("free_balance", "paid_balance", "wallet"))


# ---- 備份還原後的重新抹除 ---------------------------------------------------------------------------------


@pytest.fixture()
def app():
    application = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


def test_replaying_deletions_after_a_restore_removes_the_revived_players_wallet_too(app):
    with Session(app.extensions["db_engine"]) as s:
        zombie = add_user(s, 1, created_at=START - timedelta(days=90))
        with_wallet(s, zombie)
        keeper = add_user(s, 2, created_at=START - timedelta(days=90))
        with_wallet(s, keeper, open_hold=False)
        DeletionTombstoneRepository(s).record(user_id=zombie.id, reason="user_requested", deleted_at=START)
        s.commit()
        zombie_id, keeper_id = zombie.id, keeper.id
    result = app.test_cli_runner().invoke(args=["replay-deletions"])
    assert result.exit_code == 0 and "已抹除：1 個帳號" in result.output
    with Session(app.extensions["db_engine"]) as s:
        assert s.scalar(select(func.count()).select_from(Wallet).where(Wallet.user_id == zombie_id)) == 0
        assert s.scalar(select(func.count()).select_from(WalletEntry).where(WalletEntry.user_id == zombie_id)) == 0
        assert s.scalar(select(func.count()).select_from(Wallet).where(Wallet.user_id == keeper_id)) == 1
