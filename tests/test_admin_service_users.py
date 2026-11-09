"""admin-user-management 規格：信箱查詢、最小資料、停權、解除、調整等級、可操作的目標、重新輸入密碼與稽核。"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.models.user import (
    STATUS_ACTIVE, STATUS_DELETION_PENDING, STATUS_SUSPENDED, User,
)
from app.services.current_user import CurrentUser
from app.services.errors import AdminTargetForbidden, Conflict, NotFound, ValidationError
from app.services.normalization import canonical_email
from tests.support.admin import GOOD_PASSWORD, WRONG_PASSWORD, AdminKit
from tests.support.auth import add_user


@pytest.fixture()
def kit(member_session):
    return AdminKit(member_session)


def actions(kit):
    return [row.action for row in kit.audit_rows()]


# ---- 信箱查詢 -------------------------------------------------------------------------------

def test_an_exact_email_finds_the_user_and_is_audited_without_the_email(kit):
    admin = kit.admin()
    member = kit.member(2)
    view = kit.service.lookup_user(admin, member.email)
    assert view["public_id"] == member.public_id and view["email"] == member.email
    [row] = kit.audit_rows()
    assert (row.action, row.target_user_public_id, row.detail) == ("user.lookup", member.public_id, {"via": "email", "found": 1})
    assert member.email not in repr(row) and row.reason is None


def test_surrounding_spaces_and_case_are_forgiven(kit):
    admin = kit.admin()
    member = kit.member(2)
    assert kit.service.lookup_user(admin, f"  {member.email.upper()}  ")["public_id"] == member.public_id


def test_a_gmail_alias_finds_the_same_user(kit):
    admin = kit.admin()
    member = kit.member(2, email="Some.One@gmail.com", email_canonical=canonical_email("some.one@gmail.com"))
    assert kit.service.lookup_user(admin, "someone+work@gmail.com")["public_id"] == member.public_id


def test_an_unknown_email_is_a_404_but_the_search_itself_is_still_recorded_and_kept(kit):
    admin = kit.admin()
    with pytest.raises(NotFound):
        kit.service.lookup_user(admin, "nobody@example.com")
    [row] = kit.audit_rows()
    assert (row.action, row.target_user_public_id, row.detail) == ("user.lookup", None, {"via": "email", "found": 0})
    assert "nobody" not in repr(row)
    assert kit.persisted.calls == 1                                   # 回應是 404，請求交易預設會回復：明確要求保存


@pytest.mark.parametrize("email", [None, "", "   ", "not-an-email", 123, ["a@b.com"], "a@b"])
def test_an_invalid_email_is_a_400_on_the_email_field_and_nothing_is_recorded(kit, email):
    with pytest.raises(ValidationError) as exc:
        kit.service.lookup_user(kit.admin(), email)
    assert set(exc.value.fields) == {"email"} and kit.audit_rows() == []


@pytest.mark.parametrize("probe", ["user", "user2@example.co", "ser2@example.com", "user2@example.com%"])
def test_there_is_no_partial_matching(kit, probe):
    admin = kit.admin()
    kit.member(2)
    with pytest.raises((NotFound, ValidationError)):
        kit.service.lookup_user(admin, probe)


# ---- 以識別碼檢視與資料範圍 ------------------------------------------------------------------

def test_viewing_by_id_returns_the_same_profile_and_is_audited_as_via_id(kit):
    admin = kit.admin()
    member = kit.member(2)
    by_email = kit.service.lookup_user(admin, member.email)
    by_id = kit.service.get_user(admin, member.public_id)
    assert by_email == by_id
    assert [(r.action, r.detail) for r in kit.audit_rows()] == [("user.lookup", {"via": "id", "found": 1}), ("user.lookup", {"via": "email", "found": 1})]


@pytest.mark.parametrize("public_id", ["00000000-0000-4000-8000-000000000000", "", None, 5])
def test_an_unknown_id_is_a_404_and_is_not_audited(kit, public_id):
    with pytest.raises(NotFound):
        kit.service.get_user(kit.admin(), public_id)
    assert kit.audit_rows() == []


def test_the_profile_has_exactly_the_minimum_fields_and_today_usage(kit):
    admin = kit.admin()
    member = kit.member(2, member_tier="plus")
    view = kit.service.get_user(admin, member.public_id)
    assert set(view) == {
        "public_id", "display_name", "email", "role", "status", "tier", "email_verified", "created_at", "last_login_at", "usage",
        "wallet",                                                                  # game-wallet：只有兩個餘額
    }
    assert view["wallet"] == {"free_balance": 0, "paid_balance": 0}
    assert (view["tier"], view["role"], view["status"], view["email_verified"]) == ("plus", "user", "active", False)
    assert set(view["usage"]) == {"tier", "daily", "per_minute_limit", "conversations", "suspended_until"}
    assert view["usage"]["daily"]["limit"] == 100 and view["usage"]["per_minute_limit"] == 10


def test_the_profile_has_no_age_data_of_any_kind_and_no_secrets(kit):
    admin = kit.admin()
    member = kit.member(2)
    assert member.birth_date is not None and member.age_range is not None          # 這位使用者確實有年齡資料
    text = json.dumps(kit.service.get_user(admin, member.public_id), ensure_ascii=False, default=str)
    for secret in (str(member.birth_date), member.age_range, member.password_hash, "token_version"):
        assert secret not in text
    for word in ("birth", "age_range", "age_group", "adult", "minor", "password", "token"):
        assert word not in text.lower()


def test_the_profile_shows_the_users_chat_suspension(kit):
    admin = kit.admin()
    member = kit.member(2)
    for minutes in (3, 2, 1):
        kit.block(member, None, at=kit.clock() - timedelta(minutes=minutes))
    assert kit.service.get_user(admin, member.public_id)["usage"]["suspended_until"] is not None


# ---- 停權 -----------------------------------------------------------------------------------

def test_suspending_changes_the_status_revokes_sessions_and_leaves_one_audit_row(kit, log_lines):
    status = STATUS_ACTIVE
    admin = kit.admin()
    member = kit.member(2, status=status)
    version = member.token_version
    view = kit.service.suspend_user(admin, member.public_id, reason="  反覆違規  ", password=GOOD_PASSWORD)
    row = kit.reload(member)
    assert (row.status, row.token_version) == (STATUS_SUSPENDED, version + 1) and view["status"] == STATUS_SUSPENDED
    [entry] = kit.audit_rows()
    assert (entry.action, entry.target_user_public_id, entry.reason, entry.detail) == ("user.suspend", member.public_id, "反覆違規", {"from_status": status})
    events = [line for line in log_lines() if line.get("event") == "admin.action"]
    assert [(e["user_id"], e["reason"]) for e in events] == [(admin.id, "user.suspend")]
    assert "反覆違規" not in log_lines.raw() and member.email not in log_lines.raw()


def test_a_suspended_users_session_is_gone(kit):
    """停權使既有的登入狀態立即失效（Google 登入對停權帳號的拒絕見 test_google_sign_in_service）。"""
    admin = kit.admin()
    member = kit.member(2)
    session_version = member.token_version
    kit.service.suspend_user(admin, member.public_id, reason="違規", password=GOOD_PASSWORD)
    assert kit.auth.resolve_session(member.id, session_version) is None


@pytest.mark.parametrize("reason", [None, "", "   ", 5, "x" * 201, "含\x00控制字元" * 0 + "\x00"])
def test_a_missing_or_too_long_reason_is_a_400_on_the_reason_field_and_changes_nothing(kit, reason):
    admin = kit.admin()
    member = kit.member(2)
    with pytest.raises(ValidationError) as exc:
        kit.service.suspend_user(admin, member.public_id, reason=reason, password=GOOD_PASSWORD)
    assert set(exc.value.fields) == {"reason"}
    assert kit.reload(member).status == STATUS_ACTIVE and kit.audit_rows() == []


def test_a_reason_of_exactly_200_characters_is_accepted(kit):
    admin = kit.admin()
    member = kit.member(2)
    kit.service.suspend_user(admin, member.public_id, reason="因" * 200, password=GOOD_PASSWORD)
    assert kit.audit_rows()[0].reason == "因" * 200


def test_a_wrong_password_changes_nothing_but_the_failed_attempt_is_audited_and_kept(kit, log_lines):
    admin = kit.admin()
    member = kit.member(2)
    with pytest.raises(ValidationError) as exc:
        kit.service.suspend_user(admin, member.public_id, reason="違規", password=WRONG_PASSWORD)
    assert set(exc.value.fields) == {"password"}
    assert kit.reload(member).status == STATUS_ACTIVE
    [entry] = kit.audit_rows()
    assert (entry.action, entry.target_user_public_id, entry.detail) == ("reauth.failed", member.public_id, {"attempted": "user.suspend"})
    assert kit.persisted.calls >= 2                                    # 密碼失敗計數與稽核各要求保存一次
    events = [line for line in log_lines() if line.get("event") == "admin.reauth_failed"]
    assert [(e["user_id"], e["reason"]) for e in events] == [(admin.id, "user.suspend")]
    assert WRONG_PASSWORD not in log_lines.raw() and WRONG_PASSWORD not in repr(entry)


@pytest.mark.parametrize("password", [None, "", 12345])
def test_a_missing_password_is_a_400_on_the_password_field_and_is_also_audited(kit, password):
    admin = kit.admin()
    member = kit.member(2)
    with pytest.raises(ValidationError) as exc:
        kit.service.suspend_user(admin, member.public_id, reason="違規", password=password)
    assert set(exc.value.fields) == {"password"}
    assert kit.reload(member).status == STATUS_ACTIVE
    assert actions(kit) == ["reauth.failed"]


def test_an_admin_without_a_password_gets_no_password_and_nothing_is_audited(kit):
    google_admin = CurrentUser.from_model(add_user(kit.session, 7, role="admin", age=30, password=None))
    member = kit.member(2)
    with pytest.raises(ValidationError) as exc:
        kit.service.suspend_user(google_admin, member.public_id, reason="違規", password="anything")
    assert exc.value.code == "no_password" and kit.audit_rows() == []
    assert kit.reload(member).status == STATUS_ACTIVE


def test_five_wrong_passwords_lock_the_admin_and_then_even_the_right_one_is_refused(kit):
    admin = kit.admin()
    member = kit.member(2)
    for _ in range(5):
        with pytest.raises(ValidationError):
            kit.service.suspend_user(admin, member.public_id, reason="違規", password=WRONG_PASSWORD)
    with pytest.raises(ValidationError):
        kit.service.suspend_user(admin, member.public_id, reason="違規", password=GOOD_PASSWORD)
    assert kit.reload(member).status == STATUS_ACTIVE
    assert actions(kit).count("reauth.failed") == 6


def test_suspending_an_already_suspended_user_is_a_409_and_adds_no_audit(kit):
    admin = kit.admin()
    member = kit.member(2, status=STATUS_SUSPENDED)
    with pytest.raises(Conflict) as exc:
        kit.service.suspend_user(admin, member.public_id, reason="違規", password=GOOD_PASSWORD)
    assert exc.value.code == "already_suspended" and kit.audit_rows() == []


def test_a_state_that_changed_between_the_check_and_the_write_is_a_409_with_no_audit(kit, monkeypatch):
    admin = kit.admin()
    member = kit.member(2)
    monkeypatch.setattr(kit.admins, "suspend_user", lambda user, allowed_from: False)       # 別的管理員剛好先處理了
    with pytest.raises(Conflict) as exc:
        kit.service.suspend_user(admin, member.public_id, reason="違規", password=GOOD_PASSWORD)
    assert exc.value.code == "state_changed" and kit.audit_rows() == []


# ---- 可操作的目標 ---------------------------------------------------------------------------

def test_an_admin_cannot_be_the_target_not_even_themselves(kit):
    admin = kit.admin()
    other = kit.member(2, role="admin")
    for target in (other, User(public_id=admin.public_id)):
        with pytest.raises(AdminTargetForbidden):
            kit.service.suspend_user(admin, target.public_id, reason="違規", password=GOOD_PASSWORD)
        with pytest.raises(AdminTargetForbidden):
            kit.service.unsuspend_user(admin, target.public_id, password=GOOD_PASSWORD)
        with pytest.raises(AdminTargetForbidden):
            kit.service.change_tier(admin, target.public_id, tier="vip", password=GOOD_PASSWORD)
    assert kit.audit_rows() == [] and kit.reload(other).status == STATUS_ACTIVE


def test_a_user_waiting_for_deletion_cannot_be_operated_on(kit):
    admin = kit.admin()
    member = kit.member(2, status=STATUS_DELETION_PENDING)
    for call in (
        lambda: kit.service.suspend_user(admin, member.public_id, reason="違規", password=GOOD_PASSWORD),
        lambda: kit.service.change_tier(admin, member.public_id, tier="plus", password=GOOD_PASSWORD),
    ):
        with pytest.raises(Conflict) as exc:
            call()
        assert exc.value.code == "target_pending_deletion"
    assert kit.audit_rows() == []


@pytest.mark.parametrize("public_id", ["00000000-0000-4000-8000-000000000000", None, ""])
def test_an_unknown_target_is_a_404(kit, public_id):
    admin = kit.admin()
    for call in (
        lambda: kit.service.suspend_user(admin, public_id, reason="違規", password=GOOD_PASSWORD),
        lambda: kit.service.unsuspend_user(admin, public_id, password=GOOD_PASSWORD),
        lambda: kit.service.change_tier(admin, public_id, tier="plus", password=GOOD_PASSWORD),
    ):
        with pytest.raises(NotFound):
            call()


# ---- 解除停權 -------------------------------------------------------------------------------

def test_unsuspending_restores_an_active_account_whose_session_works_again(kit):
    admin = kit.admin()
    member = kit.member(2, status=STATUS_SUSPENDED)
    view = kit.service.unsuspend_user(admin, member.public_id, password=GOOD_PASSWORD)
    assert view["status"] == STATUS_ACTIVE
    assert kit.auth.resolve_session(member.id, member.token_version).id == member.id
    [entry] = kit.audit_rows()
    assert (entry.action, entry.detail) == ("user.unsuspend", {"to_status": "active"}) and entry.reason is None


@pytest.mark.parametrize("verified", [datetime(2026, 1, 1, tzinfo=timezone.utc), None])
def test_unsuspending_always_returns_to_active_whether_or_not_the_email_was_ever_verified(kit, verified):
    admin = kit.admin()
    member = kit.member(2, status=STATUS_SUSPENDED, email_verified_at=verified)
    assert kit.service.unsuspend_user(admin, member.public_id, password=GOOD_PASSWORD)["status"] == STATUS_ACTIVE
    assert kit.audit_rows()[0].detail == {"to_status": "active"}


def test_only_a_suspended_user_can_be_unsuspended(kit):
    admin = kit.admin()
    member = kit.member(2, status=STATUS_ACTIVE)
    with pytest.raises(Conflict) as exc:
        kit.service.unsuspend_user(admin, member.public_id, password=GOOD_PASSWORD)
    assert exc.value.code == "not_suspended" and kit.audit_rows() == []


def test_unsuspending_needs_the_password_too(kit):
    admin = kit.admin()
    member = kit.member(2, status=STATUS_SUSPENDED)
    with pytest.raises(ValidationError):
        kit.service.unsuspend_user(admin, member.public_id, password=WRONG_PASSWORD)
    assert kit.reload(member).status == STATUS_SUSPENDED
    assert actions(kit) == ["reauth.failed"] and kit.audit_rows()[0].detail == {"attempted": "user.unsuspend"}


# ---- 調整等級 -------------------------------------------------------------------------------

def test_changing_the_tier_takes_effect_on_the_limits_at_once_and_records_before_and_after(kit):
    admin = kit.admin()
    member = kit.member(2)
    view = kit.service.change_tier(admin, member.public_id, tier="plus", password=GOOD_PASSWORD)
    assert view["tier"] == "plus" and view["usage"]["daily"]["limit"] == 100 and view["usage"]["per_minute_limit"] == 10
    [entry] = kit.audit_rows()
    assert (entry.action, entry.detail, entry.reason) == ("user.change_tier", {"from_tier": "free", "to_tier": "plus"}, None)


@pytest.mark.parametrize("tier", ["gold", "", None, 5, "FREE", "plus "])
def test_an_invalid_tier_is_a_400_on_the_tier_field(kit, tier):
    admin = kit.admin()
    member = kit.member(2)
    with pytest.raises(ValidationError) as exc:
        kit.service.change_tier(admin, member.public_id, tier=tier, password=GOOD_PASSWORD)
    assert set(exc.value.fields) == {"tier"} and kit.reload(member).member_tier == "free" and kit.audit_rows() == []


def test_the_same_tier_is_a_409_and_writes_nothing(kit):
    admin = kit.admin()
    member = kit.member(2, member_tier="vip")
    with pytest.raises(Conflict) as exc:
        kit.service.change_tier(admin, member.public_id, tier="vip", password=GOOD_PASSWORD)
    assert exc.value.code == "tier_unchanged" and kit.audit_rows() == []


def test_changing_the_tier_with_a_wrong_password_changes_nothing(kit):
    admin = kit.admin()
    member = kit.member(2)
    with pytest.raises(ValidationError):
        kit.service.change_tier(admin, member.public_id, tier="vip", password=WRONG_PASSWORD)
    assert kit.reload(member).member_tier == "free" and actions(kit) == ["reauth.failed"]


# ---- 非管理員 -------------------------------------------------------------------------------

def test_a_non_admin_cannot_do_any_of_it_and_nothing_changes(kit):
    victim = kit.member(2)
    intruder = kit.current(kit.member(3, member_tier="vip"))
    calls = (
        lambda: kit.service.lookup_user(intruder, victim.email),
        lambda: kit.service.get_user(intruder, victim.public_id),
        lambda: kit.service.suspend_user(intruder, victim.public_id, reason="違規", password=GOOD_PASSWORD),
        lambda: kit.service.unsuspend_user(intruder, victim.public_id, password=GOOD_PASSWORD),
        lambda: kit.service.change_tier(intruder, victim.public_id, tier="vip", password=GOOD_PASSWORD),
    )
    for call in calls:
        with pytest.raises(NotFound):
            call()
    row = kit.reload(victim)
    assert (row.status, row.member_tier) == (STATUS_ACTIVE, "free") and kit.audit_rows() == []
    assert kit.persisted.calls == 0


def test_no_audit_row_ever_contains_an_email_or_a_name(kit):
    admin = kit.admin()
    member = kit.member(2)
    kit.service.lookup_user(admin, member.email)
    kit.service.suspend_user(admin, member.public_id, reason="違規", password=GOOD_PASSWORD)
    kit.service.unsuspend_user(admin, member.public_id, password=GOOD_PASSWORD)
    kit.service.change_tier(admin, member.public_id, tier="vip", password=GOOD_PASSWORD)
    with pytest.raises(ValidationError):
        kit.service.suspend_user(admin, member.public_id, reason="違規", password=WRONG_PASSWORD)
    text = " ".join(repr(row) for row in kit.audit_rows())
    for personal in (member.email, member.display_name, admin.email, GOOD_PASSWORD, WRONG_PASSWORD):
        assert personal not in text
