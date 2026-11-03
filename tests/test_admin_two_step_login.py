"""admin-two-step-login 規格：管理員每次先 Google、再密碼，兩者都通過才建立登入 session；不能只靠密碼登入。

走真實的 Flask 應用程式、Google 回呼與資料庫（供應商是假的）。忘記密碼寄信的部分見本檔最後一節與 `test_email_flow_api.py`。
"""
from __future__ import annotations

import json
import re
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base, User, UserIdentity
from app.security.admin_step import ADMIN_STEP_COOKIE, ADMIN_STEP_SALT
from app.security.cookies import SignedCookies
from app.services.google_oidc import GoogleProfile
from tests.support.auth import GOOD_PASSWORD, START, FakeClock, add_user
from tests.support.email_flow import FakeSender
from tests.support.google import SUB, FakeProvider, google_sign_in

ADMIN_EMAIL = "user1@example.com"
STATE_PATTERN = re.compile(r'<script type="application/json" id="initial-state">(.*?)</script>', re.S)


@pytest.fixture()
def provider():
    return FakeProvider()


@pytest.fixture()
def clock():
    return FakeClock()


@pytest.fixture()
def app(provider, clock):
    application = create_app(load_config(environ={"APP_ENV": "testing"}), identity_provider=provider, clock=clock)
    Base.metadata.create_all(application.extensions["db_engine"])
    with Session(application.extensions["db_engine"]) as s:
        add_user(s, 1, role="admin", email_verified_at=START)                       # 管理員：由指令建立，信箱已驗證，有密碼
        add_user(s, 2, email_verified_at=START)                                     # 一般玩家
    provider.profile = GoogleProfile(SUB, ADMIN_EMAIL, True, "管理員")
    return application


def through_google(app, **start_body):
    """通過 Google：回傳（client, 回呼的回應）。管理員此時只有中間狀態 cookie。"""
    client = app.test_client()
    return client, google_sign_in(app, client, **start_body)


def enter_password(client, password=GOOD_PASSWORD):
    return client.post("/api/auth/admin-password", json={"password": password})


def admin_row(app) -> User:
    with Session(app.extensions["db_engine"]) as s:
        user = s.query(User).filter_by(email=ADMIN_EMAIL).one()
        s.expunge(user)
        return user


# ---- 兩個步驟 -----------------------------------------------------------------------------------------

def test_after_google_the_admin_goes_to_the_password_page_with_a_step_cookie_and_no_session(app):
    client, response = through_google(app)
    assert response.status_code == 302 and response.headers["Location"] == "/login/password"
    assert client.get_cookie(ADMIN_STEP_COOKIE) is not None
    assert client.get_cookie("session") is None
    assert client.get_cookie("oauth_tx") is None                                  # Google 的交易 cookie 已刪除


def test_google_alone_does_not_open_the_console_or_any_api(app):
    client, _ = through_google(app)
    assert client.get("/api/admin/overview").status_code == 401
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/cs/conversations").status_code == 401
    assert client.get("/admin").headers["Location"].startswith("/login?next=")


def test_google_and_the_right_password_open_the_console(app):
    client, _ = through_google(app)
    response = enter_password(client)
    assert response.status_code == 200 and response.get_json() == {"ok": True, "next": "/cs"}
    assert client.get_cookie(ADMIN_STEP_COOKIE) is None                           # 中間狀態用完即清除
    me = client.get("/api/auth/me").get_json()["user"]
    assert me["role"] == "admin" and me["email"] == ADMIN_EMAIL
    assert client.get("/api/admin/overview").status_code == 200
    assert client.get("/admin").status_code == 200


def test_the_session_after_both_steps_holds_only_the_user_id_and_the_token_version(app):
    client, _ = through_google(app)
    enter_password(client)
    payload = app.session_interface.get_signing_serializer(app).loads(client.get_cookie("session").value)
    assert set(payload) == {"uid", "tv"}


