"""age-confirmation 與 access-control 規格：年齡閘門的 HTTP 行為。

目前還沒有任何會呼叫 Gemini 的正式端點（客服是 C-3），所以用一個「只在測試應用程式中註冊」的端點代表它，
寫法與 C-3 的 AI 端點應有的寫法相同：先 `require_login()`，再 `require_chat_access(user)`，之後才做事。
"""
import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base
from app.models.user import ROLE_ADMIN, ROLE_USER, STATUS_PENDING_VERIFICATION
from tests.support.age_gate import ENDPOINT, add_ai_endpoint
from tests.support.auth import GOOD_PASSWORD, FakeClock, add_user
from tests.support.config_env import AUTH_ENV
from tests.support.email_flow import FakeSender


@pytest.fixture()
def app():
    application = create_app(load_config(environ={"APP_ENV": "testing"}))
    Base.metadata.create_all(application.extensions["db_engine"])
    add_ai_endpoint(application)
    return application


def seed(app, n: int, **overrides):
    with Session(app.extensions["db_engine"]) as session:
        return add_user(session, n, **overrides).id


def signed_in(app, n: int):
    client = app.test_client()
    response = client.post("/api/auth/login", json={"email": f"user{n}@example.com", "password": GOOD_PASSWORD})
    assert response.status_code == 200, response.get_data(as_text=True)
    return client


def confirm(client):
    assert client.post("/api/auth/confirm-adult", json={"confirm_adult": True}).status_code == 200


# =============================================================================
# 未確認：403 與固定代碼；補確認後同一個端點放行
# =============================================================================

def test_an_unconfirmed_member_gets_403_with_the_dedicated_code_and_the_work_never_runs(app):
    seed(app, 1)
    response = signed_in(app, 1).post(ENDPOINT, json={})
    assert response.status_code == 403
    error = response.get_json()["error"]
    assert error["code"] == "age_confirmation_required"
    assert "18" in error["message"] and "帳號設定" in error["message"]
    assert app.work_done == []


def test_confirming_later_opens_the_same_endpoint_without_logging_in_again(app):
    seed(app, 1)
    client = signed_in(app, 1)
    assert client.post(ENDPOINT, json={}).status_code == 403
    confirm(client)
    response = client.post(ENDPOINT, json={})
    assert response.status_code == 200 and response.get_json() == {"ok": True}
    assert len(app.work_done) == 1


def test_a_member_who_confirmed_at_registration_passes_straight_away(app):
    client = app.test_client()
    body = {"email": "new@example.com", "password": GOOD_PASSWORD, "display_name": "新會員", "accept_terms": True, "confirm_adult": True}
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
    confirm(client)
    assert client.post(ENDPOINT, json={}).status_code == 200


def test_confirming_for_one_member_does_not_open_the_gate_for_another(app):
    seed(app, 1)
    seed(app, 2)
    confirm(signed_in(app, 1))
    other = signed_in(app, 2)
    assert other.post(ENDPOINT, json={}).status_code == 403
    assert app.work_done == []


# =============================================================================
# 日誌
# =============================================================================

def test_a_denial_is_logged_with_the_internal_user_id_and_nothing_personal(app, log_lines):
    user_id = seed(app, 1)
    signed_in(app, 1).post(ENDPOINT, json={})
    denials = [line for line in log_lines() if line.get("event") == "auth.age_gate_denied"]
    assert len(denials) == 1
    assert denials[0]["user_id"] == user_id and denials[0]["level"] == "INFO"
    raw = log_lines.raw()
    assert "user1@example.com" not in raw and GOOD_PASSWORD not in raw


def test_an_allowed_request_leaves_no_denial_in_the_log(app, log_lines):
    seed(app, 1, age_confirmed_at=FakeClock()(), age_policy_version="v1")
    assert signed_in(app, 1).post(ENDPOINT, json={}).status_code == 200
    assert [line for line in log_lines() if line.get("event") == "auth.age_gate_denied"] == []


# =============================================================================
# 檢查順序：帳號與信箱條件先於年齡
# =============================================================================

def test_an_unverified_member_is_told_about_the_account_not_sent_to_confirm_the_age(log_lines):
    """信箱驗證開啟時，尚未驗證的人補確認年齡也沒有用，所以不能回 `age_confirmation_required`。"""
    config = load_config(environ={"APP_ENV": "testing", "EMAIL_VERIFICATION_REQUIRED": "true", **AUTH_ENV})
    app = create_app(config, mail_sender=FakeSender(), clock=FakeClock())
    Base.metadata.create_all(app.extensions["db_engine"])
    add_ai_endpoint(app)
    seed(app, 1, status=STATUS_PENDING_VERIFICATION)

    response = signed_in(app, 1).post(ENDPOINT, json={})

    assert response.status_code == 403 and response.get_json()["error"]["code"] == "forbidden"
    assert app.work_done == []
    assert [line for line in log_lines() if line.get("event") == "auth.age_gate_denied"] == []
