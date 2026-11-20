"""service-observability 與 admin-audit-log 規格（rpg-script-publishing）的整合驗證：玩家建立並送審（假模型）→ 管理員讀取、退回（含備註）
→ 作者修改再送審 → 管理員核可 → 管理員以錯誤密碼下架（被拒）→ 下架 → 恢復 → 作者自行下架與恢復（不寫稽核），
全程收集日誌、稽核與回應，**找不到審核備註與劇本文字**。

規格情境對應（逐一）：
- service-observability「欄位白名單」：事件測試的 `ALLOWED_FIELDS` 檢查；「不含文字」「審核與下架不含備註」：日誌掃描（本檔，含作者下架
  與恢復的那一半，第 3 組補進同一個流程；作者端自己的 HTTP 與日誌斷言另見 `tests/test_script_api_takedown.py`）。
- admin-audit-log「核可劇本」「退回不記備註」「讀取劇本」「未知的動作」：本檔的稽核斷言（核可與退回的細節、讀取先於回傳）與
  `tests/test_admin_audit_repository.py`（列舉以外被拒絕）、`tests/test_admin_service_scripts.py`（稽核寫入失敗就不回傳）。
"""
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import AdminAuditLog, Base
from app.observability.json_logging import ALLOWED_FIELDS
from tests.support.auth import GOOD_PASSWORD, FakeClock, add_user, sign_in_as
from tests.support.llm import FakeLLMProvider, Step
from tests.support.script_data import valid

NOTE = "審核備註SCAN-REVIEW-NOTE-ONE"
NOTE_TWO = "下架備註SCAN-REMOVE-NOTE-TWO"
SECRETS = {
    "note": NOTE, "note_two": NOTE_TWO,
    "nickname": "掃描暱稱SCAN-NICK", "code": "ZQ91",
    "title": "掃描劇本標題SCAN-TITLE", "title_two": "掃描第二版標題SCAN-TITLE-TWO", "tag": "掃描標籤SCANTAG",
    "background": "掃描背景SCAN-BACKGROUND-TEXT", "character": "掃描角色SCAN-CHAR",
    "milestone": "掃描里程碑SCAN-MILESTONE", "location": "掃描地點SCAN-PLACE", "summary": "掃描簡介SCAN-SUMMARY-TEXT",
}
BASE_KEYS = {"time", "level", "logger", "message"}
PASS = json.dumps({"verdict": "pass", "categories": []})


def content(**changes) -> dict:
    fields = {
        "title": SECRETS["title"], "summary": SECRETS["summary"], "tags": [SECRETS["tag"]], "background": SECRETS["background"],
        "locations": [SECRETS["location"]],
        "characters": [{"name": SECRETS["character"], "personality": "冷靜", "age": 30, "appearance": "高瘦"}],
        "milestones": [{"title": SECRETS["milestone"], "trigger": {"kind": "age", "value": 20}}],
    }
    return valid(**{**fields, **changes})


@pytest.fixture()
def world():
    app = create_app(
        load_config(environ={"APP_ENV": "testing"}), clock=FakeClock(), rpg_llm_provider=FakeLLMProvider(default=Step(PASS)),
    )
    engine = app.extensions["db_engine"]
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        author = add_user(s, 1, nickname=SECRETS["nickname"], nickname_code=SECRETS["code"])
        admin = add_user(s, 2, role="admin")
        ids = {"author": (author.id, author.token_version, author.public_id), "admin": (admin.id, admin.token_version, admin.public_id)}
    clients = {}
    for name in ("author", "admin"):
        clients[name] = app.test_client()
        sign_in_as(clients[name], ids[name][0], ids[name][1])
    return app, clients, ids


