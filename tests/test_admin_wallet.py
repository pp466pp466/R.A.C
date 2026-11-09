"""admin-user-management 規格「調整遊戲幣餘額」與 admin-audit-log 規格的 `wallet.adjust`（Service 層）：
增加付費幣與稽核內容、扣到負數、欄位驗證、超過上限、不能調整管理員、密碼錯誤、稽核與調整同一個交易、檢視餘額不建立錢包。
SQLite 與真實 SQL Server 各跑一次。
"""
import pytest
from sqlalchemy import func, select

from app.models import AdminAuditLog, Wallet, WalletEntry
from app.models.admin_audit_log import AUDIT_ACTIONS
from app.repositories.admin_audit_repository import AdminAuditRepository
from app.services.errors import AdminTargetForbidden, Conflict, InsufficientCoins, NotFound, ValidationError
from tests.support.admin import GOOD_PASSWORD, WRONG_PASSWORD, AdminKit


@pytest.fixture()
def kit(member_session):
    return AdminKit(member_session)


def adjust(kit, admin, member, **overrides):
    values = dict(bucket="paid", delta=500, reason="活動補償", password=GOOD_PASSWORD)
    values.update(overrides)
    return kit.service.adjust_wallet(admin, member.public_id, **values)


def count(kit, model) -> int:
    kit.session.expire_all()
    return kit.session.scalar(select(func.count()).select_from(model))


def balances(kit, member) -> tuple[int, int]:
    kit.session.expire_all()
    row = kit.session.execute(select(Wallet.free_balance, Wallet.paid_balance).where(Wallet.user_id == member.id)).one_or_none()
    return (0, 0) if row is None else tuple(row)


# ---- 成功 ----------------------------------------------------------------------------------------


def test_adding_paid_coins_changes_the_balance_and_writes_one_audit_row_with_only_the_allowed_detail(kit):
    admin, member = kit.admin(), kit.member(2)
    view = adjust(kit, admin, member)
    assert view["wallet"] == {"free_balance": 0, "paid_balance": 500} and balances(kit, member) == (0, 500)
    [row] = kit.audit_rows()
    assert (row.action, row.target_user_public_id, row.reason) == ("wallet.adjust", member.public_id, "活動補償")
    assert row.detail == {"bucket": "paid", "delta": 500, "balance_after": 500}
    assert "wallet.adjust" in AUDIT_ACTIONS


def test_the_audit_row_belongs_to_the_administrator_not_to_the_system(kit):
    admin, member = kit.admin(), kit.member(2)
    adjust(kit, admin, member)
    kit.session.expire_all()
    stored = kit.session.scalar(select(AdminAuditLog).where(AdminAuditLog.action == "wallet.adjust"))
    assert stored.admin_user_id == admin.id


def test_subtracting_free_coins_reports_the_balance_after(kit):
    admin, member = kit.admin(), kit.member(2)
    adjust(kit, admin, member, bucket="free", delta=300)
    adjust(kit, admin, member, bucket="free", delta=-120, reason="更正誤發")
    rows = kit.audit_rows()
    assert [r.detail for r in rows][0] == {"bucket": "free", "delta": -120, "balance_after": 180}


def test_an_adjustment_may_exceed_the_free_cap(kit):
    admin, member = kit.admin(), kit.member(2)
    adjust(kit, admin, member, bucket="free", delta=1000)
    view = adjust(kit, admin, member, bucket="free", delta=500)
    assert view["wallet"]["free_balance"] == 1500


def test_the_player_is_not_granted_the_signup_coins_by_the_adjustment(kit):
    admin, member = kit.admin(), kit.member(2)
    adjust(kit, admin, member)
    kit.session.expire_all()
    kinds = [e.kind for e in kit.session.scalars(select(WalletEntry).where(WalletEntry.user_id == member.id))]
    assert kinds == ["admin_adjust"]


# ---- 失敗 ----------------------------------------------------------------------------------------


def test_taking_more_than_the_balance_is_a_409_with_no_change_and_no_audit(kit):
    admin, member = kit.admin(), kit.member(2)
    adjust(kit, admin, member, bucket="free", delta=100)
    before = kit.audit_rows()
    with pytest.raises(InsufficientCoins):
        adjust(kit, admin, member, bucket="free", delta=-300)
    assert balances(kit, member) == (100, 0) and kit.audit_rows() == before


@pytest.mark.parametrize(
    "overrides, field",
    [
        ({"bucket": "gold"}, "bucket"), ({"bucket": None}, "bucket"), ({"bucket": "FREE"}, "bucket"),
        ({"delta": 0}, "delta"), ({"delta": 1.5}, "delta"), ({"delta": "5"}, "delta"), ({"delta": True}, "delta"), ({"delta": None}, "delta"),
        ({"reason": ""}, "reason"), ({"reason": None}, "reason"), ({"reason": "字" * 201}, "reason"), ({"reason": "  \n "}, "reason"),
    ],
)
def test_invalid_fields_are_a_400_naming_the_field_and_nothing_changes(kit, overrides, field):
    admin, member = kit.admin(), kit.member(2)
    with pytest.raises(ValidationError) as caught:
        adjust(kit, admin, member, **overrides)
    assert field in caught.value.fields
    assert balances(kit, member) == (0, 0) and kit.audit_rows() == [] and count(kit, Wallet) == 0


