"""admin-script-review 與 script-takedown（管理員部分）規格的 HTTP 行為：`/api/admin/scripts*` 六個端點——身分矩陣（未登入 401、
一般使用者 404 且與不存在的端點逐字相同，含格式錯誤的本文）、CSRF、查詢參數與本文驗證（400）、狀態衝突（409）、密碼（下架與恢復才要）、
回應只含明確組出的欄位（沒有內部編號與 email，清單沒有內文與備註）、`no-store`，以及一整輪審核流程。
"""
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import AdminAuditLog, Base, ScriptVersion
from app.repositories.script_repository import ScriptRepository
from app.services import admin_script_views
from tests.support.auth import GOOD_PASSWORD, FakeClock, add_user
from tests.support.client import raw_client
from tests.support.idor import logged_in_client
from tests.support.llm import FakeLLMProvider
from tests.support.script_review import put_in_status

NOTE = "只有管理員看得到的審核備註"
MISSING = "00000000-0000-4000-8000-000000000000"
PASSWORD_BODY = {"password": GOOD_PASSWORD}


class World:
    def __init__(self):
        self.clock = FakeClock()
        self.app = create_app(
            load_config(environ={"APP_ENV": "testing"}), clock=self.clock, rpg_llm_provider=FakeLLMProvider(default=None),
        )
        self.engine = self.app.extensions["db_engine"]
        Base.metadata.create_all(self.engine)
        with Session(self.engine) as s:
            self.admin = add_user(s, 1, role="admin")
            self.author = add_user(s, 2)
            self.player = add_user(s, 3)
            self.admin_ref, self.author_ref, self.player_ref = (_Ref(u) for u in (self.admin, self.author, self.player))

    def client(self, who):
        return self.app.test_client() if who == "anonymous" else logged_in_client(self.app, getattr(self, f"{who}_ref"))

    def make(self, status: str, **kwargs) -> str:
        """作者的一個新劇本（第 1 版）放進指定狀態，回傳版本的對外識別碼。"""
        with Session(self.engine) as s:
            author = s.get(type(self.author), self.author_ref.id)
            version = put_in_status(ScriptRepository(s), author, status, **kwargs)
            s.commit()
            return version.public_id

    def version(self, public_id: str) -> ScriptVersion:
        with Session(self.engine) as s:
            return s.scalar(select(ScriptVersion).where(ScriptVersion.public_id == public_id))

    def status(self, public_id: str) -> str:
        return self.version(public_id).status

    def audits(self) -> list[AdminAuditLog]:
        with Session(self.engine) as s:
            return list(s.scalars(select(AdminAuditLog).order_by(AdminAuditLog.id)))


class _Ref:
    def __init__(self, user):
        self.id, self.token_version, self.public_id = user.id, user.token_version, user.public_id


@pytest.fixture()
def world():
    return World()


def scripts_url(suffix: str = "") -> str:
    return f"/api/admin/scripts{suffix}"


def action_url(public_id: str, action: str) -> str:
    return scripts_url(f"/versions/{public_id}/{action}")


def error_of(response) -> dict:
    return response.get_json()["error"]


ENDPOINTS = [
    ("GET", scripts_url(), None),
    ("GET", scripts_url("/versions/{id}"), None),
    ("POST", scripts_url("/versions/{id}/approve"), {}),
    ("POST", scripts_url("/versions/{id}/reject"), {"category": "other_policy", "note": NOTE}),
    ("POST", scripts_url("/versions/{id}/remove"), {"category": "other_policy", "note": NOTE, **PASSWORD_BODY}),
    ("POST", scripts_url("/versions/{id}/restore"), PASSWORD_BODY),
]


# ---- 身分矩陣 ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("method, path, body", ENDPOINTS)
def test_an_anonymous_visitor_gets_401(world, method, path, body):
    version = world.make("pending_review")
    response = world.client("anonymous").open(path.format(id=version), method=method, json=body)
    assert response.status_code == 401
    assert world.status(version) == "pending_review"


@pytest.mark.parametrize("who", ["author", "player"])
@pytest.mark.parametrize("method, path, body", ENDPOINTS)
def test_a_non_admin_gets_the_same_404_as_a_missing_endpoint_and_nothing_changes(world, who, method, path, body):
    version = world.make("pending_review")
    response = world.client(who).open(path.format(id=version), method=method, json=body)
    missing = world.client(who).get("/api/admin/this-endpoint-does-not-exist")
    assert response.status_code == 404 and response.get_json() == missing.get_json()
    assert world.status(version) == "pending_review" and world.audits() == []


