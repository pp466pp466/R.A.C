"""member-registration 與 session-authentication 規格：`/api/auth/*` 端到端（註冊、登入、登出、查詢、變更密碼）。"""
import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base, LockStrike, User
from app.models.user import STATUS_PENDING_VERIFICATION, STATUS_SUSPENDED
from tests.support.age import age_fields, birth_date_for_age
from tests.support.auth import GOOD_PASSWORD, FakeClock, add_user
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


def register(client, **overrides):
    body = {
        "email": "new@example.com", "password": GOOD_PASSWORD, "display_name": "王小明",
        "accept_terms": True, **age_fields(30),
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
    response = register(client, email="bad", password="short", display_name="", accept_terms=False, birth_date=None, age_range=None)
    assert response.status_code == 400
    error = response.get_json()["error"]
    assert error["code"] == "validation_error"
    assert set(error["fields"]) == {"email", "password", "display_name", "accept_terms", "birth_date", "age_range"}
    assert users_in(app) == []


def test_register_without_the_birthday_or_range_creates_nothing_and_names_the_missing_field(app, client):
    assert set(register(client, birth_date=None).get_json()["error"]["fields"]) == {"birth_date"}
    assert set(register(client, age_range=None).get_json()["error"]["fields"]) == {"age_range"}
    payload = {"email": "new@example.com", "password": GOOD_PASSWORD, "display_name": "x", "accept_terms": True}
    omitted = client.post("/api/auth/register", json=payload)                 # 完全沒有帶年齡欄位
    assert omitted.status_code == 400
    assert set(omitted.get_json()["error"]["fields"]) == {"birth_date", "age_range"}
    assert users_in(app) == []


def test_register_with_a_mismatched_birthday_says_the_data_is_wrong_and_creates_nothing(app, client):
    response = register(client, **age_fields(30, age_range="13_17"))
    assert response.status_code == 400
    error = response.get_json()["error"]
    assert error["code"] == "age_mismatch" and error["message"] == "資料有誤，請重新註冊。" and "fields" not in error
    assert users_in(app) == []


def test_register_under_13_is_refused_with_its_own_code_and_nothing_is_stored(app, client):
    response = register(client, **age_fields(12))
    assert response.status_code == 400 and response.get_json()["error"]["code"] == "age_not_eligible"
    assert users_in(app) == []
    assert birth_date_for_age(12).isoformat() not in response.get_data(as_text=True)


def test_after_a_mismatch_the_same_email_and_password_register_straight_away(app, client):
    """資料有誤常是輸入太快：不封鎖信箱，使用者提醒後可以立刻以同一組資料重新註冊。"""
    for _ in range(3):
        assert register(client, **age_fields(30, age_range="13_17")).status_code == 400
    assert register(client).status_code == 200
    (user,) = users_in(app)
    assert user.email == "new@example.com" and user.birth_date == birth_date_for_age(30)
    assert login(client).status_code == 200


def test_register_records_the_birthday_and_range_and_keeps_them_out_of_the_response(app, client):
    response = register(client, **age_fields(15))
    assert response.get_json() == {"ok": True, "verification_required": False, "message": "註冊完成，請前往登入。"}
    (user,) = users_in(app)
    assert user.birth_date == birth_date_for_age(15) and user.age_range == "13_17"
    assert user.is_adult_at_confirmation is False and user.age_confirmed_at is not None and user.age_policy_version


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
    assert user["age_confirmed"] is True and user["age_group"] == "adult"            # 註冊時已完成
    assert not any(secret in user for secret in AGE_SECRETS)
    assert client.get_cookie("session") is not None


# =============================================================================
# 補填生日與年齡區間
# =============================================================================

def unverified_member_client(app):
    """升級前就存在、沒有年齡資料的帳號（直接寫入資料庫，再以 API 登入）。"""
    with db(app) as s:
        add_user(s, 1, age=None)
    client = app.test_client()
    user = login(client, email="user1@example.com").get_json()["user"]
    assert user["age_confirmed"] is False and user["restriction"] == "age_pending" and user["age_group"] is None
    return client


def test_an_account_without_age_data_sees_age_confirmed_false_in_login_and_me(app):
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
    """走一遍註冊、登入、查詢、補填，所有回應文字都不含生日、區間代碼與其欄位名稱。"""
    client = app.test_client()
    texts = [register(client, **age_fields(15)).get_data(as_text=True), login(client).get_data(as_text=True)]
    texts.append(client.get("/api/auth/me").get_data(as_text=True))
    with db(app) as s:
        add_user(s, 5, age=None)
    other = app.test_client()
    login(other, email="user5@example.com")
    texts.append(other.post("/api/auth/verify-age", json=age_fields(40)).get_data(as_text=True))
    texts.append(other.post("/api/auth/verify-age", json=age_fields(40, age_range="13_17")).get_data(as_text=True))
    joined = " ".join(texts)
    for secret in (birth_date_for_age(15).isoformat(), birth_date_for_age(40).isoformat(), "13_17", "35_44", *AGE_SECRETS):
        assert secret not in joined, secret


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
    client.post("/api/auth/register", json={"email": "keep@example.com", "password": GOOD_PASSWORD, "display_name": "x", "accept_terms": True, **age_fields(30)}, headers=headers)
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