class Tour:
    """走完整個審核流程，並收集作者看得到的全部回應文字。"""

    def __init__(self, app, clients, ids):
        self.app, self.author, self.admin, self.ids = app, clients["author"], clients["admin"], ids
        self.author_texts: list[str] = []
        self.admin_responses: list = []

    def as_author(self, method: str, path: str, **kwargs):
        response = self.author.open(path, method=method, **kwargs)
        self.author_texts.append(response.get_data(as_text=True))
        return response

    def as_admin(self, method: str, path: str, **kwargs):
        response = self.admin.open(path, method=method, **kwargs)
        self.admin_responses.append(response)
        return response

    def run(self) -> dict:
        created = self.as_author("POST", "/api/scripts", json={"content": content()})
        assert created.status_code == 201, created.get_data(as_text=True)
        first = created.get_json()["version"]
        sent = self.as_author("POST", f"/api/scripts/versions/{first['id']}/submit")
        assert sent.status_code == 200 and sent.get_json()["version"]["status"] == "pending_review"

        queue = self.as_admin("GET", "/api/admin/scripts")
        assert [i["id"] for i in queue.get_json()["versions"]] == [first["id"]]
        read = self.as_admin("GET", f"/api/admin/scripts/versions/{first['id']}")
        assert read.status_code == 200 and SECRETS["background"] in read.get_data(as_text=True)
        rejected = self.as_admin("POST", f"/api/admin/scripts/versions/{first['id']}/reject", json={"category": "other_policy", "note": NOTE})
        assert rejected.status_code == 200 and rejected.get_json()["version"]["status"] == "rejected"

        listed = self.as_author("GET", "/api/scripts")                              # 作者看到結果，但沒有備註
        assert listed.status_code == 200
        self.as_author("GET", f"/api/scripts/versions/{first['id']}")
        again = self.as_author("POST", f"/api/scripts/{first['script']['id']}/versions")      # 作者由被退回的內容建立下一版
        assert again.status_code == 201, again.get_data(as_text=True)
        second = again.get_json()["version"]
        edited = self.as_author(
            "PUT", f"/api/scripts/versions/{second['id']}",
            json={"content": content(title=SECRETS["title_two"]), "base_hash": second["content_hash"]},
        )
        assert edited.status_code == 200
        resent = self.as_author("POST", f"/api/scripts/versions/{second['id']}/submit")
        assert resent.status_code == 200 and resent.get_json()["version"]["status"] == "pending_review"

        approved = self.as_admin("POST", f"/api/admin/scripts/versions/{second['id']}/approve")
        assert approved.status_code == 200 and approved.get_json()["version"]["status"] == "published"
        assert SECRETS["title_two"] in self.catalog_titles()

        refused = self.as_admin(
            "POST", f"/api/admin/scripts/versions/{second['id']}/remove",
            json={"category": "tier_mismatch", "note": NOTE_TWO, "password": "definitely-not-the-password"},
        )
        assert refused.status_code == 400 and SECRETS["title_two"] in self.catalog_titles()             # 密碼錯誤：沒有下架
        removed = self.as_admin(
            "POST", f"/api/admin/scripts/versions/{second['id']}/remove",
            json={"category": "tier_mismatch", "note": NOTE_TWO, "password": GOOD_PASSWORD},
        )
        assert removed.status_code == 200 and removed.get_json()["version"]["status"] == "removed"
        assert SECRETS["title_two"] not in self.catalog_titles()
        self.as_author("GET", "/api/scripts")
        restored = self.as_admin("POST", f"/api/admin/scripts/versions/{second['id']}/restore", json={"password": GOOD_PASSWORD})
        assert restored.status_code == 200 and restored.get_json()["version"]["status"] == "published"
        assert SECRETS["title_two"] in self.catalog_titles()

        down = self.as_author("POST", f"/api/scripts/versions/{second['id']}/remove")           # 作者自行下架與恢復（第 3 組）：不寫稽核
        assert down.status_code == 200 and down.get_json()["version"]["removed_by"] == "author"
        assert SECRETS["title_two"] not in self.catalog_titles()
        up = self.as_author("POST", f"/api/scripts/versions/{second['id']}/restore")
        assert up.status_code == 200 and up.get_json()["version"]["status"] == "published"
        assert SECRETS["title_two"] in self.catalog_titles()
        return {"first": first["id"], "second": second["id"], "script": first["script"]["id"]}

    def catalog_titles(self) -> set[str]:
        response = self.as_author("GET", "/api/sessions/catalog")
        assert response.status_code == 200
        return {card["title"] for card in response.get_json()["scripts"]}


@pytest.fixture()
def toured(world, log_lines):
    app, clients, ids = world
    tour = Tour(app, clients, ids)
    versions = tour.run()
    with Session(app.extensions["db_engine"]) as s:
        audits = list(s.scalars(select(AdminAuditLog).order_by(AdminAuditLog.id)))
    return tour, versions, audits, log_lines


