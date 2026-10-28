"""email-token-flows 規格：信箱驗證／重設密碼的 API，端到端（含「驗證關閉時整組 404」「提交後才寄」「偽造主機標頭」）。"""
import re

import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base, User
from app.models.user import STATUS_ACTIVE, STATUS_PENDING_VERIFICATION, STATUS_SUSPENDED
from tests.support.auth import GOOD_PASSWORD, FakeClock, add_user
from tests.support.client import raw_client
from tests.support.config_env import AUTH_ENV
from tests.support.email_flow import FakeSender

BASE = "https://cs.example.com"
NEW_PASSWORD = "a-brand-new-password"
ENDPOINTS = (
    ("POST", "/api/auth/verify-email", {"token": "x"}),
    ("POST", "/api/auth/resend-verification", {}),
    ("POST", "/api/auth/forgot-password", {"email": "a@example.com"}),
    ("POST", "/api/auth/reset-password", {"token": "x", "new_password": NEW_PASSWORD}),
)


def make_app(*, verification=True, sender=None, clock=None):
    environ = {"APP_ENV": "testing"}
    if verification:
        environ.update({"EMAIL_VERIFICATION_REQUIRED": "true", **AUTH_ENV, "BASE_URL": BASE})
    app = create_app(load_config(environ=environ), mail_sender=sender, clock=clock)
    Base.metadata.create_all(app.extensions["db_engine"])
    return app


@pytest.fixture()
def sender():
    return FakeSender()


@pytest.fixture()
def clock():
    return FakeClock()


@pytest.fixture()
def app(sender, clock):
    return make_app(sender=sender, clock=clock)


def db(app) -> Session:
    return Session(app.extensions["db_engine"])


def register(client, email="new@example.com", **overrides):
    body = {"email": email, "password": GOOD_PASSWORD, "display_name": "新朋友", "accept_terms": True}
    body.update(overrides)
    return client.post("/api/auth/register", json=body)


def login(client, email="new@example.com", password=GOOD_PASSWORD):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def status_of(app, email="new@example.com") -> str:
    with db(app) as s:
        return s.query(User).filter_by(email=email).one().status


# =============================================================================
# 驗證關閉：整組不存在
# =============================================================================

@pytest.mark.parametrize(("method", "path", "body"), ENDPOINTS)
def test_every_email_flow_endpoint_is_a_404_when_verification_is_off(method, path, body):
    sender = FakeSender()
    app = make_app(verification=False, sender=sender)
    client = app.test_client()
    response = client.open(path, method=method, json=body)
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "not_found"
    assert sender.sent == []


def test_with_verification_off_registration_sends_nothing_and_logs_no_token(log_lines):
    sender = FakeSender()
    app = make_app(verification=False, sender=sender)
    client = app.test_client()
    assert register(client).get_json()["verification_required"] is False
    assert login(client).status_code == 200
    assert sender.sent == []
    assert "token" not in log_lines.raw().lower().replace("token_version", "")


# =============================================================================
# 註冊 → 驗證信 → 驗證
# =============================================================================

def test_registering_sends_the_verification_mail_after_the_response_and_the_link_verifies(app, sender):
    client = app.test_client()
    response = register(client)
    assert response.status_code == 200
    assert response.get_json() == {
        "ok": True, "verification_required": True,
        "message": "若這個信箱可以註冊，我們已寄出驗證信，請於 24 小時內點擊信中的連結完成驗證。",
    }
    assert [m["to"] for m in sender.sent] == ["new@example.com"]
    link = re.search(r"https://\S+", sender.sent[0]["body"]).group(0)
    assert link.startswith(f"{BASE}/verify-email#token=")
    assert status_of(app, "new@example.com") == STATUS_PENDING_VERIFICATION

    verified = client.post("/api/auth/verify-email", json={"token": sender.last_token()})
    assert verified.status_code == 200 and verified.get_json()["ok"] is True
    assert status_of(app, "new@example.com") == STATUS_ACTIVE


def test_mail_is_sent_only_after_the_response_has_been_produced(app, sender):
    client = raw_client(app)
    client.get("/")                                                # 取得 CSRF cookie
    csrf = client.get_cookie("csrf").value
    response = client.post(
        "/api/auth/register",
        json={"email": "late@example.com", "password": GOOD_PASSWORD, "display_name": "x", "accept_terms": True},
        headers={"Origin": "http://localhost", "X-CSRF-Token": csrf},
        buffered=False,
    )
    assert response.status_code == 200 and sender.sent == []       # 回應已經產生，信還沒寄
    response.close()                                               # 回應送出之後
    assert [m["to"] for m in sender.sent] == ["late@example.com"]


