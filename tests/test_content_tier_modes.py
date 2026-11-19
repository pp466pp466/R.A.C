"""content-tier-modes 規格：從環境設定經 `create_app` 與組裝層一路到 HTTP 回應的緊急煞車（`SAFE_MODE_FORCED`）。

政策物件本身的生效模式（偏好 × 同意 × 劇本綁定 × 緊急煞車）見 `test_effective_mode.py`，API 與切換流程見 `test_content_mode_api.py`。
這裡只驗證「設定真的被接到政策物件」：預設未啟用、啟用時所有生效模式為 `safe` 且保存的偏好不被改寫、舊的 `ADULT_MODE_*` 不改變任何東西。
"""
from datetime import timedelta

from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base
from app.services.content_tier import SAFE_RULES_VERSION
from tests.support.age import TODAY_UTC
from tests.support.age_gate import ENDPOINT, add_ai_endpoint
from tests.support.auth import FakeClock, add_user, sign_in_as

FREE_MODEL = "vendor/narrator:free"
RPG_ENV = {"OPENROUTER_API_KEY": "sk-or-v1-fake-key-for-tests-0123456789", "OPENROUTER_MODEL": FREE_MODEL}
CONFIRMED = {"adult_confirmed_at": TODAY_UTC - timedelta(days=3), "adult_rules_version": SAFE_RULES_VERSION}
ADULT_PLAYER = {"age": 30, "content_tier_preference": "adult", **CONFIRMED}


def make_app(**environ):
    config = load_config(environ={"APP_ENV": "testing", **environ})
    application = create_app(config, clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    add_ai_endpoint(application)
    return application


def signed_in(app, n: int, **user_overrides):
    with Session(app.extensions["db_engine"]) as session:
        user_id = add_user(session, n, **user_overrides).id
    client = app.test_client()
    sign_in_as(client, user_id)                                     # 管理員不能只靠密碼登入，所以直接寫入登入狀態
    return client


def mode_for(app, n: int, **user_overrides) -> dict:
    """以登入後的玩家身分查 `GET /api/account/content-mode`（從環境設定一路到 HTTP 回應）。"""
    response = signed_in(app, n, **user_overrides).get("/api/account/content-mode")
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["content_mode"]


def test_by_default_the_brake_is_off_and_a_player_who_agreed_and_prefers_adult_has_the_adult_mode_through_the_whole_stack():
    app = make_app()
    assert mode_for(app, 1, **ADULT_PLAYER) == {"preference": "adult", "effective": "adult", "confirmed": True, "forced": False}


def test_with_the_brake_on_every_mode_is_safe_through_the_whole_stack_and_the_saved_preference_is_kept():
    app = make_app(SAFE_MODE_FORCED="true")
    assert mode_for(app, 1, **ADULT_PLAYER) == {"preference": "adult", "effective": "safe", "confirmed": True, "forced": True}


def test_the_endpoint_that_calls_the_model_sees_the_same_mode_state_as_the_content_mode_api():
    for environ, expected in (({}, "adult"), ({"SAFE_MODE_FORCED": "true"}, "safe")):
        app = make_app(**environ)
        response = signed_in(app, 1, **ADULT_PLAYER).post(ENDPOINT, json={})
        assert response.status_code == 200 and response.get_json()["content"]["effective"] == expected


def test_the_old_adult_mode_variables_change_nothing_whatever_their_value():
    for environ in ({"ADULT_MODE_ENABLED": "true", "ADULT_MODE_MODELS": FREE_MODEL}, {"ADULT_MODE_ENABLED": "false"},
                    {"ADULT_MODE_ENABLED": "maybe"}):
        app = make_app(**RPG_ENV, **environ)
        assert mode_for(app, 1, **ADULT_PLAYER)["effective"] == "adult"
        assert mode_for(app, 2, **ADULT_PLAYER)["forced"] is False


def test_the_policy_the_requests_use_is_built_from_the_config_not_from_a_constant():
    for environ, expected in (({}, False), ({"SAFE_MODE_FORCED": "true"}, True), ({"SAFE_MODE_FORCED": "false"}, False)):
        app = make_app(**environ)
        with app.app_context():
            from app.services.registry import get_access_policy

            assert get_access_policy().safe_mode_forced is expected