def test_the_destination_chosen_before_google_survives_to_the_end_of_the_password_step(app):
    client, _ = through_google(app, next="/admin")
    assert enter_password(client).get_json()["next"] == "/admin"


def test_an_unsafe_destination_never_gets_through_the_step(app):
    client, _ = through_google(app, next="https://evil.example/")
    assert enter_password(client).get_json()["next"] == "/cs"


def test_the_first_google_sign_in_of_the_admin_binds_the_identity_and_the_password_is_still_required(app):
    client, _ = through_google(app)
    with Session(app.extensions["db_engine"]) as s:
        (identity,) = s.query(UserIdentity).all()
        assert identity.provider_subject == SUB and identity.user_id == admin_row(app).id
    assert client.get("/api/auth/me").status_code == 401                          # 綁定了，但還沒輸入密碼
    again, response = through_google(app)                                         # 之後每次登入都一樣要再輸入密碼
    assert response.headers["Location"] == "/login/password" and again.get("/api/auth/me").status_code == 401


def test_a_player_signing_in_with_google_is_unaffected_and_gets_a_session_straight_away(app, provider):
    provider.profile = GoogleProfile("sub-player", "user2@example.com", True, "玩家")
    client, response = through_google(app)
    assert response.headers["Location"] == "/cs" and client.get_cookie(ADMIN_STEP_COOKIE) is None
    assert client.get("/api/auth/me").status_code == 200


# ---- 中間狀態的限制 -----------------------------------------------------------------------------------

def test_the_step_expires_after_ten_minutes(app, clock):
    client, _ = through_google(app)
    clock.advance(minutes=9, seconds=59)
    assert client.get("/login/password").status_code == 200
    clock.advance(seconds=2)
    response = enter_password(client)
    assert response.status_code == 401 and response.get_json()["error"]["code"] == "unauthorized"
    assert client.get("/login/password").headers["Location"] == "/login"
    assert client.get("/api/auth/me").status_code == 401


def test_without_a_step_the_password_endpoint_is_401(app):
    response = enter_password(app.test_client())
    assert response.status_code == 401 and response.get_json()["error"]["code"] == "unauthorized"


def test_a_cookie_signed_for_another_purpose_or_tampered_with_is_not_a_step(app):
    client, _ = through_google(app)
    good = client.get_cookie(ADMIN_STEP_COOKIE).value
    cookies = SignedCookies(app.extensions["app_config"], app.extensions["clock"])
    for forged in (cookies.dumps("oauth.google.v1", {"uid": 1, "tv": 1, "x": "/cs"}), good[:-3] + "AAA", "garbage"):
        client.set_cookie(ADMIN_STEP_COOKIE, forged)
        assert enter_password(client).status_code == 401
    # 形狀不對（缺欄位、型別不對）同樣無效
    for payload in ({"uid": 1}, {"uid": True, "tv": 1, "x": "/"}, {"uid": "1", "tv": 1, "x": "/"}, ["uid", 1]):
        client.set_cookie(ADMIN_STEP_COOKIE, cookies.dumps(ADMIN_STEP_SALT, payload))
        assert enter_password(client).status_code == 401, payload


def test_a_step_for_a_player_account_never_logs_anyone_in(app):
    """即使有人以簽章金鑰以外的方式取得玩家編號的中間狀態，玩家沒有密碼步驟，端點只接受管理員。"""
    cookies = SignedCookies(app.extensions["app_config"], app.extensions["clock"])
    client = app.test_client()
    client.set_cookie(ADMIN_STEP_COOKIE, cookies.dumps(ADMIN_STEP_SALT, {"uid": 2, "tv": 1, "x": "/cs"}))
    assert enter_password(client).status_code == 401 and client.get("/api/auth/me").status_code == 401


def test_a_token_version_change_between_the_two_steps_voids_the_step(app):
    client, _ = through_google(app)
    with Session(app.extensions["db_engine"]) as s:
        s.query(User).filter_by(email=ADMIN_EMAIL).one().token_version += 1             # 例如在別處登出所有裝置
        s.commit()
    assert enter_password(client).status_code == 401 and client.get("/api/auth/me").status_code == 401