def test_a_mail_server_failure_does_not_change_the_registration_result(app, sender, log_lines):
    sender.fail = True
    response = register(app.test_client())
    assert response.status_code == 200 and response.get_json()["ok"] is True
    assert status_of(app) == STATUS_PENDING_VERIFICATION
    assert [line["event"] for line in log_lines() if line.get("event") == "mail.send_failed"] == ["mail.send_failed"]
    assert "new@example.com" not in log_lines.raw() and "s3cret-should-never-be-logged" not in log_lines.raw()


def test_a_failed_registration_sends_no_mail(app, sender):
    assert register(app.test_client(), password="short").status_code == 400
    assert sender.sent == []


def test_registering_the_same_pending_address_twice_quickly_sends_one_mail_and_answers_identically(app, sender):
    first = register(app.test_client())
    second = register(app.test_client())
    assert (first.status_code, first.get_data()) == (second.status_code, second.get_data())
    assert len(sender.sent) == 1                                   # 60 秒內不重複寄


def test_registering_a_verified_address_sends_no_mail(app, sender):
    with db(app) as s:
        add_user(s, 1, email_verified_at=app.extensions["clock"]())
    register(app.test_client(), email="user1@example.com")
    assert sender.sent == []


def test_invalid_verification_tokens_get_the_generic_400(app):
    client = app.test_client()
    for body in ({"token": "nope"}, {"token": ""}, {}, {"token": 5}):
        response = client.post("/api/auth/verify-email", json=body)
        assert response.status_code == 400 and response.get_json()["error"]["code"] == "invalid_token"


def test_verifying_logs_out_the_old_session_and_a_fresh_login_works(app, sender):
    client = app.test_client()
    register(client)
    assert login(client).status_code == 200                        # 待驗證帳號可登入
    assert client.get("/api/auth/me").get_json()["user"]["email_verified"] is False
    client.post("/api/auth/verify-email", json={"token": sender.last_token()})
    assert client.get("/api/auth/me").status_code == 401           # token_version 已加 1
    assert login(client).get_json()["user"]["email_verified"] is True


# =============================================================================
# 重寄
# =============================================================================

def test_resend_needs_a_login(app, sender):
    assert app.test_client().post("/api/auth/resend-verification").status_code == 401
    assert sender.sent == []


def test_resend_then_rate_limit_then_allowed_again(app, sender, clock):
    client = app.test_client()
    register(client)
    login(client)
    assert client.post("/api/auth/resend-verification").status_code == 429       # 剛註冊時已經寄過一封
    clock.advance(seconds=61)
    response = client.post("/api/auth/resend-verification")
    assert response.status_code == 200 and len(sender.sent) == 2
    assert client.post("/api/auth/resend-verification").status_code == 429
    assert response.get_json()["message"] == "驗證信已重新寄出，請檢查您的信箱。"


def test_resend_for_a_verified_member_is_a_400(app, sender):
    client = app.test_client()
    register(client)
    client.post("/api/auth/verify-email", json={"token": sender.last_token()})
    login(client)
    response = client.post("/api/auth/resend-verification")
    assert response.status_code == 400 and response.get_json()["error"]["code"] == "already_verified"


# =============================================================================
# 忘記密碼與重設密碼
# =============================================================================

def test_forgot_password_answers_identically_for_every_kind_of_address(app, sender, clock):
    with db(app) as s:
        add_user(s, 1)
        add_user(s, 2, status=STATUS_SUSPENDED)
    probe = app.test_client()
    unknown = probe.post("/api/auth/forgot-password", json={"email": "nobody@example.com"})
    known = probe.post("/api/auth/forgot-password", json={"email": "user1@example.com"})
    limited = probe.post("/api/auth/forgot-password", json={"email": "user1@example.com"})       # 60 秒內第二次
    suspended = probe.post("/api/auth/forgot-password", json={"email": "user2@example.com"})
    assert {r.status_code for r in (unknown, known, limited, suspended)} == {200}
    assert len({r.get_data() for r in (unknown, known, limited, suspended)}) == 1
    assert [m["to"] for m in sender.sent] == ["user1@example.com"]


def test_forgot_password_rejects_only_malformed_addresses(app):
    response = app.test_client().post("/api/auth/forgot-password", json={"email": "not an address"})
    assert response.status_code == 400 and "email" in response.get_json()["error"]["fields"]


