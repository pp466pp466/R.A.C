"""script-authoring 規格：`/api/scripts/*`——擁有者隔離（IDOR）、閘門、建立與額度、作者的內容模式上限、儲存時的規則層、
以內容雜湊偵測的編輯衝突、新草稿、刪除與撤回、筆名、範例與額度查詢、日誌不含劇本文字。

用測試應用程式與直接寫入的登入狀態；審查的模型層與送審另見 `test_script_review.py`、`test_script_submission.py`。
"""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base, RuntimeLimit, Script, ScriptSubmission, ScriptVersion, User
from app.repositories.script_repository import ScriptRepository
from app.services.script_seed_service import SEED_DIRECTORY, ScriptSeedService
from app.services.script_spec import validate_script
from tests.support.auth import FakeClock, add_user, sign_in_as
from tests.support.client import raw_client
from tests.support.idor import EndpointSpec, IdorFailure, check_endpoint, logged_in_client
from tests.support.script_data import valid, validated

UNIQUE = "獨特的劇本文字SCRIPT-API-SECRET-TEXT"


@pytest.fixture()
def app():
    application = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


def db(app) -> Session:
    return Session(app.extensions["db_engine"])


def author(app, n: int = 1, *, nickname: bool = True, **overrides):
    """已完成年齡確認、有暱稱的玩家，及其登入狀態。"""
    extra = dict(nickname="雲遊者", nickname_code=f"AB{n:02d}") if nickname else {}
    with db(app) as s:
        user = add_user(s, n, **{**extra, **overrides})
        uid, tv = user.id, user.token_version
    client = app.test_client()
    sign_in_as(client, uid, tv)
    return client


def user_row(app, n: int = 1) -> User:
    with db(app) as s:
        return s.scalar(select(User).where(User.email == f"user{n}@example.com"))


def set_limit(app, key: str, value: int) -> None:
    with db(app) as s:
        s.add(RuntimeLimit(key=key, int_value=value, updated_at=FakeClock()()))
        s.commit()


def create(client, **changes):
    return client.post("/api/scripts", json={"content": valid(**changes)})


def version_of(response) -> dict:
    return response.get_json()["version"]


def put(client, version, **changes):
    return client.put(
        f"/api/scripts/versions/{version['id']}", json={"content": valid(**changes), "base_hash": version["content_hash"]},
    )


def rows(app, model):
    with db(app) as s:
        return list(s.scalars(select(model)))


def publish(app, version_public_id: str, owner_n: int = 1) -> None:
    owner = user_row(app, owner_n)
    with db(app) as s:
        scripts = ScriptRepository(s)
        scripts.submit_for_review(owner.id, version_public_id)
        scripts.approve_version(version_public_id, now=FakeClock()())
        s.commit()


def set_status(app, version_public_id: str, owner_n: int, to: str) -> None:
    owner = user_row(app, owner_n)
    with db(app) as s:
        scripts = ScriptRepository(s)
        scripts.submit_for_review(owner.id, version_public_id)
        if to == "rejected":
            scripts.reject_version(version_public_id)
        s.commit()


# ---- 閘門 --------------------------------------------------------------------------------------

ENDPOINTS = [
    ("GET", "/api/scripts"), ("POST", "/api/scripts"), ("GET", "/api/scripts/example"), ("GET", "/api/scripts/limits"),
    ("GET", "/api/scripts/versions/x"), ("PUT", "/api/scripts/versions/x"), ("DELETE", "/api/scripts/versions/x"),
    ("POST", "/api/scripts/versions/x/withdraw"), ("POST", "/api/scripts/x/versions"), ("POST", "/api/scripts/x/author-name/refresh"),
]


@pytest.mark.parametrize(("method", "path"), ENDPOINTS)
def test_every_endpoint_needs_a_login(app, method, path):
    response = app.test_client().open(path, method=method, json={})
    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path"), ENDPOINTS)
def test_a_player_who_has_not_confirmed_the_age_gets_the_age_gate_code(app, method, path):
    client = author(app, age=None)
    response = client.open(path, method=method, json={})
    assert response.status_code == 403 and response.get_json()["error"]["code"] == "age_confirmation_required"


