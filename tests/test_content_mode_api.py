"""content-tier-modes 規格：`GET`／`PUT /api/account/content-mode` 與成人模式的成年確認（端到端）。

涵蓋：查詢的回應內容（不含任何個資）、未登入與尚未確認年齡、不合法的模式、CSRF、未成年者的限制、滿 18 歲不自動調高、
成人模式的各種條件（部署開關、兩項勾選、已確認後再切換、規範版本改變、離開不清除紀錄）、事件不含輸入，以及只能讀寫自己的。
"""
import json
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base, User
from app.models.user import ROLE_ADMIN
from app.services.content_tier import ADULT_RULES_VERSION
from tests.support.age import TODAY_UTC, birth_date_for_age
from tests.support.auth import FakeClock, add_user, sign_in_as
from tests.support.client import raw_client

URL = "/api/account/content-mode"
FREE_MODEL = "vendor/narrator:free"
ADULT_ENV = {
    "OPENROUTER_API_KEY": "sk-or-v1-fake-key-for-tests-0123456789", "OPENROUTER_MODEL": FREE_MODEL,
    "ADULT_MODE_ENABLED": "true", "ADULT_MODE_MODELS": FREE_MODEL,
}
BOTH = {"confirm_adult": True, "confirm_rules": True}
STATE_KEYS = {"preference", "effective", "selectable", "adult_reason", "adult_confirmed"}
CONFIRMED = {"adult_confirmed_at": TODAY_UTC - timedelta(days=3), "adult_rules_version": ADULT_RULES_VERSION, "adult_confirm_method": "self_declared"}


