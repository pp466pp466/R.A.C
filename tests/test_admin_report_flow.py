"""rpg-script-reports 的 admin-script-reports、admin-audit-log 與 service-observability 規格的整合驗證（全程 HTTP，作者的創作與送審走假模型）：

流程一：玩家甲建立並送審劇本 → 管理員核可 → 玩家乙、丙以含特殊字串的文字檢舉（丙勾紅線理由；另一份劇本有更多筆但都不是紅線）
→ 被檢舉清單的排序 → 管理員讀取檢舉（稽核先於回傳）→ 結案為「不成立」→ 玩家丁再檢舉 → 管理員下架（密碼）→ 檢舉自動結為「下架」→
管理員恢復上架，檢舉不重開 → 玩家戊檢舉 → 新版本核可，舊版本的檢舉結為「被取代」。
流程二：新版本核可 → 舊版本的檢舉結為「被取代」，管理員仍讀得到（結果為 `superseded`）。

全程收集日誌、稽核與全部回應，**找不到自寫文字、理由與標題**（作者與玩家端的回應也不含任何檢舉資訊）；每一行日誌的欄位都在白名單內。
"""
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import AdminAuditLog, Base, ScriptReport, ScriptVersion, User
from app.models.script import VERSION_PUBLISHED
from app.observability.json_logging import ALLOWED_FIELDS
from app.repositories.script_repository import ScriptRepository
from tests.support.auth import GOOD_PASSWORD, FakeClock, add_user, sign_in_as
from tests.support.llm import FakeLLMProvider, Step
from tests.support.script_data import valid
from tests.support.script_review import put_in_status

TEXTS = {
    "b": "掃描檢舉文字乙SCAN-REPORT-TEXT-B <b>x</b> 'q'; DROP TABLE users -- 😀",
    "c": "掃描檢舉文字丙SCAN-REPORT-TEXT-C",
    "d": "掃描檢舉文字丁SCAN-REPORT-TEXT-D",
    "e": "掃描檢舉文字戊SCAN-REPORT-TEXT-E",
}
SECRETS = {
    **{f"text_{name}": text for name, text in TEXTS.items()},
    "note": "審核備註SCAN-REVIEW-NOTE",
    "title": "掃描劇本標題SCAN-TITLE", "title_two": "掃描第二版標題SCAN-TITLE-TWO", "other_title": "掃描另一份劇本SCAN-OTHER-TITLE",
    "tag": "掃描標籤SCANTAG", "background": "掃描背景SCAN-BACKGROUND-TEXT", "character": "掃描角色SCAN-CHAR",
    "nickname": "掃描筆名甲SCAN-NICK", "code": "ZQ91",
}
BASE_KEYS = {"time", "level", "logger", "message"}
PASS = json.dumps({"verdict": "pass", "categories": []})
REPORT = "/api/sessions/catalog/{}/report"


