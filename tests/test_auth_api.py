"""member-registration 與 session-authentication 規格：`/api/auth/*` 端到端（註冊、登入、登出、查詢、變更密碼）。"""
import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base, User
from app.models.user import STATUS_PENDING_VERIFICATION, STATUS_SUSPENDED
from tests.support.auth import GOOD_PASSWORD, add_user
from tests.support.client import raw_client

NEW_PASSWORD = "a-brand-new-password"
FORBIDDEN_KEYS = {"id", "token_version", "password_hash", "password", "tv"}
PROFILE_KEYS = {
    "public_id", "email", "display_name", "member_tier", "role", "status",
    "email_verified", "has_password", "age_confirmed", "google_linked",
}


@pytest.fixture()
def app():
    application = create_app(load_config(environ={"APP_ENV": "testing"}))
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


def db(app) -> Session:
    return Session(app.extensions["db_engine"])


def register(client, **overrides):
    body = {
        "email": "new@example.com", "password": GOOD_PASSWORD, "display_name": "王小明",
        "accept_terms": True, "confirm_adult": True,
    }
    body.update(overrides)
    return client.post("/api/auth/register", json=body)


def login(client, email="new@example.com", password=GOOD_PASSWORD):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def registered_member(client, **overrides):
    assert register(client, **overrides).status_code == 200
    return client


def users_in(app) -> list[User]:
    with db(app) as s:
        return list(s.query(User))


# =============================================================================
# 註冊
# =============================================================================

def test_register_creates_an_active_account_and_does_not_log_in(app, client):
    response = register(client)
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "verification_required": False, "message": "註冊完成，請前往登入。"}
    (user,) = users_in(app)
    assert user.email == "new@example.com" and user.role == "user" and user.member_tier == "free"
    assert client.get("/api/auth/me").status_code == 401      # 沒有自動登入
    assert client.get_cookie("session") is None


def test_register_ignores_fields_that_would_raise_privileges(app, client):
    response = register(
        client, role="admin", member_tier="vip", status="active", is_admin=True, email_verified_at="2026-01-01",
        token_version=99, id=1, public_id="00000000-0000-4000-8000-000000000000",
    )
    assert response.status_code == 200
    (user,) = users_in(app)
    assert (user.role, user.member_tier, user.token_version) == ("user", "free", 1)
    assert user.email_verified_at is None and user.public_id != "00000000-0000-4000-8000-000000000000"


def test_register_reports_field_errors_and_creates_nothing(app, client):
    response = register(client, email="bad", password="short", display_name="", accept_terms=False, confirm_adult=False)
    assert response.status_code == 400
    error = response.get_json()["error"]
    assert error["code"] == "validation_error"
    assert set(error["fields"]) == {"email", "password", "display_name", "accept_terms", "confirm_adult"}
    assert users_in(app) == []


def test_register_without_the_age_confirmation_creates_nothing_and_names_the_missing_field(app, client):
    for value in (False, "true", None):
        response = register(client, confirm_adult=value)
        assert response.status_code == 400
        assert set(response.get_json()["error"]["fields"]) == {"confirm_adult"}
    payload = {"email": "new@example.com", "password": GOOD_PASSWORD, "display_name": "x", "accept_terms": True}
    omitted = client.post("/api/auth/register", json=payload)                  # 完全沒有帶 confirm_adult
    assert omitted.status_code == 400
    assert set(omitted.get_json()["error"]["fields"]) == {"confirm_adult"}
    assert users_in(app) == []


def test_register_records_the_age_confirmation_and_keeps_it_out_of_the_response(app, client):
    response = register(client)
    assert response.get_json() == {"ok": True, "verification_required": False, "message": "註冊完成，請前往登入。"}
    (user,) = users_in(app)
    assert user.age_confirmed_at is not None and user.age_policy_version


def test_register_requires_a_json_object_body(client):
    assert client.post("/api/auth/register", data={"email": "a@example.com"}).status_code == 415
    assert client.post("/api/auth/register", data="[1]", content_type="application/json").status_code == 400


def test_register_without_csrf_is_rejected(app):
    response = raw_client(app).post("/api/auth/register", json={"email": "a@example.com"}, headers={"Origin": "http://localhost"})
    assert response.status_code == 403 and users_in(app) == []


# =============================================================================
# 登入與查詢
# =============================================================================

