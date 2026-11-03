"""session-authentication 規格：`/api/auth/*` 端到端（登入狀態、補填年齡、登出、查詢、變更密碼、已移除的端點）。

玩家只用 Google 登入、管理員先 Google 再密碼，都不經過 `/api/auth/*` 的密碼端點；註冊、密碼登入、信箱驗證與玩家的忘記密碼已移除。"""
import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base, LockStrike, User
from app.models.user import STATUS_SUSPENDED
from tests.support.age import age_fields, birth_date_for_age
from tests.support.auth import GOOD_PASSWORD, FakeClock, add_user, sign_in_as
from tests.support.client import raw_client
from tests.support.idor import logged_in_client

NEW_PASSWORD = "a-brand-new-password"
FORBIDDEN_KEYS = {"id", "token_version", "password_hash", "password", "tv"}
PROFILE_KEYS = {
    "public_id", "email", "display_name", "member_tier", "role", "status",
    "email_verified", "has_password", "age_confirmed", "age_group", "google_linked", "restriction",
}
AGE_SECRETS = ("birth_date", "age_range", "age_confirmed_at", "age_policy_version", "is_adult")


@pytest.fixture()
def app():
    application = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())   # 固定的「今天」，生日不受真實日期影響
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


def db(app) -> Session:
    return Session(app.extensions["db_engine"])


def login(client, email="user1@example.com"):
    """代表「通過 Google 登入」：以該信箱目前的 (uid, token_version) 寫入登入 cookie（玩家沒有密碼登入）。"""
    with db(client.application) as s:
        user = s.query(User).filter(User.email == email).one()
        sign_in_as(client, user.id, user.token_version)
    return client


def member_client(app, n: int = 1, **overrides):
    """一位已完成年齡確認的玩家，及其登入狀態。"""
    with db(app) as s:
        add_user(s, n, display_name="王小明", **overrides)
    return login(app.test_client(), email=f"user{n}@example.com")


def users_in(app) -> list[User]:
    with db(app) as s:
        return list(s.query(User))


# =============================================================================
# 已移除的玩家端密碼與信箱驗證端點（remove-player-password-auth）
# =============================================================================

REMOVED_ENDPOINTS = ("register", "login", "verify-email", "resend-verification", "forgot-password")


@pytest.mark.parametrize("name", REMOVED_ENDPOINTS)
def test_the_removed_endpoints_answer_404_without_creating_anything(app, name):
    with db(app) as s:
        add_user(s, 1)
    body = {"email": "user1@example.com", "password": GOOD_PASSWORD, "display_name": "x", "accept_terms": True, "token": "t"}
    response = app.test_client().post(f"/api/auth/{name}", json=body)
    assert response.status_code == 404
    assert "session=" not in response.headers.get("Set-Cookie", "")
    assert len(users_in(app)) == 1


def test_the_admin_cannot_use_the_removed_password_login_either_and_nothing_is_counted(app):
    with db(app) as s:
        add_user(s, 1, role="admin")
    probe = app.test_client()
    response = probe.post("/api/auth/login", json={"email": "user1@example.com", "password": GOOD_PASSWORD})
    assert response.status_code == 404 and probe.get_cookie("session") is None
    with db(app) as s:
        assert s.query(User).one().failed_login_count == 0


# =============================================================================
# 登入狀態與查詢
# =============================================================================

def test_me_returns_the_profile_of_the_signed_in_member_and_401_before(app, client):
    assert client.get("/api/auth/me").status_code == 401
    signed_in = member_client(app)
    user = signed_in.get("/api/auth/me").get_json()["user"]
    assert set(user) == PROFILE_KEYS
    assert not (FORBIDDEN_KEYS & set(user))
    assert user["email"] == "user1@example.com" and user["display_name"] == "王小明"
    assert user["member_tier"] == "free" and user["role"] == "user" and user["status"] == "active"
    assert user["email_verified"] is False and user["has_password"] is True and user["google_linked"] is False
    assert user["age_confirmed"] is True and user["age_group"] == "adult"
    assert not any(secret in user for secret in AGE_SECRETS)


def test_a_suspended_members_cookie_no_longer_works(app):
    client = member_client(app, status=STATUS_SUSPENDED)
    assert client.get("/api/auth/me").status_code == 401


# =============================================================================
# 補填生日與年齡區間
# =============================================================================

