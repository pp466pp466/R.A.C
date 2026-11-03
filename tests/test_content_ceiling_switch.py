"""age-confirmation 規格：內容上限在「等級 3 開關」開啟時才可能是 3，其餘情況與開關無關（openai-compatible-provider）。

兩層驗證：政策物件本身（年齡 × 開關 × 角色的組合），以及從環境設定經 `create_app` 與組裝層一路到 HTTP 回應。
"""
import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base
from app.models.user import ROLE_ADMIN, ROLE_USER
from app.services.access_policy import (
    LEVEL3_SEXUAL_CONTENT_LEVEL, MAX_ADULT_SEXUAL_CONTENT_LEVEL, AccessPolicy, ContentCeiling,
)
from tests.support.age import TODAY_UTC
from tests.support.age_gate import ENDPOINT, add_ai_endpoint
from tests.support.auth import GOOD_PASSWORD, FakeClock, add_user, sign_in_as
from tests.test_access_policy import member

FREE_MODEL = "vendor/narrator:free"
LEVEL3_ENV = {
    "OPENROUTER_API_KEY": "sk-or-v1-fake-key-for-tests-0123456789", "OPENROUTER_MODEL": FREE_MODEL,
    "SEXUAL_CONTENT_LEVEL3_ENABLED": "true", "SEXUAL_CONTENT_LEVEL3_MODELS": FREE_MODEL,
}


def policy(adult_ceiling: int | None = None) -> AccessPolicy:
    extra = {} if adult_ceiling is None else {"adult_ceiling": adult_ceiling}
    return AccessPolicy(email_verification_required=False, clock=lambda: TODAY_UTC, **extra)


# ---- 政策物件 ------------------------------------------------------------------------------------------

def test_the_default_is_unchanged_an_adult_is_capped_at_level_2():
    assert (MAX_ADULT_SEXUAL_CONTENT_LEVEL, LEVEL3_SEXUAL_CONTENT_LEVEL) == (2, 3)
    assert policy().content_ceiling(member(age=30)) == ContentCeiling(2, safe_mode=False)
    assert policy(2).content_ceiling(member(age=30)) == ContentCeiling(2, safe_mode=False)


@pytest.mark.parametrize("age", [18, 19, 30, 65])
@pytest.mark.parametrize("role", [ROLE_USER, ROLE_ADMIN])
def test_with_level_3_on_an_adult_may_reach_3_and_is_not_in_safe_mode(age, role):
    assert policy(3).content_ceiling(member(age=age, role=role)) == ContentCeiling(3, safe_mode=False)


@pytest.mark.parametrize("age", [13, 15, 17, None])
@pytest.mark.parametrize("role", [ROLE_USER, ROLE_ADMIN])
@pytest.mark.parametrize("ceiling", [2, 3])
def test_a_minor_or_a_member_without_an_age_check_is_in_safe_mode_whatever_the_switch(age, role, ceiling):
    assert policy(ceiling).content_ceiling(member(age=age, role=role)) == ContentCeiling(0, safe_mode=True)


def test_the_switch_does_not_make_the_age_boundary_any_softer():
    """剛好 17 歲與剛好 18 歲：開關開啟也一樣在 18 歲才解除。"""
    assert policy(3).content_ceiling(member(age=17)).sexual_content_level == 0
    assert policy(3).content_ceiling(member(age=18)).sexual_content_level == 3


@pytest.mark.parametrize("value", [0, 1, 4, 99, -1, None, True, "3"])
def test_only_2_and_3_are_accepted_as_an_adult_ceiling(value):
    with pytest.raises(ValueError):
        AccessPolicy(email_verification_required=False, adult_ceiling=value)


# ---- 從環境設定到 HTTP ---------------------------------------------------------------------------------

def make_app(**environ):
    config = load_config(environ={"APP_ENV": "testing", **environ})
    application = create_app(config, clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    add_ai_endpoint(application)
    return application


def ceiling_for(app, n: int, **user_overrides) -> dict:
    with Session(app.extensions["db_engine"]) as session:
        user_id = add_user(session, n, **user_overrides).id
    client = app.test_client()
    sign_in_as(client, user_id)                                     # 管理員不能只靠密碼登入，所以直接寫入登入狀態
    response = client.post(ENDPOINT, json={})
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["ceiling"]


def test_by_default_the_whole_stack_caps_an_adult_at_level_2():
    app = make_app()
    assert ceiling_for(app, 1, age=30) == {"sexual_content_level": 2, "safe_mode": False}


def test_a_configured_but_disabled_switch_changes_nothing():
    app = make_app(**{**LEVEL3_ENV, "SEXUAL_CONTENT_LEVEL3_ENABLED": "false"})
    assert ceiling_for(app, 1, age=30) == {"sexual_content_level": 2, "safe_mode": False}


def test_with_the_switch_on_the_whole_stack_lets_an_adult_reach_level_3_and_still_protects_minors():
    app = make_app(**LEVEL3_ENV)
    assert ceiling_for(app, 1, age=30) == {"sexual_content_level": 3, "safe_mode": False}
    assert ceiling_for(app, 2, age=17) == {"sexual_content_level": 0, "safe_mode": True}
    assert ceiling_for(app, 3, age=15, role=ROLE_ADMIN) == {"sexual_content_level": 0, "safe_mode": True}      # 管理員不豁免


def test_the_policy_the_requests_use_is_built_from_the_config_not_from_a_constant():
    for environ, expected in (({}, 2), (LEVEL3_ENV, 3)):
        app = make_app(**environ)
        with app.app_context():
            from app.services.registry import get_access_policy

            assert get_access_policy().content_ceiling(member(age=30)).sexual_content_level == expected
