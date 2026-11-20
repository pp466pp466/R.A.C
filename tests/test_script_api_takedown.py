"""script-takedown 規格（作者的 HTTP 層）：`POST /api/scripts/versions/<id>/remove`、`/restore`——登入與年齡閘門、鎖定的受限登入狀態、
CSRF、擁有者隔離（他人與不存在的 404 逐字相同）、前置狀態的 409 與 403 代碼、回應與列表的鍵集合與**不含審核備註**、
不寫管理員稽核、不寄信、不快取、日誌只有事件與固定列舉。

用測試應用程式（SQLite）與直接寫入的登入狀態；Repository 與 Service 的轉移規則另見 `test_script_repository_publishing`、
`test_script_takedown_service`。
"""
import smtplib
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import AdminAuditLog, Base, ScriptVersion
from app.models.script import REMOVER_ADMIN, VERSION_PUBLISHED, VERSION_REMOVED
from app.repositories.script_repository import ScriptRepository
from tests.support.auth import FakeClock, add_user, sign_in_as
from tests.support.client import raw_client
from tests.support.idor import EndpointSpec, check_endpoint, logged_in_client
from tests.support.script_data import validated
from tests.support.script_review import at

NOTE = "只有管理員看得到的下架備註"
TITLE = "獨特的劇本標題TAKEDOWN-API-TITLE"
SUMMARY_KEYS = {
    "id", "version_no", "status", "title", "summary", "content_tier", "content_hash", "total_chars", "created_at", "updated_at",
    "published_at", "decision_category", "decided_at", "removed_by", "script", "content",
}


@pytest.fixture()
def app():
    application = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


def db(app) -> Session:
    return Session(app.extensions["db_engine"])


def player(app, n: int = 1, **overrides):
    """已完成年齡確認、有暱稱的玩家（單純物件，Session 關閉後仍可讀）。"""
    with db(app) as s:
        user = add_user(s, n, nickname=f"旅人{n}", nickname_code=f"AB{n:02d}", **overrides)
        return SimpleNamespace(id=user.id, token_version=user.token_version)


def put_published(app, owner, **changes) -> str:
    """作者有一個上架中的版本（走正式的轉移方法）；回傳版本的對外識別碼。"""
    content = validated(**changes)
    with db(app) as s:
        scripts = ScriptRepository(s)
        version = scripts.create_script(
            owner.id, author_name="甲", author_code="AAAA", content=content.content, content_hash=content.content_hash,
            total_chars=content.total_chars,
        )
        scripts.submit_for_review(owner.id, version.public_id, now=at())
        scripts.approve_version(version.public_id, now=at())
        s.commit()
        return version.public_id


def admin_takedown(app, version_public_id: str) -> None:
    with db(app) as s:
        ScriptRepository(s).remove_by_admin(version_public_id, "other_policy", NOTE, at(1))
        s.commit()


def status_of(app, version_public_id: str) -> tuple:
    with db(app) as s:
        version = s.scalar(select(ScriptVersion).where(ScriptVersion.public_id == version_public_id))
        return version.status, version.removed_by, version.decision_category, version.decided_at


def remove(client, version_public_id: str):
    return client.post(f"/api/scripts/versions/{version_public_id}/remove")


def restore(client, version_public_id: str):
    return client.post(f"/api/scripts/versions/{version_public_id}/restore")


def error_code(response) -> str:
    return response.get_json()["error"]["code"]


# ---- 閘門 --------------------------------------------------------------------------------------

TAKEDOWN_ENDPOINTS = [("POST", "/api/scripts/versions/x/remove"), ("POST", "/api/scripts/versions/x/restore")]


@pytest.mark.parametrize(("method", "path"), TAKEDOWN_ENDPOINTS)
def test_every_takedown_endpoint_needs_a_login(app, method, path):
    assert app.test_client().open(path, method=method, json={}).status_code == 401


@pytest.mark.parametrize(("method", "path"), TAKEDOWN_ENDPOINTS)
def test_a_player_without_the_age_confirmation_gets_the_age_gate_code(app, method, path):
    client = logged_in_client(app, player(app, age=None))
    response = client.open(path, method=method, json={})
    assert response.status_code == 403 and error_code(response) == "age_confirmation_required"


