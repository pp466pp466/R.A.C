"""story-preference 規格：`GET`／`PUT /api/account/story-preference` 端到端（任務 3.2），以及偏好與內容分級互不影響（任務 3.3）。

涵蓋：預設與更新讀回、冪等、不合法的值（400 與欄位錯誤、偏好不變）、未登入 401、缺 CSRF 被拒、三種受限登入狀態各自的 403 代碼、
只能改自己的、回應不含個資且禁止快取、事件只有名稱與編號、`/api/auth/me` 帶出目前值；
偏好不改變生效的內容模式、可開存檔的劇本集合與內容模式 API 的回應，年齡確認與切換內容模式也不改變偏好。
"""
import json

import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base, User
from app.models.user import ROLE_ADMIN, STATUS_DELETION_PENDING
from app.repositories.script_repository import ScriptRepository
from app.services.access_policy import AccessPolicy
from app.services.current_user import CurrentUser
from app.services.startable import StartableVersions
from tests.support.age import age_fields
from tests.support.auth import START, FakeClock, add_user, sign_in_as
from tests.support.client import raw_client
from tests.support.idor import logged_in_client
from tests.support.script_data import validated

URL = "/api/account/story-preference"
CONTENT_MODE_URL = "/api/account/content-mode"
EVENT = "account.story_preference_changed"


@pytest.fixture()
def app():
    application = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


def seed(app, n: int = 1, **overrides) -> User:
    with Session(app.extensions["db_engine"]) as s:
        user = add_user(s, n, **overrides)
        s.refresh(user)
        s.expunge(user)
        return user


def stored(app, user_id: int) -> str:
    with Session(app.extensions["db_engine"]) as s:
        return s.get(User, user_id).story_preference


def put(client, value):
    return client.put(URL, json={"story_preference": value})


def error(response) -> dict:
    return response.get_json()["error"]


# ---- 查詢與更新 -----------------------------------------------------------------------------

def test_a_new_player_reads_all_and_the_response_is_json_that_is_never_cached(app):
    client = logged_in_client(app, seed(app))
    response = client.get(URL)
    assert response.status_code == 200 and response.mimetype == "application/json"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.get_json() == {"ok": True, "story_preference": "all"}


@pytest.mark.parametrize("value", ["male", "female", "all"])
def test_a_player_can_update_the_preference_and_read_it_back(app, value):
    user = seed(app)
    client = logged_in_client(app, user)
    response = put(client, value)
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    assert response.get_json() == {"ok": True, "story_preference": value}
    assert client.get(URL).get_json()["story_preference"] == value and stored(app, user.id) == value


def test_updating_to_the_current_value_succeeds_again(app):
    user = seed(app)
    client = logged_in_client(app, user)
    assert put(client, "female").status_code == 200
    again = put(client, "female")
    assert again.status_code == 200 and again.get_json()["story_preference"] == "female" and stored(app, user.id) == "female"


def test_the_account_profile_reports_the_current_preference(app):
    user = seed(app)
    client = logged_in_client(app, user)
    assert client.get("/api/auth/me").get_json()["user"]["story_preference"] == "all"
    put(client, "male")
    assert client.get("/api/auth/me").get_json()["user"]["story_preference"] == "male"


# ---- 不合法的輸入 ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value", ["Male", "FEMALE", "All", " male", "male ", "boys", "", 1, 0, True, None, ["male"], {"v": "male"}],
    ids=repr,
)
def test_a_value_that_is_not_exactly_one_of_the_three_is_a_400_with_the_field_and_changes_nothing(app, value):
    user = seed(app)
    client = logged_in_client(app, user)
    put(client, "female")
    response = put(client, value)
    assert response.status_code == 400
    assert error(response)["code"] == "validation_error" and set(error(response)["fields"]) == {"story_preference"}
    assert stored(app, user.id) == "female"


def test_a_missing_field_is_a_400_with_the_field_and_changes_nothing(app):
    user = seed(app)
    client = logged_in_client(app, user)
    put(client, "male")
    response = client.put(URL, json={"other": "female"})
    assert response.status_code == 400 and set(error(response)["fields"]) == {"story_preference"}
    assert stored(app, user.id) == "male"


def test_the_error_does_not_echo_the_rejected_input(app):
    client = logged_in_client(app, seed(app))
    response = put(client, "ECHO-ME-1234")
    assert response.status_code == 400 and "ECHO-ME-1234" not in response.get_data(as_text=True)


def test_the_body_must_be_a_json_object(app):
    client = logged_in_client(app, seed(app))
    assert client.put(URL, data="story_preference=male").status_code == 415
    assert client.put(URL, json=["male"]).status_code == 400


# ---- 身分、CSRF 與受限登入狀態 --------------------------------------------------------------

def test_anonymous_requests_get_401(app):
    anonymous = app.test_client()
    assert anonymous.get(URL).status_code == 401
    assert anonymous.put(URL, json={"story_preference": "male"}).status_code == 401


def test_a_put_without_a_csrf_token_is_refused_and_changes_nothing(app):
    user = seed(app)
    cookie = logged_in_client(app, user).get_cookie("session").value
    forged = raw_client(app)
    forged.set_cookie("session", cookie)
    response = forged.put(URL, json={"story_preference": "male"}, headers={"Origin": "http://localhost"})
    assert response.status_code == 403
    assert stored(app, user.id) == "all"


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
    for response in (client.get(URL), put(client, "male")):
        assert response.status_code == 403 and error(response)["code"] == code
    assert stored(app, user.id) == "all"


