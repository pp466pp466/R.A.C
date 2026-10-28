"""google-sign-in 規格：發起與回呼端點（授權碼流程的防護、選用功能、結果導向）。以假的身分供應商端到端測試。"""
import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base, User, UserIdentity
from app.models.user import STATUS_SUSPENDED
from app.security.cookies import SignedCookies
from app.security.oauth_cookie import OAUTH_SALT
from app.services.google_oidc import GoogleAuthError, GoogleProfile
from tests.support.auth import FakeClock, add_user
from tests.support.client import raw_client
from tests.support.google import SUB, FakeProvider

FIXED_CODES = {"google_state", "google_unavailable", "google_rejected", "terms_required", "age_required", "account_disabled"}


@pytest.fixture()
def clock():
    return FakeClock()


@pytest.fixture()
def provider():
    return FakeProvider()


@pytest.fixture()
def app(provider, clock):
    application = create_app(load_config(environ={"APP_ENV": "testing"}), identity_provider=provider, clock=clock)
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


def db(app) -> Session:
    return Session(app.extensions["db_engine"])


def start(client, **body):
    return client.post("/api/auth/google/start", json={"accept_terms": True, "confirm_adult": True, **body})


def transaction_of(app, client) -> dict:
    cookie = client.get_cookie("oauth_tx")
    assert cookie is not None, "沒有 oauth_tx cookie"
    payload = SignedCookies(app.extensions["app_config"], app.extensions["clock"]).loads(OAUTH_SALT, cookie.value, max_age_seconds=600)
    assert payload is not None
    return payload


def callback(client, state, **extra):
    query = "&".join(f"{k}={v}" for k, v in {"code": "code-from-google", "state": state, **extra}.items() if v is not None)
    return client.get(f"/auth/google/callback?{query}")


def error_of(response) -> str:
    assert response.status_code == 302
    location = response.headers["Location"]
    assert location.startswith("/login?error="), location
    return location.split("error=", 1)[1]


def run_flow(app, client, **start_body):
    start(client, **start_body)
    return callback(client, transaction_of(app, client)["s"])


# =============================================================================
# 發起
# =============================================================================

def test_start_returns_the_authorization_url_and_binds_the_transaction_to_this_browser(app, provider):
    client = app.test_client()
    response = start(client, next="/account")
    assert response.status_code == 200
    body = response.get_json()
    assert set(body) == {"authorization_url"} and body["authorization_url"].startswith("https://accounts.example/auth?")
    tx = transaction_of(app, client)
    assert set(tx) == {"s", "n", "v", "t", "a", "x"} and tx["t"] is True and tx["a"] is True and tx["x"] == "/account"
    assert f"state={tx['s']}" in body["authorization_url"]
    assert tx["v"] not in response.get_data(as_text=True)                                  # PKCE 驗證碼不回傳給前端


def test_the_transaction_cookie_is_httponly_lax_and_expires_in_ten_minutes(app):
    header = next(h for h in start(app.test_client()).headers.getlist("Set-Cookie") if h.startswith("oauth_tx="))
    assert "HttpOnly" in header and "SameSite=Lax" in header and "Path=/" in header and "Max-Age=600" in header
    assert "Domain" not in header


def test_start_needs_csrf_and_a_json_object(app):
    raw = raw_client(app)
    assert raw.post("/api/auth/google/start", json={"accept_terms": True}, headers={"Origin": "http://localhost"}).status_code == 403
    assert app.test_client().post("/api/auth/google/start", data="x=1").status_code == 415


@pytest.mark.parametrize("bad_next", ["https://evil.example/", "//evil.example", "javascript:alert(1)", None, 5])
def test_an_unsafe_next_is_replaced_by_the_default_when_the_flow_starts(app, bad_next):
    client = app.test_client()
    start(client, next=bad_next)
    assert transaction_of(app, client)["x"] == "/account"


# =============================================================================
# 回呼：成功
# =============================================================================