@pytest.mark.parametrize("action", ["approve", "reject", "remove", "restore"])
def test_a_non_admin_gets_404_even_for_a_malformed_body_not_400_or_415(world, action):
    version = world.make("pending_review")
    client = world.client("player")
    for kwargs in ({"data": "not json at all", "content_type": "text/plain"}, {"data": "[1, 2", "content_type": "application/json"}, {}):
        response = client.post(action_url(version, action), **kwargs)
        assert response.status_code == 404, (action, kwargs, response.status_code)


@pytest.mark.parametrize("path", [scripts_url(), scripts_url("/versions/{id}")])
def test_a_non_admin_gets_404_for_a_malformed_query_too(world, path):
    version = world.make("pending_review")
    response = world.client("player").get(path.format(id=version) + "?limit=abc&status=%00")
    assert response.status_code == 404


@pytest.mark.parametrize("method, path, body", [e for e in ENDPOINTS if e[0] == "POST"])
def test_a_post_without_the_csrf_token_is_refused_and_nothing_changes(world, method, path, body):
    version = world.make("pending_review")
    raw = raw_client(world.app)
    with raw.session_transaction() as session:
        session["uid"], session["tv"] = world.admin_ref.id, world.admin_ref.token_version
    response = raw.post(path.format(id=version), json=body)
    assert response.status_code == 403 and world.status(version) == "pending_review" and world.audits() == []


@pytest.mark.parametrize("method, path, body", ENDPOINTS)
def test_every_response_including_errors_is_not_cacheable(world, method, path, body):
    version = world.make("pending_review")
    ok = world.client("admin").open(path.format(id=version), method=method, json=body)
    missing = world.client("admin").open(path.format(id=MISSING), method=method, json=body) if "{id}" in path else ok
    for response in (ok, missing):
        assert "no-store" in response.headers["Cache-Control"], (method, path, response.status_code)


# ---- 清單 ----------------------------------------------------------------------------------------------


def test_the_list_defaults_to_pending_and_has_the_documented_shape(world):
    pending = world.make("pending_review", title="待審的")
    world.make("published", title="上架的")
    response = world.client("admin").get(scripts_url())
    assert response.status_code == 200
    [item] = response.get_json()["versions"]
    assert item["id"] == pending and set(item) == set(admin_script_views.LIST_KEYS)
    assert item["author_id"] == world.author_ref.public_id and item["status"] == "pending_review"


@pytest.mark.parametrize("status, title", [("pending_review", "待審"), ("published", "上架"), ("removed", "下架")])
def test_each_status_lists_only_its_own_versions(world, status, title):
    for s, t in (("pending_review", "待審"), ("published", "上架"), ("removed", "下架"), ("rejected", "退回"), ("draft", "草稿")):
        world.make(s, title=t)
    titles = [i["title"] for i in world.client("admin").get(scripts_url() + f"?status={status}").get_json()["versions"]]
    assert titles == [title]


@pytest.mark.parametrize("query, field", [
    ("status=draft", "status"), ("status=nope", "status"), ("status=", "status"), ("limit=101", "limit"), ("limit=0", "limit"),
    ("limit=abc", "limit"), ("offset=-1", "offset"), ("offset=x", "offset"),
])
def test_an_invalid_query_is_a_400_naming_the_field(world, query, field):
    response = world.client("admin").get(scripts_url() + "?" + query)
    assert response.status_code == 400 and field in error_of(response)["fields"]


def test_paging_with_limit_and_offset(world):
    for index in (1, 2, 3):
        world.make("pending_review", title=f"第{index}份")
    page = world.client("admin").get(scripts_url() + "?limit=1&offset=1").get_json()["versions"]
    assert [i["title"] for i in page] == ["第2份"]


def test_the_list_has_no_internal_ids_email_script_text_or_note_text(world):
    world.make("removed", removed_by="admin")
    text = world.client("admin").get(scripts_url() + "?status=removed").get_data(as_text=True)
    for forbidden in ("user_id", "creator_id", "password", "token_version", world.author.email, "靈氣復甦", "林曉", "下架的備註"):
        assert forbidden not in text, forbidden
    assert world.audits() == []


