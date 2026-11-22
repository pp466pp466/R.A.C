"""script-anonymous-retention 規格「偏好預設關閉且只有本人能讀寫」「刪除帳號視窗再次確認偏好」（任務 4.1 的 HTTP 層）：
`GET`／`PUT /api/account/script-retention` 與 `POST /api/account/delete` 的選填欄位 `keep_published_scripts` 端到端。

涵蓋：預設與更新讀回、冪等、`live_count` 只算目前上架的版本、非布林的值（400 與 `keep` 欄位、偏好不變）、未登入 401、缺 CSRF 被拒、
三種受限登入狀態各自的 403 代碼、管理員一律 403、只能改自己的、回應只有三個鍵且不含劇本文字、日誌沒有劇本文字、
`/api/auth/me` 沒有新增欄位；刪除申請時一併寫入偏好（確認文字錯誤不寫、非布林 400 且不計入登入失敗、沒帶或 `null` 不碰偏好、管理員不能申請）。
"""
import json

import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base, User
from app.models.script import VERSION_DRAFT, VERSION_PUBLISHED, VERSION_REMOVED
from app.models.user import ROLE_ADMIN, STATUS_DELETION_PENDING
from app.repositories.script_repository import ScriptRepository
from tests.support.auth import GOOD_PASSWORD, START, FakeClock, add_user
from tests.support.client import raw_client
from tests.support.idor import logged_in_client
from tests.support.script_review import put_in_status

URL = "/api/account/script-retention"
DELETE_URL = "/api/account/delete"
CONFIRM = "刪除我的帳號"
SECRET_TITLE = "只該出現在劇本裡的標題ZXQ"


@pytest.fixture()
def app():
    application = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


def seed(app, n: int = 1, *, live: int = 0, **overrides) -> User:
    """一位使用者；`live` 個上架中的版本（各自一份劇本），另有一個下架的與一個草稿（不應被算進 `live_count`）。"""
    with Session(app.extensions["db_engine"]) as s:
        user = add_user(s, n, **overrides)
        scripts = ScriptRepository(s)
        for index in range(live):
            put_in_status(scripts, user, VERSION_PUBLISHED, title=f"{SECRET_TITLE}{index}")
        if live:
            put_in_status(scripts, user, VERSION_REMOVED, title=f"{SECRET_TITLE}下架")
            put_in_status(scripts, user, VERSION_DRAFT, title=f"{SECRET_TITLE}草稿")
        s.commit()
        s.refresh(user)
        s.expunge(user)
        return user


def stored(app, user_id: int) -> bool:
    with Session(app.extensions["db_engine"]) as s:
        return s.get(User, user_id).keep_published_scripts


def put(client, value):
    return client.put(URL, json={"keep": value})


def error(response) -> dict:
    return response.get_json()["error"]


# ---- 查詢與更新 -----------------------------------------------------------------------------

def test_a_new_player_reads_false_and_the_response_is_json_that_is_never_cached(app):
    client = logged_in_client(app, seed(app))
    response = client.get(URL)
    assert response.status_code == 200 and response.mimetype == "application/json"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.get_json() == {"ok": True, "keep": False, "live_count": 0}


@pytest.mark.parametrize("value", [True, False])
def test_a_player_can_update_the_preference_and_read_it_back(app, value):
    user = seed(app)
    client = logged_in_client(app, user)
    response = put(client, value)
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    assert response.get_json() == {"ok": True, "keep": value, "live_count": 0}
    assert client.get(URL).get_json()["keep"] is value and stored(app, user.id) is value


def test_turning_it_on_and_then_off_again_restores_false(app):
    user = seed(app)
    client = logged_in_client(app, user)
    put(client, True)
    assert put(client, False).get_json()["keep"] is False and stored(app, user.id) is False


def test_updating_to_the_current_value_succeeds_again(app):
    user = seed(app)
    client = logged_in_client(app, user)
    assert put(client, True).status_code == 200
    again = put(client, True)
    assert again.status_code == 200 and again.get_json()["keep"] is True and stored(app, user.id) is True


def test_live_count_counts_only_the_live_versions_of_the_caller(app):
    author = seed(app, 1, live=2)
    other = seed(app, 2, live=3)
    client = logged_in_client(app, author)
    assert client.get(URL).get_json()["live_count"] == 2                      # 下架的與草稿不算，別人的也不算
    assert put(client, True).get_json() == {"ok": True, "keep": True, "live_count": 2}
    assert logged_in_client(app, other).get(URL).get_json() == {"ok": True, "keep": False, "live_count": 3}