def test_the_password_step_needs_csrf_like_every_other_post(app):
    client, _ = through_google(app)
    client.auto_csrf = False
    response = client.post("/api/auth/admin-password", json={"password": GOOD_PASSWORD}, headers={"Origin": "http://localhost"})
    assert response.status_code == 403 and client.get("/api/auth/me").status_code == 401


def test_a_missing_password_is_a_field_error_not_a_failure_count(app):
    client, _ = through_google(app)
    for body in ({}, {"password": ""}, {"password": 5}):
        response = client.post("/api/auth/admin-password", json=body)
        assert response.status_code == 400 and "password" in response.get_json()["error"]["fields"]
    assert admin_row(app).failed_login_count == 0


# ---- 管理員不能只靠密碼 -------------------------------------------------------------------------------

def test_there_is_no_password_login_endpoint_so_an_admin_cannot_log_in_with_only_the_password(app):
    """`POST /api/auth/login` 已不存在：任何信箱與密碼的組合（對的、錯的、不存在的）都是同樣的 404，沒有登入狀態。"""
    client = app.test_client()
    right = client.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": GOOD_PASSWORD})
    wrong = client.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": "definitely-wrong-password"})
    unknown = client.post("/api/auth/login", json={"email": "nobody@example.com", "password": GOOD_PASSWORD})
    assert {r.status_code for r in (right, wrong, unknown)} == {404}
    assert {r.get_data() for r in (right, wrong, unknown)} == {unknown.get_data()}
    assert client.get_cookie("session") is None and client.get("/api/auth/me").status_code == 401


def test_attempts_at_the_removed_password_login_never_count_toward_the_admins_lockout(app):
    client = app.test_client()
    for _ in range(12):
        client.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": GOOD_PASSWORD})
    row = admin_row(app)
    assert row.failed_login_count == 0 and row.locked_until is None
    through, _ = through_google(app)
    assert enter_password(through).status_code == 200                                 # 沒被鎖


# ---- 密碼步驟的失敗限制 -------------------------------------------------------------------------------

def test_five_wrong_passwords_lock_the_admin_for_fifteen_minutes_even_for_the_right_one(app, clock):
    client, _ = through_google(app)
    for attempt in range(1, 6):
        response = enter_password(client, "wrong-password-%d" % attempt)
        assert response.status_code == 401 and response.get_json()["error"]["code"] == "invalid_credentials"
    row = admin_row(app)
    assert row.failed_login_count == 5 and row.locked_until == START + timedelta(minutes=15)
    locked = enter_password(client)                                                     # 鎖定中，即使密碼正確
    assert locked.status_code == 401 and locked.get_json()["error"]["code"] == "invalid_credentials"
    clock.advance(minutes=15, seconds=1)
    fresh, _ = through_google(app)
    assert enter_password(fresh).status_code == 200                                    # 到期自動解除


def test_the_wrong_password_and_the_locked_response_are_byte_identical(app):
    client, _ = through_google(app)
    first = enter_password(client, "wrong-password-0")
    for attempt in range(1, 5):
        enter_password(client, "wrong-password-%d" % attempt)
    locked = enter_password(client)
    assert first.get_data() == locked.get_data() and first.status_code == locked.status_code == 401


def test_a_successful_password_resets_the_failure_count(app):
    client, _ = through_google(app)
    for attempt in range(3):
        enter_password(client, "wrong-password-%d" % attempt)
    assert admin_row(app).failed_login_count == 3
    assert enter_password(client).status_code == 200
    assert admin_row(app).failed_login_count == 0


# ---- /login/password 頁面 -----------------------------------------------------------------------------