# ---- 單一版本 ------------------------------------------------------------------------------------------


def test_reading_a_pending_version_returns_the_full_text_and_one_read_audit(world):
    version = world.make("rejected")
    response = world.client("admin").get(scripts_url(f"/versions/{version}"))
    assert response.status_code == 200
    detail = response.get_json()["version"]
    assert set(detail) == set(admin_script_views.DETAIL_KEYS)
    assert detail["content"]["background"].startswith("靈氣復甦") and detail["decision_note"] == "管理員的備註"
    [audit] = world.audits()
    assert audit.action == "script.read" and audit.target_user_public_id == world.author_ref.public_id
    assert audit.detail_json["version_id"] == version


def test_a_draft_and_a_missing_id_are_the_same_404_without_an_audit(world):
    draft = world.make("draft")
    first = world.client("admin").get(scripts_url(f"/versions/{draft}"))
    second = world.client("admin").get(scripts_url(f"/versions/{MISSING}"))
    assert first.status_code == second.status_code == 404 and first.get_json() == second.get_json()
    assert world.audits() == []


# ---- 核可與退回（不需要密碼）-------------------------------------------------------------------------------


def test_approving_without_any_body_or_password_publishes_the_version(world):
    version = world.make("pending_review")
    response = world.client("admin").post(action_url(version, "approve"))
    assert response.status_code == 200
    assert response.get_json()["version"]["status"] == "published" and world.status(version) == "published"
    [audit] = world.audits()
    assert audit.action == "script.approve"


def test_approving_something_that_is_no_longer_pending_is_a_409_and_a_missing_id_a_404(world):
    version = world.make("pending_review")
    assert world.client("admin").post(action_url(version, "approve")).status_code == 200
    again = world.client("admin").post(action_url(version, "approve"))
    assert again.status_code == 409 and error_of(again)["code"] == "not_pending" and len(world.audits()) == 1
    assert world.client("admin").post(action_url(MISSING, "approve")).status_code == 404


def test_rejecting_records_the_category_and_keeps_the_note_out_of_the_response(world):
    version = world.make("pending_review")
    response = world.client("admin").post(action_url(version, "reject"), json={"category": "other_policy", "note": NOTE})
    assert response.status_code == 200
    body = response.get_json()["version"]
    assert (body["status"], body["decision_category"], body["has_note"]) == ("rejected", "other_policy", True)
    assert NOTE not in response.get_data(as_text=True)
    assert world.version(version).decision_note == NOTE
    assert NOTE not in repr([a.detail_json for a in world.audits()])


@pytest.mark.parametrize("body, field", [
    ({}, "category"), ({"category": "nope"}, "category"), ({"category": "other_policy", "note": "字" * 501}, "note"),
    ({"category": 5, "note": ["x"]}, "category"),
])
def test_rejecting_with_an_invalid_body_is_a_400_and_the_version_stays_pending(world, body, field):
    version = world.make("pending_review")
    response = world.client("admin").post(action_url(version, "reject"), json=body)
    assert response.status_code == 400 and field in error_of(response)["fields"]
    assert world.status(version) == "pending_review" and world.audits() == []


def test_rejecting_requires_a_json_object_body(world):
    version = world.make("pending_review")
    client = world.client("admin")
    assert client.post(action_url(version, "reject"), data="x", content_type="text/plain").status_code == 415
    assert client.post(action_url(version, "reject"), data="[1]", content_type="application/json").status_code == 400
    assert world.status(version) == "pending_review"


# ---- 下架與恢復（要密碼）-----------------------------------------------------------------------------------


def test_removing_needs_the_password(world):
    version = world.make("published")
    client = world.client("admin")
    missing = client.post(action_url(version, "remove"), json={"category": "other_policy"})
    wrong = client.post(action_url(version, "remove"), json={"category": "other_policy", "password": "definitely-not-the-password"})
    for response in (missing, wrong):
        assert response.status_code == 400 and "password" in error_of(response)["fields"]
    assert world.status(version) == "published"
    assert [a.action for a in world.audits()] == ["reauth.failed", "reauth.failed"]       # 兩次失敗都保存了稽核（4xx 也保留）


