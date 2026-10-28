"""access-control 規格：權限判斷集中在政策物件；非擁有者一律 404；角色與會員等級的語意。"""
import ast

import pytest

from app.config import PROJECT_ROOT
from app.models.user import (
    MEMBER_TIERS, ROLE_ADMIN, ROLE_USER, STATUS_ACTIVE, STATUS_DELETION_PENDING, STATUS_PENDING_VERIFICATION,
    STATUS_SUSPENDED, USER_STATUSES,
)
from app.services.access_policy import AccessPolicy
from app.services.current_user import CurrentUser
from app.services.errors import AgeConfirmationRequired, Forbidden, NotFound


def member(**overrides) -> CurrentUser:
    values = dict(
        id=1, public_id="00000000-0000-4000-8000-000000000001", email="a@example.com", display_name="甲",
        member_tier="free", role=ROLE_USER, status=STATUS_ACTIVE, email_verified=True, has_password=True,
        age_confirmed=True, token_version=1,
    )
    values.update(overrides)
    return CurrentUser(**values)


ON = AccessPolicy(email_verification_required=True)
OFF = AccessPolicy(email_verification_required=False)


# ---- 是否可使用客服 -----------------------------------------------------------

def test_an_unverified_member_cannot_use_chat_when_verification_is_on():
    assert ON.can_use_chat(member(email_verified=False)) is False


def test_an_active_unverified_member_can_use_chat_when_verification_is_off():
    assert OFF.can_use_chat(member(email_verified=False)) is True


def test_a_verified_active_member_can_use_chat_in_both_modes():
    assert ON.can_use_chat(member()) is True and OFF.can_use_chat(member()) is True


@pytest.mark.parametrize("status", [STATUS_SUSPENDED, STATUS_DELETION_PENDING, STATUS_PENDING_VERIFICATION])
@pytest.mark.parametrize("policy", [ON, OFF])
def test_only_active_accounts_can_use_chat(policy, status):
    assert policy.can_use_chat(member(status=status)) is False


# ---- 年齡閘門 -----------------------------------------------------------------

@pytest.mark.parametrize("policy", [ON, OFF])
def test_an_unconfirmed_member_cannot_use_chat_even_when_every_other_condition_holds(policy):
    assert policy.can_use_chat(member(age_confirmed=False)) is False


@pytest.mark.parametrize("policy", [ON, OFF])
@pytest.mark.parametrize("tier", ["free", "vip"])
@pytest.mark.parametrize("role", [ROLE_USER, ROLE_ADMIN])
def test_neither_admins_nor_top_tier_members_are_exempt_from_the_age_gate(policy, tier, role):
    unconfirmed = member(age_confirmed=False, member_tier=tier, role=role)
    assert policy.can_use_chat(unconfirmed) is False
    with pytest.raises(AgeConfirmationRequired):
        policy.require_chat_access(unconfirmed)
    assert policy.can_use_chat(member(member_tier=tier, role=role)) is True


@pytest.mark.parametrize("policy", [ON, OFF])
@pytest.mark.parametrize("tier", ["free", "vip"])
@pytest.mark.parametrize("role", [ROLE_USER, ROLE_ADMIN])
@pytest.mark.parametrize("age_confirmed", [True, False])
@pytest.mark.parametrize("verified", [True, False])
@pytest.mark.parametrize("status", USER_STATUSES)
def test_chat_access_requires_every_condition_and_the_two_methods_agree(policy, tier, role, age_confirmed, verified, status):
    user = member(member_tier=tier, role=role, age_confirmed=age_confirmed, email_verified=verified, status=status)
    allowed = status == STATUS_ACTIVE and (verified or policy is OFF) and age_confirmed
    assert policy.can_use_chat(user) is allowed
    if allowed:
        policy.require_chat_access(user)
    else:
        with pytest.raises(Forbidden):
            policy.require_chat_access(user)


def test_a_missing_age_confirmation_raises_the_dedicated_error_code():
    with pytest.raises(AgeConfirmationRequired) as denied:
        ON.require_chat_access(member(age_confirmed=False))
    assert isinstance(denied.value, Forbidden)
    assert denied.value.code == "age_confirmation_required"
    assert "18" in denied.value.message and "帳號設定" in denied.value.message