def test_the_password_page_opens_with_a_step_and_carries_nothing_personal(app):
    client, _ = through_google(app, next="/admin")
    response = client.get("/login/password")
    assert response.status_code == 200
    state = json.loads(STATE_PATTERN.search(response.get_data(as_text=True)).group(1))
    assert state["next"] == "/admin" and "user" not in state
    assert ADMIN_EMAIL not in response.get_data(as_text=True)


def test_the_password_page_without_a_step_goes_back_to_the_login_page(app):
    response = app.test_client().get("/login/password")
    assert response.status_code == 302 and response.headers["Location"] == "/login"


# ---- 日誌 ---------------------------------------------------------------------------------------------

def test_the_two_steps_are_logged_without_secrets(app, log_lines):
    client, _ = through_google(app)
    step_value = client.get_cookie(ADMIN_STEP_COOKIE).value
    enter_password(client, "a-wrong-password-for-the-log")
    enter_password(client)
    events = [line["event"] for line in log_lines() if "event" in line]
    assert "auth.admin_google_passed" in events and "auth.login_failed.bad_password" in events and "auth.login_success" in events
    raw = log_lines.raw()
    for secret in (step_value, GOOD_PASSWORD, "a-wrong-password-for-the-log", ADMIN_EMAIL):
        assert secret not in raw


# =============================================================================
# 忘記密碼（密碼驗證那一步的按鈕）
# =============================================================================



@pytest.fixture()
def sender():
    return FakeSender()


@pytest.fixture()
def mail_app(provider, clock, sender):
    """有寄信設定（寄信器與對外網址），但信箱驗證關閉：管理員的忘記密碼不受信箱驗證開關控制。"""
    config = load_config(environ={"APP_ENV": "testing", "BASE_URL": "https://cs.example.com"})
    application = create_app(config, identity_provider=provider, clock=clock, mail_sender=sender)
    Base.metadata.create_all(application.extensions["db_engine"])
    with Session(application.extensions["db_engine"]) as s:
        add_user(s, 1, role="admin", email_verified_at=START)
        add_user(s, 2, email_verified_at=START)
    provider.profile = GoogleProfile(SUB, ADMIN_EMAIL, True, "管理員")
    return application


def forgot(client):
    return client.post("/api/auth/admin-password/forgot", json={})


def test_pressing_forgot_sends_one_reset_mail_to_the_admin_and_reveals_nothing_else(mail_app, sender):
    client, _ = through_google(mail_app)
    response = forgot(client)
    assert response.status_code == 200 and set(response.get_json()) == {"ok", "message"}
    assert [mail["to"] for mail in sender.sent] == [ADMIN_EMAIL]
    assert "/reset-password#token=" in sender.sent[0]["body"] and sender.last_token()


def test_the_response_is_identical_whether_a_mail_was_sent_or_the_request_was_rate_limited(mail_app, sender):
    client, _ = through_google(mail_app)
    first, second = forgot(client), forgot(client)
    assert first.get_data() == second.get_data() and first.status_code == second.status_code == 200
    assert len(sender.sent) == 1                                                       # 頻率限制：第二次不寄


def test_an_anonymous_request_gets_404_and_sends_nothing(mail_app, sender):
    for client in (mail_app.test_client(),):
        assert forgot(client).status_code == 404
    assert sender.sent == []


def test_a_forged_or_expired_step_gets_404_and_sends_nothing(mail_app, sender, clock):
    client, _ = through_google(mail_app)
    clock.advance(minutes=11)
    assert forgot(client).status_code == 404
    client.set_cookie(ADMIN_STEP_COOKIE, "garbage")
    assert forgot(client).status_code == 404
    assert sender.sent == []


def test_a_signed_in_session_alone_is_not_a_step(mail_app, sender):
    from tests.support.auth import sign_in_as

    client = mail_app.test_client()
    sign_in_as(client, admin_row(mail_app).id)
    assert forgot(client).status_code == 404 and sender.sent == []