def test_the_reset_flow_end_to_end(app, sender):
    with db(app) as s:
        add_user(s, 1)
    client = app.test_client()
    login(client, "user1@example.com")
    client.post("/api/auth/forgot-password", json={"email": "user1@example.com"})
    assert sender.sent[0]["body"].count(f"{BASE}/reset-password#token=") == 1

    weak = client.post("/api/auth/reset-password", json={"token": sender.last_token(), "new_password": "short"})
    assert weak.status_code == 400 and "new_password" in weak.get_json()["error"]["fields"]

    done = client.post("/api/auth/reset-password", json={"token": sender.last_token(), "new_password": NEW_PASSWORD})
    assert done.status_code == 200 and done.get_json()["message"] == "密碼已重設，請使用新密碼登入。"
    assert client.get("/api/auth/me").status_code == 401                                  # 舊登入狀態已失效
    assert login(app.test_client(), "user1@example.com", GOOD_PASSWORD).status_code == 401
    assert login(app.test_client(), "user1@example.com", NEW_PASSWORD).status_code == 200
    assert "已經變更" in sender.sent[-1]["body"]                                          # 通知信
    again = client.post("/api/auth/reset-password", json={"token": sender.tokens()[0], "new_password": "yet-another-password-1"})
    assert again.status_code == 400 and again.get_json()["error"]["code"] == "invalid_token"


def test_changing_the_password_sends_a_notice_when_verification_is_on(app, sender):
    with db(app) as s:
        add_user(s, 1, email_verified_at=app.extensions["clock"]())
    client = app.test_client()
    login(client, "user1@example.com")
    client.post("/api/auth/change-password", json={"current_password": GOOD_PASSWORD, "new_password": NEW_PASSWORD})
    assert len(sender.sent) == 1 and "已經變更" in sender.sent[0]["body"]


# =============================================================================
# 跨流程的攻擊情境（7.4）
# =============================================================================

def test_the_pre_hijack_scenario_over_http_heals_when_the_real_owner_resets_the_password(app, sender, clock):
    """攻擊者先用受害者的信箱註冊並設定自己的密碼；受害者之後註冊（原密碼不被覆蓋）；受害者改走忘記密碼。"""
    attacker, victim = app.test_client(), app.test_client()
    address = "victim@example.com"
    register(attacker, address, password="attackers-chosen-pw", display_name="攻擊者")
    assert login(attacker, address, "attackers-chosen-pw").status_code == 200          # 待驗證帳號可登入
    assert attacker.get("/api/auth/me").status_code == 200

    clock.advance(minutes=5)
    assert register(victim, address, password="victims-real-password", display_name="受害者").status_code == 200
    assert login(victim, address, "victims-real-password").status_code == 401           # 密碼沒被覆蓋：受害者登入失敗
    with db(app) as s:
        assert s.query(User).one().display_name == "攻擊者"                              # 帳號資料也沒被覆蓋

    clock.advance(minutes=5)
    victim.post("/api/auth/forgot-password", json={"email": address})
    done = victim.post("/api/auth/reset-password", json={"token": sender.last_token(), "new_password": "victims-real-password"})
    assert done.status_code == 200

    assert attacker.get("/api/auth/me").status_code == 401                              # 攻擊者的登入狀態失效
    assert login(app.test_client(), address, "attackers-chosen-pw").status_code == 401  # 攻擊者的密碼失效
    healed = login(app.test_client(), address, "victims-real-password")
    assert healed.status_code == 200
    assert healed.get_json()["user"]["status"] == "active" and healed.get_json()["user"]["email_verified"] is True


# =============================================================================
# 連結來源與日誌
# =============================================================================

def test_a_forged_host_header_cannot_redirect_the_reset_link(app, sender):
    with db(app) as s:
        add_user(s, 1)
    client = app.test_client()
    csrf = client.csrf_token()
    # 讓請求通過 CSRF（偽造的主機會使來源檢查把 evil.example 視為同源），這樣才真的會寄信
    client.set_cookie("csrf", csrf, domain="evil.example")
    response = client.post(
        "/api/auth/forgot-password", json={"email": "user1@example.com"},
        headers={"Host": "evil.example", "Origin": "http://evil.example", "X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, "請求必須通過 CSRF，測試才有意義"
    assert len(sender.sent) == 1
    body = sender.sent[0]["body"]
    assert "evil.example" not in body
    assert f"{BASE}/reset-password#token=" in body


def test_the_whole_flow_never_puts_addresses_or_tokens_in_the_logs(app, sender, log_lines):
    client = app.test_client()
    register(client, email="trace.me@example.com")
    token = sender.last_token()
    client.post("/api/auth/verify-email", json={"token": token})
    client.post("/api/auth/forgot-password", json={"email": "trace.me@example.com"})
    reset_token = sender.last_token()
    client.post("/api/auth/reset-password", json={"token": reset_token, "new_password": NEW_PASSWORD})
    raw = log_lines.raw()
    for secret in ("trace.me@example.com", token, reset_token, NEW_PASSWORD, GOOD_PASSWORD):
        assert secret not in raw
