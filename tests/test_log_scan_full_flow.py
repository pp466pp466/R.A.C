"""service-observability 規格「完整流程後掃描日誌」：註冊、驗證、登入、忘記與重設密碼、Google 登入、登出、建立管理員，
全程收集日誌，確認找不到任何 email、密碼、權杖、授權碼、state、Cookie 值。"""
import json

import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base
from app.observability.json_logging import ALLOWED_FIELDS
from app.services.google_oidc import GoogleProfile
from tests.support.auth import FakeClock
from tests.support.config_env import AUTH_ENV
from tests.support.email_flow import FakeSender
from tests.support.google import FakeProvider

MEMBER_EMAIL = "scan.member@example.com"
GOOGLE_EMAIL = "scan.google@example.com"
ADMIN_EMAIL = "scan.admin@example.com"
GOOGLE_SUB = "sub-9876543210-scan"
PASSWORDS = {
    "first": "scan-first-password-A1",
    "wrong": "scan-WRONG-password-B2",
    "reset": "scan-reset-password-C3",
    "changed": "scan-changed-password-D4",
    "admin": "scan-admin-password-E5",
}
AUTH_CODE = "scan-google-authorization-code"
BEARER = "scan-bearer-credential-xyz"
BASE_KEYS = {"time", "level", "logger", "message"}


@pytest.fixture()
def world():
    sender, provider = FakeSender(), FakeProvider()
    provider.profile = GoogleProfile(GOOGLE_SUB, GOOGLE_EMAIL, True, "掃描用 Google 使用者")
    config = load_config(environ={"APP_ENV": "testing", "EMAIL_VERIFICATION_REQUIRED": "true", **AUTH_ENV, "BASE_URL": "http://localhost:5000"})
    app = create_app(config, mail_sender=sender, identity_provider=provider, clock=FakeClock())
    Base.metadata.create_all(app.extensions["db_engine"])
    return app, sender, provider


def test_negative_control_the_capture_would_notice_a_careless_log_line(log_lines):
    """若有人寫了 logger.info(f"user {email}")，這套收集方式必須抓得到；否則「找不到洩漏」就沒有意義。"""
    import logging

    logging.getLogger("app.careless").info(f"註冊了 {MEMBER_EMAIL}")
    assert MEMBER_EMAIL in log_lines.raw()


def test_a_complete_tour_of_every_flow_leaves_no_secret_in_the_logs(world, log_lines):
    app, sender, provider = world
    secrets_seen: set[str] = set()

    def remember_cookies(client):
        for name in ("session", "csrf", "oauth_tx"):
            cookie = client.get_cookie(name)
            if cookie:
                secrets_seen.add(cookie.value)

    api = "/api/auth"
    clock = app.extensions["clock"]
    client = app.test_client()
    extra = {"headers": {"Authorization": f"Bearer {BEARER}"}}

    # 註冊 → 驗證信 → 驗證
    assert client.post(f"{api}/register", json={"email": MEMBER_EMAIL, "password": PASSWORDS["first"], "display_name": "掃描使用者", "accept_terms": True}, **extra).status_code == 200
    verify_token = sender.last_token()
    assert client.post(f"{api}/verify-email", json={"token": verify_token}).status_code == 200

    # 登入失敗一次、成功一次，再變更密碼
    assert client.post(f"{api}/login", json={"email": MEMBER_EMAIL, "password": PASSWORDS["wrong"]}).status_code == 401
    assert client.post(f"{api}/login", json={"email": MEMBER_EMAIL, "password": PASSWORDS["first"]}).status_code == 200
    remember_cookies(client)
    assert client.post(f"{api}/change-password", json={"current_password": PASSWORDS["first"], "new_password": PASSWORDS["changed"]}).status_code == 200
    assert client.post(f"{api}/logout").status_code == 200

    # 忘記密碼 → 重設
    clock.advance(minutes=5)
    assert client.post(f"{api}/forgot-password", json={"email": MEMBER_EMAIL}).status_code == 200
    reset_token = sender.last_token()
    assert client.post(f"{api}/reset-password", json={"token": reset_token, "new_password": PASSWORDS["reset"]}).status_code == 200
    assert client.post(f"{api}/login", json={"email": MEMBER_EMAIL, "password": PASSWORDS["reset"]}).status_code == 200
    remember_cookies(client)
    assert client.post(f"{api}/logout-all").status_code == 200

    # Google 登入（新帳號）
    google = app.test_client()
    assert google.post(f"{api}/google/start", json={"accept_terms": True}).status_code == 200
    remember_cookies(google)
    from app.security.cookies import SignedCookies
    from app.security.oauth_cookie import OAUTH_SALT

    transaction = SignedCookies(app.extensions["app_config"], app.extensions["clock"]).loads(
        OAUTH_SALT, google.get_cookie("oauth_tx").value, max_age_seconds=600
    )
    secrets_seen.update({transaction["s"], transaction["n"], transaction["v"]})
    assert google.get(f"/auth/google/callback?code={AUTH_CODE}&state={transaction['s']}").status_code == 302
    remember_cookies(google)
    google.get(f"/auth/google/callback?code={AUTH_CODE}&state=forged-state-for-scan")          # 被拒絕的一次

    # 一般的頁面請求（帶著 Cookie 與授權標頭）
    client.get("/login?next=/account", **extra)

    # 建立管理員
    result = app.test_cli_runner().invoke(args=["create-admin", "--email", ADMIN_EMAIL, "--display-name", "掃描管理員", "--password-stdin"], input=f"{PASSWORDS['admin']}\n")
    assert result.exit_code == 0, result.output

    # ---- 掃描 ----
    raw = log_lines.raw()
    assert raw.strip(), "測試沒有產生任何日誌，掃描沒有意義"
    samples = {
        MEMBER_EMAIL, GOOGLE_EMAIL, ADMIN_EMAIL, GOOGLE_SUB, AUTH_CODE, BEARER, verify_token, reset_token,
        "forged-state-for-scan", *PASSWORDS.values(), *secrets_seen,
    }
    assert len(secrets_seen) >= 4
    for sample in samples:
        assert sample and sample not in raw, f"日誌洩漏了：{sample[:12]}…"

    # 每一行都是合法的 JSON，且只有允許的欄位
    lines = log_lines()
    assert len(lines) > 20
    for line in lines:
        assert set(line) <= BASE_KEYS | set(ALLOWED_FIELDS), f"出現不允許的欄位：{set(line) - BASE_KEYS - set(ALLOWED_FIELDS)}"
    events = {line["event"] for line in lines if "event" in line}
    assert {
        "auth.register", "auth.verification_sent", "auth.email_verified", "auth.login_failed.bad_password", "auth.login_success",
        "auth.password_changed", "auth.logout", "auth.password_reset_requested", "auth.password_reset", "auth.logout_all",
        "auth.google_started", "auth.google_registered", "auth.google_rejected.state", "auth.admin_created",
    } <= events
    # 請求日誌的路徑只有路由規則，不含查詢字串（Google 回呼的 code 與 state 在查詢字串裡）
    assert all("?" not in line.get("path", "") for line in lines)
    json.dumps(lines)                                                                       # 全部可序列化