def test_the_profile_does_not_carry_the_new_preference(app):
    """設計 D7：`CurrentUser` 快照不新增欄位，帳號頁自己取得偏好。"""
    client = logged_in_client(app, seed(app))
    put(client, True)
    assert "keep_published_scripts" not in client.get("/api/auth/me").get_data(as_text=True)


# ---- 不合法的輸入 ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value", ["true", "false", "yes", "on", "", 1, 0, None, [True], {"keep": True}], ids=repr,
)
def test_a_value_that_is_not_a_real_boolean_is_a_400_with_the_field_and_changes_nothing(app, value):
    user = seed(app)
    client = logged_in_client(app, user)
    put(client, True)
    response = put(client, value)
    assert response.status_code == 400
    assert error(response)["code"] == "validation_error" and set(error(response)["fields"]) == {"keep"}
    assert stored(app, user.id) is True


def test_a_missing_field_is_a_400_with_the_field_and_changes_nothing(app):
    user = seed(app)
    client = logged_in_client(app, user)
    put(client, True)
    response = client.put(URL, json={"other": False})
    assert response.status_code == 400 and set(error(response)["fields"]) == {"keep"}
    assert stored(app, user.id) is True


def test_the_error_does_not_echo_the_rejected_input(app):
    client = logged_in_client(app, seed(app))
    response = put(client, "ECHO-ME-1234")
    assert response.status_code == 400 and "ECHO-ME-1234" not in response.get_data(as_text=True)


def test_the_body_must_be_a_json_object(app):
    client = logged_in_client(app, seed(app))
    assert client.put(URL, data="keep=true").status_code == 415
    assert client.put(URL, json=[True]).status_code == 400


# ---- 身分、CSRF、受限登入狀態與管理員 ----------------------------------------------------------

def test_anonymous_requests_get_401(app):
    anonymous = app.test_client()
    assert anonymous.get(URL).status_code == 401
    assert anonymous.put(URL, json={"keep": True}).status_code == 401


def test_a_put_without_a_csrf_token_is_refused_and_changes_nothing(app):
    user = seed(app)
    cookie = logged_in_client(app, user).get_cookie("session").value
    forged = raw_client(app)
    forged.set_cookie("session", cookie)
    response = forged.put(URL, json={"keep": True}, headers={"Origin": "http://localhost"})
    assert response.status_code == 403
    assert stored(app, user.id) is False


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        (dict(age=30, locked_at=START, lock_reason="rate_limited"), "account_locked"),
        (dict(age=30, status=STATUS_DELETION_PENDING, deletion_requested_at=START, status_before_deletion="active"), "account_deletion_pending"),
        (dict(age=None), "age_confirmation_required"),
    ],
    ids=["locked", "deletion-pending", "age-pending"],
)
def test_a_restricted_session_gets_403_with_its_code_and_the_preference_is_unchanged(app, overrides, code):
    user = seed(app, **overrides)
    client = logged_in_client(app, user)
    for response in (client.get(URL), put(client, True)):
        assert response.status_code == 403 and error(response)["code"] == code
    assert stored(app, user.id) is False


@pytest.mark.parametrize("age", [30, None], ids=["age-confirmed", "age-not-confirmed"])
def test_an_admin_can_neither_read_nor_change_it(app, age):
    """管理員不能自助刪除帳號，這個偏好對他們沒有意義；不論有沒有完成年齡確認都一樣。"""
    admin = seed(app, role=ROLE_ADMIN, age=age)
    client = logged_in_client(app, admin)
    for response in (client.get(URL), put(client, True), put(client, "not-a-boolean")):
        assert response.status_code == 403 and error(response)["code"] == "admin_script_retention_forbidden"
    assert stored(app, admin.id) is False


def test_a_player_can_only_change_their_own_preference(app):
    alice, bob = seed(app, 1), seed(app, 2)
    client = logged_in_client(app, alice)
    response = client.put(URL, json={"keep": True, "user_id": bob.id, "public_id": bob.public_id, "id": bob.id})
    assert response.status_code == 200
    assert (stored(app, alice.id), stored(app, bob.id)) == (True, False)


# ---- 回應與日誌不含劇本文字或個資 -----------------------------------------------------------------