@pytest.mark.parametrize(("method", "path"), ENDPOINTS)
def test_an_admin_without_the_age_confirmation_cannot_use_them_either(app, method, path):
    client = author(app, role="admin", age=None)
    response = client.open(path, method=method, json={})
    assert response.status_code == 403 and response.get_json()["error"]["code"] == "age_confirmation_required"


@pytest.mark.parametrize(("method", "path"), ENDPOINTS)
def test_a_locked_player_is_refused(app, method, path):
    client = author(app, locked_at=FakeClock()(), lock_reason="rate_limited")
    response = client.open(path, method=method, json={})
    assert response.status_code == 403 and response.get_json()["error"]["code"] == "account_locked"


def test_a_post_without_the_csrf_token_is_refused(app):
    author(app)
    raw = raw_client(app)
    sign_in_as(raw, user_row(app).id, 1)
    assert raw.post("/api/scripts", json={"content": valid()}).status_code == 403
    assert rows(app, Script) == []


def test_every_response_is_marked_no_store(app):
    client = author(app)
    for response in (client.get("/api/scripts"), client.get("/api/scripts/limits"), create(client), client.get("/api/scripts/example")):
        assert "no-store" in response.headers.get("Cache-Control", "")


# ---- 建立 ----------------------------------------------------------------------------------------


def test_creating_a_script_makes_version_one_as_a_draft_with_the_pen_name_snapshot(app):
    client = author(app)
    response = create(client, title="我的第一個劇本")
    assert response.status_code == 201
    version = version_of(response)
    assert version["version_no"] == 1 and version["status"] == "draft" and version["title"] == "我的第一個劇本"
    assert version["script"]["author_name"] == "雲遊者" and version["script"]["author_code"] == "AB01" and version["script"]["author_locked"] is False
    assert version["content"]["title"] == "我的第一個劇本" and version["content_hash"] == validated(title="我的第一個劇本").content_hash
    with db(app) as s:
        script = s.scalar(select(Script))
        assert script.creator_id == user_row(app).id and script.author_name == "雲遊者"


def test_the_response_carries_public_ids_only(app):
    text = create(author(app)).get_data(as_text=True)
    data = json.loads(text)["version"]
    assert len(data["id"]) == 36 and len(data["script"]["id"]) == 36
    for forbidden in ("creator_id", "user_id", "live_script_id", "open_script_id", "review_hash", "review_verdict", "review_categories"):
        assert forbidden not in text


def test_a_player_without_a_nickname_cannot_create_a_script(app):
    response = create(author(app, nickname=False))
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "nickname_required"
    assert rows(app, Script) == []


def test_the_script_limit_of_the_tier_stops_new_scripts_but_keeps_the_existing_ones(app):
    set_limit(app, "script_limit.free", 2)
    client = author(app)
    assert create(client, title="一").status_code == 201 and create(client, title="二").status_code == 201
    third = create(client, title="三")
    assert third.status_code == 409 and third.get_json()["error"]["code"] == "script_limit_reached"
    assert len(rows(app, Script)) == 2
    set_limit(app, "script_limit.plus", 5)
    plus = author(app, 2, member_tier="plus")
    assert create(plus).status_code == 201


def test_lowering_the_limit_below_the_current_count_only_blocks_new_scripts(app):
    client = author(app)
    for index in range(3):
        assert create(client, title=f"劇本{index}").status_code == 201
    with db(app) as s:
        s.query(RuntimeLimit).filter_by(key="script_limit.free").delete()
        s.add(RuntimeLimit(key="script_limit.free", int_value=1, updated_at=FakeClock()()))
        s.commit()
    assert len(client.get("/api/scripts").get_json()["scripts"]) == 3
    assert create(client).status_code == 409


def test_content_errors_are_reported_per_field_all_at_once_without_echoing_the_input(app):
    client = author(app)
    response = client.post("/api/scripts", json={"content": {"title": UNIQUE * 3, "summary": "", "content_tier": "nope", "foo": 1}})
    assert response.status_code == 400
    error = response.get_json()["error"]
    assert error["code"] == "validation_error" and {"title", "summary", "background", "world_name", "content_tier", "foo"} <= set(error["fields"])
    assert UNIQUE not in response.get_data(as_text=True)
    assert rows(app, Script) == []


