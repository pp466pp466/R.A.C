"""rpg-notebook 規格：`GET`／`PUT /api/game/sessions/<id>/notebook` 與存檔快照的記事簿摘要（HTTP 這一層）。

用測試應用程式（SQLite）與直接寫入的登入狀態；容量、清理、規則層、版本與已完結的細節由 `test_notebook_service` 涵蓋（含真實 SQL Server），
這裡驗證的是 HTTP：誰能呼叫、CSRF 與 Origin、狀態碼與錯誤代碼、錯誤回應只帶該帶的欄位、禁止快取、沒有供應商也能用。
"""
import json
import logging

import pytest
from sqlalchemy import select

from app.models import RpgSession, User
from app.repositories.session_repository import SessionRepository
from tests.support.auth import FakeClock, add_user, sign_in_as
from tests.support.client import raw_client
from tests.support.game import BAD_SENTENCE
from tests.support.idor import EndpointSpec, check_endpoint
from tests.test_game_api import GHOST, db, make_save, player
from tests.test_llm_providers_by_purpose import BOTH, GEMINI_KEY, make_app

SETTING = "師妹的父親是宗門長老，主角答應三年內回來"


@pytest.fixture()
def app():
    return make_app({**BOTH})


def url(save_id: str) -> str:
    return f"/api/game/sessions/{save_id}/notebook"


def put(client, save_id, text, version=1, **headers):
    return client.put(url(save_id), json={"text": text, "expected_version": version}, headers=headers or None)


def stored(app, save_id) -> tuple[str, int, int]:
    with db(app) as s:
        save = s.scalar(select(RpgSession).where(RpgSession.public_id == save_id))
        return save.notebook_text, save.notebook_version, save.notebook_upto_turn


def set_stored(app, save_id, text, version=None):
    with db(app) as s:
        save = s.scalar(select(RpgSession).where(RpgSession.public_id == save_id))
        save.notebook_text = text
        if version is not None:
            save.notebook_version = version
        s.commit()


# ---- GET ----------------------------------------------------------------------------------------

def test_the_owner_reads_a_new_notebook_with_the_capacity_and_reserve(app):
    client, uid = player(app)
    save_id = make_save(app, uid)
    response = client.get(url(save_id))
    assert response.status_code == 200 and response.mimetype == "application/json"
    assert response.get_json() == {"notebook": {"text": "", "version": 1, "length": 0, "capacity": 2000, "reserve": 500, "full": False}}


def test_the_response_must_not_be_cached(app):
    client, uid = player(app)
    save_id = make_save(app, uid)
    assert client.get(url(save_id)).headers["Cache-Control"] == "no-store"
    assert put(client, save_id, SETTING).headers["Cache-Control"] == "no-store"
    assert put(client, save_id, "x", version=99).headers["Cache-Control"] == "no-store"            # 錯誤回應也一樣


def test_the_text_is_returned_in_full_to_the_owner(app):
    client, uid = player(app)
    save_id = make_save(app, uid)
    set_stored(app, save_id, "甲" * 2240, version=4)
    body = client.get(url(save_id)).get_json()["notebook"]
    assert body["text"] == "甲" * 2240 and body["full"] is True and body["version"] == 4


def test_an_ended_save_is_still_readable(app):
    client, uid = player(app)
    save_id = make_save(app, uid)
    set_stored(app, save_id, "完結前的設定")
    with db(app) as s:
        SessionRepository(s).end_session(uid, save_id, now=FakeClock()())
        s.commit()
    assert client.get(url(save_id)).get_json()["notebook"]["text"] == "完結前的設定"


def test_unauthenticated_requests_are_401_and_nothing_is_written(app):
    client, uid = player(app)
    save_id = make_save(app, uid)
    anonymous = app.test_client()
    assert anonymous.get(url(save_id)).status_code == 401
    assert put(anonymous, save_id, "x").status_code == 401
    assert stored(app, save_id) == ("", 1, 0)


def test_another_player_and_a_missing_save_get_the_same_404(app):
    _, uid = player(app)
    save_id = make_save(app, uid)
    intruder, _ = player(app, 2)
    responses = [intruder.get(url(save_id)), intruder.get(url(GHOST)), put(intruder, save_id, "別人的"), put(intruder, GHOST, "別人的")]
    assert [r.status_code for r in responses] == [404, 404, 404, 404]
    assert len({json.dumps(r.get_json(), sort_keys=True) for r in responses}) == 1
    assert stored(app, save_id) == ("", 1, 0)


