"""admin-two-step-login 規格的頁面部分：`/login/password` 的外殼與腳本的接線（存取規則與兩個步驟見 `test_admin_two_step_login.py`）。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base
from app.services.google_oidc import GoogleProfile
from tests.support.auth import START, FakeClock, add_user
from tests.support.email_flow import FakeSender
from tests.support.google import SUB, FakeProvider, google_sign_in


def page(with_mail: bool) -> str:
    provider = FakeProvider()
    provider.profile = GoogleProfile(SUB, "user1@example.com", True, "管理員")
    config = load_config(environ={"APP_ENV": "testing", "BASE_URL": "https://cs.example.com"})
    app = create_app(config, identity_provider=provider, clock=FakeClock(), mail_sender=FakeSender() if with_mail else None)
    Base.metadata.create_all(app.extensions["db_engine"])
    with Session(app.extensions["db_engine"]) as s:
        add_user(s, 1, role="admin", email_verified_at=START)
    client = app.test_client()
    google_sign_in(app, client)
    return client.get("/login/password").get_data(as_text=True)


def asset(kind: str, name: str) -> str:
    from app.config import PROJECT_ROOT

    base = PROJECT_ROOT / "app" / ("templates" if kind == "template" else "static/js")
    return (base / name).read_text(encoding="utf-8")


def test_the_page_has_one_labelled_password_field_and_nothing_else_to_fill_in():
    html = page(with_mail=False)
    assert 'for="password"' in html and 'type="password"' in html and 'autocomplete="current-password"' in html
    for forbidden in ('type="email"', 'type="date"', "<select", "<textarea", 'type="checkbox"'):
        assert forbidden not in html, forbidden
    assert "Google 登入已通過" in html and 'href="/login"' in html and "重新以 Google 登入" in html


def test_the_forgot_password_button_is_in_the_page_but_only_shown_when_the_server_says_it_is_available():
    html = page(with_mail=True)
    assert 'v-if="forgotAvailable"' in html and '@click="forgotPassword"' in html and "忘記密碼" in html


def test_the_submit_button_is_disabled_and_busy_while_sending():
    html = page(with_mail=False)
    assert ':disabled="form.state.submitting"' in html and ":aria-busy=" in html


def test_the_script_clears_the_password_before_the_request_and_keeps_nothing_in_the_browser():
    script = asset("script", "auth/login-password.js")
    submit = script.split("function submit()", 1)[1].split("function forgotPassword()", 1)[0]
    assert submit.index("form.values.password = ''") < submit.index("axios.post('/api/auth/admin-password'")     # 送出的當下立即清空
    assert "Auth.validateAdminPassword(form.values.password)" in script and "Auth.adminPasswordOutcome(info)" in script
    assert "Auth.safeDestination(response.data && response.data.next" in script
    assert "axios.post('/api/auth/admin-password/forgot'" in script and "Auth.adminForgotOutcome(info)" in script
    for storage in ("localStorage", "sessionStorage", "indexedDB", "document.cookie"):
        assert storage not in script, storage
    template = asset("template", "auth/login_password.html")
    assert template.index("auth-flows.js") < template.index("login-password.js")


def test_a_missing_or_expired_step_sends_the_admin_back_through_the_login_page_with_a_reason():
    assert "LOGIN_PATH + '?error=google_state'" in asset("script", "auth/auth-flows.js")