@pytest.mark.parametrize("body", [{}, {"content": "text"}, {"content": [1]}, {"content": None}])
def test_a_missing_or_malformed_content_is_a_validation_error(app, body):
    response = author(app).post("/api/scripts", json=body)
    assert response.status_code == 400 and "content" in response.get_json()["error"]["fields"]


# ---- 作者的內容模式上限 -----------------------------------------------------------------------------


@pytest.mark.parametrize(("age", "tier", "ok"), [
    (15, "safe", True), (15, "standard", False), (15, "adult", False), (17, "standard", False),
    (18, "standard", True), (18, "adult", True), (30, "adult", True),
])
def test_the_declared_tier_may_not_exceed_the_authors_age_cap(app, age, tier, ok):
    response = create(author(app, age=age), content_tier=tier)
    if ok:
        assert response.status_code == 201
    else:
        assert response.status_code == 400 and set(response.get_json()["error"]["fields"]) == {"content_tier"}


def test_an_adult_author_whose_own_preference_is_safe_may_still_declare_adult(app):
    assert create(author(app, content_tier_preference="safe"), content_tier="adult").status_code == 201


def test_the_cap_is_computed_from_the_birthday_now_not_stored(app):
    """17 歲時確認年齡的玩家，滿 18 歲之後可以宣告成人模式（偏好不變，上限由生日於當下計算）。"""
    from datetime import timedelta

    from tests.support.age import birth_date_for_age

    just_adult = birth_date_for_age(18) - timedelta(days=0)
    client = author(app, birth_date=just_adult, content_tier_preference="safe", is_adult_at_confirmation=False)
    assert create(client, content_tier="adult").status_code == 201


# ---- 儲存時的規則層 ------------------------------------------------------------------------------------


def test_an_explicit_red_line_is_refused_with_only_the_category_and_nothing_is_stored(app):
    client = author(app)
    response = create(client, background="十歲的少女與他做愛。" + UNIQUE)
    assert response.status_code == 422
    error = response.get_json()["error"]
    assert error["code"] == "script_auto_rejected" and response.get_json()["categories"] == ["minor_sexual"]
    body = response.get_data(as_text=True)
    assert UNIQUE not in body and "做愛" not in body and "少女" not in body
    assert rows(app, Script) == [] and rows(app, ScriptVersion) == []


def test_a_rejected_save_leaves_the_existing_draft_untouched_and_counts_nothing(app):
    client = author(app)
    version = version_of(create(client, title="原本的標題"))
    response = put(client, version, background="他迷暈她後發生性交。")
    assert response.status_code == 422 and response.get_json()["categories"] == ["non_consensual"]
    stored = client.get(f"/api/scripts/versions/{version['id']}").get_json()["version"]
    assert stored["title"] == "原本的標題" and stored["content_hash"] == version["content_hash"]
    assert rows(app, ScriptSubmission) == []                           # 儲存時的拒絕不計入送審


def test_a_suspicious_but_not_explicit_script_is_saved_and_the_author_is_not_told_about_the_flag(app):
    client = author(app)
    response = create(client, content_tier="safe", background="兩人情慾難耐。")
    assert response.status_code == 201
    text = response.get_data(as_text=True)
    assert "tier_mismatch" not in text and "flag" not in text and "review" not in text.lower()


# ---- 儲存草稿與衝突偵測 ---------------------------------------------------------------------------------


def test_saving_a_draft_replaces_the_content_and_returns_the_new_hash(app):
    client = author(app)
    version = version_of(create(client, title="舊標題"))
    response = put(client, version, title="新標題", tags=["科幻"])
    assert response.status_code == 200
    saved = version_of(response)
    assert saved["title"] == "新標題" and saved["content"]["tags"] == ["科幻"] and saved["content_hash"] != version["content_hash"]
    assert saved["id"] == version["id"] and saved["version_no"] == 1


def test_resubmitting_the_same_content_succeeds_and_changes_nothing(app):
    client = author(app)
    version = version_of(create(client))
    again = put(client, version)
    assert again.status_code == 200 and version_of(again)["content_hash"] == version["content_hash"]