@pytest.mark.parametrize(("method", "path"), TAKEDOWN_ENDPOINTS)
def test_a_locked_player_is_refused_and_the_published_version_stays_up(app, method, path):
    owner = player(app, locked_at=FakeClock()(), lock_reason="rate_limited")
    version = put_published(app, owner)
    response = logged_in_client(app, owner).open(path.replace("/x/", f"/{version}/"), method=method, json={})
    assert response.status_code == 403 and error_code(response) == "account_locked"
    assert status_of(app, version)[0] == VERSION_PUBLISHED


@pytest.mark.parametrize("action", [remove, restore])
def test_a_post_without_the_csrf_token_is_refused_and_nothing_changes(app, action):
    owner = player(app)
    version = put_published(app, owner)
    raw = raw_client(app)
    sign_in_as(raw, owner.id, owner.token_version)
    assert action(raw, version).status_code == 403
    assert status_of(app, version)[0] == VERSION_PUBLISHED


# ---- 下架與恢復 ----------------------------------------------------------------------------------


def test_the_author_takes_down_then_restores_and_each_response_has_exactly_the_documented_keys(app):
    owner = player(app)
    version = put_published(app, owner)
    client = logged_in_client(app, owner)
    published_at = status_of(app, version)

    down = remove(client, version)
    assert down.status_code == 200 and down.get_json()["ok"] is True
    body = down.get_json()["version"]
    assert set(body) == SUMMARY_KEYS and body["status"] == VERSION_REMOVED and body["removed_by"] == "author"
    assert body["decision_category"] is None and body["decided_at"] is not None
    assert status_of(app, version)[0] == VERSION_REMOVED

    up = restore(client, version)
    assert up.status_code == 200
    body = up.get_json()["version"]
    assert set(body) == SUMMARY_KEYS and body["status"] == VERSION_PUBLISHED
    assert (body["removed_by"], body["decision_category"], body["decided_at"]) == (None, None, None)
    assert status_of(app, version)[:3] == published_at[:3]


def test_the_list_shows_the_removed_state_with_the_new_fields(app):
    owner = player(app)
    version = put_published(app, owner)
    client = logged_in_client(app, owner)
    remove(client, version)
    (script,) = client.get("/api/scripts").get_json()["scripts"]
    (summary,) = script["versions"]
    assert summary["status"] == VERSION_REMOVED and summary["removed_by"] == "author" and summary["decided_at"] is not None
    assert "script" not in summary and "content" not in summary          # 列表的摘要沒有內容


def test_a_player_who_cannot_publish_can_still_take_down_and_restore(app):
    owner = player(app, publish_suspended_at=FakeClock()())
    version = put_published(app, owner)
    client = logged_in_client(app, owner)
    assert remove(client, version).status_code == 200
    assert restore(client, version).status_code == 200


# ---- 錯誤碼 --------------------------------------------------------------------------------------


def test_taking_down_something_that_is_not_published_is_409_not_published(app):
    owner = player(app)
    version = put_published(app, owner)
    client = logged_in_client(app, owner)
    remove(client, version)
    again = remove(client, version)                                   # 已經是下架
    assert again.status_code == 409 and error_code(again) == "not_published"


def test_restoring_something_that_is_not_removed_is_409_not_removed(app):
    owner = player(app)
    version = put_published(app, owner)
    response = restore(logged_in_client(app, owner), version)
    assert response.status_code == 409 and error_code(response) == "not_removed"
    assert status_of(app, version)[0] == VERSION_PUBLISHED


def test_an_admin_takedown_is_403_removal_locked_for_the_author_and_stays_removed(app):
    owner = player(app)
    version = put_published(app, owner)
    admin_takedown(app, version)
    response = restore(logged_in_client(app, owner), version)
    assert response.status_code == 403 and error_code(response) == "removal_locked"
    assert status_of(app, version)[:3] == (VERSION_REMOVED, REMOVER_ADMIN, "other_policy")


def test_the_author_sees_the_admin_takedown_state_but_never_the_note(app):
    owner = player(app)
    version = put_published(app, owner)
    admin_takedown(app, version)
    client = logged_in_client(app, owner)
    listing = client.get("/api/scripts")
    detail = client.get(f"/api/scripts/versions/{version}")
    refused = restore(client, version)
    for response in (listing, detail, refused):
        assert NOTE not in response.get_data(as_text=True)
    (summary,) = listing.get_json()["scripts"][0]["versions"]
    assert (summary["status"], summary["removed_by"], summary["decision_category"]) == (VERSION_REMOVED, "admin", "other_policy")


