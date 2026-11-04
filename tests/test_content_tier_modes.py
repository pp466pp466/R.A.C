"""content-tier-modes 規格：年齡上限、生效模式取最保守者、成人模式的可用條件與成年確認的失效。

兩層驗證：政策物件本身（年齡 × 部署開關 × 成年確認 × 偏好 × 劇本與存檔的組合），以及從環境設定經 `create_app`
與組裝層一路到 HTTP 回應。API 與切換流程另見 `test_content_mode_api.py`。
"""
import itertools
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base
from app.models.user import CONTENT_TIERS, ROLE_ADMIN, ROLE_USER
from app.services.access_policy import AccessPolicy
from app.services.content_tier import ADULT_RULES_VERSION
from tests.support.age import TODAY_UTC
from tests.support.age_gate import ENDPOINT, add_ai_endpoint
from tests.support.auth import FakeClock, add_user, sign_in_as
from tests.test_access_policy import member

FREE_MODEL = "vendor/narrator:free"
ADULT_ENV = {
    "OPENROUTER_API_KEY": "sk-or-v1-fake-key-for-tests-0123456789", "OPENROUTER_MODEL": FREE_MODEL,
    "ADULT_MODE_ENABLED": "true", "ADULT_MODE_MODELS": FREE_MODEL,
}
CONFIRMED = {"adult_confirmed_at": TODAY_UTC - timedelta(days=3), "adult_rules_version": ADULT_RULES_VERSION}


def policy(enabled: bool | None = None) -> AccessPolicy:
    extra = {} if enabled is None else {"adult_mode_enabled": enabled}
    return AccessPolicy(clock=lambda: TODAY_UTC, **extra)


def adult(preference="adult", **overrides):
    return member(age=30, content_tier_preference=preference, **overrides)


# ---- 政策物件：預設與部署開關 ---------------------------------------------------------------------------

def test_the_adult_mode_is_off_by_default_in_the_policy():
    assert policy().adult_mode_enabled is False
    assert AccessPolicy().adult_mode_enabled is False


@pytest.mark.parametrize("value", [1, 0, None, "true", "yes"])
def test_the_switch_must_be_a_real_boolean(value):
    with pytest.raises(ValueError):
        AccessPolicy(adult_mode_enabled=value)


def test_with_the_switch_off_a_confirmed_adult_who_prefers_adult_falls_back_to_standard_without_an_error():
    access = policy(False).content_access(adult(**CONFIRMED))
    assert (access.preference, access.age_cap, access.effective, access.reason) == ("adult", "standard", "standard", "disabled")
    assert access.adult_selectable is False and access.adult_confirmed is True
    assert access.selectable == ("safe", "standard")


def test_with_the_switch_on_but_no_confirmation_the_adult_mode_needs_confirming():
    access = policy(True).content_access(adult())
    assert (access.age_cap, access.effective, access.reason) == ("standard", "standard", "needs_confirmation")
    assert access.adult_selectable is True and access.adult_confirmed is False
    assert access.selectable == ("safe", "standard", "adult")


def test_with_the_switch_on_and_a_current_confirmation_the_adult_mode_is_active():
    access = policy(True).content_access(adult(**CONFIRMED))
    assert (access.age_cap, access.effective, access.reason) == ("adult", "adult", None)
    assert access.adult_confirmed is True


def test_the_preference_still_limits_an_active_adult_mode():
    for preference, expected in (("safe", "safe"), ("standard", "standard"), ("adult", "adult")):
        assert policy(True).content_access(adult(preference, **CONFIRMED)).effective == expected


def test_a_confirmation_for_an_older_rules_version_no_longer_counts():
    stale = {**CONFIRMED, "adult_rules_version": "2020-01-01-old"}
    access = policy(True).content_access(adult(**stale))
    assert (access.effective, access.reason, access.adult_confirmed) == ("standard", "needs_confirmation", False)


def test_a_missing_confirmation_time_or_rules_version_does_not_count():
    assert policy(True).content_access(adult(adult_confirmed_at=None, adult_rules_version=ADULT_RULES_VERSION)).adult_confirmed is False
    assert policy(True).content_access(adult(adult_confirmed_at=CONFIRMED["adult_confirmed_at"], adult_rules_version=None)).adult_confirmed is False


@pytest.mark.parametrize("age", [13, 15, 17, None])
@pytest.mark.parametrize("role", [ROLE_USER, ROLE_ADMIN])
@pytest.mark.parametrize("enabled", [False, True])
def test_a_minor_or_a_member_without_an_age_check_is_always_safe_whatever_the_switch_or_the_record(age, role, enabled):
    user = member(age=age, role=role, content_tier_preference="adult", **CONFIRMED)
    access = policy(enabled).content_access(user)
    assert (access.age_cap, access.effective, access.reason) == ("safe", "safe", "not_adult")
    assert access.adult_selectable is False and access.selectable == ("safe",)
    assert policy(enabled).effective_tier(user, script_tier="adult", save_tier="adult") == "safe"


@pytest.mark.parametrize("role", [ROLE_USER, ROLE_ADMIN])
def test_an_adult_admin_is_treated_like_any_other_adult(role):
    assert policy(True).content_access(adult(role=role, **CONFIRMED)).effective == "adult"


def test_the_switch_does_not_make_the_age_boundary_any_softer():
    """剛好 17 歲與剛好 18 歲：開關開啟、已有確認紀錄也一樣在 18 歲才解除。"""
    assert policy(True).content_access(member(age=17, content_tier_preference="adult", **CONFIRMED)).effective == "safe"
    assert policy(True).content_access(member(age=18, content_tier_preference="adult", **CONFIRMED)).effective == "adult"