def unverified_member_client(app):
    """沒有年齡資料的帳號（例如剛以 Google 建立的玩家；直接寫入資料庫，再寫入登入 cookie）。"""
    with db(app) as s:
        add_user(s, 1, age=None)
    client = login(app.test_client())
    user = client.get("/api/auth/me").get_json()["user"]
    assert user["age_confirmed"] is False and user["restriction"] == "age_pending" and user["age_group"] is None
    return client


def test_an_account_without_age_data_sees_age_confirmed_false_in_me(app):
    client = unverified_member_client(app)
    user = client.get("/api/auth/me").get_json()["user"]
    assert user["age_confirmed"] is False and user["age_group"] is None


@pytest.mark.parametrize(("age", "group"), [(15, "minor"), (30, "adult")])
def test_verify_age_records_everything_and_returns_the_fresh_profile_without_the_birthday(app, age, group):
    client = unverified_member_client(app)
    response = client.post("/api/auth/verify-age", json=age_fields(age))
    assert response.status_code == 200
    user = response.get_json()["user"]
    assert set(user) == PROFILE_KEYS and user["age_confirmed"] is True and user["age_group"] == group
    assert not any(secret in user for secret in AGE_SECRETS)
    assert birth_date_for_age(age).isoformat() not in response.get_data(as_text=True)
    me = client.get("/api/auth/me").get_json()["user"]                                       # 登入狀態沒有失效
    assert me["age_confirmed"] is True and me["age_group"] == group
    (stored,) = users_in(app)
    assert stored.birth_date == birth_date_for_age(age) and stored.age_range and stored.age_policy_version


@pytest.mark.parametrize(
    ("body", "code", "fields"),
    [
        ({}, "validation_error", {"birth_date", "age_range"}),
        ({"birth_date": "2026-02-30", "age_range": "25_34"}, "validation_error", {"birth_date"}),
        (age_fields(30, age_range=None), "validation_error", {"age_range"}),
        (age_fields(30, age_range="13_17"), "age_mismatch", None),
    ],
)
def test_verify_age_with_invalid_input_is_a_400_and_writes_nothing(app, body, code, fields):
    client = unverified_member_client(app)
    response = client.post("/api/auth/verify-age", json=body)
    assert response.status_code == 400
    error = response.get_json()["error"]
    assert error["code"] == code and (set(error["fields"]) == fields if fields else "fields" not in error)
    (stored,) = users_in(app)
    assert stored.birth_date is None and stored.age_range is None and stored.age_confirmed_at is None


def test_a_mismatch_at_the_age_step_gets_the_generic_message_and_the_strike_survives_the_400(app):
    client = unverified_member_client(app)
    response = client.post("/api/auth/verify-age", json=age_fields(30, age_range="13_17"))
    assert response.status_code == 400
    error = response.get_json()["error"]
    assert error == {"code": "age_mismatch", "message": "資料有誤，請重新填寫。"}
    with db(app) as s:
        assert s.query(LockStrike).count() == 1                       # 4xx 的請求也保留計次


def test_five_mismatches_lock_the_account_and_the_next_request_is_account_locked(app):
    client = unverified_member_client(app)
    for _ in range(5):
        assert client.post("/api/auth/verify-age", json=age_fields(30, age_range="13_17")).status_code == 400
    with db(app) as s:
        assert s.query(User).one().locked_at is not None
    again = client.get("/api/cs/conversations")
    assert again.status_code == 403 and again.get_json()["error"]["code"] == "account_locked"
    me = client.get("/api/auth/me").get_json()["user"]
    assert me["restriction"] == "locked"


@pytest.mark.parametrize("body", [age_fields(12), age_fields(12, age_range="18_24"), age_fields(30, age_range="under_13")])
def test_a_player_under_13_at_the_age_step_is_erased_and_signed_out_with_a_generic_400(app, body, log_lines):
    client = unverified_member_client(app)
    response = client.post("/api/auth/verify-age", json=body)
    assert response.status_code == 400
    error = response.get_json()["error"]
    assert error["code"] == "age_not_eligible" and "fields" not in error
    assert users_in(app) == []                                                        # 帳號與個資已抹除
    assert client.get("/api/auth/me").status_code == 401                              # 目前裝置的登入狀態已清除
    assert any(h.startswith("session=;") for h in response.headers.getlist("Set-Cookie"))
    assert body["birth_date"] not in response.get_data(as_text=True) + log_lines.raw()


