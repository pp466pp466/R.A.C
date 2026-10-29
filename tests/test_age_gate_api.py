"""age-confirmation 與 access-control 規格：年齡閘門的 HTTP 行為。

目前還沒有任何會呼叫 Gemini 的正式端點（客服是 C-3），所以用一個「只在測試應用程式中註冊」的端點代表它，
寫法與 C-3 的 AI 端點應有的寫法相同：先 `require_login()`，再 `require_chat_access(user)`，之後才做事。
這個端點也回報 `content_ceiling`，證明內容上限在 HTTP 層同樣由當下的年齡決定。
"""
import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base
from app.models.user import ROLE_ADMIN, ROLE_USER, STATUS_PENDING_VERIFICATION
from app.services.age_policy import AgePolicy
from tests.support.age import age_fields, birth_date_for_age
from tests.support.age_gate import ENDPOINT, add_ai_endpoint
from tests.support.auth import GOOD_PASSWORD, FakeClock, add_user
from tests.support.config_env import AUTH_ENV
from tests.support.email_flow import FakeSender


def make_app(*, age_policy: AgePolicy | None = None, environ: dict | None = None, **kwargs):
    config = load_config(environ={"APP_ENV": "testing", **(environ or {})})
    application = create_app(config, clock=FakeClock(), age_policy=age_policy, **kwargs)
    Base.metadata.create_all(application.extensions["db_engine"])
    add_ai_endpoint(application)
    return application


@pytest.fixture()
def app():
    return make_app()


def seed(app, n: int, **overrides):
    with Session(app.extensions["db_engine"]) as session:
        return add_user(session, n, **overrides).id


def signed_in(app, n: int):
    client = app.test_client()
    response = client.post("/api/auth/login", json={"email": f"user{n}@example.com", "password": GOOD_PASSWORD})
    assert response.status_code == 200, response.get_data(as_text=True)
    return client


def verify(client, age: int = 30):
    assert client.post("/api/auth/verify-age", json=age_fields(age)).status_code == 200


# =============================================================================
# 尚未完成：403 與固定代碼；補填後同一個端點放行
# =============================================================================

def test_a_member_without_age_data_gets_403_with_the_dedicated_code_and_the_work_never_runs(app):
    seed(app, 1)
    response = signed_in(app, 1).post(ENDPOINT, json={})
    assert response.status_code == 403
    error = response.get_json()["error"]
    assert error["code"] == "age_confirmation_required"
    assert "生日" in error["message"] and "帳號設定" in error["message"]
    assert app.work_done == []


def test_verifying_later_opens_the_same_endpoint_without_logging_in_again(app):
    seed(app, 1)
    client = signed_in(app, 1)
    assert client.post(ENDPOINT, json={}).status_code == 403
    verify(client, 30)
    response = client.post(ENDPOINT, json={})
    assert response.status_code == 200 and response.get_json()["ok"] is True
    assert len(app.work_done) == 1


def test_a_member_who_registered_with_a_birthday_passes_straight_away(app):
    client = app.test_client()
    body = {"email": "new@example.com", "password": GOOD_PASSWORD, "display_name": "新會員", "accept_terms": True, **age_fields(30)}
    assert client.post("/api/auth/register", json=body).status_code == 200
    assert client.post("/api/auth/login", json={"email": "new@example.com", "password": GOOD_PASSWORD}).status_code == 200
    assert client.post(ENDPOINT, json={}).status_code == 200


def test_being_signed_out_is_still_a_401_not_an_age_error(app):
    response = app.test_client().post(ENDPOINT, json={})
    assert response.status_code == 401 and response.get_json()["error"]["code"] == "unauthorized"


# =============================================================================
# 管理員與各會員等級都不豁免
# =============================================================================

@pytest.mark.parametrize(("role", "tier"), [(ROLE_USER, "free"), (ROLE_USER, "vip"), (ROLE_ADMIN, "free"), (ROLE_ADMIN, "vip")])
def test_neither_admins_nor_vip_members_skip_the_age_gate(app, role, tier):
    seed(app, 1, role=role, member_tier=tier)
    client = signed_in(app, 1)
    response = client.post(ENDPOINT, json={})
    assert response.status_code == 403 and response.get_json()["error"]["code"] == "age_confirmation_required"
    verify(client, 30)
    assert client.post(ENDPOINT, json={}).status_code == 200


def test_verifying_one_member_does_not_open_the_gate_for_another(app):
    seed(app, 1)
    seed(app, 2)
    verify(signed_in(app, 1), 30)
    assert signed_in(app, 2).post(ENDPOINT, json={}).status_code == 403
    assert app.work_done == []


# =============================================================================
# 13 至 17 歲可用但固定安全模式；成年者上限為 2
# =============================================================================