def test_a_second_tab_saving_with_a_stale_hash_gets_draft_changed_and_does_not_overwrite(app):
    client = author(app)
    version = version_of(create(client, title="開始編輯的版本"))
    assert put(client, version, title="分頁一的修改").status_code == 200
    stale = put(client, version, title="分頁二的修改")
    assert stale.status_code == 409 and stale.get_json()["error"]["code"] == "draft_changed"
    assert client.get(f"/api/scripts/versions/{version['id']}").get_json()["version"]["title"] == "分頁一的修改"


@pytest.mark.parametrize("base_hash", [None, "", 123])
def test_the_base_hash_is_required(app, base_hash):
    client = author(app)
    version = version_of(create(client))
    response = client.put(f"/api/scripts/versions/{version['id']}", json={"content": valid(title="改"), "base_hash": base_hash})
    assert response.status_code == 400 and "base_hash" in response.get_json()["error"]["fields"]


def test_a_version_that_is_no_longer_a_draft_cannot_be_saved(app):
    client = author(app)
    version = version_of(create(client))
    set_status(app, version["id"], 1, "pending")
    response = put(client, version, title="想改")
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "not_draft"


# ---- 新草稿 -----------------------------------------------------------------------------------------------


def test_a_new_draft_copies_the_published_version_including_the_milestone_ids_and_leaves_it_alone(app):
    client = author(app)
    first = version_of(create(client, title="已發佈的版本"))
    publish(app, first["id"])
    response = client.post(f"/api/scripts/{first['script']['id']}/versions")
    assert response.status_code == 201
    second = version_of(response)
    assert second["version_no"] == 2 and second["status"] == "draft" and second["content_hash"] == first["content_hash"]
    original = client.get(f"/api/scripts/versions/{first['id']}").get_json()["version"]
    assert original["status"] == "published" and original["content"]["milestones"] == second["content"]["milestones"]


def test_a_new_draft_can_start_from_a_rejected_version(app):
    client = author(app)
    first = version_of(create(client, title="被退回的版本"))
    set_status(app, first["id"], 1, "rejected")
    second = version_of(client.post(f"/api/scripts/{first['script']['id']}/versions"))
    assert second["version_no"] == 2 and second["title"] == "被退回的版本"


def test_a_script_with_an_open_version_cannot_get_another_draft(app):
    client = author(app)
    first = version_of(create(client))
    for _ in range(2):
        response = client.post(f"/api/scripts/{first['script']['id']}/versions")
        assert response.status_code == 409 and response.get_json()["error"]["code"] == "open_version_exists"
    assert len(rows(app, ScriptVersion)) == 1


# ---- 刪除與撤回 ---------------------------------------------------------------------------------------------


def test_deleting_the_only_draft_removes_the_script_too(app):
    client = author(app)
    version = version_of(create(client))
    assert client.delete(f"/api/scripts/versions/{version['id']}").status_code == 200
    assert rows(app, Script) == [] and client.get("/api/scripts").get_json()["scripts"] == []
    assert client.get(f"/api/scripts/versions/{version['id']}").status_code == 404


def test_a_published_version_cannot_be_deleted(app):
    client = author(app)
    version = version_of(create(client))
    publish(app, version["id"])
    response = client.delete(f"/api/scripts/versions/{version['id']}")
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "not_draft"
    assert len(rows(app, ScriptVersion)) == 1


def test_withdrawing_a_pending_version_returns_it_to_a_draft_that_can_be_edited_again(app):
    client = author(app)
    version = version_of(create(client))
    set_status(app, version["id"], 1, "pending")
    response = client.post(f"/api/scripts/versions/{version['id']}/withdraw")
    assert response.status_code == 200 and version_of(response)["status"] == "draft"
    assert put(client, version, title="撤回後再編輯").status_code == 200


def test_only_a_pending_version_can_be_withdrawn(app):
    client = author(app)
    version = version_of(create(client))
    response = client.post(f"/api/scripts/versions/{version['id']}/withdraw")
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "not_pending"


# ---- 筆名 -----------------------------------------------------------------------------------------------------


def test_changing_the_nickname_alone_does_not_change_any_pen_name_until_the_author_refreshes(app):
    client = author(app)
    script_id = version_of(create(client))["script"]["id"]
    assert client.put("/api/account/nickname", json={"nickname": "新的暱稱"}).status_code == 200
    assert client.get("/api/scripts").get_json()["scripts"][0]["author_name"] == "雲遊者"
    refreshed = client.post(f"/api/scripts/{script_id}/author-name/refresh")
    assert refreshed.status_code == 200
    assert refreshed.get_json()["script"]["author_name"] == "新的暱稱" and refreshed.get_json()["script"]["author_code"] == "AB01"


