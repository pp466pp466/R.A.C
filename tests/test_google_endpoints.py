"""google-sign-in 規格：發起與回呼端點（授權碼流程的防護、選用功能、結果導向）。以假的身分供應商端到端測試。"""
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base, LockStrike, User, UserIdentity
from app.models.user import STATUS_SUSPENDED
from app.security.cookies import SignedCookies
from app.security.oauth_cookie import OAUTH_SALT
from app.security.redirects import DEFAULT_AFTER_LOGIN
from app.services.google_oidc import GoogleAuthError, GoogleProfile
from tests.support.age import age_fields
from tests.support.auth import FakeClock, add_user
from tests.support.client import raw_client
from tests.support.google import SUB, FakeProvider

FIXED_CODES = {"google_state", "google_unavailable", "google_rejected", "terms_required", "account_disabled"}


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
    return client.post("/api/auth/google/start", json={"accept_terms": True, **body})


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
    assert set(tx) == {"s", "n", "v", "t", "x"} and tx["t"] is True and tx["x"] == "/account"      # 交易裡沒有年齡資料
    assert f"state={tx['s']}" in body["authorization_url"]
    assert tx["v"] not in response.get_data(as_text=True)                                  # PKCE 驗證碼不回傳給前端


def test_a_birthday_and_range_in_the_start_request_are_ignored_and_never_saved(app, log_lines):
    """年齡確認改在登入之後才填：發起不要求、不驗證，請求即使仍帶著這兩個欄位（甚至是未滿 13 歲的值）也一律忽略。"""
    client = app.test_client()
    for fields in (age_fields(30), age_fields(12), age_fields(30, age_range="13_17"), {"birth_date": "2026-02-30", "age_range": None}):
        response = start(client, **fields)
        assert response.status_code == 200 and set(response.get_json()) == {"authorization_url"}
        tx = transaction_of(app, client)
        assert not {"b", "r", "birth_date", "age_range"} & set(tx)
    assert age_fields(12)["birth_date"] not in log_lines.raw()
    with db(app) as s:
        assert s.query(User).count() == 0


def test_a_returning_member_signs_in_from_an_empty_request_without_being_asked_for_the_terms(app, provider):
    with db(app) as s:
        add_user(s, 1, email_verified_at=app.extensions["clock"]())
    provider.profile = GoogleProfile(SUB, "user1@example.com", True, "x")
    client = app.test_client()
    response = client.post("/api/auth/google/start", json={})
    assert response.status_code == 200 and response.get_json()["authorization_url"].startswith("https://accounts.example/auth?")
    assert transaction_of(app, client)["t"] is False                                       # 沒有勾選，也沒有被要求
    done = callback(client, transaction_of(app, client)["s"])
    assert done.status_code == 302 and done.headers["Location"] == DEFAULT_AFTER_LOGIN
    assert client.get("/api/auth/me").get_json()["user"]["email"] == "user1@example.com"


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
    assert transaction_of(app, client)["x"] == DEFAULT_AFTER_LOGIN


# =============================================================================
# 回呼：成功
# =============================================================================

def test_a_successful_callback_logs_in_redirects_and_deletes_the_transaction(app, provider):
    client = app.test_client()
    start(client, next="/account?tab=security")
    csrf_before = client.get_cookie("csrf").value
    response = callback(client, transaction_of(app, client)["s"])
    # 新帳號還沒有年齡資料：先去年齡確認頁，補填之後才回到原本要去的位置
    assert response.status_code == 302 and response.headers["Location"] == "/age?next=%2Faccount%3Ftab%3Dsecurity"
    me = client.get("/api/auth/me").get_json()["user"]
    assert me["email"] == "person@example.com" and me["google_linked"] is True and me["has_password"] is False
    assert me["age_confirmed"] is False and me["age_group"] is None and me["restriction"] == "age_pending"
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
    assert callback(client, state).headers["Location"] == "/age"
    assert error_of(callback(client, state)) == "google_state"                               # 綁定已刪除


def test_an_existing_verified_member_is_linked_and_logged_in(app, provider):
    with db(app) as s:
        add_user(s, 1, age=None, email_verified_at=app.extensions["clock"]())
    provider.profile = GoogleProfile(SUB, "user1@example.com", True, "x")
    client = app.test_client()
    start(client, accept_terms=False)                                                       # 已有帳號：不必勾選條款
    response = callback(client, transaction_of(app, client)["s"])
    assert response.headers["Location"] == "/age"                                           # 沒有年齡資料：只能去年齡確認頁
    me = client.get("/api/auth/me").get_json()["user"]
    assert me["has_password"] is True                                                       # 原有密碼仍在
    assert me["age_confirmed"] is False and me["age_group"] is None                         # 登入不會順便標記為已完成


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