def make_app(clock=None, **environ):
    application = create_app(load_config(environ={"APP_ENV": "testing", **environ}), clock=clock or FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


@pytest.fixture()
def app():
    return make_app()


@pytest.fixture()
def adult_app():
    return make_app(**ADULT_ENV)


def seed(app, n: int = 1, **overrides) -> int:
    with Session(app.extensions["db_engine"]) as s:
        return add_user(s, n, **overrides).id


def row(app, user_id: int) -> User:
    with Session(app.extensions["db_engine"]) as s:
        user = s.get(User, user_id)
        s.expunge(user)
        return user


def client_for(app, user_id: int):
    client = app.test_client()
    sign_in_as(client, user_id)
    return client


def put(client, mode, **extra):
    return client.put(URL, json={"mode": mode, **extra})


def state(response) -> dict:
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["content_mode"]


def error(response) -> dict:
    return response.get_json()["error"]


# ---- 查詢 -----------------------------------------------------------------------------

def test_an_adult_sees_the_preference_the_effective_mode_and_which_modes_can_be_chosen(app):
    client = client_for(app, seed(app, age=30))
    response = client.get(URL)
    assert response.headers["Cache-Control"] == "no-store"
    assert state(response) == {
        "preference": "standard", "effective": "standard", "selectable": {"safe": True, "standard": True, "adult": False},
        "adult_reason": "disabled", "adult_confirmed": False,
    }


def test_the_response_contains_only_the_documented_keys_and_no_personal_data(app):
    user_id = seed(app, age=30)
    body = client_for(app, user_id).get(URL).get_data(as_text=True)
    assert set(json.loads(body)["content_mode"]) == STATE_KEYS
    user = row(app, user_id)
    for secret in (user.email, user.public_id, str(user.birth_date), user.age_range, "age_confirmed", "birth", "user_id"):
        assert secret not in body


def test_anonymous_users_get_401(app):
    anonymous = app.test_client()
    assert anonymous.get(URL).status_code == 401
    assert anonymous.put(URL, json={"mode": "safe"}).status_code == 401


def test_a_player_who_has_not_confirmed_the_age_gets_403_with_the_age_code(app):
    client = client_for(app, seed(app, age=None))
    for response in (client.get(URL), put(client, "safe")):
        assert response.status_code == 403 and error(response)["code"] == "age_confirmation_required"


def test_an_admin_who_has_not_confirmed_the_age_gets_the_same_403(app):
    client = client_for(app, seed(app, age=None, role=ROLE_ADMIN))
    for response in (client.get(URL), put(client, "safe")):
        assert response.status_code == 403 and error(response)["code"] == "age_confirmation_required"


# ---- 切換：基本與不合法的輸入 -----------------------------------------------------------

def test_an_adult_can_choose_the_safe_mode_and_back_to_the_standard_mode(app):
    user_id = seed(app, age=30)
    client = client_for(app, user_id)
    changed = state(put(client, "safe"))
    assert changed == state(client.get(URL)) and (changed["preference"], changed["effective"]) == ("safe", "safe")
    assert row(app, user_id).content_tier_preference == "safe"
    result = state(put(client, "standard"))
    assert (result["preference"], result["effective"]) == ("standard", "standard")
    assert row(app, user_id).content_tier_preference == "standard"


@pytest.mark.parametrize("mode", ["mature", "", "SAFE", None, 1, ["safe"], {"mode": "safe"}, True])
def test_an_invalid_mode_is_a_400_naming_the_field_and_changes_nothing(app, mode):
    user_id = seed(app, age=30)
    response = put(client_for(app, user_id), mode)
    assert response.status_code == 400 and "mode" in error(response)["fields"]
    assert row(app, user_id).content_tier_preference == "standard"


def test_a_missing_mode_is_a_400(app):
    client = client_for(app, seed(app, age=30))
    response = client.put(URL, json={})
    assert response.status_code == 400 and "mode" in error(response)["fields"]


def test_a_put_without_a_csrf_token_is_refused_and_changes_nothing(app):
    user_id = seed(app, age=30)
    attacker = client_for(app, user_id)
    forged = raw_client(app)
    forged.set_cookie("session", attacker.get_cookie("session").value)
    response = forged.put(URL, json={"mode": "safe"}, headers={"Origin": "http://localhost"})
    assert response.status_code == 403
    assert row(app, user_id).content_tier_preference == "standard"


# ---- 未成年者 ---------------------------------------------------------------------------

@pytest.mark.parametrize("age", [13, 15, 17])
@pytest.mark.parametrize("mode", ["standard", "adult"])
def test_a_minor_cannot_choose_the_standard_or_adult_mode(adult_app, age, mode):
    user_id = seed(adult_app, age=age)
    response = put(client_for(adult_app, user_id), mode, **BOTH)
    assert response.status_code == 403 and error(response)["code"] == "content_mode_restricted"
    user = row(adult_app, user_id)
    assert user.content_tier_preference == "safe" and user.adult_confirmed_at is None


def test_a_minor_sees_the_safe_mode_and_the_reason_and_may_choose_the_safe_mode(adult_app):
    client = client_for(adult_app, seed(adult_app, age=15))
    assert state(client.get(URL)) == {
        "preference": "safe", "effective": "safe", "selectable": {"safe": True, "standard": False, "adult": False},
        "adult_reason": "not_adult", "adult_confirmed": False,
    }
    assert state(put(client, "safe"))["effective"] == "safe"


def test_an_admin_who_is_a_minor_is_not_exempt(adult_app):
    client = client_for(adult_app, seed(adult_app, age=15, role=ROLE_ADMIN))
    assert error(put(client, "standard"))["code"] == "content_mode_restricted"


def test_turning_18_lifts_the_limit_but_not_the_preference_and_the_player_can_then_switch():
    clock = FakeClock()
    app = make_app(clock=clock)
    tomorrow_18 = birth_date_for_age(18) + timedelta(days=1)
    user_id = seed(app, age=17, birth_date=tomorrow_18)
    client = client_for(app, user_id)
    assert state(client.get(URL))["selectable"]["standard"] is False
    assert error(put(client, "standard"))["code"] == "content_mode_restricted"
    clock.advance(days=2)
    after = state(client.get(URL))
    assert (after["preference"], after["effective"]) == ("safe", "safe")          # 不自動調高
    assert after["selectable"] == {"safe": True, "standard": True, "adult": False}  # 介面據此提示「現在可以切換為一般模式」
    assert state(put(client, "standard"))["effective"] == "standard"


# ---- 成人模式 ---------------------------------------------------------------------------

def test_the_first_switch_to_the_adult_mode_records_the_confirmation(adult_app):
    user_id = seed(adult_app, age=30)
    result = state(put(client_for(adult_app, user_id), "adult", **BOTH))
    assert (result["preference"], result["effective"], result["adult_confirmed"], result["adult_reason"]) == ("adult", "adult", True, None)
    user = row(adult_app, user_id)
    assert user.content_tier_preference == "adult"
    assert user.adult_confirmed_at == TODAY_UTC and user.adult_rules_version == ADULT_RULES_VERSION
    assert user.adult_confirm_method == "self_declared"


def test_the_state_before_confirming_says_the_adult_mode_needs_confirming(adult_app):
    result = state(client_for(adult_app, seed(adult_app, age=30)).get(URL))
    assert result["selectable"]["adult"] is True and result["adult_reason"] == "needs_confirmation" and result["adult_confirmed"] is False


@pytest.mark.parametrize(
    ("extra", "missing"),
    [
        ({"confirm_adult": True}, {"confirm_rules"}),
        ({"confirm_rules": True}, {"confirm_adult"}),
        ({"confirm_adult": True, "confirm_rules": False}, {"confirm_rules"}),
        ({}, {"confirm_adult", "confirm_rules"}),
        ({"confirm_adult": "true", "confirm_rules": 1}, {"confirm_adult", "confirm_rules"}),
    ],
)
def test_a_missing_confirmation_is_a_400_naming_what_is_missing_and_changes_nothing(adult_app, extra, missing):
    user_id = seed(adult_app, age=30)
    response = put(client_for(adult_app, user_id), "adult", **extra)
    assert response.status_code == 400 and set(error(response)["fields"]) == missing
    user = row(adult_app, user_id)
    assert user.content_tier_preference == "standard"
    assert (user.adult_confirmed_at, user.adult_rules_version, user.adult_confirm_method) == (None, None, None)


def test_with_the_deployment_switch_off_the_adult_mode_is_unavailable(app):
    user_id = seed(app, age=30)
    response = put(client_for(app, user_id), "adult", **BOTH)
    assert response.status_code == 403 and error(response)["code"] == "adult_mode_unavailable"
    user = row(app, user_id)
    assert user.content_tier_preference == "standard" and user.adult_confirmed_at is None


def test_a_confirmed_player_can_switch_between_modes_without_ticking_again(adult_app):
    user_id = seed(adult_app, age=30, **CONFIRMED)
    client = client_for(adult_app, user_id)
    assert state(put(client, "adult"))["effective"] == "adult"
    assert state(put(client, "standard"))["effective"] == "standard"
    assert state(put(client, "adult"))["effective"] == "adult"


def test_leaving_the_adult_mode_keeps_the_confirmation_record(adult_app):
    user_id = seed(adult_app, age=30)
    client = client_for(adult_app, user_id)
    put(client, "adult", **BOTH)
    before = row(adult_app, user_id)
    assert state(put(client, "standard"))["adult_confirmed"] is True
    after = row(adult_app, user_id)
    assert (after.adult_confirmed_at, after.adult_rules_version, after.adult_confirm_method) == (
        before.adult_confirmed_at, before.adult_rules_version, before.adult_confirm_method,
    )
    assert after.content_tier_preference == "standard"


def test_a_new_rules_version_invalidates_the_old_confirmation(adult_app):
    stale = {**CONFIRMED, "adult_rules_version": "2020-01-01-old"}
    user_id = seed(adult_app, age=30, content_tier_preference="adult", **stale)
    client = client_for(adult_app, user_id)
    current = state(client.get(URL))
    assert (current["preference"], current["effective"], current["adult_reason"], current["adult_confirmed"]) == (
        "adult", "standard", "needs_confirmation", False,
    )
    assert put(client, "adult").status_code == 400                     # 要重新勾選
    renewed = state(put(client, "adult", **BOTH))
    assert (renewed["effective"], renewed["adult_confirmed"]) == ("adult", True)
    assert row(adult_app, user_id).adult_rules_version == ADULT_RULES_VERSION


def test_every_account_is_treated_alike_whatever_the_role_or_tier(adult_app):
    for n, overrides in enumerate(({"role": ROLE_ADMIN}, {"member_tier": "vip"}, {"member_tier": "free"}), start=1):
        user_id = seed(adult_app, n, age=30, **overrides)
        assert state(put(client_for(adult_app, user_id), "adult", **BOTH))["effective"] == "adult"


# ---- 事件與日誌 -------------------------------------------------------------------------

def test_a_successful_switch_logs_one_event_with_only_the_name_and_the_user_id(adult_app, log_lines):
    user_id = seed(adult_app, age=30)
    put(client_for(adult_app, user_id), "adult", **BOTH)
    events = [line for line in log_lines() if str(line.get("event", "")).startswith("account.content_mode.")]
    assert len(events) == 1 and events[0]["event"] == "account.content_mode.adult" and events[0]["user_id"] == user_id
    raw = log_lines.raw()
    for text in ("confirm_adult", "confirm_rules", "self_declared", ADULT_RULES_VERSION, "已滿 18", "安全規範"):
        assert text not in raw


@pytest.mark.parametrize("mode", ["safe", "standard"])
def test_the_event_name_expresses_the_new_mode(app, log_lines, mode):
    put(client_for(app, seed(app, age=30)), mode)
    assert [line["event"] for line in log_lines() if str(line.get("event", "")).startswith("account.content_mode.")] == [
        f"account.content_mode.{mode}",
    ]


def test_a_refused_switch_logs_no_content_mode_event(adult_app, log_lines):
    minor = client_for(adult_app, seed(adult_app, 1, age=15))
    adult = client_for(adult_app, seed(adult_app, 2, age=30))
    put(minor, "adult", **BOTH)
    put(adult, "mature")
    put(adult, "adult", confirm_adult=True)
    assert not [line for line in log_lines() if str(line.get("event", "")).startswith("account.content_mode.")]


# ---- 只能讀寫自己的 ---------------------------------------------------------------------

def test_a_player_can_only_read_and_write_their_own_mode(adult_app):
    alice_id, bob_id = seed(adult_app, 1, age=30), seed(adult_app, 2, age=30)
    alice, bob = client_for(adult_app, alice_id), client_for(adult_app, bob_id)
    put(alice, "adult", **BOTH)
    assert state(alice.get(URL))["preference"] == "adult" and state(bob.get(URL))["preference"] == "standard"
    assert (row(adult_app, alice_id).content_tier_preference, row(adult_app, bob_id).content_tier_preference) == ("adult", "standard")
    assert row(adult_app, bob_id).adult_confirmed_at is None
    # 沒有任何路徑或參數能指定別人的帳號
    assert alice.put(URL + "/2", json={"mode": "safe"}).status_code in (404, 405)
    response = alice.put(URL, json={"mode": "safe", "user_id": bob_id, "id": bob_id, "public_id": "x"})
    assert response.status_code == 200
    assert row(adult_app, bob_id).content_tier_preference == "standard"          # Bob 沒有被改動
    assert row(adult_app, alice_id).content_tier_preference == "safe"            # 被改動的是呼叫者自己