def test_a_remove_and_restore_round_trip_through_the_http_api(world):
    version = world.make("published", title="來回的")
    client = world.client("admin")
    removed = client.post(action_url(version, "remove"), json={"category": "tier_mismatch", "note": NOTE, **PASSWORD_BODY})
    assert removed.status_code == 200
    body = removed.get_json()["version"]
    assert (body["status"], body["removed_by"], body["decision_category"]) == ("removed", "admin", "tier_mismatch")
    assert [i["title"] for i in client.get(scripts_url() + "?status=removed").get_json()["versions"]] == ["來回的"]
    assert client.get(scripts_url() + "?status=published").get_json()["versions"] == []
    restored = client.post(action_url(version, "restore"), json=PASSWORD_BODY)
    assert restored.status_code == 200 and restored.get_json()["version"]["status"] == "published"
    assert restored.get_json()["version"]["removed_by"] is None
    assert [a.action for a in world.audits()] == ["script.remove", "script.restore"]
    assert NOTE not in repr([a.detail_json for a in world.audits()])


@pytest.mark.parametrize("status", ["draft", "pending_review", "rejected", "removed", "superseded"])
def test_removing_something_that_is_not_live_is_a_409_not_published(world, status):
    version = world.make(status)
    response = world.client("admin").post(action_url(version, "remove"), json={"category": "other_policy", **PASSWORD_BODY})
    assert response.status_code == 409 and error_of(response)["code"] == "not_published" and world.status(version) == status


@pytest.mark.parametrize("status", ["draft", "pending_review", "published", "rejected", "superseded"])
def test_restoring_something_that_is_not_removed_is_a_409_not_removed(world, status):
    version = world.make(status)
    response = world.client("admin").post(action_url(version, "restore"), json=PASSWORD_BODY)
    assert response.status_code == 409 and error_of(response)["code"] == "not_removed" and world.status(version) == status


def test_the_admin_cannot_restore_what_the_author_removed(world):
    version = world.make("removed", removed_by="author")
    response = world.client("admin").post(action_url(version, "restore"), json=PASSWORD_BODY)
    assert response.status_code == 409 and error_of(response)["code"] == "removed_by_author" and world.status(version) == "removed"


def test_a_builtin_script_cannot_be_removed_over_http(world):
    from tests.support.script_data import validated

    result = validated(title="官方劇本")
    with Session(world.engine) as s:
        version, _ = ScriptRepository(s).publish_builtin(
            "official", content=result.content, content_hash=result.content_hash, total_chars=result.total_chars,
            now=world.clock(),
        )
        s.commit()
        public_id = version.public_id
    response = world.client("admin").post(action_url(public_id, "remove"), json={"category": "other_policy", **PASSWORD_BODY})
    assert response.status_code == 409 and error_of(response)["code"] == "builtin_not_removable" and world.status(public_id) == "published"


def test_removing_or_restoring_with_an_invalid_body_is_a_400(world):
    version = world.make("published")
    client = world.client("admin")
    assert client.post(action_url(version, "remove"), json={"password": GOOD_PASSWORD}).status_code == 400          # 沒有類別
    assert client.post(action_url(version, "remove"), data="x", content_type="text/plain").status_code == 415
    assert client.post(action_url(version, "restore"), data="[]", content_type="application/json").status_code == 400
    assert world.status(version) == "published"


# ---- 回應不含不該有的欄位 ------------------------------------------------------------------------------


def test_no_response_exposes_internal_ids_or_the_authors_email(world):
    version = world.make("pending_review")
    client = world.client("admin")
    responses = [
        client.get(scripts_url()), client.get(scripts_url(f"/versions/{version}")),
        client.post(action_url(version, "reject"), json={"category": "other_policy", "note": NOTE}),
    ]
    for response in responses:
        text = response.get_data(as_text=True)
        assert world.author.email not in text and "token_version" not in text and "password_hash" not in text
        walk = [response.get_json()]
        while walk:
            node = walk.pop()
            if isinstance(node, dict):
                assert not {"user_id", "creator_id", "script_pk", "email"} & set(node)
                walk.extend(node.values())
            elif isinstance(node, list):
                walk.extend(node)
    detail_text = responses[1].get_data(as_text=True)
    assert json.loads(detail_text)["version"]["author_id"] == world.author_ref.public_id