def test_a_player_who_has_not_confirmed_the_age_is_refused(app):
    with db(app) as s:
        user = add_user(s, 3, password=None, age=None)
        uid, tv = user.id, user.token_version
    client = app.test_client()
    sign_in_as(client, uid, tv)
    save_id = make_save(app, player(app, 4)[1])
    for response in (client.get(url(save_id)), put(client, save_id, "x")):
        assert response.status_code == 403 and response.get_json()["error"]["code"] == "age_confirmation_required"


def two_players(app) -> tuple[User, User]:
    with db(app) as s:
        owner, intruder = add_user(s, 11, password=None), add_user(s, 12, password=None)
        owner_id, intruder_id = owner.id, intruder.id
        return s.get(User, owner_id), s.get(User, intruder_id)           # 取回時已載入屬性，Session 關閉後仍可讀


def make_owned_save(app, owner: User) -> str:
    return make_save(app, owner.id)


@pytest.mark.parametrize("spec", [
    EndpointSpec("GET", "/api/game/sessions/{id}/notebook", create_resource=make_owned_save, description="讀記事簿"),
    EndpointSpec(
        "PUT", "/api/game/sessions/{id}/notebook", create_resource=make_owned_save, json={"text": SETTING, "expected_version": 1}, description="儲存記事簿",
    ),
], ids=lambda spec: spec.description)
def test_someone_elses_notebook_is_a_byte_identical_404_to_a_missing_one(app, spec):
    owner, intruder = two_players(app)
    check_endpoint(app, spec, owner=owner, intruder=intruder)


# ---- PUT：成功 ----------------------------------------------------------------------------------

def test_a_save_returns_the_new_text_and_version_and_writes_it(app):
    client, uid = player(app)
    save_id = make_save(app, uid)
    response = put(client, save_id, SETTING)
    assert response.status_code == 200
    assert response.get_json() == {"notebook": {
        "text": SETTING, "version": 2, "length": len(SETTING), "capacity": 2000, "reserve": 500, "full": False,
    }}
    assert stored(app, save_id) == (SETTING, 2, 0)
    assert client.get(url(save_id)).get_json()["notebook"]["text"] == SETTING


def test_an_empty_text_clears_the_notebook(app):
    client, uid = player(app)
    save_id = make_save(app, uid)
    set_stored(app, save_id, "舊設定", version=2)
    assert put(client, save_id, "", version=2).get_json()["notebook"]["version"] == 3
    assert stored(app, save_id) == ("", 3, 0)


# ---- PUT：錯誤 ----------------------------------------------------------------------------------

def test_csrf_and_origin_are_checked_and_nothing_is_written(app):
    _, uid = player(app)
    save_id = make_save(app, uid)
    with db(app) as s:
        user = add_user(s, 5, password=None)
        other, tv = user.id, user.token_version
    save_other = make_save(app, other)
    client = raw_client(app)
    sign_in_as(client, other, tv)
    assert client.put(url(save_other), json={"text": "x", "expected_version": 1}, headers={"Origin": "http://localhost"}).status_code == 403   # 沒有權杖
    good = app.test_client()
    sign_in_as(good, other, tv)
    assert put(good, save_other, "x", Origin="http://evil.example").status_code == 403                                                      # 外部來源
    assert stored(app, save_other) == ("", 1, 0) and stored(app, save_id) == ("", 1, 0)


def test_too_long_is_422_with_only_the_two_integers(app):
    client, uid = player(app)
    save_id = make_save(app, uid)
    response = put(client, save_id, "甲" * 2001)
    body = response.get_json()
    assert response.status_code == 422 and body["error"]["code"] == "notebook_too_long"
    assert (body["length"], body["capacity"]) == (2001, 2000) and "甲" not in json.dumps(body, ensure_ascii=False)
    assert stored(app, save_id) == ("", 1, 0)


def test_a_full_notebook_cannot_be_cut_in_steps(app):
    client, uid = player(app)
    save_id = make_save(app, uid)
    set_stored(app, save_id, "甲" * 2240, version=3)
    assert put(client, save_id, "甲" * 2100, version=3).status_code == 422
    assert put(client, save_id, "甲" * 2240, version=3).status_code == 422
    assert put(client, save_id, "甲" * 2000, version=3).status_code == 200