def test_an_admin_under_13_at_the_age_step_is_refused_but_stays_signed_in(app):
    with db(app) as s:
        admin = add_user(s, 9, age=None, role="admin")
        client = logged_in_client(app, admin)
    response = client.post("/api/auth/verify-age", json=age_fields(12))
    assert response.status_code == 400 and response.get_json()["error"]["code"] == "age_not_eligible"
    assert len(users_in(app)) == 1 and client.get("/api/auth/me").status_code == 200


def test_verify_age_needs_a_login(app, client):
    response = client.post("/api/auth/verify-age", json=age_fields(30))
    assert response.status_code == 401 and response.get_json()["error"]["code"] == "unauthorized"


def test_the_old_confirm_adult_endpoint_no_longer_exists(app):
    client = unverified_member_client(app)
    assert client.post("/api/auth/confirm-adult", json={"confirm_adult": True}).status_code == 404
    (stored,) = users_in(app)
    assert stored.age_confirmed_at is None


def test_verify_age_twice_succeeds_and_keeps_the_first_record(app):
    client = unverified_member_client(app)
    assert client.post("/api/auth/verify-age", json=age_fields(30)).status_code == 200
    (first,) = users_in(app)
    recorded = (first.birth_date, first.age_range, first.age_confirmed_at, first.age_policy_version)
    assert client.post("/api/auth/verify-age", json=age_fields(15)).status_code == 200      # 內容不同仍成功，但不改動
    (again,) = users_in(app)
    assert (again.birth_date, again.age_range, again.age_confirmed_at, again.age_policy_version) == recorded


def test_verify_age_without_csrf_is_rejected_and_writes_nothing(app):
    unverified_member_client(app)
    raw = raw_client(app)
    response = raw.post("/api/auth/verify-age", json=age_fields(30), headers={"Origin": "http://localhost"})
    assert response.status_code == 403
    (stored,) = users_in(app)
    assert stored.birth_date is None


def test_verify_age_only_changes_the_callers_own_account(app):
    with db(app) as s:
        add_user(s, 2, age=None)
    client = unverified_member_client(app)
    client.post("/api/auth/verify-age", json=age_fields(30))
    done = {user.email: user.birth_date is not None for user in users_in(app)}
    assert done == {"user1@example.com": True, "user2@example.com": False}


def test_no_auth_response_ever_contains_the_birthday_or_the_range(app):
    """走一遍查詢與補填，所有回應文字都不含生日、區間代碼與其欄位名稱。"""
    client = member_client(app, age=15)
    texts = [client.get("/api/auth/me").get_data(as_text=True)]
    with db(app) as s:
        add_user(s, 5, age=None)
    other = login(app.test_client(), email="user5@example.com")
    texts.append(other.post("/api/auth/verify-age", json=age_fields(40)).get_data(as_text=True))
    texts.append(other.post("/api/auth/verify-age", json=age_fields(40, age_range="13_17")).get_data(as_text=True))
    joined = " ".join(texts)
    for secret in (birth_date_for_age(15).isoformat(), birth_date_for_age(40).isoformat(), "13_17", "35_44", *AGE_SECRETS):
        assert secret not in joined, secret


# =============================================================================
# 登出
# =============================================================================

def test_logout_ends_this_device_only_and_is_idempotent(app):
    this_device = member_client(app)
    other_device = login(app.test_client())
    assert this_device.post("/api/auth/logout").status_code == 200
    assert this_device.get("/api/auth/me").status_code == 401
    assert other_device.get("/api/auth/me").status_code == 200
    assert this_device.post("/api/auth/logout").status_code == 200       # 已經登出，再登出也成功


def test_logout_all_ends_every_device_including_this_one(app):
    this_device = member_client(app)
    other_device = login(app.test_client())
    response = this_device.post("/api/auth/logout-all")
    assert response.status_code == 200
    assert this_device.get("/api/auth/me").status_code == 401
    assert other_device.get("/api/auth/me").status_code == 401
    assert login(this_device).get("/api/auth/me").status_code == 200       # 之後以新的版本重新登入（Google）即可


def test_logout_all_requires_a_login(client):
    assert client.post("/api/auth/logout-all").status_code == 401


# =============================================================================
# 變更密碼
# =============================================================================

