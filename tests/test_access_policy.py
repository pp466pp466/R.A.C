"""access-control 規格：權限判斷集中在政策物件；非擁有者一律 404；角色與會員等級的語意；年齡閘門與內容上限。"""
import ast
from datetime import date

import pytest

from app.config import PROJECT_ROOT
from app.models.user import (
    MEMBER_TIERS, ROLE_ADMIN, ROLE_USER, STATUS_ACTIVE, STATUS_DELETION_PENDING, STATUS_PENDING_VERIFICATION,
    STATUS_SUSPENDED, USER_STATUSES,
)
from app.services.access_policy import AccessPolicy, ContentCeiling
from app.services.age_policy import DEFAULT_AGE_POLICY, AgePolicy
from app.services.current_user import CurrentUser
from app.services.errors import AgeConfirmationRequired, AgeRestricted, Forbidden, NotFound
from tests.support.age import TODAY, TODAY_UTC, birth_date_for_age


def member(age: int | None = 30, **overrides) -> CurrentUser:
    """預設是 30 歲、已完成年齡確認、其他條件齊全的會員；`age=None` 代表尚未完成年齡確認。"""
    values = dict(
        id=1, public_id="00000000-0000-4000-8000-000000000001", email="a@example.com", display_name="甲",
        member_tier="free", role=ROLE_USER, status=STATUS_ACTIVE, email_verified=True, has_password=True,
        birth_date=birth_date_for_age(age) if age is not None else None, token_version=1,
    )
    values.update(overrides)
    return CurrentUser(**values)


def policy_for(*, verification: bool, age_policy: AgePolicy = DEFAULT_AGE_POLICY) -> AccessPolicy:
    return AccessPolicy(email_verification_required=verification, age_policy=age_policy, clock=lambda: TODAY_UTC)


ON = policy_for(verification=True)
OFF = policy_for(verification=False)
ALL_STATUSES_AND_MODES = [ON, OFF]


# ---- 是否可使用客服 -----------------------------------------------------------

def test_an_unverified_member_cannot_use_chat_when_verification_is_on():
    assert ON.can_use_chat(member(email_verified=False)) is False


def test_an_active_unverified_member_can_use_chat_when_verification_is_off():
    assert OFF.can_use_chat(member(email_verified=False)) is True


def test_a_verified_active_member_can_use_chat_in_both_modes():
    assert ON.can_use_chat(member()) is True and OFF.can_use_chat(member()) is True


@pytest.mark.parametrize("status", [STATUS_SUSPENDED, STATUS_DELETION_PENDING, STATUS_PENDING_VERIFICATION])
@pytest.mark.parametrize("policy", ALL_STATUSES_AND_MODES)
def test_only_active_accounts_can_use_chat(policy, status):
    assert policy.can_use_chat(member(status=status)) is False


# ---- 年齡閘門 -----------------------------------------------------------------

@pytest.mark.parametrize("policy", ALL_STATUSES_AND_MODES)
def test_a_member_who_has_not_completed_the_age_check_cannot_use_chat_even_when_every_other_condition_holds(policy):
    assert policy.can_use_chat(member(age=None)) is False


@pytest.mark.parametrize("policy", ALL_STATUSES_AND_MODES)
@pytest.mark.parametrize("tier", ["free", "vip"])
@pytest.mark.parametrize("role", [ROLE_USER, ROLE_ADMIN])
def test_neither_admins_nor_top_tier_members_are_exempt_from_the_age_gate(policy, tier, role):
    incomplete = member(age=None, member_tier=tier, role=role)
    assert policy.can_use_chat(incomplete) is False
    with pytest.raises(AgeConfirmationRequired):
        policy.require_chat_access(incomplete)
    assert policy.can_use_chat(member(member_tier=tier, role=role)) is True


@pytest.mark.parametrize("age", [13, 15, 17, 18, 30, 80])
def test_everyone_from_the_minimum_use_age_up_may_use_chat_including_teens(age):
    assert ON.can_use_chat(member(age=age)) is True
    ON.require_chat_access(member(age=age))


@pytest.mark.parametrize("age", [0, 5, 12])
def test_someone_below_the_minimum_use_age_cannot_even_if_an_account_somehow_exists(age):
    """正常流程不會有這樣的帳號（註冊就被擋），但規則被提高或資料被改過時，閘門仍要擋。"""
    assert ON.can_use_chat(member(age=age)) is False
    with pytest.raises(AgeRestricted):
        ON.require_chat_access(member(age=age))


@pytest.mark.parametrize("policy", ALL_STATUSES_AND_MODES)
@pytest.mark.parametrize("tier", ["free", "vip"])
@pytest.mark.parametrize("role", [ROLE_USER, ROLE_ADMIN])
@pytest.mark.parametrize("age", [None, 12, 13, 17, 18, 45])
@pytest.mark.parametrize("verified", [True, False])
@pytest.mark.parametrize("status", USER_STATUSES)
def test_chat_access_requires_every_condition_and_the_two_methods_agree(policy, tier, role, age, verified, status):
    user = member(age=age, member_tier=tier, role=role, email_verified=verified, status=status)
    allowed = status == STATUS_ACTIVE and (verified or policy is OFF) and age is not None and age >= 13
    assert policy.can_use_chat(user) is allowed
    if allowed:
        policy.require_chat_access(user)
    else:
        with pytest.raises(Forbidden):
            policy.require_chat_access(user)