def test_a_locked_pen_name_cannot_be_refreshed(app):
    client = author(app)
    first = version_of(create(client))
    publish(app, first["id"])
    client.put("/api/account/nickname", json={"nickname": "新的暱稱"})
    response = client.post(f"/api/scripts/{first['script']['id']}/author-name/refresh")
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "author_name_locked"
    assert client.get("/api/scripts").get_json()["scripts"][0]["author_name"] == "雲遊者"


# ---- 範例與額度 -------------------------------------------------------------------------------------------------


def seed(app):
    with db(app) as s:
        ScriptSeedService(
            scripts=ScriptRepository(s), clock=FakeClock(), commit=s.commit, rollback=s.rollback, directory=SEED_DIRECTORY,
        ).run()


def test_the_example_is_the_builtin_script_content_and_passes_the_fixed_spec(app):
    seed(app)
    response = author(app).get("/api/scripts/example")
    assert response.status_code == 200
    content = response.get_json()["content"]
    assert validate_script(content, max_tier="adult").content["title"] == content["title"]
    text = response.get_data(as_text=True)
    assert "content_hash" not in text and "public_id" not in text and "seed_key" not in text


def test_without_the_seeded_builtin_script_there_is_no_example(app):
    assert author(app).get("/api/scripts/example").status_code == 404


def test_the_limits_report_only_the_callers_own_numbers(app):
    client, other = author(app, 1), author(app, 2)
    create(client), create(client, title="二"), create(other)
    set_status(app, version_of(create(client, title="三"))["id"], 1, "pending")
    limits = client.get("/api/scripts/limits").get_json()["limits"]
    spec = limits.pop("spec")
    assert limits == {
        "scripts": {"used": 3, "limit": 3}, "submissions_today": {"used": 0, "limit": 3},
        "publish_suspended": False, "has_nickname": True, "max_content_tier": "adult",
    }
    assert spec["title"] == 30 and spec["total"] == 8000 and spec["character_count"] == 10 and spec["milestone_count"] == 8
    assert other.get("/api/scripts/limits").get_json()["limits"]["scripts"]["used"] == 1


def test_a_suspended_author_sees_it_in_the_limits_and_can_still_edit(app):
    client = author(app, publish_suspended_at=FakeClock()())
    assert client.get("/api/scripts/limits").get_json()["limits"]["publish_suspended"] is True
    version = version_of(create(client))
    assert put(client, version, title="暫停期間照常編輯").status_code == 200


# ---- 列表 -------------------------------------------------------------------------------------------------------


def test_the_list_groups_versions_under_their_script_newest_first_and_never_shows_anyone_elses(app):
    client, other = author(app, 1), author(app, 2)
    first = version_of(create(client, title="甲"))
    publish(app, first["id"])
    client.post(f"/api/scripts/{first['script']['id']}/versions")
    create(other, title="別人的")
    scripts = client.get("/api/scripts").get_json()["scripts"]
    assert len(scripts) == 1 and [v["version_no"] for v in scripts[0]["versions"]] == [2, 1]
    assert [v["status"] for v in scripts[0]["versions"]] == ["draft", "published"]
    assert "別人的" not in json.dumps(scripts, ensure_ascii=False)
    assert all("content" not in v for v in scripts[0]["versions"])          # 列表只有摘要


# ---- 擁有者隔離（IDOR）-------------------------------------------------------------------------------------------


def make_draft(app, owner):
    with db(app) as s:
        v = validated()
        version = ScriptRepository(s).create_script(
            owner.id, author_name="甲", author_code="AAAA", content=v.content, content_hash=v.content_hash, total_chars=v.total_chars,
        )
        s.commit()
        return version.public_id


def make_script_id(app, owner):
    with db(app) as s:
        scripts = ScriptRepository(s)
        version = scripts.get_owned_version(owner.id, make_draft(app, owner))
        return scripts.get_script_of(version).public_id