def test_a_new_member_who_accepted_the_terms_gets_an_account_without_age_data_and_goes_to_the_age_page(app):
    client = app.test_client()
    response = run_flow(app, client)
    assert response.status_code == 302 and response.headers["Location"] == "/age"
    with db(app) as s:
        (user,) = s.query(User).all()
        assert user.birth_date is None and user.age_range is None and user.age_confirmed_at is None
        assert user.age_deadline_at == app.extensions["clock"]() + timedelta(hours=24)
        assert user.terms_version and user.terms_accepted_at is not None
        assert s.query(UserIdentity).count() == 1
    me = client.get("/api/auth/me").get_json()["user"]
    assert me["restriction"] == "age_pending"


def test_a_new_member_who_is_still_a_child_is_not_detected_here_that_happens_at_the_age_step(app):
    """發起與回呼都不再看年齡：未滿 13 歲的判斷在年齡確認頁（見 `AuthService.verify_age`）。"""
    client = app.test_client()
    assert run_flow(app, client, **age_fields(12)).headers["Location"] == "/age"
    with db(app) as s:
        assert s.query(User).one().birth_date is None


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
    seen.add(error_of(callback(app.test_client(), "nope")))
    assert seen <= FIXED_CODES and len(seen) == 4


# =============================================================================
# 帳號鎖定計次：只有「已辨識出帳號卻被拒絕」才計次（account-lockout 規格）
# =============================================================================

def lock_state(app, email="user1@example.com"):
    with db(app) as s:
        user = s.query(User).filter_by(email=email).one()
        return user.locked_at, user.lock_reason, s.query(LockStrike).filter_by(user_id=user.id).count()


def test_refusals_of_an_identified_account_are_counted_and_the_fifth_within_fifteen_minutes_locks_it(app, provider, clock):
    with db(app) as s:
        add_user(s, 1, status=STATUS_SUSPENDED, email_verified_at=clock())
    provider.profile = GoogleProfile(SUB, "user1@example.com", True, "x")
    for attempt in range(1, 6):
        assert error_of(run_flow(app, app.test_client())) == "account_disabled"
        locked_at, reason, strikes = lock_state(app)
        assert strikes == attempt                                                    # 回應是 302，計次仍然保留
        assert (locked_at is not None) == (attempt == 5)
    assert reason == "identity_rejected"


def test_refusals_spread_over_more_than_fifteen_minutes_do_not_lock(app, provider, clock):
    with db(app) as s:
        add_user(s, 1, status=STATUS_SUSPENDED, email_verified_at=clock())
    provider.profile = GoogleProfile(SUB, "user1@example.com", True, "x")
    for _ in range(4):
        run_flow(app, app.test_client())
    clock.advance(minutes=16)
    run_flow(app, app.test_client())
    assert lock_state(app)[0] is None


def test_a_conflict_with_another_google_identity_is_counted_too(app, provider, clock):
    with db(app) as s:
        user = add_user(s, 1, email_verified_at=clock())
        s.add(UserIdentity(user_id=user.id, provider="google", provider_subject="the-original-subject"))
        s.commit()
    provider.profile = GoogleProfile(SUB, "user1@example.com", True, "x")           # 同信箱、不同的 Google 身分
    assert error_of(run_flow(app, app.test_client())) == "google_rejected"
    assert lock_state(app) == (None, None, 1)


def test_failures_that_cannot_be_tied_to_an_account_are_never_counted(app, provider, clock):
    with db(app) as s:
        add_user(s, 1, email_verified_at=clock())
    for _ in range(12):
        client = app.test_client()
        start(client)
        callback(client, "forged-state")                                             # 狀態不符
    provider.error = GoogleAuthError("token")
    for _ in range(12):
        run_flow(app, app.test_client())                                             # 授權碼或憑證無效
    provider.error = None
    for _ in range(12):
        run_flow(app, app.test_client(), accept_terms=False)                         # 還沒有帳號的身分（沒勾條款）
    assert lock_state(app) == (None, None, 0)
    with db(app) as s:
        assert s.query(LockStrike).count() == 0 and s.query(User).count() == 1


def test_a_successful_sign_in_adds_no_strike(app, provider, clock):
    with db(app) as s:
        add_user(s, 1, email_verified_at=clock())
    provider.profile = GoogleProfile(SUB, "user1@example.com", True, "x")
    assert run_flow(app, app.test_client()).headers["Location"] == DEFAULT_AFTER_LOGIN
    assert lock_state(app) == (None, None, 0)


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
    assert events == ["auth.google_started", "auth.google_registered", "auth.google_rejected.state"]
    raw = log_lines.raw()
    for secret in (state, "very-secret-authorization-code", "forged-state-value", SUB, "person@example.com"):
        assert secret not in raw