@pytest.mark.parametrize("policy", ALL_STATUSES_AND_MODES)
@pytest.mark.parametrize("age", [None, 12, 13, 18, 45])
@pytest.mark.parametrize("verified", [True, False])
@pytest.mark.parametrize("status", USER_STATUSES)
def test_chat_gate_names_the_same_denial_as_the_exception_and_never_logs(policy, age, verified, status, log_lines):
    """頁面載入時用 `chat_gate` 得知結果；它與 `require_chat_access` 必須逐一一致，且查詢本身不產生事件。"""
    user = member(age=age, email_verified=verified, status=status)
    gate = policy.chat_gate(user)
    assert [line for line in log_lines() if line.get("event")] == []
    if gate is None:
        policy.require_chat_access(user)
    else:
        with pytest.raises(Forbidden) as denied:
            policy.require_chat_access(user)
        assert denied.value.code == gate


def test_chat_gate_returns_the_documented_codes():
    assert ON.chat_gate(member()) is None
    assert ON.chat_gate(member(age=None)) == "age_confirmation_required"
    assert ON.chat_gate(member(age=12)) == "age_restricted"
    assert ON.chat_gate(member(status=STATUS_SUSPENDED)) == "forbidden"
    assert ON.chat_gate(member(email_verified=False, age=None)) == "forbidden"       # 信箱未驗證先於年齡，不引導補填
    assert OFF.chat_gate(member(email_verified=False)) is None


def test_a_missing_age_check_raises_the_dedicated_error_code_that_points_to_the_account_page():
    with pytest.raises(AgeConfirmationRequired) as denied:
        ON.require_chat_access(member(age=None))
    assert isinstance(denied.value, Forbidden) and not isinstance(denied.value, AgeRestricted)
    assert denied.value.code == "age_confirmation_required"
    assert "生日" in denied.value.message and "帳號設定" in denied.value.message


def test_a_too_young_member_gets_a_different_code_that_does_not_point_to_the_account_page():
    with pytest.raises(AgeRestricted) as denied:
        ON.require_chat_access(member(age=12))
    assert isinstance(denied.value, Forbidden) and not isinstance(denied.value, AgeConfirmationRequired)
    assert denied.value.code == "age_restricted" and "帳號設定" not in denied.value.message


def test_raising_the_minimum_age_to_18_in_the_policy_blocks_teens_and_nobody_else():
    strict = policy_for(verification=False, age_policy=AgePolicy(min_register_age=18, min_use_age=18))
    assert strict.can_use_chat(member(age=17)) is False and strict.can_use_chat(member(age=18)) is True
    with pytest.raises(AgeRestricted):
        strict.require_chat_access(member(age=17))
    strict.require_chat_access(member(age=18))
    assert OFF.can_use_chat(member(age=17)) is True                  # 預設規則不受影響


def test_turning_18_lifts_the_limit_without_any_stored_flag_changing():
    """年齡由生日於當下計算：同一個人，時鐘前進過生日後內容上限自動改變。"""
    teen = member(age=None, birth_date=date(2008, 10, 28))           # 明天才滿 18 歲
    assert ON.content_ceiling(teen) == ContentCeiling(0, safe_mode=True)
    tomorrow = AccessPolicy(
        email_verification_required=True, clock=lambda: TODAY_UTC.replace(day=28)
    )
    assert tomorrow.content_ceiling(teen) == ContentCeiling(2, safe_mode=False)


@pytest.mark.parametrize("status", [STATUS_SUSPENDED, STATUS_DELETION_PENDING, STATUS_PENDING_VERIFICATION])
def test_the_account_status_is_checked_before_age_so_the_user_is_not_sent_to_the_account_page_for_nothing(status, log_lines):
    with pytest.raises(Forbidden) as denied:
        ON.require_chat_access(member(status=status, age=None))
    assert not isinstance(denied.value, AgeConfirmationRequired) and denied.value.code == "forbidden"
    assert all(line.get("event") != "auth.age_gate_denied" for line in log_lines())


def test_unverified_email_is_checked_before_age_when_verification_is_on(log_lines):
    with pytest.raises(Forbidden) as denied:
        ON.require_chat_access(member(email_verified=False, age=None))
    assert not isinstance(denied.value, AgeConfirmationRequired)
    with pytest.raises(AgeConfirmationRequired):
        OFF.require_chat_access(member(email_verified=False, age=None))        # 驗證關閉時不看信箱
    assert [line["event"] for line in log_lines() if line.get("event") == "auth.age_gate_denied"] == ["auth.age_gate_denied"]