def test_a_successful_callback_logs_in_redirects_and_deletes_the_transaction(app, provider):
    client = app.test_client()
    start(client, next="/account?tab=security")
    csrf_before = client.get_cookie("csrf").value
    response = callback(client, transaction_of(app, client)["s"])
    assert response.status_code == 302 and response.headers["Location"] == "/account?tab=security"
    me = client.get("/api/auth/me").get_json()["user"]
    assert me["email"] == "person@example.com" and me["google_linked"] is True and me["has_password"] is False
    assert me["age_confirmed"] is True                                                      # 首次以 Google 註冊時已確認
    assert client.get_cookie("oauth_tx") is None                                           # 交易 cookie 已刪除
    assert any(h.startswith("oauth_tx=;") for h in response.headers.getlist("Set-Cookie"))
    assert client.get_cookie("csrf").value != csrf_before                                   # 與密碼登入一樣換發 CSRF 權杖
    session_payload = app.session_interface.get_signing_serializer(app).loads(client.get_cookie("session").value)
    assert set(session_payload) == {"uid", "tv"}                                            # 登入狀態的建立方式與密碼登入相同
    assert len(provider.exchanges) == 1


def test_the_callback_url_cannot_be_used_twice(app):
    client = app.test_client()
    start(client)
    state = transaction_of(app, client)["s"]
    assert callback(client, state).headers["Location"] == "/account"
    assert error_of(callback(client, state)) == "google_state"                               # 綁定已刪除


def test_an_existing_verified_member_is_linked_and_logged_in(app, provider):
    with db(app) as s:
        add_user(s, 1, email_verified_at=app.extensions["clock"]())
    provider.profile = GoogleProfile(SUB, "user1@example.com", True, "x")
    client = app.test_client()
    start(client, accept_terms=False, confirm_adult=False)                                  # 已有帳號：不必勾選條款與年齡
    response = callback(client, transaction_of(app, client)["s"])
    assert response.headers["Location"] == "/account"
    me = client.get("/api/auth/me").get_json()["user"]
    assert me["has_password"] is True                                                       # 原有密碼仍在
    assert me["age_confirmed"] is False                                                     # 登入不會順便標記為已確認


# =============================================================================
# 回呼：防護
# =============================================================================

def test_a_state_that_differs_from_the_binding_is_refused(app, provider):
    client = app.test_client()
    start(client)
    assert error_of(callback(client, "an-attackers-forged-state")) == "google_state"
    assert client.get("/api/auth/me").status_code == 401 and provider.exchanges == []
    assert client.get_cookie("oauth_tx") is None                                            # 失敗也清除綁定


def test_opening_the_callback_without_having_started_is_refused(app, provider):
    assert error_of(callback(app.test_client(), "any-state")) == "google_state"
    assert provider.exchanges == []


def test_a_missing_state_or_code_is_refused(app):
    client = app.test_client()
    start(client)
    assert error_of(client.get("/auth/google/callback?code=abc")) == "google_state"
    start(client)
    assert error_of(client.get(f"/auth/google/callback?state={transaction_of(app, client)['s']}")) == "google_rejected"


def test_a_transaction_older_than_ten_minutes_is_refused(app, clock):
    client = app.test_client()
    start(client)
    state = transaction_of(app, client)["s"]
    clock.advance(minutes=9, seconds=59)
    assert transaction_of(app, client)["s"] == state                                         # 仍有效
    clock.advance(seconds=2)
    assert error_of(callback(client, state)) == "google_state"


def test_a_tampered_transaction_cookie_is_refused(app):
    client = app.test_client()
    start(client)
    state = transaction_of(app, client)["s"]
    cookie = client.get_cookie("oauth_tx").value
    client.set_cookie("oauth_tx", cookie[:-3] + "AAA")
    assert error_of(callback(client, state)) == "google_state"


def test_a_transaction_signed_for_another_purpose_is_refused(app):
    client = app.test_client()
    cookies = SignedCookies(app.extensions["app_config"], app.extensions["clock"])
    client.set_cookie("oauth_tx", cookies.dumps("csrf.v1", {"s": "x", "n": "y", "v": "z"}))
    assert error_of(callback(client, "x")) == "google_state"


# =============================================================================
# 回呼：各種拒絕（固定代碼，不夾帶外部文字）
# =============================================================================