def test_login_returns_the_profile_and_sets_the_login_cookie(client):
    registered_member(client)
    response = login(client)
    assert response.status_code == 200
    user = response.get_json()["user"]
    assert set(user) == PROFILE_KEYS
    assert not (FORBIDDEN_KEYS & set(user))
    assert user["email"] == "new@example.com" and user["display_name"] == "王小明"
    assert user["member_tier"] == "free" and user["role"] == "user" and user["status"] == "active"
    assert user["email_verified"] is False and user["has_password"] is True and user["google_linked"] is False
    assert user["age_confirmed"] is True                       # 註冊時已確認
    assert client.get_cookie("session") is not None


# =============================================================================
# 補確認年滿 18 歲
# =============================================================================

def unconfirmed_member_client(app):
    """升級前就存在、沒有年齡確認紀錄的帳號（直接寫入資料庫，再以 API 登入）。"""
    with db(app) as s:
        add_user(s, 1)
    client = app.test_client()
    assert login(client, email="user1@example.com").get_json()["user"]["age_confirmed"] is False
    return client


def test_an_account_without_a_confirmation_sees_age_confirmed_false_in_login_and_me(app):
    client = unconfirmed_member_client(app)
    assert client.get("/api/auth/me").get_json()["user"]["age_confirmed"] is False


def test_confirm_adult_records_it_and_returns_the_fresh_profile_without_internal_details(app):
    client = unconfirmed_member_client(app)
    response = client.post("/api/auth/confirm-adult", json={"confirm_adult": True})
    assert response.status_code == 200
    user = response.get_json()["user"]
    assert set(user) == PROFILE_KEYS and user["age_confirmed"] is True
    assert "age_confirmed_at" not in user and "age_policy_version" not in user
    assert client.get("/api/auth/me").get_json()["user"]["age_confirmed"] is True        # 登入狀態沒有失效
    (stored,) = users_in(app)
    assert stored.age_confirmed_at is not None and stored.age_policy_version


@pytest.mark.parametrize("body", [{}, {"confirm_adult": False}, {"confirm_adult": "true"}, {"confirm_adult": 1}, {"confirm_adult": None}])
def test_confirm_adult_without_an_explicit_true_is_a_400_and_writes_nothing(app, body):
    client = unconfirmed_member_client(app)
    response = client.post("/api/auth/confirm-adult", json=body)
    assert response.status_code == 400
    error = response.get_json()["error"]
    assert error["code"] == "validation_error" and set(error["fields"]) == {"confirm_adult"}
    (stored,) = users_in(app)
    assert stored.age_confirmed_at is None and stored.age_policy_version is None


def test_confirm_adult_needs_a_login(app, client):
    response = client.post("/api/auth/confirm-adult", json={"confirm_adult": True})
    assert response.status_code == 401 and response.get_json()["error"]["code"] == "unauthorized"


def test_confirm_adult_twice_succeeds_and_keeps_the_first_record(app):
    client = unconfirmed_member_client(app)
    assert client.post("/api/auth/confirm-adult", json={"confirm_adult": True}).status_code == 200
    (first,) = users_in(app)
    recorded = (first.age_confirmed_at, first.age_policy_version)
    assert client.post("/api/auth/confirm-adult", json={"confirm_adult": True}).status_code == 200
    (again,) = users_in(app)
    assert (again.age_confirmed_at, again.age_policy_version) == recorded


def test_confirm_adult_without_csrf_is_rejected_and_writes_nothing(app):
    client = unconfirmed_member_client(app)
    raw = raw_client(app)
    response = raw.post("/api/auth/confirm-adult", json={"confirm_adult": True}, headers={"Origin": "http://localhost"})
    assert response.status_code == 403
    (stored,) = users_in(app)
    assert stored.age_confirmed_at is None


def test_confirm_adult_only_changes_the_callers_own_account(app):
    with db(app) as s:
        add_user(s, 2)
    client = unconfirmed_member_client(app)
    client.post("/api/auth/confirm-adult", json={"confirm_adult": True})
    confirmed = {user.email: user.age_confirmed_at is not None for user in users_in(app)}
    assert confirmed == {"user1@example.com": True, "user2@example.com": False}


def test_me_returns_the_same_profile_after_login_and_401_before(client):
    assert client.get("/api/auth/me").status_code == 401
    registered_member(client)
    profile = login(client).get_json()["user"]
    assert client.get("/api/auth/me").get_json() == {"user": profile}


def test_a_pending_member_can_log_in_and_sees_that_the_email_is_unverified(app, client):
    registered_member(client)
    with db(app) as s:
        s.query(User).one().status = STATUS_PENDING_VERIFICATION
        s.commit()
    user = login(client).get_json()["user"]
    assert user["status"] == "pending_verification" and user["email_verified"] is False


def test_a_suspended_member_gets_a_403_and_no_cookie(app, client):
    registered_member(client)
    with db(app) as s:
        s.query(User).one().status = STATUS_SUSPENDED
        s.commit()
    response = login(client)
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "account_disabled"
    assert client.get_cookie("session") is None