def test_a_rules_layer_hit_is_422_with_only_the_category(app, caplog):
    client, uid = player(app)
    save_id = make_save(app, uid)
    with caplog.at_level(logging.DEBUG):
        response = put(client, save_id, SETTING + "\n" + BAD_SENTENCE)
    body = response.get_json()
    assert response.status_code == 422 and body["error"]["code"] == "notebook_blocked" and body["categories"] == ["minor_sexual"]
    raw = response.get_data(as_text=True)
    assert BAD_SENTENCE not in raw and SETTING not in raw and BAD_SENTENCE not in caplog.text and SETTING not in caplog.text
    assert stored(app, save_id) == ("", 1, 0)


def test_a_stale_version_is_409_with_the_latest_notebook(app):
    client, uid = player(app)
    save_id = make_save(app, uid)
    with db(app) as s:                                                                  # AI 在玩家編輯期間追加了內容
        repository = SessionRepository(s)
        assert repository.lock_notebook_for_capture(uid, save_id, 10) is not None
        assert repository.append_notebook(uid, save_id, text="AI 追加的重點", boundary=10) is True
        s.commit()
    response = put(client, save_id, "玩家以舊版本寫的", version=1)
    body = response.get_json()
    assert response.status_code == 409 and body["error"]["code"] == "notebook_conflict"
    assert body["notebook"] == {
        "text": "AI 追加的重點", "version": 2, "length": len("AI 追加的重點"), "capacity": 2000, "reserve": 500, "full": False,
    }
    assert stored(app, save_id) == ("AI 追加的重點", 2, 10)
    assert put(client, save_id, "確認後再寫的", version=body["notebook"]["version"]).status_code == 200


def test_an_ended_save_is_409_session_ended(app):
    client, uid = player(app)
    save_id = make_save(app, uid)
    with db(app) as s:
        SessionRepository(s).end_session(uid, save_id, now=FakeClock()())
        s.commit()
    response = put(client, save_id, "完結後想改")
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "session_ended"
    assert stored(app, save_id) == ("", 1, 0)


@pytest.mark.parametrize("payload", [
    {"expected_version": 1}, {"text": None, "expected_version": 1}, {"text": 5, "expected_version": 1}, {"text": "x"},
    {"text": "x", "expected_version": "1"}, {"text": "x", "expected_version": True}, {"text": "x", "expected_version": -1},
])
def test_a_malformed_body_is_400_and_changes_nothing(app, payload):
    client, uid = player(app)
    save_id = make_save(app, uid)
    response = client.put(url(save_id), json=payload)
    assert response.status_code == 400 and response.get_json()["error"]["code"] == "validation_error"
    assert stored(app, save_id) == ("", 1, 0)


def test_a_body_that_is_not_a_json_object_is_refused(app):
    client, uid = player(app)
    save_id = make_save(app, uid)
    assert client.put(url(save_id), data="text=x", content_type="application/x-www-form-urlencoded").status_code == 415
    assert client.put(url(save_id), json=["x"]).status_code == 400
    assert stored(app, save_id) == ("", 1, 0)


# ---- 其他 --------------------------------------------------------------------------------------

def test_the_notebook_works_without_an_rpg_provider():
    app = make_app({"GEMINI_API_KEY": GEMINI_KEY})
    client, uid = player(app)
    save_id = make_save(app, uid)
    assert put(client, save_id, SETTING).status_code == 200 and client.get(url(save_id)).status_code == 200


def test_the_snapshot_summary_follows_the_notebook_and_never_carries_the_text(app):
    client, uid = player(app)
    save_id = make_save(app, uid)
    put(client, save_id, "秘" * 1800)
    response = client.get(f"/api/game/sessions/{save_id}")
    assert response.get_json()["session"]["notebook"] == {"version": 2, "length": 1800, "capacity": 2000, "full": False}
    assert "秘" not in response.get_data(as_text=True)
    set_stored(app, save_id, "甲" * 2240)
    assert client.get(f"/api/game/sessions/{save_id}").get_json()["session"]["notebook"]["full"] is True


def test_the_snapshot_of_an_ended_save_keeps_the_summary(app):
    client, uid = player(app)
    save_id = make_save(app, uid)
    with db(app) as s:
        SessionRepository(s).end_session(uid, save_id, now=FakeClock()())
        s.commit()
    body = client.get(f"/api/game/sessions/{save_id}").get_json()["session"]
    assert body["ended"] is True and body["notebook"]["version"] == 1
