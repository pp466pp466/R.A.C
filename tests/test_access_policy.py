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
from app.services.errors import Forbidden, NotFound


def member(**overrides) -> CurrentUser:
    values = dict(
        id=1, public_id="00000000-0000-4000-8000-000000000001", email="a@example.com", display_name="甲",
        member_tier="free", role=ROLE_USER, status=STATUS_ACTIVE, email_verified=True, has_password=True, token_version=1,
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