def test_a_removed_version_cannot_be_edited_deleted_withdrawn_or_submitted_over_http(app):
    """3.4 盤點：其餘依版本狀態分支的端點對 `removed` 一律 409（草稿才能編輯、刪除與送審，待審才能撤回），版本不變。"""
    owner = player(app)
    version = put_published(app, owner)
    admin_takedown(app, version)
    client = logged_in_client(app, owner)
    before = status_of(app, version)
    detail = client.get(f"/api/scripts/versions/{version}").get_json()["version"]
    put = client.put(f"/api/scripts/versions/{version}", json={"content": detail["content"], "base_hash": detail["content_hash"]})
    assert (put.status_code, error_code(put)) == (409, "not_draft")
    deleted = client.delete(f"/api/scripts/versions/{version}")
    assert (deleted.status_code, error_code(deleted)) == (409, "not_draft")
    withdrawn = client.post(f"/api/scripts/versions/{version}/withdraw")
    assert (withdrawn.status_code, error_code(withdrawn)) == (409, "not_pending")
    submitted = client.post(f"/api/scripts/versions/{version}/submit")
    assert (submitted.status_code, error_code(submitted)) == (409, "not_draft")
    assert status_of(app, version) == before


# ---- 擁有者隔離 ----------------------------------------------------------------------------------


def test_other_players_versions_look_exactly_like_missing_ones_on_both_endpoints(app):
    owner, intruder = player(app, 1), player(app, 2)
    specs = [
        EndpointSpec("POST", "/api/scripts/versions/{id}/remove", lambda a, u: put_published(a, u)),
        EndpointSpec("POST", "/api/scripts/versions/{id}/restore", lambda a, u: _removed_by_author(a, u)),
    ]
    for spec in specs:
        check_endpoint(app, spec, owner=owner, intruder=intruder)


def _removed_by_author(app, owner) -> str:
    version = put_published(app, owner)
    remove(logged_in_client(app, owner), version)
    return version


def test_a_failed_attempt_by_someone_else_leaves_the_version_untouched(app):
    owner, intruder = player(app, 1), player(app, 2)
    version = put_published(app, owner)
    assert remove(logged_in_client(app, intruder), version).status_code == 404
    assert status_of(app, version)[0] == VERSION_PUBLISHED
    remove(logged_in_client(app, owner), version)
    assert restore(logged_in_client(app, intruder), version).status_code == 404
    assert status_of(app, version)[0] == VERSION_REMOVED


# ---- 不快取、不稽核、不寄信、日誌 ---------------------------------------------------------------------


def test_every_response_is_marked_no_store(app):
    owner = player(app)
    version = put_published(app, owner)
    client = logged_in_client(app, owner)
    for response in (remove(client, version), restore(client, version), restore(client, version)):
        assert "no-store" in response.headers.get("Cache-Control", "")


def test_the_takedown_writes_no_admin_audit_and_sends_no_mail(app, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("下架與恢復不寄信")

    monkeypatch.setattr(smtplib, "SMTP", forbidden)
    monkeypatch.setattr(smtplib, "SMTP_SSL", forbidden)
    owner = player(app)
    version = put_published(app, owner)
    client = logged_in_client(app, owner)
    assert remove(client, version).status_code == 200 and restore(client, version).status_code == 200
    with db(app) as s:
        assert s.scalars(select(AdminAuditLog)).all() == []


def test_the_takedown_leaves_no_script_text_or_title_in_the_logs_and_only_the_fixed_events(app, log_lines):
    owner = player(app)
    version = put_published(app, owner, title=TITLE)
    client = logged_in_client(app, owner)
    remove(client, version)
    restore(client, version)
    remove(client, version)
    admin_takedown_target = put_published(app, owner, title=TITLE + "二")
    admin_takedown(app, admin_takedown_target)
    restore(client, admin_takedown_target)                              # 403
    raw = log_lines.raw()
    assert "TAKEDOWN-API-TITLE" not in raw and NOTE not in raw and version not in raw
    events = [line for line in log_lines() if line.get("event") in {"script.removed", "script.restored"}]
    assert [(line["event"], line["kind"]) for line in events] == [
        ("script.removed", "author"), ("script.restored", "author"), ("script.removed", "author"),
    ]
    for line in events:
        assert set(line) <= {"time", "level", "logger", "message", "request_id", "event", "user_id", "kind", "reason"}