def test_change_password_keeps_this_device_and_logs_out_the_others(app):
    """變更密碼只有管理員有意義（玩家沒有密碼）；端點本身對任何已設定密碼的帳號運作。"""
    this_device = member_client(app, role="admin")
    other_device = login(app.test_client())
    response = this_device.post(
        "/api/auth/change-password", json={"current_password": GOOD_PASSWORD, "new_password": NEW_PASSWORD}
    )
    assert response.status_code == 200
    assert this_device.get("/api/auth/me").status_code == 200
    assert other_device.get("/api/auth/me").status_code == 401


def test_changing_the_password_keeps_the_page_usable_because_the_csrf_token_is_not_replaced(app):
    """帳號頁在變更密碼後不會重新載入：頁面上的權杖必須仍然有效，否則之後的登出會被擋成 403。"""
    with db(app) as s:
        add_user(s, 1, role="admin")
    client = raw_client(app)
    client.get("/")
    token = client.get_cookie("csrf").value
    login(client)
    headers = {"Origin": "http://localhost", "X-CSRF-Token": token}
    changed = client.post("/api/auth/change-password", json={"current_password": GOOD_PASSWORD, "new_password": NEW_PASSWORD}, headers=headers)
    assert changed.status_code == 200
    assert client.get_cookie("csrf").value == token           # 沒有被換掉
    assert client.post("/api/auth/logout", headers=headers).status_code == 200     # 仍可用同一個權杖繼續操作


def test_change_password_with_a_wrong_current_password_is_a_400_and_changes_nothing(app):
    client = member_client(app, role="admin")
    response = client.post("/api/auth/change-password", json={"current_password": "not-mine-at-all", "new_password": NEW_PASSWORD})
    assert response.status_code == 400
    assert response.get_json()["error"]["fields"] == {"current_password": "目前的密碼不正確。"}
    assert client.get("/api/auth/me").status_code == 200                       # token_version 沒變，仍登入


def test_change_password_requires_a_login(client):
    response = client.post("/api/auth/change-password", json={"current_password": GOOD_PASSWORD, "new_password": NEW_PASSWORD})
    assert response.status_code == 401


def test_a_google_only_account_cannot_change_a_password_this_way(app, client):
    with db(app) as s:
        add_user(s, 1, password=None)
    # 純 Google 帳號沒有密碼可登入；以測試用的登入狀態直接建立 session
    login(client)
    response = client.post("/api/auth/change-password", json={"current_password": "anything-at-all", "new_password": NEW_PASSWORD})
    assert response.status_code == 400 and response.get_json()["error"]["code"] == "no_password"


# =============================================================================
# 失敗計數：變更密碼時輸入錯誤的目前密碼也累積（4xx 之後計數仍在）
# =============================================================================

def test_wrong_current_passwords_when_changing_the_password_accumulate_over_http(app):
    client = member_client(app, role="admin")
    for _ in range(3):
        client.post("/api/auth/change-password", json={"current_password": "not-mine-at-all", "new_password": NEW_PASSWORD})
    with db(app) as s:
        assert s.query(User).one().failed_login_count == 3


# =============================================================================
# 安全事件
# =============================================================================

def test_the_whole_flow_is_logged_as_events_without_personal_data(app, log_lines):
    with db(app) as s:
        add_user(s, 1, role="admin", email="log.me@example.com", email_canonical="log.me@example.com", password="log-me-password-xyz")
    client = login(app.test_client(), email="log.me@example.com")
    client.post("/api/auth/change-password", json={"current_password": "wrong-one-entirely", "new_password": "another-new-password-1"})
    client.post("/api/auth/change-password", json={"current_password": "log-me-password-xyz", "new_password": "another-new-password-1"})
    client.post("/api/auth/logout")
    login(client, email="log.me@example.com")
    client.post("/api/auth/logout-all")
    events = [line["event"] for line in log_lines() if "event" in line]
    assert events == ["auth.password_change_refused", "auth.password_changed", "auth.logout", "auth.logout_all"]
    raw = log_lines.raw()
    for secret in ("log.me@example.com", "log-me-password-xyz", "wrong-one-entirely", "another-new-password-1"):
        assert secret not in raw


def test_events_carry_the_internal_user_id_when_known(app, log_lines):
    client = member_client(app)
    client.post("/api/auth/logout")
    logout = next(line for line in log_lines() if line.get("event") == "auth.logout")
    assert isinstance(logout["user_id"], int)