@pytest.mark.parametrize("status", [STATUS_SUSPENDED, STATUS_DELETION_PENDING, STATUS_PENDING_VERIFICATION])
def test_the_account_status_is_checked_before_age_so_the_user_is_not_sent_to_confirm_for_nothing(status, log_lines):
    with pytest.raises(Forbidden) as denied:
        ON.require_chat_access(member(status=status, age_confirmed=False))
    assert not isinstance(denied.value, AgeConfirmationRequired) and denied.value.code == "forbidden"
    assert all(line.get("event") != "auth.age_gate_denied" for line in log_lines())


def test_unverified_email_is_checked_before_age_when_verification_is_on(log_lines):
    with pytest.raises(Forbidden) as denied:
        ON.require_chat_access(member(email_verified=False, age_confirmed=False))
    assert not isinstance(denied.value, AgeConfirmationRequired)
    with pytest.raises(AgeConfirmationRequired):
        OFF.require_chat_access(member(email_verified=False, age_confirmed=False))        # 驗證關閉時不看信箱
    assert [line["event"] for line in log_lines() if line.get("event") == "auth.age_gate_denied"] == ["auth.age_gate_denied"]


def test_an_age_denial_is_logged_once_with_the_internal_user_id_and_nothing_personal(log_lines):
    user = member(id=42, age_confirmed=False)
    with pytest.raises(AgeConfirmationRequired):
        ON.require_chat_access(user)
    denials = [line for line in log_lines() if line.get("event") == "auth.age_gate_denied"]
    assert len(denials) == 1 and denials[0]["user_id"] == 42 and denials[0]["level"] == "INFO"
    assert user.email not in str(denials[0]) and user.public_id not in str(denials[0])


def test_a_confirmed_member_passes_without_any_log_line(log_lines):
    ON.require_chat_access(member())
    assert [line for line in log_lines() if line.get("event") == "auth.age_gate_denied"] == []


# ---- 是否為管理員 -------------------------------------------------------------

def test_only_an_active_admin_is_an_admin():
    assert ON.is_admin(member(role=ROLE_ADMIN)) is True
    assert ON.is_admin(member(role=ROLE_ADMIN, status=STATUS_SUSPENDED)) is False
    assert ON.is_admin(member(role=ROLE_USER)) is False


def test_a_vip_member_is_not_an_admin():
    assert ON.is_admin(member(member_tier="vip")) is False


# ---- 會員等級不影響權限 -------------------------------------------------------

@pytest.mark.parametrize("status", USER_STATUSES)
@pytest.mark.parametrize("role", [ROLE_USER, ROLE_ADMIN])
@pytest.mark.parametrize("verified", [True, False])
def test_the_decision_is_identical_for_every_member_tier(status, role, verified):
    answers = {
        (ON.is_admin(u), ON.is_owner(u, 1), ON.can_use_chat(u), OFF.can_use_chat(u))
        for u in (member(member_tier=tier, status=status, role=role, email_verified=verified) for tier in MEMBER_TIERS)
    }
    assert len(answers) == 1


def test_the_policy_source_never_reads_the_member_tier():
    """以語法樹檢查：程式碼裡不得有任何 `member_tier` 的屬性存取或名稱（說明文字不算）。"""
    tree = ast.parse((PROJECT_ROOT / "app" / "services" / "access_policy.py").read_text(encoding="utf-8-sig"))
    touched = [
        node for node in ast.walk(tree)
        if (isinstance(node, ast.Attribute) and node.attr == "member_tier")
        or (isinstance(node, ast.Name) and node.id == "member_tier")
    ]
    assert touched == []


# ---- 擁有者 -------------------------------------------------------------------

def test_the_owner_passes_and_everyone_else_gets_not_found():
    ON.require_owner(member(id=7), 7)
    with pytest.raises(NotFound):
        ON.require_owner(member(id=8), 7)


def test_a_missing_owner_is_never_a_match():
    assert ON.is_owner(member(id=1), None) is False
    with pytest.raises(NotFound):
        ON.require_owner(member(id=1), None)


def test_not_owner_is_not_found_and_never_forbidden_and_looks_exactly_like_a_missing_resource():
    with pytest.raises(NotFound) as someone_elses:
        ON.require_owner(member(id=2), 1)
    assert not isinstance(someone_elses.value, Forbidden)
    genuinely_missing = NotFound()
    assert (someone_elses.value.code, someone_elses.value.message) == (genuinely_missing.code, genuinely_missing.message)


def test_even_an_admin_does_not_bypass_the_owner_check():
    with pytest.raises(NotFound):
        ON.require_owner(member(id=2, role=ROLE_ADMIN), 1)