def test_the_whole_review_flow_runs_in_the_documented_order_and_audits_every_step(toured):
    tour, versions, audits, _ = toured
    assert [a.action for a in audits] == [
        "script.read", "script.reject", "script.approve", "reauth.failed", "script.remove", "script.restore",
    ]
    admin_id = tour.ids["admin"][0]
    assert {a.admin_user_id for a in audits} == {admin_id}
    assert {a.target_user_public_id for a in audits} == {tour.ids["author"][2]}


def test_the_audit_details_carry_only_identifiers_status_and_category(toured):
    _, versions, audits, _ = toured
    by_action = {a.action: a.detail_json for a in audits}
    assert by_action["script.read"] == {"version_id": versions["first"], "script_id": versions["script"], "status": "pending_review"}
    assert by_action["script.reject"] == {"version_id": versions["first"], "script_id": versions["script"], "status": "rejected", "category": "other_policy"}
    assert by_action["script.approve"] == {"version_id": versions["second"], "script_id": versions["script"], "status": "published"}
    assert by_action["reauth.failed"] == {"attempted": "script.remove"}
    assert by_action["script.remove"]["category"] == "tier_mismatch" and by_action["script.remove"]["status"] == "removed"
    assert by_action["script.restore"]["status"] == "published"
    stored = repr([(a.action, a.reason, a.detail_json, a.target_user_public_id) for a in audits])
    for name, secret in SECRETS.items():
        assert secret not in stored, f"稽核洩漏了：{name}"


def test_the_authors_own_responses_never_contain_either_review_note(toured):
    tour, *_ = toured
    assert tour.author_texts
    for text in tour.author_texts:
        assert NOTE not in text and NOTE_TWO not in text


def test_the_logs_never_contain_the_notes_or_any_script_text(toured):
    *_, log_lines = toured
    raw = log_lines.raw()
    assert raw, "日誌是空的，掃描沒有意義"
    for name, secret in SECRETS.items():
        assert secret not in raw, f"日誌洩漏了：{name}"


def test_the_review_events_are_logged_with_only_fixed_values_and_every_field_is_whitelisted(toured):
    tour, _, _, log_lines = toured
    lines = log_lines()
    events = [line for line in lines if str(line.get("event", "")).startswith(("script.", "admin."))]
    admin_id = tour.ids["admin"][0]
    by_event = {}
    for line in events:
        by_event.setdefault(line["event"], []).append(line)
    assert [l["reason"] for l in by_event["script.rejected"]] == ["other_policy"]
    author_id = tour.ids["author"][0]
    assert [(l["kind"], l.get("reason")) for l in by_event["script.removed"]] == [("admin", "tier_mismatch"), ("author", None)]
    assert [l["kind"] for l in by_event["script.restored"]] == ["admin", "author"] and len(by_event["script.approved"]) == 1
    assert {l["user_id"] for name in ("script.approved", "script.rejected") for l in by_event[name]} == {admin_id}
    assert [l["user_id"] for l in by_event["script.removed"]] == [admin_id, author_id]       # 作者自行下架記的是作者
    assert [l["user_id"] for l in by_event["script.restored"]] == [admin_id, author_id]
    # 讀取不寫「管理員操作」事件；改變資料的四個動作與失敗的重新輸入密碼各一筆
    actions = [l["reason"] for l in by_event["admin.action"]]
    assert sorted(actions) == ["script.approve", "script.reject", "script.remove", "script.restore"]
    assert [l["reason"] for l in by_event["admin.reauth_failed"]] == ["script.remove"]
    for line in lines:
        assert set(line) <= BASE_KEYS | set(ALLOWED_FIELDS), f"出現不允許的欄位：{set(line) - BASE_KEYS - set(ALLOWED_FIELDS)}"


def test_the_admin_can_read_the_note_back_only_through_the_read_endpoint(toured):
    tour, versions, *_ = toured
    read = tour.as_admin("GET", f"/api/admin/scripts/versions/{versions['first']}").get_json()["version"]
    assert read["decision_note"] == NOTE and read["decision_category"] == "other_policy"
    listing = tour.as_admin("GET", "/api/admin/scripts?status=published")
    assert NOTE not in listing.get_data(as_text=True) and NOTE_TWO not in listing.get_data(as_text=True)