def test_login_with_missing_fields_is_a_400(client):
    response = client.post("/api/auth/login", json={"email": "a@example.com"})
    assert response.status_code == 400 and "password" in response.get_json()["error"]["fields"]


def test_login_replaces_the_csrf_token(client):
    registered_member(client)
    before = client.csrf_token()
    login(client)
    assert client.get_cookie("csrf").value != before


# =============================================================================
# 登出
# =============================================================================

def test_logout_ends_this_device_only_and_is_idempotent(app):
    this_device, other_device = app.test_client(), app.test_client()
    registered_member(this_device)
    login(this_device)
    login(other_device)
    assert this_device.post("/api/auth/logout").status_code == 200
    assert this_device.get("/api/auth/me").status_code == 401
    assert other_device.get("/api/auth/me").status_code == 200
    assert this_device.post("/api/auth/logout").status_code == 200       # 已經登出，再登出也成功


def test_logout_all_ends_every_device_including_this_one(app):
    this_device, other_device = app.test_client(), app.test_client()
    registered_member(this_device)
    login(this_device)
    login(other_device)
    response = this_device.post("/api/auth/logout-all")
    assert response.status_code == 200
    assert this_device.get("/api/auth/me").status_code == 401
    assert other_device.get("/api/auth/me").status_code == 401
    assert login(this_device).status_code == 200                           # 之後仍可重新登入


def test_logout_all_requires_a_login(client):
    assert client.post("/api/auth/logout-all").status_code == 401


# =============================================================================
# 變更密碼
# =============================================================================

def test_change_password_keeps_this_device_and_logs_out_the_others(app):
    this_device, other_device = app.test_client(), app.test_client()
    registered_member(this_device)
    login(this_device)
    login(other_device)
    response = this_device.post(
        "/api/auth/change-password", json={"current_password": GOOD_PASSWORD, "new_password": NEW_PASSWORD}
    )
    assert response.status_code == 200
    assert this_device.get("/api/auth/me").status_code == 200
    assert other_device.get("/api/auth/me").status_code == 401
    assert login(app.test_client(), password=GOOD_PASSWORD).status_code == 401
    assert login(app.test_client(), password=NEW_PASSWORD).status_code == 200


def test_changing_the_password_keeps_the_page_usable_because_the_csrf_token_is_not_replaced(app):
    """帳號頁在變更密碼後不會重新載入：頁面上的權杖必須仍然有效，否則之後的登出會被擋成 403。"""
    client = raw_client(app)
    client.get("/")
    token = client.get_cookie("csrf").value
    headers = {"Origin": "http://localhost", "X-CSRF-Token": token}
    client.post("/api/auth/register", json={"email": "keep@example.com", "password": GOOD_PASSWORD, "display_name": "x", "accept_terms": True, "confirm_adult": True}, headers=headers)
    assert client.post("/api/auth/login", json={"email": "keep@example.com", "password": GOOD_PASSWORD}, headers=headers).status_code == 200
    token = client.get_cookie("csrf").value                   # 登入換發了新權杖（頁面會跟著重新載入）
    headers = {"Origin": "http://localhost", "X-CSRF-Token": token}
    changed = client.post("/api/auth/change-password", json={"current_password": GOOD_PASSWORD, "new_password": NEW_PASSWORD}, headers=headers)
    assert changed.status_code == 200
    assert client.get_cookie("csrf").value == token           # 沒有被換掉
    assert client.post("/api/auth/logout", headers=headers).status_code == 200     # 仍可用同一個權杖繼續操作


def test_change_password_with_a_wrong_current_password_is_a_400_and_changes_nothing(app, client):
    registered_member(client)
    login(client)
    response = client.post("/api/auth/change-password", json={"current_password": "not-mine-at-all", "new_password": NEW_PASSWORD})
    assert response.status_code == 400
    assert response.get_json()["error"]["fields"] == {"current_password": "目前的密碼不正確。"}
    assert login(app.test_client(), password=GOOD_PASSWORD).status_code == 200


def test_change_password_requires_a_login(client):
    response = client.post("/api/auth/change-password", json={"current_password": GOOD_PASSWORD, "new_password": NEW_PASSWORD})
    assert response.status_code == 401


def test_a_google_only_account_cannot_change_a_password_this_way(app, client):
    with db(app) as s:
        add_user(s, 1, password=None)
    # 純 Google 帳號沒有密碼可登入；以測試用的登入狀態直接建立 session
    from app.security.cookies import SignedCookies  # noqa: F401 - 僅確認模組可用
    with client.session_transaction() as session:
        session["uid"], session["tv"] = 1, 1
    response = client.post("/api/auth/change-password", json={"current_password": "anything-at-all", "new_password": NEW_PASSWORD})
    assert response.status_code == 400 and response.get_json()["error"]["code"] == "no_password"