@pytest.mark.parametrize("age", [13, 15, 17])
def test_a_teen_can_use_the_endpoint_but_only_in_safe_mode(app, age):
    seed(app, 1, age=age)
    response = signed_in(app, 1).post(ENDPOINT, json={})
    assert response.status_code == 200
    assert response.get_json()["ceiling"] == {"sexual_content_level": 0, "safe_mode": True}


@pytest.mark.parametrize("age", [18, 30, 70])
def test_an_adult_reaches_level_2_and_never_3(app, age):
    seed(app, 1, age=age)
    assert signed_in(app, 1).post(ENDPOINT, json={}).get_json()["ceiling"] == {"sexual_content_level": 2, "safe_mode": False}


def test_an_admin_who_is_a_teen_is_still_in_safe_mode(app):
    seed(app, 1, age=15, role=ROLE_ADMIN)
    assert signed_in(app, 1).post(ENDPOINT, json={}).get_json()["ceiling"]["safe_mode"] is True


def test_a_teen_who_turns_18_is_released_without_logging_in_again():
    clock = FakeClock()
    config = load_config(environ={"APP_ENV": "testing"})
    application = create_app(config, clock=clock)
    Base.metadata.create_all(application.extensions["db_engine"])
    add_ai_endpoint(application)
    with Session(application.extensions["db_engine"]) as session:
        tomorrow_18 = birth_date_for_age(18).replace(day=birth_date_for_age(18).day + 1)
        add_user(session, 1, age=17, birth_date=tomorrow_18)
    client = signed_in(application, 1)
    assert client.post(ENDPOINT, json={}).get_json()["ceiling"]["safe_mode"] is True
    clock.advance(days=1)
    assert client.post(ENDPOINT, json={}).get_json()["ceiling"] == {"sexual_content_level": 2, "safe_mode": False}


# =============================================================================
# 年齡規則被提高：只改規則，其他不動
# =============================================================================

def test_raising_the_minimum_age_to_18_blocks_existing_teens_with_their_own_code_and_leaves_adults_alone():
    strict = make_app(age_policy=AgePolicy(min_register_age=18, min_use_age=18, version="test-18-only"))
    seed(strict, 1, age=15)
    adult_id = seed(strict, 2, age=40)
    teen = signed_in(strict, 1).post(ENDPOINT, json={})
    assert teen.status_code == 403 and teen.get_json()["error"]["code"] == "age_restricted"
    assert "帳號設定" not in teen.get_json()["error"]["message"]                  # 補填也沒有用，所以不引導
    assert signed_in(strict, 2).post(ENDPOINT, json={}).status_code == 200
    assert strict.work_done == [adult_id]                                        # 只有成年者的請求真的做了事
    refused = strict.test_client().post(
        "/api/auth/register",
        json={"email": "t@example.com", "password": GOOD_PASSWORD, "display_name": "x", "accept_terms": True, **age_fields(15)},
    )
    assert refused.status_code == 400 and refused.get_json()["error"]["code"] == "age_not_eligible"


# =============================================================================
# 日誌
# =============================================================================

@pytest.mark.parametrize(("age", "code"), [(None, "age_confirmation_required"), (12, "age_restricted")])
def test_a_denial_is_logged_with_the_internal_user_id_and_nothing_personal(app, log_lines, age, code):
    user_id = seed(app, 1, age=age)
    response = signed_in(app, 1).post(ENDPOINT, json={})
    assert response.get_json()["error"]["code"] == code
    denials = [line for line in log_lines() if line.get("event") == "auth.age_gate_denied"]
    assert len(denials) == 1 and denials[0]["user_id"] == user_id and denials[0]["level"] == "INFO"
    raw = log_lines.raw()
    assert "user1@example.com" not in raw and GOOD_PASSWORD not in raw
    assert birth_date_for_age(12).isoformat() not in raw and "birth" not in raw


def test_an_allowed_request_leaves_no_denial_in_the_log(app, log_lines):
    seed(app, 1, age=30)
    assert signed_in(app, 1).post(ENDPOINT, json={}).status_code == 200
    assert [line for line in log_lines() if line.get("event") == "auth.age_gate_denied"] == []


# =============================================================================
# 檢查順序：帳號與信箱條件先於年齡
# =============================================================================

def test_an_unverified_member_is_told_about_the_account_not_sent_to_fill_in_the_age(log_lines):
    """信箱驗證開啟時，尚未驗證的人補填年齡也沒有用，所以不能回 `age_confirmation_required`。"""
    app = make_app(environ={"EMAIL_VERIFICATION_REQUIRED": "true", **AUTH_ENV}, mail_sender=FakeSender())
    seed(app, 1, status=STATUS_PENDING_VERIFICATION)

    response = signed_in(app, 1).post(ENDPOINT, json={})

    assert response.status_code == 403 and response.get_json()["error"]["code"] == "forbidden"
    assert app.work_done == []
    assert [line for line in log_lines() if line.get("event") == "auth.age_gate_denied"] == []