def test_the_responses_contain_only_three_keys_and_no_script_text_or_personal_data(app):
    user = seed(app, display_name="王小明", live=1)
    client = logged_in_client(app, user)
    bodies = [client.get(URL).get_data(as_text=True), put(client, True).get_data(as_text=True)]
    for body in bodies:
        assert set(json.loads(body)) == {"ok", "keep", "live_count"}
        for secret in (SECRET_TITLE, user.email, user.public_id, "王小明", "birth", "age_range", "25_34"):
            assert secret not in body


def test_nothing_about_the_scripts_reaches_the_log(app, log_lines):
    user = seed(app, display_name="機密姓名", live=1)
    client = logged_in_client(app, user)
    client.get(URL)
    put(client, True)
    put(client, "nonsense")
    text = json.dumps(log_lines(), ensure_ascii=False)
    for secret in (SECRET_TITLE, "機密姓名", user.email):
        assert secret not in text


# ---- 刪除視窗：申請刪除時一併寫入偏好 ------------------------------------------------------------------

def delete(client, **body):
    return client.post(DELETE_URL, json=body)


def test_requesting_deletion_with_keep_true_writes_the_preference_in_the_same_request(app):
    user = seed(app, live=1)
    response = delete(logged_in_client(app, user), password=GOOD_PASSWORD, keep_published_scripts=True)
    assert response.status_code == 200 and response.get_json()["ok"] is True
    assert stored(app, user.id) is True
    with Session(app.extensions["db_engine"]) as s:
        assert s.get(User, user.id).status == STATUS_DELETION_PENDING


def test_requesting_deletion_with_keep_false_turns_a_previous_on_off(app):
    user = seed(app, live=1, keep_published_scripts=True)
    response = delete(logged_in_client(app, user), password=GOOD_PASSWORD, keep_published_scripts=False)
    assert response.status_code == 200 and stored(app, user.id) is False


def test_a_google_only_account_can_send_the_field_with_the_confirmation_text(app):
    user = seed(app, password=None, live=1)
    response = delete(logged_in_client(app, user), confirm_text=CONFIRM, keep_published_scripts=True)
    assert response.status_code == 200 and stored(app, user.id) is True


@pytest.mark.parametrize("body", [{}, {"keep_published_scripts": None}], ids=["absent", "null"])
def test_leaving_the_field_out_or_null_keeps_the_existing_preference(app, body):
    user = seed(app, live=1, keep_published_scripts=True)
    response = delete(logged_in_client(app, user), password=GOOD_PASSWORD, **body)
    assert response.status_code == 200 and stored(app, user.id) is True


def test_a_wrong_confirmation_leaves_the_preference_and_the_account_alone(app):
    user = seed(app, live=1)
    response = delete(logged_in_client(app, user), password="definitely-not-the-password", keep_published_scripts=True)
    assert response.status_code == 400 and "password" in error(response)["fields"]
    assert stored(app, user.id) is False
    with Session(app.extensions["db_engine"]) as s:
        assert s.get(User, user.id).status != STATUS_DELETION_PENDING


def test_a_wrong_confirmation_text_leaves_the_preference_alone_for_a_google_only_account(app):
    user = seed(app, password=None, live=1)
    response = delete(logged_in_client(app, user), confirm_text="不對", keep_published_scripts=True)
    assert response.status_code == 400 and "confirm_text" in error(response)["fields"]
    assert stored(app, user.id) is False


@pytest.mark.parametrize("value", ["true", "yes", 1, 0, "", [True]], ids=repr)
def test_a_non_boolean_field_is_a_400_names_the_field_and_does_not_count_as_a_failed_login(app, value):
    user = seed(app, live=1)
    response = delete(logged_in_client(app, user), password=GOOD_PASSWORD, keep_published_scripts=value)
    assert response.status_code == 400 and set(error(response)["fields"]) == {"keep_published_scripts"}
    with Session(app.extensions["db_engine"]) as s:
        row = s.get(User, user.id)
        assert row.keep_published_scripts is False and row.failed_login_count == 0
        assert row.status != STATUS_DELETION_PENDING                         # 申請沒有成立


def test_an_admin_cannot_request_deletion_even_with_the_field(app):
    admin = seed(app, role=ROLE_ADMIN)
    response = delete(logged_in_client(app, admin), password=GOOD_PASSWORD, keep_published_scripts=True)
    assert response.status_code == 403 and stored(app, admin.id) is False