@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        (lambda p: setattr(p, "error", GoogleAuthError("unavailable")), "google_unavailable"),
        (lambda p: setattr(p, "error", GoogleAuthError("token")), "google_rejected"),
    ],
)
def test_google_problems_redirect_to_the_login_page_with_a_fixed_code(app, provider, setup, expected):
    setup(provider)
    client = app.test_client()
    assert error_of(run_flow(app, client)) == expected
    assert client.get("/api/auth/me").status_code == 401


def test_a_new_member_who_did_not_accept_the_terms_is_sent_back_and_nothing_is_created(app):
    client = app.test_client()
    assert error_of(run_flow(app, client, accept_terms=False)) == "terms_required"
    with db(app) as s:
        assert s.query(User).count() == 0 and s.query(UserIdentity).count() == 0


def test_a_new_member_who_did_not_confirm_adulthood_is_sent_back_and_nothing_is_created(app):
    client = app.test_client()
    assert error_of(run_flow(app, client, confirm_adult=False)) == "age_required"
    assert client.get("/api/auth/me").status_code == 401
    with db(app) as s:
        assert s.query(User).count() == 0 and s.query(UserIdentity).count() == 0


def test_the_two_confirmations_are_independent_and_the_missing_one_is_named(app):
    assert error_of(run_flow(app, app.test_client(), confirm_adult=False)) == "age_required"     # 只缺年齡
    assert error_of(run_flow(app, app.test_client(), accept_terms=False)) == "terms_required"    # 只缺條款
    for body in ({"confirm_adult": "true"}, {"confirm_adult": 1}):                               # 不是真正的 True 不算
        assert error_of(run_flow(app, app.test_client(), **body)) == "age_required"
    with db(app) as s:
        assert s.query(User).count() == 0


def test_a_suspended_member_is_sent_back_and_not_logged_in(app, provider):
    with db(app) as s:
        add_user(s, 1, status=STATUS_SUSPENDED, email_verified_at=app.extensions["clock"]())
    provider.profile = GoogleProfile(SUB, "user1@example.com", True, "x")
    client = app.test_client()
    assert error_of(run_flow(app, client)) == "account_disabled"
    assert client.get("/api/auth/me").status_code == 401


def test_cancelling_at_google_is_a_rejection_and_attacker_text_never_reaches_the_url(app):
    client = app.test_client()
    start(client)
    state = transaction_of(app, client)["s"]
    response = client.get(f"/auth/google/callback?state={state}&error=<script>alert(1)</script>&error_description=%3Cb%3Eevil")
    assert error_of(response) == "google_rejected"
    assert "script" not in response.headers["Location"] and "evil" not in response.headers["Location"]


def test_every_error_code_is_from_the_fixed_set(app, provider):
    seen = set()
    for error in (GoogleAuthError("unavailable"), GoogleAuthError("token")):
        provider.error = error
        seen.add(error_of(run_flow(app, app.test_client())))
    provider.error = None
    seen.add(error_of(run_flow(app, app.test_client(), accept_terms=False)))
    seen.add(error_of(run_flow(app, app.test_client(), confirm_adult=False)))
    seen.add(error_of(callback(app.test_client(), "nope")))
    assert seen <= FIXED_CODES and len(seen) == 5


# =============================================================================
# 未啟用
# =============================================================================

def test_without_a_google_client_both_endpoints_are_404():
    app = create_app(load_config(environ={"APP_ENV": "testing"}))
    client = app.test_client()
    assert client.post("/api/auth/google/start", json={"accept_terms": True}).status_code == 404
    assert client.get("/auth/google/callback?code=a&state=b").status_code == 404


# =============================================================================
# 日誌
# =============================================================================

def test_the_flow_is_logged_without_state_subject_code_or_address(app, log_lines):
    client = app.test_client()
    start(client)
    state = transaction_of(app, client)["s"]
    callback(client, state, code="very-secret-authorization-code")
    callback(client, "forged-state-value")
    events = [line["event"] for line in log_lines() if "event" in line]
    assert events == [
        "auth.google_started", "auth.age_confirmed.google", "auth.google_registered", "auth.google_rejected.state",
    ]
    raw = log_lines.raw()
    for secret in (state, "very-secret-authorization-code", "forged-state-value", SUB, "person@example.com"):
        assert secret not in raw
