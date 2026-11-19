"""content-tier-modes 規格：`GET`／`PUT /api/account/content-mode` 與關閉安全模式的同意與紀錄、緊急煞車（端到端）。

涵蓋：查詢的回應內容（只有四個欄位、不含任何個資）、未登入與尚未確認年齡、不合法的模式（含舊值 `standard`）、CSRF、
關閉安全模式只需同意安全規範（不再要求年滿 18 歲的勾選）、已同意後再切換、規範版本改變後同意失效、回到安全模式不清除紀錄、
緊急煞車（要求關閉 403、切回安全仍可）、事件不含輸入，以及只能讀寫自己的。
"""
import json
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base, User
from app.models.user import ROLE_ADMIN
from app.services.content_tier import SAFE_RULES_VERSION
from tests.support.age import TODAY_UTC
from tests.support.auth import FakeClock, add_user, sign_in_as
from tests.support.client import raw_client

URL = "/api/account/content-mode"
STATE_KEYS = {"preference", "effective", "confirmed", "forced"}
CONFIRMED = {"adult_confirmed_at": TODAY_UTC - timedelta(days=3), "adult_rules_version": SAFE_RULES_VERSION, "adult_confirm_method": "self_declared"}
STALE = {**CONFIRMED, "adult_rules_version": "2020-01-01-old"}


