"""email-token-flows 規格：管理員重設密碼的 API，端到端（含「沒有寄信設定整組 404」「提交後才寄」「偽造主機標頭」）。

玩家只用 Google 登入：信箱驗證、重寄驗證信、玩家的忘記密碼與註冊端點都已移除（回 404）。管理員在密碼驗證步驟按
「忘記密碼」的完整流程另見 test_admin_two_step_login.py。
"""
import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base, User
from app.security.admin_step import ADMIN_STEP_COOKIE
from app.services.google_oidc import GoogleProfile
from tests.support.auth import GOOD_PASSWORD, START, FakeClock, add_user, sign_in_as
from tests.support.config_env import AUTH_ENV
from tests.support.email_flow import FakeSender
from tests.support.google import SUB, FakeProvider, google_sign_in

BASE = "https://cs.example.com"
NEW_PASSWORD = "a-brand-new-password"
ADMIN_EMAIL = "user1@example.com"
REMOVED = (
    ("/api/auth/register", {"email": "a@example.com", "password": GOOD_PASSWORD, "display_name": "x", "accept_terms": True}),
    ("/api/auth/verify-email", {"token": "x"}),
    ("/api/auth/resend-verification", {}),
    ("/api/auth/forgot-password", {"email": "a@example.com"}),
)


def make_app(*, mail=True, sender=None, clock=None, provider=None):
    environ = {"APP_ENV": "testing"}
    if mail:
        environ.update({"ALERT_EMAIL_TO": "ops@example.com", **AUTH_ENV, "BASE_URL": BASE})
    app = create_app(load_config(environ=environ), mail_sender=sender, clock=clock, identity_provider=provider)
    Base.metadata.create_all(app.extensions["db_engine"])
    return app


@pytest.fixture()
def sender():
    return FakeSender()


@pytest.fixture()
def clock():
    return FakeClock()


@pytest.fixture()
def provider():
    return FakeProvider()


@pytest.fixture()
def app(sender, clock, provider):
    application = make_app(sender=sender, clock=clock, provider=provider)
    with Session(application.extensions["db_engine"]) as s:
        add_user(s, 1, role="admin", email_verified_at=START)
        add_user(s, 2, email_verified_at=START)
    provider.profile = GoogleProfile(SUB, ADMIN_EMAIL, True, "管理員")
    return application


def db(app) -> Session:
    return Session(app.extensions["db_engine"])


def request_reset(app, client):
    """通過 Google 之後在密碼驗證步驟按「忘記密碼」。"""
    google_sign_in(app, client)
    return client.post("/api/auth/admin-password/forgot", json={})


# =============================================================================
# 已移除的端點：有寄信設定也一樣 404，什麼都不寄、不建立
# =============================================================================

@pytest.mark.parametrize(("path", "body"), REMOVED)
def test_the_removed_player_endpoints_are_404_even_with_mail_settings_and_send_nothing(app, sender, path, body):
    with db(app) as s:
        before = s.query(User).count()
    assert app.test_client().post(path, json=body).status_code == 404
    with db(app) as s:
        assert s.query(User).count() == before
    assert sender.sent == []


# =============================================================================
# 沒有寄信設定：重設端點整組不存在
# =============================================================================

def test_without_mail_settings_the_reset_endpoint_and_the_admin_forgot_button_are_404(provider):
    app = make_app(mail=False, provider=provider)
    with db(app) as s:
        add_user(s, 1, role="admin", email_verified_at=START)
    provider.profile = GoogleProfile(SUB, ADMIN_EMAIL, True, "管理員")
    client = app.test_client()
    assert client.post("/api/auth/reset-password", json={"token": "x", "new_password": NEW_PASSWORD}).status_code == 404
    google_sign_in(app, client)
    assert client.post("/api/auth/admin-password/forgot", json={}).status_code == 404
    assert app.extensions["mail_sender"] is None


def test_with_mail_settings_a_bad_token_is_the_generic_400(app):
    response = app.test_client().post("/api/auth/reset-password", json={"token": "no-such-token", "new_password": NEW_PASSWORD})
    assert response.status_code == 400 and response.get_json()["error"]["code"] == "invalid_token"


# =============================================================================
# 管理員重設流程
# =============================================================================

def test_the_reset_flow_end_to_end(app, sender):
    client = app.test_client()
    assert request_reset(app, client).status_code == 200
    assert sender.sent[0]["body"].count(f"{BASE}/reset-password#token=") == 1

    weak = client.post("/api/auth/reset-password", json={"token": sender.last_token(), "new_password": "short"})
    assert weak.status_code == 400 and "new_password" in weak.get_json()["error"]["fields"]

    done = client.post("/api/auth/reset-password", json={"token": sender.last_token(), "new_password": NEW_PASSWORD})
    assert done.status_code == 200 and done.get_json()["message"] == "密碼已重設，請使用新密碼登入。"
    assert "已經變更" in sender.sent[-1]["body"]                                          # 通知信
    again = client.post("/api/auth/reset-password", json={"token": sender.tokens()[0], "new_password": "yet-another-password-1"})
    assert again.status_code == 400 and again.get_json()["error"]["code"] == "invalid_token"


def test_changing_the_password_sends_a_notice_when_mail_is_configured(app, sender):
    client = app.test_client()
    with db(app) as s:
        admin_id = s.query(User).filter_by(role="admin").one().id
    sign_in_as(client, admin_id)
    client.post("/api/auth/change-password", json={"current_password": GOOD_PASSWORD, "new_password": NEW_PASSWORD})
    assert len(sender.sent) == 1 and "已經變更" in sender.sent[0]["body"]


def test_changing_the_password_sends_nothing_without_mail_settings(provider):
    app = make_app(mail=False, provider=provider)
    with db(app) as s:
        admin_id = add_user(s, 1, role="admin", email_verified_at=START).id
    client = app.test_client()
    sign_in_as(client, admin_id)
    assert client.post("/api/auth/change-password", json={"current_password": GOOD_PASSWORD, "new_password": NEW_PASSWORD}).status_code == 200


# =============================================================================
# 連結來源與日誌
# =============================================================================

def test_a_forged_host_header_cannot_redirect_the_reset_link(app, sender):
    client = app.test_client()
    google_sign_in(app, client)
    step, csrf = client.get_cookie(ADMIN_STEP_COOKIE).value, client.csrf_token()
    # 讓請求通過 CSRF 與中間狀態（偽造的主機使來源檢查把 evil.example 視為同源），這樣才真的會寄信
    client.set_cookie("csrf", csrf, domain="evil.example")
    client.set_cookie(ADMIN_STEP_COOKIE, step, domain="evil.example")
    response = client.post(
        "/api/auth/admin-password/forgot", json={},
        headers={"Host": "evil.example", "Origin": "http://evil.example", "X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, "請求必須通過 CSRF 與中間狀態，測試才有意義"
    assert len(sender.sent) == 1
    body = sender.sent[0]["body"]
    assert "evil.example" not in body
    assert f"{BASE}/reset-password#token=" in body


def test_the_whole_flow_never_puts_addresses_or_tokens_in_the_logs(app, sender, log_lines):
    client = app.test_client()
    request_reset(app, client)
    reset_token = sender.last_token()
    client.post("/api/auth/reset-password", json={"token": reset_token, "new_password": NEW_PASSWORD})
    raw = log_lines.raw()
    for secret in (ADMIN_EMAIL, reset_token, NEW_PASSWORD, GOOD_PASSWORD):
        assert secret not in raw