def test_all_invalid_fields_are_reported_together(kit):
    admin, member = kit.admin(), kit.member(2)
    with pytest.raises(ValidationError) as caught:
        adjust(kit, admin, member, bucket="x", delta=0, reason="")
    assert set(caught.value.fields) == {"bucket", "delta", "reason"}


def test_a_wrong_password_changes_nothing_and_leaves_a_reauth_failure_naming_the_action(kit):
    admin, member = kit.admin(), kit.member(2)
    with pytest.raises(ValidationError) as caught:
        adjust(kit, admin, member, password=WRONG_PASSWORD)
    assert "password" in caught.value.fields
    assert balances(kit, member) == (0, 0)
    [row] = kit.audit_rows()
    assert row.action == "reauth.failed" and row.detail == {"attempted": "wallet.adjust"}


def test_another_administrator_cannot_be_adjusted(kit):
    admin = kit.admin(1)
    other = kit.member(3, role="admin")
    with pytest.raises(AdminTargetForbidden):
        adjust(kit, admin, other)
    with pytest.raises(AdminTargetForbidden):
        adjust(kit, admin, kit.member(1, role="admin") if False else other)
    assert balances(kit, other) == (0, 0) and kit.audit_rows() == []


def test_an_administrator_cannot_adjust_their_own_wallet(kit):
    admin = kit.admin(1)
    me = kit.reload(kit.member(5, role="admin"))
    with pytest.raises(AdminTargetForbidden):
        kit.service.adjust_wallet(CurrentUser_of(kit, me), me.public_id, bucket="free", delta=5, reason="自己", password=GOOD_PASSWORD)
    assert admin is not None and kit.audit_rows() == []


def CurrentUser_of(kit, user):
    from app.services.current_user import CurrentUser

    return CurrentUser.from_model(user)


def test_an_unknown_target_is_not_found(kit):
    admin = kit.admin()
    with pytest.raises(NotFound):
        kit.service.adjust_wallet(admin, "00000000-0000-4000-8000-000000000999", bucket="free", delta=5, reason="x", password=GOOD_PASSWORD)
    with pytest.raises(NotFound):
        kit.service.adjust_wallet(admin, None, bucket="free", delta=5, reason="x", password=GOOD_PASSWORD)


def test_an_account_waiting_for_deletion_cannot_be_adjusted(kit):
    admin = kit.admin()
    member = kit.member(2, status="deletion_pending")
    with pytest.raises(Conflict) as caught:
        adjust(kit, admin, member)
    assert caught.value.code == "target_pending_deletion" and balances(kit, member) == (0, 0)


def test_a_non_administrator_cannot_adjust_anything(kit):
    victim = kit.member(2)
    intruder = kit.current(kit.member(3, member_tier="vip"))
    with pytest.raises(NotFound):
        kit.service.adjust_wallet(intruder, victim.public_id, bucket="paid", delta=999, reason="偷偷加", password=GOOD_PASSWORD)
    assert balances(kit, victim) == (0, 0) and kit.audit_rows() == []


# ---- 稽核與調整同一個交易 -----------------------------------------------------------------------------


def test_the_adjustment_and_its_audit_row_stand_or_fall_together(kit, monkeypatch):
    """稽核寫入失敗時整個請求回復：餘額不會在沒有稽核的情況下被改變（以回復交易模擬請求失敗）。"""
    admin, member = kit.admin(), kit.member(2)
    kit.session.commit()

    def failing_add(self, **_kwargs):
        raise RuntimeError("稽核寫入失敗")

    monkeypatch.setattr(AdminAuditRepository, "add", failing_add)
    with pytest.raises(RuntimeError):
        adjust(kit, admin, member)
    kit.session.rollback()                                               # 請求失敗：交易回復
    monkeypatch.undo()
    assert balances(kit, member) == (0, 0) and kit.audit_rows() == []


# ---- 檢視餘額 --------------------------------------------------------------------------------------


def test_viewing_a_player_shows_two_integers_and_never_creates_a_wallet(kit):
    admin, member = kit.admin(), kit.member(2)
    view = kit.service.get_user(admin, member.public_id)
    assert view["wallet"] == {"free_balance": 0, "paid_balance": 0} and count(kit, Wallet) == 0


def test_viewing_does_not_release_stale_holds_or_show_the_ledger(kit):
    from app.services.current_user import CurrentUser
    from tests.support.wallet import WalletKit

    admin, member = kit.admin(), kit.member(2)
    wallet = WalletKit(kit.session)
    me = CurrentUser.from_model(member)
    wallet.service.hold(me, key="turn:a")                                  # 免費 200 → 170
    wallet.clock.advance(seconds=900)
    view = kit.service.get_user(admin, member.public_id)
    assert view["wallet"] == {"free_balance": 170, "paid_balance": 0}
    assert set(view["wallet"]) == {"free_balance", "paid_balance"} and "entries" not in view
    assert wallet.entry(me, "turn:a").resolution is None


def test_the_profile_after_an_adjustment_shows_the_new_balances(kit):
    admin, member = kit.admin(), kit.member(2)
    adjust(kit, admin, member, bucket="paid", delta=70)
    assert kit.service.get_user(admin, member.public_id)["wallet"] == {"free_balance": 0, "paid_balance": 70}