def make_pending(app, owner):
    public_id = make_draft(app, owner)
    with db(app) as s:
        ScriptRepository(s).submit_for_review(owner.id, public_id)
        s.commit()
    return public_id


def idor_users(app):
    """框架只需要 `id` 與 `token_version`；回傳單純的物件，避免 Session 關閉後的實體無法讀取屬性。"""
    with db(app) as s:
        users = [add_user(s, 1, nickname="擁有者", nickname_code="OWN1"), add_user(s, 2, nickname="闖入者", nickname_code="INT2")]
        return [SimpleNamespace(id=u.id, token_version=u.token_version) for u in users]


def test_other_players_resources_look_exactly_like_missing_ones(app):
    owner, intruder = idor_users(app)
    specs = [
        EndpointSpec("GET", "/api/scripts/versions/{id}", lambda a, u: make_draft(a, u)),
        EndpointSpec(
            "PUT", "/api/scripts/versions/{id}", lambda a, u: make_draft(a, u),
            json={"content": valid(title="想改的標題"), "base_hash": validated().content_hash},
        ),
        EndpointSpec("DELETE", "/api/scripts/versions/{id}", lambda a, u: make_draft(a, u)),
        EndpointSpec("POST", "/api/scripts/versions/{id}/withdraw", lambda a, u: make_pending(a, u)),
        EndpointSpec("POST", "/api/scripts/{id}/author-name/refresh", lambda a, u: make_script_id(a, u)),
    ]
    for spec in specs:
        check_endpoint(app, spec, owner=owner, intruder=intruder)


def test_a_new_draft_for_someone_elses_script_is_a_404_even_when_it_has_a_closed_version(app):
    owner, intruder = idor_users(app)
    script_id = make_script_id(app, owner)
    version_public_id = next(v.public_id for v in rows(app, ScriptVersion))
    publish(app, version_public_id)
    intruder_client = logged_in_client(app, intruder)
    missing = intruder_client.post("/api/scripts/00000000-0000-4000-8000-000000000000/versions")
    other = intruder_client.post(f"/api/scripts/{script_id}/versions")
    assert missing.status_code == other.status_code == 404 and missing.get_data() == other.get_data()
    assert len(rows(app, ScriptVersion)) == 1


def test_the_idor_framework_would_catch_a_leaky_endpoint(app):
    """自我驗證：把『他人』當成擁有者呼叫（回 200）時，框架必須拋出 IdorFailure，否則上面的通過沒有意義。"""
    owner, intruder = idor_users(app)
    spec = EndpointSpec("GET", "/api/scripts/versions/{id}", lambda a, u: make_draft(a, intruder))
    with pytest.raises(IdorFailure):
        check_endpoint(app, spec, owner=owner, intruder=intruder)


# ---- 日誌 -------------------------------------------------------------------------------------------------------------


def test_the_whole_authoring_flow_leaves_no_script_text_title_tag_or_pen_name_in_the_logs(app, log_lines):
    client = author(app, nickname=True)
    version = version_of(create(client, rules=UNIQUE, title="獨特的劇本標題SCRIPT-API-TITLE", tags=["標籤SCRIPTTAG"]))
    put(client, version, rules=UNIQUE + "二", title="獨特的劇本標題SCRIPT-API-TITLE")
    create(client, title="第二個", background="他迷暈她後發生性交。" + UNIQUE)     # 儲存時被拒絕
    client.delete(f"/api/scripts/versions/{version['id']}")
    raw = log_lines.raw()
    for secret in ("SCRIPT-API-SECRET-TEXT", "SCRIPT-API-TITLE", "SCRIPTTAG", "雲遊者"):
        assert secret not in raw
    events = {line["event"] for line in log_lines() if line.get("event", "").startswith("script.")}
    assert {"script.created", "script.updated", "script.deleted", "script.auto_rejected"} <= events
    for line in log_lines():
        if line.get("event", "").startswith("script."):
            assert set(line) <= {"time", "level", "logger", "message", "request_id", "event", "user_id", "count", "kind", "outcome", "reason"}
    rejected = next(line for line in log_lines() if line.get("event") == "script.auto_rejected")
    assert rejected["kind"] == "save" and rejected["outcome"] == "rules" and rejected["reason"] == "non_consensual"