# ---- 政策物件：生效模式取最保守者 -------------------------------------------------------------------------

def test_the_effective_tier_is_the_most_conservative_of_the_four_inputs():
    user = adult("adult", **CONFIRMED)                 # 偏好 adult、年齡上限 adult
    assert policy(True).effective_tier(user, script_tier="standard", save_tier="safe") == "safe"
    assert policy(True).effective_tier(user, script_tier="standard") == "standard"
    assert policy(True).effective_tier(user, save_tier="standard") == "standard"
    assert policy(True).effective_tier(user, script_tier="adult", save_tier="adult") == "adult"
    assert policy(True).effective_tier(user) == "adult"


def test_a_script_cannot_raise_the_effective_tier_above_the_player_setting():
    assert policy(True).effective_tier(adult("safe", **CONFIRMED), script_tier="adult") == "safe"
    assert policy(True).effective_tier(adult("standard", **CONFIRMED), script_tier="adult") == "standard"


def test_an_unrecognised_script_or_save_value_is_treated_as_safe_not_ignored():
    assert policy(True).effective_tier(adult(**CONFIRMED), script_tier="mature") == "safe"
    assert policy(True).effective_tier(adult(**CONFIRMED), save_tier="") == "safe"


def test_no_combination_ever_yields_a_tier_above_any_of_its_inputs():
    """存檔記錄的模式只能在上限之內調整：生效模式永遠不高於偏好、年齡上限、劇本宣告與存檔記錄中的任何一個。"""
    ranks = {tier: rank for rank, tier in enumerate(CONTENT_TIERS)}
    options = (None, *CONTENT_TIERS)
    for preference, confirmed, enabled, script_tier, save_tier in itertools.product(CONTENT_TIERS, (False, True), (False, True), options, options):
        user = adult(preference, **(CONFIRMED if confirmed else {}))
        result = policy(enabled).effective_tier(user, script_tier=script_tier, save_tier=save_tier)
        access = policy(enabled).content_access(user)
        for bound in (access.preference, access.age_cap, script_tier, save_tier):
            if bound is not None:
                assert ranks[result] <= ranks[bound], (preference, confirmed, enabled, script_tier, save_tier, result)


# ---- 滿 18 歲不自動調高 -------------------------------------------------------------------------------------

def test_turning_18_lifts_the_cap_but_not_the_preference_and_offers_the_standard_mode():
    from datetime import date

    teen = member(age=None, birth_date=date(2008, 10, 27), content_tier_preference="safe")     # 今天剛好 18 歲
    day_before = AccessPolicy(clock=lambda: TODAY_UTC - timedelta(days=1))
    assert day_before.content_access(teen).age_cap == "safe" and day_before.content_access(teen).selectable == ("safe",)
    after = policy(False).content_access(teen)
    assert (after.preference, after.effective, after.is_adult) == ("safe", "safe", True)
    assert after.selectable == ("safe", "standard")                      # 介面據此提示「現在可以切換為一般模式」


def test_an_adult_may_choose_the_safe_mode_and_keeps_it():
    access = policy(True).content_access(adult("safe", **CONFIRMED))
    assert access.effective == "safe" and "safe" in access.selectable


# ---- 從環境設定到 HTTP ---------------------------------------------------------------------------------------

def make_app(**environ):
    config = load_config(environ={"APP_ENV": "testing", **environ})
    application = create_app(config, clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    add_ai_endpoint(application)
    return application


def content_for(app, n: int, **user_overrides) -> dict:
    with Session(app.extensions["db_engine"]) as session:
        user_id = add_user(session, n, **user_overrides).id
    client = app.test_client()
    sign_in_as(client, user_id)                                     # 管理員不能只靠密碼登入，所以直接寫入登入狀態
    response = client.post(ENDPOINT, json={})
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["content"]


def test_by_default_the_whole_stack_caps_an_adult_at_the_standard_mode():
    app = make_app()
    assert content_for(app, 1, age=30, content_tier_preference="adult", **CONFIRMED)["effective"] == "standard"


def test_a_configured_but_disabled_switch_changes_nothing():
    app = make_app(**{**ADULT_ENV, "ADULT_MODE_ENABLED": "false"})
    assert content_for(app, 1, age=30, content_tier_preference="adult", **CONFIRMED)["effective"] == "standard"


def test_with_the_switch_on_the_whole_stack_lets_a_confirmed_adult_reach_the_adult_mode_and_still_protects_minors():
    app = make_app(**ADULT_ENV)
    assert content_for(app, 1, age=30, content_tier_preference="adult", **CONFIRMED) == {
        "preference": "adult", "age_cap": "adult", "effective": "adult", "reason": None,
    }
    assert content_for(app, 2, age=30, content_tier_preference="adult")["reason"] == "needs_confirmation"       # 開關開了，沒有確認
    assert content_for(app, 3, age=17, content_tier_preference="adult", **CONFIRMED)["effective"] == "safe"
    assert content_for(app, 4, age=15, role=ROLE_ADMIN, content_tier_preference="adult", **CONFIRMED)["effective"] == "safe"   # 管理員不豁免


def test_the_policy_the_requests_use_is_built_from_the_config_not_from_a_constant():
    for environ, expected in (({}, False), (ADULT_ENV, True)):
        app = make_app(**environ)
        with app.app_context():
            from app.services.registry import get_access_policy

            assert get_access_policy().adult_mode_enabled is expected