def make_app(**environ):
    application = create_app(load_config(environ={"APP_ENV": "testing", **environ}), clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


@pytest.fixture()
def app():
    return make_app()


@pytest.fixture()
def forced_app():
    return make_app(SAFE_MODE_FORCED="true")


def seed(app, n: int = 1, **overrides) -> int:
    """建立一位已完成年齡確認的玩家；偏好預設為 `safe`（與正式的年齡確認流程一致，測試輔助預設的 `standard` 不適用這裡）。"""
    overrides.setdefault("content_tier_preference", "safe")
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


def mode_events(log_lines) -> list[str]:
    return [line["event"] for line in log_lines() if str(line.get("event", "")).startswith("account.content_mode.")]


# ---- 查詢 -----------------------------------------------------------------------------

def test_a_new_player_sees_the_safe_mode_on_by_default(app):
    client = client_for(app, seed(app))
    response = client.get(URL)
    assert response.headers["Cache-Control"] == "no-store"
    assert state(response) == {"preference": "safe", "effective": "safe", "confirmed": False, "forced": False}


def test_the_response_contains_only_the_documented_keys_and_no_personal_data(app):
    user_id = seed(app, age=30, content_tier_preference="adult", **CONFIRMED)
    client = client_for(app, user_id)
    for body in (client.get(URL).get_data(as_text=True), put(client, "adult").get_data(as_text=True)):
        assert set(json.loads(body)["content_mode"]) == STATE_KEYS
        user = row(app, user_id)
        for secret in (user.email, user.public_id, str(user.birth_date), user.age_range, "age_confirmed", "birth", "user_id",
                       "adult_confirmed_at", "adult_rules_version", "self_declared", SAFE_RULES_VERSION):
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

def test_a_player_can_go_back_to_the_safe_mode_and_out_again_once_the_rules_are_agreed(app):
    user_id = seed(app, age=30, content_tier_preference="adult", **CONFIRMED)
    client = client_for(app, user_id)
    changed = state(put(client, "safe"))
    assert changed == state(client.get(URL)) and (changed["preference"], changed["effective"]) == ("safe", "safe")
    assert row(app, user_id).content_tier_preference == "safe"
    result = state(put(client, "adult"))
    assert (result["preference"], result["effective"]) == ("adult", "adult")
    assert row(app, user_id).content_tier_preference == "adult"


@pytest.mark.parametrize("mode", ["standard", "mature", "", "SAFE", None, 1, ["safe"], {"mode": "safe"}, True])
def test_an_invalid_mode_including_the_retired_standard_is_a_400_naming_the_field_and_changes_nothing(app, mode):
    user_id = seed(app, age=30)
    response = put(client_for(app, user_id), mode, confirm_rules=True)
    assert response.status_code == 400 and "mode" in error(response)["fields"]
    user = row(app, user_id)
    assert user.content_tier_preference == "safe" and user.adult_confirmed_at is None            # 偏好不變、沒有留下同意紀錄


def test_a_missing_mode_is_a_400(app):
    client = client_for(app, seed(app, age=30))
    response = client.put(URL, json={})
    assert response.status_code == 400 and "mode" in error(response)["fields"]


def test_a_put_without_a_csrf_token_is_refused_and_changes_nothing(app):
    user_id = seed(app, age=30, content_tier_preference="adult", **CONFIRMED)
    attacker = client_for(app, user_id)
    forged = raw_client(app)
    forged.set_cookie("session", attacker.get_cookie("session").value)
    response = forged.put(URL, json={"mode": "safe"}, headers={"Origin": "http://localhost"})
    assert response.status_code == 403
    assert row(app, user_id).content_tier_preference == "adult"


# ---- 關閉安全模式：同意與紀錄 ----------------------------------------------------------------

def test_the_first_switch_to_the_adult_mode_records_the_confirmation_with_only_the_rules_tick(app):
    user_id = seed(app, age=30)
    result = state(put(client_for(app, user_id), "adult", confirm_rules=True))          # 沒有 confirm_adult：不再要求年滿 18 歲的勾選
    assert result == {"preference": "adult", "effective": "adult", "confirmed": True, "forced": False}
    user = row(app, user_id)
    assert user.content_tier_preference == "adult"
    assert user.adult_confirmed_at == TODAY_UTC and user.adult_rules_version == SAFE_RULES_VERSION
    assert user.adult_confirm_method == "self_declared"


def test_the_old_age_tick_is_not_needed_and_is_ignored_whatever_its_value(app):
    for n, extra in enumerate(({"confirm_adult": True}, {"confirm_adult": False}, {"confirm_adult": "no"}), start=1):
        user_id = seed(app, n, age=30)
        assert state(put(client_for(app, user_id), "adult", confirm_rules=True, **extra))["effective"] == "adult"


def test_the_state_before_agreeing_says_not_confirmed(app):
    result = state(client_for(app, seed(app, age=30)).get(URL))
    assert result["confirmed"] is False and result["effective"] == "safe"


@pytest.mark.parametrize("extra", [{}, {"confirm_rules": False}, {"confirm_rules": "true"}, {"confirm_rules": 1}, {"confirm_adult": True}])
def test_a_missing_agreement_is_a_400_naming_confirm_rules_and_changes_nothing(app, extra):
    user_id = seed(app, age=30)
    response = put(client_for(app, user_id), "adult", **extra)
    assert response.status_code == 400 and set(error(response)["fields"]) == {"confirm_rules"}
    user = row(app, user_id)
    assert user.content_tier_preference == "safe"
    assert (user.adult_confirmed_at, user.adult_rules_version, user.adult_confirm_method) == (None, None, None)


def test_a_player_who_agreed_to_the_current_rules_can_switch_again_without_ticking(app):
    user_id = seed(app, age=30, **CONFIRMED)
    client = client_for(app, user_id)
    assert state(put(client, "adult"))["effective"] == "adult"
    assert state(put(client, "safe"))["effective"] == "safe"
    assert state(put(client, "adult"))["effective"] == "adult"


def test_going_back_to_the_safe_mode_keeps_the_agreement_record(app):
    user_id = seed(app, age=30)
    client = client_for(app, user_id)
    put(client, "adult", confirm_rules=True)
    before = row(app, user_id)
    assert state(put(client, "safe"))["confirmed"] is True
    after = row(app, user_id)
    assert (after.adult_confirmed_at, after.adult_rules_version, after.adult_confirm_method) == (
        before.adult_confirmed_at, before.adult_rules_version, before.adult_confirm_method,
    )
    assert after.content_tier_preference == "safe"


def test_a_new_rules_version_invalidates_the_old_agreement(app):
    user_id = seed(app, age=30, content_tier_preference="adult", **STALE)
    client = client_for(app, user_id)
    current = state(client.get(URL))
    assert current == {"preference": "adult", "effective": "safe", "confirmed": False, "forced": False}      # 生效退為 safe，沒有錯誤
    assert put(client, "adult").status_code == 400                                                         # 要重新勾選
    renewed = state(put(client, "adult", confirm_rules=True))
    assert (renewed["effective"], renewed["confirmed"]) == ("adult", True)
    assert row(app, user_id).adult_rules_version == SAFE_RULES_VERSION


def test_a_legacy_standard_preference_reads_as_adult_but_is_safe_until_the_player_agrees(app):
    user_id = seed(app, age=30, content_tier_preference="standard")
    client = client_for(app, user_id)
    assert state(client.get(URL)) == {"preference": "adult", "effective": "safe", "confirmed": False, "forced": False}
    assert row(app, user_id).content_tier_preference == "standard"             # 不改寫既有資料
    assert state(put(client, "adult", confirm_rules=True))["effective"] == "adult"
    assert row(app, user_id).content_tier_preference == "adult"                # 之後寫入的值只會是 safe 或 adult


def test_every_account_is_treated_alike_whatever_the_role_or_tier(app):
    for n, overrides in enumerate(({"role": ROLE_ADMIN}, {"member_tier": "vip"}, {"member_tier": "free"}), start=1):
        user_id = seed(app, n, age=30, **overrides)
        assert state(put(client_for(app, user_id), "adult", confirm_rules=True))["effective"] == "adult"


# ---- 緊急煞車 --------------------------------------------------------------------------

def test_with_the_brake_on_the_state_is_safe_and_forced_and_keeps_the_saved_preference(forced_app):
    user_id = seed(forced_app, age=30, content_tier_preference="adult", **CONFIRMED)
    result = state(client_for(forced_app, user_id).get(URL))
    assert result == {"preference": "adult", "effective": "safe", "confirmed": True, "forced": True}
    assert row(forced_app, user_id).content_tier_preference == "adult"


def test_with_the_brake_on_turning_the_safe_mode_off_is_a_403_and_changes_nothing(forced_app, log_lines):
    user_id = seed(forced_app, age=30)
    for extra in ({}, {"confirm_rules": True}):
        response = put(client_for(forced_app, user_id), "adult", **extra)
        assert response.status_code == 403 and error(response)["code"] == "safe_mode_forced"
    user = row(forced_app, user_id)
    assert user.content_tier_preference == "safe" and user.adult_confirmed_at is None            # 偏好不變、也沒有留下同意紀錄
    assert mode_events(log_lines) == []


def test_with_the_brake_on_going_back_to_the_safe_mode_still_succeeds(forced_app):
    user_id = seed(forced_app, age=30, content_tier_preference="adult", **CONFIRMED)
    result = state(put(client_for(forced_app, user_id), "safe"))
    assert result == {"preference": "safe", "effective": "safe", "confirmed": True, "forced": True}
    assert row(forced_app, user_id).content_tier_preference == "safe"


def test_with_the_brake_on_an_invalid_mode_is_still_a_400_not_a_403(forced_app):
    assert put(client_for(forced_app, seed(forced_app, age=30)), "standard").status_code == 400


def test_lifting_the_brake_restores_the_saved_preference_without_any_change_to_the_data(app, forced_app):
    # 兩個應用程式各自有資料庫，所以以同樣的玩家資料比較：重新啟動後（煞車解除）偏好為 adult 且已同意者生效模式恢復為 adult
    for application, expected in ((forced_app, "safe"), (app, "adult")):
        user_id = seed(application, age=30, content_tier_preference="adult", **CONFIRMED)
        assert state(client_for(application, user_id).get(URL))["effective"] == expected


# ---- 事件與日誌 -------------------------------------------------------------------------

def test_a_successful_switch_logs_one_event_with_only_the_name_and_the_user_id(app, log_lines):
    user_id = seed(app, age=30)
    put(client_for(app, user_id), "adult", confirm_rules=True)
    events = [line for line in log_lines() if str(line.get("event", "")).startswith("account.content_mode.")]
    assert len(events) == 1 and events[0]["event"] == "account.content_mode.adult" and events[0]["user_id"] == user_id
    raw = log_lines.raw()
    for text in ("confirm_adult", "confirm_rules", "self_declared", SAFE_RULES_VERSION, "已滿 18", "安全規範"):
        assert text not in raw


@pytest.mark.parametrize("mode", ["safe", "adult"])
def test_the_event_name_expresses_the_new_mode(app, log_lines, mode):
    put(client_for(app, seed(app, age=30, **CONFIRMED)), mode)
    assert mode_events(log_lines) == [f"account.content_mode.{mode}"]


def test_a_refused_switch_logs_no_content_mode_event(app, log_lines):
    adult = client_for(app, seed(app, 2, age=30))
    put(adult, "mature")
    put(adult, "standard")
    put(adult, "adult", confirm_adult=True)
    put(adult, "adult")
    assert mode_events(log_lines) == []


# ---- 只能讀寫自己的 ---------------------------------------------------------------------

def test_a_player_can_only_read_and_write_their_own_mode(app):
    alice_id, bob_id = seed(app, 1, age=30), seed(app, 2, age=30)
    alice, bob = client_for(app, alice_id), client_for(app, bob_id)
    put(alice, "adult", confirm_rules=True)
    assert state(alice.get(URL))["preference"] == "adult" and state(bob.get(URL))["preference"] == "safe"
    assert (row(app, alice_id).content_tier_preference, row(app, bob_id).content_tier_preference) == ("adult", "safe")
    assert row(app, bob_id).adult_confirmed_at is None
    # 沒有任何路徑或參數能指定別人的帳號
    assert alice.put(URL + "/2", json={"mode": "safe"}).status_code in (404, 405)
    response = alice.put(URL, json={"mode": "safe", "user_id": bob_id, "id": bob_id, "public_id": "x"})
    assert response.status_code == 200
    assert row(app, bob_id).content_tier_preference == "safe"                    # Bob 沒有被改動
    assert row(app, alice_id).content_tier_preference == "safe"                  # 被改動的是呼叫者自己