def test_a_step_that_no_longer_matches_the_account_sends_nothing_but_looks_the_same(mail_app, sender):
    client, _ = through_google(mail_app)
    with Session(mail_app.extensions["db_engine"]) as s:
        s.query(User).filter_by(email=ADMIN_EMAIL).one().token_version += 1
        s.commit()
    response = forgot(client)
    assert response.status_code == 200 and sender.sent == []


def test_without_mail_settings_the_endpoint_is_404_and_the_page_hides_the_button(app):
    client, _ = through_google(app)
    assert forgot(client).status_code == 404
    state = json.loads(STATE_PATTERN.search(client.get("/login/password").get_data(as_text=True)).group(1))
    assert state["forgotAvailable"] is False


def test_with_mail_settings_the_page_shows_the_button(mail_app):
    client, _ = through_google(mail_app)
    state = json.loads(STATE_PATTERN.search(client.get("/login/password").get_data(as_text=True)).group(1))
    assert state["forgotAvailable"] is True


def test_the_player_side_forgot_password_is_still_off_without_email_verification(mail_app):
    response = mail_app.test_client().post("/api/auth/forgot-password", json={"email": "user2@example.com"})
    assert response.status_code == 404


def test_completing_the_reset_voids_the_step_and_the_sessions_and_the_new_password_works(mail_app, sender, clock):
    client, _ = through_google(mail_app)
    forgot(client)
    token = sender.last_token()
    # 另一台裝置上已經登入的管理員 session
    other, _ = through_google(mail_app)
    assert enter_password(other).status_code == 200 and other.get("/api/auth/me").status_code == 200
    for attempt in range(3):                                                          # 先累積幾次密碼失敗
        enter_password(client, "wrong-password-%d" % attempt)

    page = mail_app.test_client().get("/reset-password")
    assert page.status_code == 200                                                    # 信箱驗證關閉，但有寄信設定：確認頁可用
    reset = client.post("/api/auth/reset-password", json={"token": token, "new_password": "brand-new-password-99"})
    assert reset.status_code == 200

    assert client.get_cookie(ADMIN_STEP_COOKIE) is None
    assert enter_password(client, "brand-new-password-99").status_code == 401        # 中間狀態已清除，需要重新 Google
    assert other.get("/api/auth/me").status_code == 401                              # 既有的登入 session 一併失效
    row = admin_row(mail_app)
    assert row.failed_login_count == 0 and row.token_version == 2
    assert len(sender.sent) == 2 and "已變更" in sender.sent[1]["subject"] + sender.sent[1]["body"]   # 重設成功後寄通知信

    fresh, _ = through_google(mail_app)
    assert enter_password(fresh, GOOD_PASSWORD).status_code == 401                    # 舊密碼不能再用
    assert enter_password(fresh, "brand-new-password-99").status_code == 200


def test_the_reset_link_works_once_only(mail_app, sender):
    client, _ = through_google(mail_app)
    forgot(client)
    token = sender.last_token()
    body = {"token": token, "new_password": "brand-new-password-99"}
    assert mail_app.test_client().post("/api/auth/reset-password", json=body).status_code == 200
    again = mail_app.test_client().post("/api/auth/reset-password", json={**body, "new_password": "another-new-password-88"})
    assert again.status_code == 400 and again.get_json()["error"]["code"] == "invalid_token"


def test_the_reset_link_expires_after_an_hour(mail_app, sender, clock):
    client, _ = through_google(mail_app)
    forgot(client)
    token = sender.last_token()
    clock.advance(hours=1, seconds=1)
    response = mail_app.test_client().post("/api/auth/reset-password", json={"token": token, "new_password": "brand-new-password-99"})
    assert response.status_code == 400 and response.get_json()["error"]["code"] == "invalid_token"


def test_the_mail_the_logs_and_the_token_never_meet(mail_app, sender, log_lines):
    client, _ = through_google(mail_app)
    forgot(client)
    token = sender.last_token()
    raw = log_lines.raw()
    assert token not in raw and ADMIN_EMAIL not in raw
    events = [line["event"] for line in log_lines() if "event" in line]
    assert "auth.password_reset_requested" in events