# =============================================================================
# 6.2：列舉防護的對照（狀態碼與本文逐字相同）
# =============================================================================

def test_registering_an_existing_and_a_new_address_look_exactly_the_same(app):
    first = register(app.test_client(), email="someone@example.com")
    repeated = register(app.test_client(), email="someone@example.com", password="different-password-1", display_name="別人")
    assert (first.status_code, first.get_data()) == (repeated.status_code, repeated.get_data())
    assert first.status_code == 200
    assert len(users_in(app)) == 1


def test_registering_a_gmail_alias_of_an_existing_account_looks_the_same_too(app):
    new = register(app.test_client(), email="john.doe@gmail.com")
    alias = register(app.test_client(), email="johndoe+x@googlemail.com")
    assert (new.status_code, new.get_data()) == (alias.status_code, alias.get_data())


def test_unknown_address_wrong_password_and_locked_account_get_byte_identical_responses(app):
    with db(app) as s:
        add_user(s, 1)
        add_user(s, 2)
    probe = app.test_client()
    unknown = login(probe, email="nobody@example.com", password="whatever-password")
    wrong = login(probe, email="user1@example.com", password="whatever-password")
    for _ in range(5):
        login(probe, email="user2@example.com", password="whatever-password")
    locked = login(probe, email="user2@example.com", password=GOOD_PASSWORD)      # 鎖定中，即使密碼正確
    assert {r.status_code for r in (unknown, wrong, locked)} == {401}
    assert {r.get_data() for r in (unknown, wrong, locked)} == {unknown.get_data()}
    assert unknown.get_json()["error"]["code"] == "invalid_credentials"


def test_failed_attempts_over_http_really_accumulate_and_the_fifth_one_locks(app):
    """回歸測試：登入失敗回 401，失敗次數必須在請求結束後仍然存在（不能隨 4xx 回復）。"""
    with db(app) as s:
        add_user(s, 1)
    probe = app.test_client()
    for _ in range(4):
        login(probe, email="user1@example.com", password="wrong-password-1")
    with db(app) as s:
        assert s.query(User).one().failed_login_count == 4
    login(probe, email="user1@example.com", password="wrong-password-1")
    with db(app) as s:
        user = s.query(User).one()
        assert user.failed_login_count == 5 and user.locked_until is not None
    assert login(probe, email="user1@example.com", password=GOOD_PASSWORD).status_code == 401   # 鎖定中


def test_wrong_current_passwords_when_changing_the_password_also_accumulate_over_http(app, client):
    registered_member(client)
    login(client)
    for _ in range(3):
        client.post("/api/auth/change-password", json={"current_password": "not-mine-at-all", "new_password": NEW_PASSWORD})
    with db(app) as s:
        assert s.query(User).one().failed_login_count == 3


def test_a_failed_login_sets_no_login_cookie(app):
    with db(app) as s:
        add_user(s, 1)
    probe = app.test_client()
    login(probe, email="user1@example.com", password="wrong-password-123")
    assert probe.get_cookie("session") is None


# =============================================================================
# 6.3：安全事件
# =============================================================================

def test_the_whole_flow_is_logged_as_events_without_personal_data(app, client, log_lines):
    registered_member(client, email="log.me@example.com", password="log-me-password-xyz")
    login(client, email="log.me@example.com", password="wrong-one-entirely")
    login(client, email="log.me@example.com", password="log-me-password-xyz")
    client.post("/api/auth/change-password", json={"current_password": "log-me-password-xyz", "new_password": "another-new-password-1"})
    client.post("/api/auth/logout")
    login(client, email="log.me@example.com", password="another-new-password-1")
    client.post("/api/auth/logout-all")
    events = [line["event"] for line in log_lines() if "event" in line]
    assert events == [
        "auth.register", "auth.age_confirmed.register", "auth.login_failed.bad_password", "auth.login_success",
        "auth.password_changed", "auth.logout", "auth.login_success", "auth.logout_all",
    ]
    raw = log_lines.raw()
    for secret in ("log.me@example.com", "log-me-password-xyz", "wrong-one-entirely", "another-new-password-1"):
        assert secret not in raw


def test_events_carry_the_internal_user_id_when_known(app, client, log_lines):
    registered_member(client)
    login(client)
    success = next(line for line in log_lines() if line.get("event") == "auth.login_success")
    assert isinstance(success["user_id"], int)
