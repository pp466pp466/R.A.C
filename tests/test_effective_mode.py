"""content-tier-modes 規格：生效模式依設定、劇本綁定與緊急煞車現算（`AccessPolicy.effective_mode`、`mode_state`）。

表格驅動：玩家偏好 × 同意狀態 × 劇本綁定 × 緊急煞車 × 角色與會員等級，逐一對照「三項取最保守者」的期望結果；
另驗證沒有依年齡的上限、舊值 `standard` 讀作 `adult`（但仍須有同意）、傳入無法判定的值不會被忽略。
"""
import itertools
from datetime import timedelta

import pytest

from app.models.user import ROLE_ADMIN, ROLE_USER
from app.services.access_policy import AccessPolicy
from app.services.content_tier import SAFE_RULES_VERSION
from tests.support.age import TODAY_UTC
from tests.test_access_policy import member

CONFIRMED = {"adult_confirmed_at": TODAY_UTC - timedelta(days=3), "adult_rules_version": SAFE_RULES_VERSION}
OLD_RULES = {"adult_confirmed_at": TODAY_UTC - timedelta(days=3), "adult_rules_version": "2026-11-03-draft-1"}
NO_CONSENT = {"adult_confirmed_at": None, "adult_rules_version": None}


def policy(forced: bool = False) -> AccessPolicy:
    return AccessPolicy(clock=lambda: TODAY_UTC, safe_mode_forced=forced)


def player(preference: str, consent: dict, **overrides):
    return member(content_tier_preference=preference, **consent, **overrides)


def expected(preference: str, confirmed: bool, binding: str | None, forced: bool) -> str:
    """規格的期望值，寫成與實作不同的形式（逐項檢查「任何一項是 safe 就是 safe」）。"""
    if forced or not confirmed or preference not in ("adult", "standard"):
        return "safe"
    if binding is not None and binding not in ("adult", "standard"):
        return "safe"
    return "adult"


@pytest.mark.parametrize(
    ("preference", "consent", "binding", "forced"),
    list(itertools.product(
        ("safe", "adult", "standard", "bogus", None),
        (("current", CONFIRMED), ("old", OLD_RULES), ("none", NO_CONSENT)),
        (None, "safe", "adult", "standard", "bogus"),
        (False, True),
    )),
    ids=lambda value: value[0] if isinstance(value, tuple) else str(value),
)
def test_the_effective_mode_is_the_most_conservative_of_the_three_inputs(preference, consent, binding, forced):
    name, record = consent
    user = player(preference, record)
    result = policy(forced).effective_mode(user, binding)
    assert result == expected(preference, name == "current", binding, forced)
    assert result in ("safe", "adult")                       # 只可能是兩種模式之一，不會是 standard


def test_all_relaxed_inputs_give_the_adult_mode():
    assert policy().effective_mode(player("adult", CONFIRMED), "adult") == "adult"
    assert policy().effective_mode(player("adult", CONFIRMED)) == "adult"               # 沒有劇本時不參與


def test_a_script_bound_to_the_safe_mode_wins_over_a_relaxed_preference():
    assert policy().effective_mode(player("adult", CONFIRMED), "safe") == "safe"


def test_the_emergency_brake_makes_every_player_and_script_safe_without_touching_the_saved_preference():
    user = player("adult", CONFIRMED)
    assert policy(True).effective_mode(user) == policy(True).effective_mode(user, "adult") == "safe"
    assert user.content_tier_preference == "adult"                                       # 偏好不被改寫
    assert policy(False).effective_mode(user) == "adult"                                 # 解除後恢復


def test_a_preference_without_a_current_consent_is_safe_not_an_error():
    for record in (OLD_RULES, NO_CONSENT):
        assert policy().effective_mode(player("adult", record)) == "safe"


def test_the_legacy_standard_preference_reads_as_adult_but_still_needs_the_current_consent():
    assert policy().effective_mode(player("standard", NO_CONSENT)) == "safe"
    assert policy().effective_mode(player("standard", CONFIRMED)) == "adult"


def test_a_script_binding_that_cannot_be_read_counts_as_bound_instead_of_being_ignored():
    user = player("adult", CONFIRMED)
    for bogus in ("", "mature", "ADULT", 1, True, ["adult"]):
        assert policy().effective_mode(user, bogus) == "safe"


@pytest.mark.parametrize("age", [18, 19, 30, 80, None])
def test_there_is_no_age_cap_any_more(age):
    # 全站只限滿 18 歲者使用（閘門另由 chat_gate 負責）；生效模式不看年齡，也不看會員等級
    for tier in ("free", "plus", "vip"):
        user = player("adult", CONFIRMED, age=age, member_tier=tier)
        assert policy().effective_mode(user) == "adult"


@pytest.mark.parametrize("role", [ROLE_USER, ROLE_ADMIN])
def test_an_admin_is_not_exempt_in_either_direction(role):
    assert policy().effective_mode(player("safe", CONFIRMED, role=role)) == "safe"
    assert policy().effective_mode(player("adult", NO_CONSENT, role=role)) == "safe"
    assert policy(True).effective_mode(player("adult", CONFIRMED, role=role)) == "safe"
    assert policy().effective_mode(player("adult", CONFIRMED, role=role)) == "adult"


def test_the_consent_follows_the_rules_version_only():
    assert policy().rules_confirmed(player("adult", CONFIRMED)) is True
    assert policy().rules_confirmed(player("adult", OLD_RULES)) is False
    assert policy().rules_confirmed(player("adult", {"adult_confirmed_at": TODAY_UTC, "adult_rules_version": None})) is False
    assert policy().rules_confirmed(player("adult", {"adult_confirmed_at": None, "adult_rules_version": SAFE_RULES_VERSION})) is False


def test_the_mode_state_has_exactly_the_four_documented_fields():
    state = policy().mode_state(player("adult", CONFIRMED))
    assert (state.preference, state.effective, state.confirmed, state.forced) == ("adult", "adult", True, False)
    forced = policy(True).mode_state(player("adult", CONFIRMED))
    assert (forced.preference, forced.effective, forced.confirmed, forced.forced) == ("adult", "safe", True, True)
    stale = policy().mode_state(player("standard", OLD_RULES))
    assert (stale.preference, stale.effective, stale.confirmed, stale.forced) == ("adult", "safe", False, False)   # 舊偏好讀作 adult
    default = policy().mode_state(player("bogus", NO_CONSENT))
    assert (default.preference, default.effective) == ("safe", "safe")
    assert set(state.__dataclass_fields__) == {"preference", "effective", "confirmed", "forced"}      # 沒有生日、年齡區間、時間戳記


@pytest.mark.parametrize("value", [1, 0, None, "true", "yes"])
def test_the_brake_must_be_a_real_boolean(value):
    with pytest.raises(ValueError):
        AccessPolicy(safe_mode_forced=value)


def test_the_brake_is_off_by_default_and_is_readable():
    assert AccessPolicy().safe_mode_forced is False
    assert AccessPolicy(safe_mode_forced=True).safe_mode_forced is True