def test_an_admin_who_has_not_confirmed_the_age_can_still_set_the_preference(app):
    """偏好不涉及模型，所以不過年齡閘門（那是「能不能呼叫模型」的檢查）；管理員不會進入受限登入狀態。"""
    admin = seed(app, age=None, role=ROLE_ADMIN)
    client = logged_in_client(app, admin)
    assert put(client, "female").status_code == 200 and stored(app, admin.id) == "female"


def test_a_player_can_only_change_their_own_preference(app):
    alice, bob = seed(app, 1), seed(app, 2)
    client = logged_in_client(app, alice)
    response = client.put(
        URL, json={"story_preference": "male", "user_id": bob.id, "public_id": bob.public_id, "id": bob.id}
    )
    assert response.status_code == 200
    assert (stored(app, alice.id), stored(app, bob.id)) == ("male", "all")


# ---- 回應與日誌不含個資 -----------------------------------------------------------------------

def test_the_responses_contain_only_the_preference_and_no_personal_data(app):
    user = seed(app, display_name="王小明")
    client = logged_in_client(app, user)
    bodies = [client.get(URL).get_data(as_text=True), put(client, "male").get_data(as_text=True)]
    for body in bodies:
        assert set(json.loads(body)) == {"ok", "story_preference"}
        for secret in (user.email, user.public_id, "王小明", "birth", "age_range", "25_34", str(user.id) + '"'):
            assert secret not in body


def test_a_successful_update_logs_the_event_name_and_the_id_only(app, log_lines):
    user = seed(app, display_name="機密姓名")
    client = logged_in_client(app, user)
    put(client, "female")
    events = [line for line in log_lines() if line.get("event") == EVENT]
    assert [e["user_id"] for e in events] == [user.id]
    for secret in ("機密姓名", user.email, "female"):
        assert secret not in json.dumps(events, ensure_ascii=False)


def test_a_rejected_value_logs_no_change_event(app, log_lines):
    client = logged_in_client(app, seed(app))
    put(client, "nonsense")
    assert [line for line in log_lines() if line.get("event") == EVENT] == []


# ---- 偏好與內容分級互不影響（任務 3.3）-------------------------------------------------------------

def content_mode_of(client) -> dict:
    response = client.get(CONTENT_MODE_URL)
    assert response.status_code == 200
    return response.get_json()


def startable_ids(app, user: User, versions) -> set[str]:
    """這位玩家可以拿來開存檔的版本（對每個版本逐一解析，被內容上限擋下的不算）。"""
    from app.services.errors import ContentTierRestricted, NotFound

    with Session(app.extensions["db_engine"]) as s:
        current = CurrentUser.from_model(s.get(User, user.id))
        resolver = StartableVersions(scripts=ScriptRepository(s), policy=AccessPolicy(clock=FakeClock()))
        found = set()
        for public_id in versions:
            try:
                resolver.resolve(current, public_id)
            except (NotFound, ContentTierRestricted):
                continue
            found.add(public_id)
        return found


@pytest.fixture()
def catalogue(app):
    """三個上架的劇本（安全、一般、成人各一）與其版本的對外識別碼。"""
    author = seed(app, 9, email="author@example.com", email_canonical="author@example.com")
    ids: list[str] = []
    with Session(app.extensions["db_engine"]) as s:
        scripts = ScriptRepository(s)
        for index, tier in enumerate(("safe", "standard", "adult")):
            audience = ("male", "female", "all")[index]
            result = validated(content_tier=tier, audience=audience, title=f"劇本{index}")
            version = scripts.create_script(
                author.id, author_name="筆名", author_code="abc123", content=result.content, content_hash=result.content_hash,
                total_chars=result.total_chars,
            )
            scripts.submit_for_review(author.id, version.public_id)
            scripts.approve_version(version.public_id, now=START)
            ids.append(version.public_id)
        s.commit()
    return ids


def test_changing_the_preference_changes_neither_the_effective_mode_nor_the_playable_scripts_nor_the_content_mode_answer(app, catalogue):
    user = seed(app, age=30)
    client = logged_in_client(app, user)
    before = (content_mode_of(client), startable_ids(app, user, catalogue))
    assert before[1] == {catalogue[0], catalogue[1]}                      # 一般模式的成年玩家：成人劇本不可玩（與偏好無關）
    for value in ("male", "female", "all"):
        assert put(client, value).status_code == 200
        assert (content_mode_of(client), startable_ids(app, user, catalogue)) == before, value


def test_a_minor_stays_in_the_safe_mode_whatever_the_preference_is(app, catalogue):
    user = seed(app, age=15)
    client = logged_in_client(app, user)
    for value in ("male", "female", "all"):
        put(client, value)
        state = content_mode_of(client)["content_mode"]
        assert state["effective"] == "safe" and state["selectable"] == {"safe": True, "standard": False, "adult": False}
        assert startable_ids(app, user, catalogue) == {catalogue[0]}


def test_switching_the_content_mode_does_not_change_the_preference(app):
    user = seed(app, age=30)
    client = logged_in_client(app, user)
    put(client, "female")
    for mode in ("safe", "standard"):
        assert client.put(CONTENT_MODE_URL, json={"mode": mode}).status_code == 200
        assert stored(app, user.id) == "female"


def test_completing_the_age_confirmation_does_not_change_the_preference(app):
    user = seed(app, age=None)
    assert stored(app, user.id) == "all"
    # 補填年齡之前就不能呼叫會碰到年齡閘門的功能，但偏好不受影響：直接在資料庫設定，再走年齡確認
    with Session(app.extensions["db_engine"]) as s:
        s.get(User, user.id).story_preference = "male"
        s.commit()
    client = app.test_client()
    sign_in_as(client, user.id, user.token_version)
    assert client.post("/api/auth/verify-age", json=age_fields(30)).status_code == 200
    assert stored(app, user.id) == "male"
    assert client.get(URL).get_json()["story_preference"] == "male"