@pytest.mark.parametrize(("age", "error"), [(None, AgeConfirmationRequired), (12, AgeRestricted)])
def test_an_age_denial_is_logged_once_with_the_internal_user_id_and_nothing_personal(log_lines, age, error):
    user = member(id=42, age=age)
    with pytest.raises(error):
        ON.require_chat_access(user)
    denials = [line for line in log_lines() if line.get("event") == "auth.age_gate_denied"]
    assert len(denials) == 1 and denials[0]["user_id"] == 42 and denials[0]["level"] == "INFO"
    raw = log_lines.raw()
    assert user.email not in raw and user.public_id not in raw and "2014" not in raw and "birth" not in raw


def test_a_member_who_passes_leaves_no_log_line(log_lines):
    ON.require_chat_access(member())
    assert [line for line in log_lines() if line.get("event") == "auth.age_gate_denied"] == []


# ---- 內容上限 -----------------------------------------------------------------

@pytest.mark.parametrize("age", [13, 14, 15, 16, 17])
def test_a_minor_is_always_in_safe_mode_level_0(age):
    assert ON.content_ceiling(member(age=age)) == ContentCeiling(sexual_content_level=0, safe_mode=True)


@pytest.mark.parametrize("age", [18, 19, 30, 65, 100])
def test_an_adult_may_reach_level_2_but_never_3(age):
    ceiling = ON.content_ceiling(member(age=age))
    assert ceiling == ContentCeiling(sexual_content_level=2, safe_mode=False) and ceiling.sexual_content_level != 3


def test_a_member_without_an_age_check_is_treated_conservatively():
    assert ON.content_ceiling(member(age=None)) == ContentCeiling(0, safe_mode=True)


@pytest.mark.parametrize("role", [ROLE_USER, ROLE_ADMIN])
@pytest.mark.parametrize("tier", MEMBER_TIERS)
def test_neither_role_nor_tier_changes_the_content_ceiling(role, tier):
    assert ON.content_ceiling(member(age=15, role=role, member_tier=tier)).safe_mode is True
    assert ON.content_ceiling(member(age=40, role=role, member_tier=tier)).sexual_content_level == 2


def test_the_ceiling_follows_the_adult_age_in_the_policy_not_a_number_in_the_code():
    older = policy_for(verification=False, age_policy=AgePolicy(adult_age=21))
    assert older.content_ceiling(member(age=20)).safe_mode is True and older.content_ceiling(member(age=21)).safe_mode is False


def test_the_policy_source_has_no_age_numbers_of_its_own():
    """年齡數字只能來自 AgePolicy：政策物件的程式碼裡不得出現 13 或 18 這兩個整數字面值。"""
    tree = ast.parse((PROJECT_ROOT / "app" / "services" / "access_policy.py").read_text(encoding="utf-8-sig"))
    literals = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and type(n.value) is int}
    assert not literals & {13, 17, 18}, literals


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


# ---- 後台入口：require_admin（admin-console）------------------------------------------------

ADMIN_EVENT = "admin.access_denied"


def test_an_active_admin_passes_the_admin_check_in_both_modes(log_lines):
    for policy in (ON, OFF):
        policy.require_admin(member(role=ROLE_ADMIN))
    assert log_lines() == []


@pytest.mark.parametrize("tier", MEMBER_TIERS)
def test_no_member_tier_makes_a_non_admin_an_admin(tier, log_lines):
    with pytest.raises(NotFound):
        ON.require_admin(member(member_tier=tier))
    assert [line["event"] for line in log_lines()] == [ADMIN_EVENT]


@pytest.mark.parametrize("status", [s for s in USER_STATUSES if s != STATUS_ACTIVE])
def test_an_admin_who_is_not_active_is_refused_like_a_stranger(status):
    with pytest.raises(NotFound):
        ON.require_admin(member(role=ROLE_ADMIN, status=status))


def test_the_refusal_is_the_same_not_found_as_a_missing_resource():
    with pytest.raises(NotFound) as refused:
        ON.require_admin(member())
    missing = NotFound()
    assert (refused.value.code, refused.value.message) == (missing.code, missing.message)
    assert type(refused.value) is NotFound                      # 不是 Forbidden 或其子類別：不能讓後台的存在洩漏出去


def test_a_refusal_logs_one_event_with_only_the_internal_id_and_a_fixed_reason(log_lines):
    with pytest.raises(NotFound):
        ON.require_admin(member(id=42, email="secret.person@example.com", display_name="秘密人物"))
    [line] = log_lines()
    assert {k: v for k, v in line.items() if k not in {"time", "level", "logger", "message"}} == {
        "event": ADMIN_EVENT, "user_id": 42, "reason": "not_admin",
    }
    assert "secret.person" not in str(line) and "秘密人物" not in str(line)


def test_the_age_gate_does_not_apply_to_the_admin_check():
    """後台不呼叫模型：沒有完成年齡確認的管理員仍可使用後台（年齡閘門只擋會呼叫模型的功能）。"""
    ON.require_admin(member(role=ROLE_ADMIN, age=None))