def content(**changes) -> dict:
    fields = {
        "title": SECRETS["title"], "summary": "掃描簡介SCAN-SUMMARY", "tags": [SECRETS["tag"]], "background": SECRETS["background"],
        "characters": [{"name": SECRETS["character"], "personality": "冷靜", "age": 30, "appearance": "高瘦"}],
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
        reporters = [add_user(s, n) for n in (3, 4, 5, 6)]
        people = {"author": author, "admin": admin, "b": reporters[0], "c": reporters[1], "d": reporters[2], "e": reporters[3]}
        ids = {name: (user.id, user.token_version, user.public_id, user.email, user.display_name) for name, user in people.items()}
    clients = {}
    for name, (uid, tv, *_rest) in ids.items():
        clients[name] = app.test_client()
        sign_in_as(clients[name], uid, tv)
    return app, clients, ids


class Tour:
    """走完整個流程，並收集每個角色拿到的全部回應文字。"""

    def __init__(self, app, clients, ids):
        self.app, self.clients, self.ids = app, clients, ids
        self.engine = app.extensions["db_engine"]
        self.texts: dict[str, list[str]] = {name: [] for name in clients}

    def call(self, who: str, method: str, path: str, **kwargs):
        response = self.clients[who].open(path, method=method, **kwargs)
        self.texts[who].append(response.get_data(as_text=True))
        return response

    def admin_script(self, version: str, action: str, **kwargs):
        return self.call("admin", "POST", f"/api/admin/scripts/versions/{version}/{action}", **kwargs)

    def create_published(self, **changes) -> dict:
        """作者建立、送審（假模型通過），管理員核可；回傳 `{id, script}`。"""
        created = self.call("author", "POST", "/api/scripts", json={"content": content(**changes)})
        assert created.status_code == 201, created.get_data(as_text=True)
        version = created.get_json()["version"]
        assert self.call("author", "POST", f"/api/scripts/versions/{version['id']}/submit").status_code == 200
        assert self.admin_script(version["id"], "approve").status_code == 200
        return {"id": version["id"], "script": version["script"]["id"]}

    def new_version(self, script_id: str, **changes) -> str:
        created = self.call("author", "POST", f"/api/scripts/{script_id}/versions")
        assert created.status_code == 201, created.get_data(as_text=True)
        version = created.get_json()["version"]
        edited = self.call(
            "author", "PUT", f"/api/scripts/versions/{version['id']}", json={"content": content(**changes), "base_hash": version["content_hash"]},
        )
        assert edited.status_code == 200, edited.get_data(as_text=True)
        assert self.call("author", "POST", f"/api/scripts/versions/{version['id']}/submit").status_code == 200
        return version["id"]

    def report(self, who: str, version: str, reasons: list[str]):
        response = self.call(who, "POST", REPORT.format(version), json={"reasons": reasons, "text": TEXTS[who]})
        assert response.status_code == 201, response.get_data(as_text=True)

    def other_published(self) -> str:
        """另一份上架中的玩家劇本（直接寫入，不經審核）。"""
        with Session(self.engine) as s:
            owner = s.get(User, self.ids["author"][0])
            version = put_in_status(ScriptRepository(s), owner, VERSION_PUBLISHED, title=SECRETS["other_title"])
            s.commit()
            return version.public_id

    def results(self, version: str) -> list:
        """這個版本全部檢舉的結案結果，依建立順序（未結案為 None）。"""
        with Session(self.engine) as s:
            rows = s.execute(
                select(ScriptReport.closed_result).join(ScriptVersion, ScriptVersion.id == ScriptReport.version_id)
                .where(ScriptVersion.public_id == version).order_by(ScriptReport.id)
            ).all()
            return [row[0] for row in rows]

    def status(self, version: str) -> str:
        with Session(self.engine) as s:
            return s.scalar(select(ScriptVersion.status).where(ScriptVersion.public_id == version))

    def queue(self) -> list[str]:
        response = self.call("admin", "GET", "/api/admin/scripts?status=reported")
        assert response.status_code == 200
        return [item["id"] for item in response.get_json()["versions"]]

    def audits(self) -> list[AdminAuditLog]:
        with Session(self.engine) as s:
            return list(s.scalars(select(AdminAuditLog).order_by(AdminAuditLog.id)))

    def catalog_ids(self) -> set[str]:
        response = self.call("b", "GET", "/api/sessions/catalog")
        assert response.status_code == 200
        return {card["id"] for card in response.get_json()["scripts"]}


def run_first_flow(tour: Tour) -> dict:
    first = tour.create_published()
    other = tour.other_published()
    # 玩家乙、丙檢舉；丙勾紅線理由。另一份劇本被三位玩家檢舉但都不是紅線
    tour.report("b", first["id"], ["plagiarism"])
    tour.report("c", first["id"], ["minor_sexual", "other"])
    for who in ("b", "c", "d"):
        tour.report(who, other, ["other"])
    assert tour.queue() == [first["id"], other], "紅線的一筆應排在較多筆但非紅線的前面"

    read = tour.call("admin", "GET", f"/api/admin/scripts/versions/{first['id']}/reports")
    assert read.status_code == 200 and len(read.get_json()["reports"]) == 2
    assert TEXTS["b"] in read.get_data(as_text=True) and TEXTS["c"] in read.get_data(as_text=True)

    dismissed = tour.admin_script(first["id"], "reports/dismiss")
    assert dismissed.status_code == 200 and dismissed.get_json()["version"]["report_count"] == 0
    assert tour.results(first["id"]) == ["dismissed", "dismissed"] and tour.status(first["id"]) == "published"
    assert tour.queue() == [other]

    tour.report("d", first["id"], ["tier_mismatch"])                         # 結案之後再被檢舉：重新出現在清單
    assert tour.queue() == [other, first["id"]]

    refused = tour.admin_script(first["id"], "remove", json={"category": "other_policy", "note": SECRETS["note"], "password": "definitely-not-the-password"})
    assert refused.status_code == 400 and tour.results(first["id"]) == ["dismissed", "dismissed", None]
    removed = tour.admin_script(first["id"], "remove", json={"category": "other_policy", "note": SECRETS["note"], "password": GOOD_PASSWORD})
    assert removed.status_code == 200 and tour.status(first["id"]) == "removed"
    assert tour.results(first["id"]) == ["dismissed", "dismissed", "removed"]
    assert tour.queue() == [other] and first["id"] not in tour.catalog_ids()

    restored = tour.admin_script(first["id"], "restore", json={"password": GOOD_PASSWORD})
    assert restored.status_code == 200 and tour.status(first["id"]) == "published"
    assert tour.results(first["id"]) == ["dismissed", "dismissed", "removed"], "恢復上架不應重開已結案的檢舉"
    assert tour.queue() == [other] and first["id"] in tour.catalog_ids()

    tour.report("e", first["id"], ["other"])                                  # 恢復之後新的玩家仍可檢舉
    assert tour.queue() == [other, first["id"]]
    second = tour.new_version(first["script"], title=SECRETS["title_two"])
    assert tour.admin_script(second, "approve").status_code == 200
    assert tour.status(first["id"]) == "superseded"
    assert tour.results(first["id"]) == ["dismissed", "dismissed", "removed", "superseded"]
    assert tour.queue() == [other] and tour.results(other) == [None] * 3
    return {"first": first["id"], "second": second, "other": other, "script": first["script"]}


@pytest.fixture()
def toured(world, log_lines):
    app, clients, ids = world
    tour = Tour(app, clients, ids)
    versions = run_first_flow(tour)
    return tour, versions, tour.audits(), log_lines


def test_the_whole_flow_audits_every_admin_step_in_order_and_only_with_identifiers_and_counts(toured):
    tour, versions, audits, _ = toured
    assert [a.action for a in audits] == [
        "script.approve", "report.read", "report.dismiss", "reauth.failed", "script.remove", "script.restore", "script.approve",
    ]
    admin_id = tour.ids["admin"][0]
    assert {a.admin_user_id for a in audits} == {admin_id}
    by_action = {a.action: a.detail_json for a in audits if a.action.startswith("report.")}
    assert by_action["report.read"] == {"version_id": versions["first"], "script_id": versions["script"], "count": 2}
    assert by_action["report.dismiss"] == {"version_id": versions["first"], "script_id": versions["script"], "count": 2}
    assert next(a for a in audits if a.action == "report.read").target_user_public_id == tour.ids["author"][2]


def test_the_report_read_is_audited_before_the_reports_are_returned(world):
    """讀取檢舉的回應回到管理員手上時，稽核早已寫入（以「請求之間」觀察：回應收到之後，資料庫裡已有該筆）。"""
    app, clients, ids = world
    tour = Tour(app, clients, ids)
    version = tour.create_published()
    tour.report("b", version["id"], ["other"])
    before = len(tour.audits())
    assert tour.call("admin", "GET", f"/api/admin/scripts/versions/{version['id']}/reports").status_code == 200
    after = tour.audits()
    assert len(after) == before + 1 and after[-1].action == "report.read"


def test_the_audit_the_logs_and_the_responses_never_contain_the_report_text_the_title_or_the_reporter(toured):
    tour, versions, audits, log_lines = toured
    stored = repr([(a.action, a.reason, a.detail_json, a.target_user_public_id, a.target_conversation_public_id) for a in audits])
    raw = log_lines.raw()
    assert raw, "日誌是空的，掃描沒有意義"
    for name, secret in SECRETS.items():
        assert secret not in stored, f"稽核洩漏了：{name}"
        assert secret not in raw, f"日誌洩漏了：{name}"
    for reporter in ("b", "c", "d", "e"):
        _, _, public_id, email, display_name = tour.ids[reporter]
        for secret in (public_id, email, display_name):
            assert secret not in raw and secret not in stored, reporter
    for identifier in (versions["first"], versions["second"], versions["other"]):
        assert identifier not in raw, "日誌含有版本的對外識別碼"
    for reason in ("minor_sexual", "plagiarism", "tier_mismatch"):
        assert f'"{reason}"' not in raw and f"'{reason}'" not in raw, f"日誌含有理由內容：{reason}"


def test_only_the_one_endpoint_that_reads_reports_ever_returns_the_report_text(toured):
    tour, *_ = toured
    for who, texts in tour.texts.items():
        for text in texts:
            hits = [name for name, secret in TEXTS.items() if secret in text]
            if who == "admin" and hits:
                assert text.startswith('{"reports"'), "管理員回應中，只有讀取檢舉的那個回應含自寫文字"
            else:
                assert not hits, f"{who} 的回應含有檢舉文字：{hits}"


def test_the_admin_never_learns_who_reported_and_the_author_and_players_see_no_report_data(toured):
    tour, *_ = toured
    admin_text = " ".join(tour.texts["admin"])
    for reporter in ("b", "c", "d", "e"):
        _, _, public_id, email, display_name = tour.ids[reporter]
        for secret in (public_id, email, display_name):
            assert secret not in admin_text, f"管理員的回應含有檢舉人的 {secret}"
    for who in ("author", "b", "c", "d", "e"):
        text = " ".join(tour.texts[who])
        assert "report_count" not in text and "red_line" not in text and "dismissed" not in text, who


def test_the_events_are_logged_with_fixed_values_only_and_every_field_is_whitelisted(toured):
    tour, _, _, log_lines = toured
    lines = log_lines()
    by_event: dict[str, list[dict]] = {}
    for line in lines:
        by_event.setdefault(str(line.get("event")), []).append(line)
    admin_id = tour.ids["admin"][0]
    assert [(l["user_id"], l["count"]) for l in by_event["script.reports_dismissed"]] == [(admin_id, 2)]
    assert [(l["count"], l["outcome"]) for l in by_event["script.reports_auto_closed"]] == [(1, "removed"), (1, "superseded")]
    actions = sorted(l["reason"] for l in by_event["admin.action"])
    assert actions == sorted(["script.approve", "script.approve", "report.dismiss", "script.remove", "script.restore"])   # 讀取不寫「管理員操作」事件
    for line in lines:
        assert set(line) <= BASE_KEYS | set(ALLOWED_FIELDS), f"出現不允許的欄位：{set(line) - BASE_KEYS - set(ALLOWED_FIELDS)}"


# ---- 流程二：新版本核可，舊版本的檢舉結為「被取代」-----------------------------------------------------------


def test_approving_a_new_version_closes_the_old_versions_reports_as_superseded_and_the_admin_still_reads_them(world, log_lines):
    app, clients, ids = world
    tour = Tour(app, clients, ids)
    first = tour.create_published()
    tour.report("b", first["id"], ["plagiarism"])
    tour.report("c", first["id"], ["other"])
    assert tour.queue() == [first["id"]]
    second = tour.new_version(first["script"], title=SECRETS["title_two"])
    assert tour.queue() == [first["id"]]                                          # 新版本只是待審，舊版本仍上架中且檢舉未結案
    assert tour.admin_script(second, "approve").status_code == 200
    assert tour.status(first["id"]) == "superseded" and tour.results(first["id"]) == ["superseded", "superseded"]
    assert tour.queue() == []

    read = tour.call("admin", "GET", f"/api/admin/scripts/versions/{first['id']}/reports")
    assert read.status_code == 200
    assert [r["closed_result"] for r in read.get_json()["reports"]] == ["superseded", "superseded"]
    assert all(r["closed_at"] for r in read.get_json()["reports"])
    assert tour.admin_script(first["id"], "reports/dismiss").status_code == 409        # 已結案：沒有未結案可結
    assert tour.call("admin", "GET", f"/api/admin/scripts/versions/{second}/reports").get_json() == {"reports": []}

    raw = log_lines.raw()
    for name, secret in SECRETS.items():
        assert secret not in raw, f"日誌洩漏了：{name}"
    stored = repr([(a.action, a.detail_json) for a in tour.audits()])
    for secret in (TEXTS["b"], TEXTS["c"], SECRETS["title"], SECRETS["title_two"]):
        assert secret not in stored
