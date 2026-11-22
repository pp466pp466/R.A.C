"""rpg-script-reports 的 admin-script-reports 規格的 HTTP 行為：`GET /api/admin/scripts?status=reported`、
`GET /api/admin/scripts/versions/<id>/reports`、`POST /api/admin/scripts/versions/<id>/reports/dismiss`——

身分矩陣（未登入 401、一般使用者 404 且與不存在的端點逐字相同、**早於本文解析**）、CSRF、`no-store`、先稽核再回傳、
草稿與不存在逐字相同的 404、回應沒有檢舉人、結案不需要密碼、`no_open_reports`，以及結案為「下架」沿用密碼保護的下架。
"""
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import AdminAuditLog, Base, ScriptReport, ScriptVersion
from app.repositories.script_report_repository import ScriptReportRepository
from app.repositories.script_repository import ScriptRepository
from app.services import admin_script_views
from tests.support.auth import GOOD_PASSWORD, FakeClock, add_user
from tests.support.client import raw_client
from tests.support.idor import logged_in_client
from tests.support.llm import FakeLLMProvider
from tests.support.script_review import put_in_status

TEXT = "檢舉的自寫文字：<b>不是標記</b> 'x'; DROP TABLE users -- 😀"
TITLE = "不該出現在稽核的標題"
MISSING = "00000000-0000-4000-8000-000000000000"
PASSWORD_BODY = {"password": GOOD_PASSWORD}


class _Ref:
    def __init__(self, user):
        self.id, self.token_version, self.public_id, self.email, self.display_name = (
            user.id, user.token_version, user.public_id, user.email, user.display_name,
        )


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
            self.reporters = [add_user(s, n) for n in (11, 12, 13)]
            self.admin_ref, self.author_ref, self.player_ref = (_Ref(u) for u in (self.admin, self.author, self.player))
            self.reporter_refs = [_Ref(u) for u in self.reporters]

    def client(self, who):
        return self.app.test_client() if who == "anonymous" else logged_in_client(self.app, getattr(self, f"{who}_ref"))

    def make(self, status: str, **kwargs) -> str:
        with Session(self.engine) as s:
            author = s.get(type(self.author), self.author_ref.id)
            version = put_in_status(ScriptRepository(s), author, status, **kwargs)
            s.commit()
            return version.public_id

    def report(self, version_public_id: str, who: int = 0, *, reasons=("other",), red_line=False, note=None) -> None:
        with Session(self.engine) as s:
            version = s.scalar(select(ScriptVersion).where(ScriptVersion.public_id == version_public_id))
            ScriptReportRepository(s).add(self.reporter_refs[who].id, version.id, list(reasons), red_line, note, now=self.clock())
            s.commit()

    def states(self, version_public_id: str) -> list[tuple]:
        with Session(self.engine) as s:
            rows = s.execute(
                select(ScriptReport.closed_result).join(ScriptVersion, ScriptVersion.id == ScriptReport.version_id)
                .where(ScriptVersion.public_id == version_public_id).order_by(ScriptReport.id)
            ).all()
            return [row[0] for row in rows]

    def status(self, version_public_id: str) -> str:
        with Session(self.engine) as s:
            return s.scalar(select(ScriptVersion.status).where(ScriptVersion.public_id == version_public_id))

    def audits(self) -> list[AdminAuditLog]:
        with Session(self.engine) as s:
            return list(s.scalars(select(AdminAuditLog).order_by(AdminAuditLog.id)))


@pytest.fixture()
def world():
    return World()


def scripts_url(suffix: str = "") -> str:
    return f"/api/admin/scripts{suffix}"


def reports_url(public_id: str, action: str = "") -> str:
    return scripts_url(f"/versions/{public_id}/reports" + (f"/{action}" if action else ""))


def error_of(response) -> dict:
    return response.get_json()["error"]


ENDPOINTS = [
    ("GET", scripts_url() + "?status=reported", None),
    ("GET", scripts_url("/versions/{id}/reports"), None),
    ("POST", scripts_url("/versions/{id}/reports/dismiss"), None),
]


def reported_version(world: World, **kwargs) -> str:
    version = world.make("published", **kwargs)
    world.report(version, 0, note=TEXT)
    return version


# ---- 身分矩陣 ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("method, path, body", ENDPOINTS)
def test_an_anonymous_visitor_gets_401_and_nothing_changes(world, method, path, body):
    version = reported_version(world)
    response = world.client("anonymous").open(path.format(id=version), method=method, json=body)
    assert response.status_code == 401
    assert world.states(version) == [None] and world.audits() == []


@pytest.mark.parametrize("who", ["author", "player"])
@pytest.mark.parametrize("method, path, body", ENDPOINTS)
def test_a_non_admin_gets_the_same_404_as_a_missing_endpoint_and_nothing_changes(world, who, method, path, body):
    version = reported_version(world)
    response = world.client(who).open(path.format(id=version), method=method, json=body)
    missing = world.client(who).get("/api/admin/this-endpoint-does-not-exist")
    assert response.status_code == 404 and response.get_json() == missing.get_json()
    assert world.states(version) == [None] and world.audits() == []


def test_a_reporter_cannot_use_the_endpoints_either(world):
    """檢舉人自己也只是一般使用者：不能讀檢舉（含自己的）、也不能結案。"""
    version = reported_version(world)
    client = logged_in_client(world.app, world.reporter_refs[0])
    assert client.get(reports_url(version)).status_code == 404
    assert client.post(reports_url(version, "dismiss")).status_code == 404
    assert world.states(version) == [None] and world.audits() == []


def test_a_non_admin_gets_404_for_a_malformed_body_or_query_not_400_or_415(world):
    version = reported_version(world)
    client = world.client("player")
    for kwargs in ({"data": "not json at all", "content_type": "text/plain"}, {"data": "[1, 2", "content_type": "application/json"}, {}):
        assert client.post(reports_url(version, "dismiss"), **kwargs).status_code == 404, kwargs
    assert client.get(reports_url(version) + "?limit=abc&x=%00").status_code == 404
    assert client.get(scripts_url() + "?status=reported&limit=abc").status_code == 404


def test_a_post_without_the_csrf_token_is_refused_and_nothing_changes(world):
    version = reported_version(world)
    raw = raw_client(world.app)
    with raw.session_transaction() as session:
        session["uid"], session["tv"] = world.admin_ref.id, world.admin_ref.token_version
    response = raw.post(reports_url(version, "dismiss"))
    assert response.status_code == 403 and world.states(version) == [None] and world.audits() == []


@pytest.mark.parametrize("method, path, body", ENDPOINTS)
def test_every_response_including_errors_is_not_cacheable(world, method, path, body):
    version = reported_version(world)
    ok = world.client("admin").open(path.format(id=version), method=method, json=body)
    missing = world.client("admin").open(path.format(id=MISSING), method=method, json=body) if "{id}" in path else ok
    for response in (ok, missing):
        assert "no-store" in response.headers["Cache-Control"], (method, path, response.status_code)


# ---- 被檢舉清單 ----------------------------------------------------------------------------------------


def test_the_reported_list_shows_only_published_versions_with_open_reports_in_the_documented_shape(world):
    hot = world.make("published", title="被檢舉的")
    world.make("published", title="沒被檢舉")
    world.make("pending_review", title="待審")
    world.report(hot, 0, reasons=("minor_sexual",), red_line=True, note=TEXT)
    world.report(hot, 1, reasons=("plagiarism",))
    response = world.client("admin").get(scripts_url() + "?status=reported")
    assert response.status_code == 200
    [item] = response.get_json()["versions"]
    assert item["id"] == hot and set(item) == set(admin_script_views.LIST_KEYS)
    assert (item["status"], item["report_count"], item["red_line_reported"]) == ("published", 2, True)
    assert world.audits() == []


def test_the_reported_list_is_ordered_by_priority_and_the_front_end_does_not_need_to_sort(world):
    plain_many = world.make("published", title="抄襲兩筆")
    red_one = world.make("published", title="紅線一筆")
    world.report(plain_many, 0, reasons=("plagiarism",)), world.report(plain_many, 1, reasons=("plagiarism",))
    world.report(red_one, 0, reasons=("real_person_sexual",), red_line=True)
    titles = [i["title"] for i in world.client("admin").get(scripts_url() + "?status=reported").get_json()["versions"]]
    assert titles == ["紅線一筆", "抄襲兩筆"]


def test_the_list_has_no_report_text_reason_or_reporter(world):
    reported_version(world)
    text = world.client("admin").get(scripts_url() + "?status=reported").get_data(as_text=True)
    for forbidden in (TEXT, "DROP TABLE", "reasons", "reporter", "reporter_id", world.reporters[0].email, world.reporter_refs[0].public_id):
        assert forbidden not in text, forbidden


def test_the_published_list_carries_the_counters_and_paging_works_for_the_reported_view(world):
    for n in (1, 2, 3):
        version = world.make("published", title=f"第{n}份")
        for who in range(3 - n + 1):
            world.report(version, who)
    page = world.client("admin").get(scripts_url() + "?status=reported&limit=1&offset=1").get_json()["versions"]
    assert [i["title"] for i in page] == ["第2份"]
    published = world.client("admin").get(scripts_url() + "?status=published").get_json()["versions"]
    assert sorted(i["report_count"] for i in published) == [1, 2, 3]


@pytest.mark.parametrize("query", ["status=draft", "status=Reported", "status="])
def test_an_unknown_status_is_a_400_that_mentions_the_reported_view(world, query):
    response = world.client("admin").get(scripts_url() + "?" + query)
    assert response.status_code == 400 and "reported" in error_of(response)["fields"]["status"]


# ---- 讀取檢舉內容 --------------------------------------------------------------------------------------


def test_reading_returns_the_reports_after_one_audit_and_without_any_reporter(world):
    version = world.make("published", title=TITLE)
    world.report(version, 0, reasons=("plagiarism", "other"), note=TEXT)
    world.report(version, 1, reasons=("minor_sexual",), red_line=True)
    response = world.client("admin").get(reports_url(version))
    assert response.status_code == 200
    reports = response.get_json()["reports"]
    assert set(response.get_json()) == {"reports"} and len(reports) == 2
    assert all(set(item) == set(admin_script_views.REPORT_KEYS) for item in reports)
    assert (reports[0]["reasons"], reports[0]["text"], reports[0]["closed_at"], reports[0]["closed_result"]) == (["plagiarism", "other"], TEXT, None, None)
    assert reports[1]["reasons"] == ["minor_sexual"] and reports[1]["text"] is None
    text = response.get_data(as_text=True)
    for reporter in world.reporters:
        for secret in (reporter.email, reporter.public_id, reporter.display_name):
            assert secret not in text, secret
    [audit] = world.audits()
    assert audit.action == "report.read" and audit.target_user_public_id == world.author_ref.public_id
    assert set(audit.detail_json) == {"version_id", "script_id", "count"} and audit.detail_json["count"] == 2
    assert audit.detail_json["version_id"] == version


def test_each_read_writes_its_own_audit(world):
    version = reported_version(world)
    client = world.client("admin")
    for _ in range(3):
        assert client.get(reports_url(version)).status_code == 200
    assert [a.action for a in world.audits()] == ["report.read"] * 3


def test_a_draft_and_a_missing_id_are_the_same_404_for_both_endpoints_without_an_audit(world):
    draft = world.make("draft")
    client = world.client("admin")
    for send, action in ((client.get, ""), (client.post, "dismiss")):
        first = send(reports_url(draft, action))
        second = send(reports_url(MISSING, action))
        assert first.status_code == second.status_code == 404 and first.get_json() == second.get_json(), action
    assert world.audits() == []


def test_the_audit_never_contains_the_report_text_the_reasons_the_reporter_or_the_title(world):
    version = world.make("published", title=TITLE)
    world.report(version, 0, reasons=("minor_sexual",), red_line=True, note=TEXT)
    client = world.client("admin")
    client.get(reports_url(version))
    client.post(reports_url(version, "dismiss"))
    stored = repr([(a.action, a.target_user_public_id, a.reason, a.detail_json) for a in world.audits()])
    for text in (TEXT, "DROP TABLE", TITLE, "minor_sexual", world.reporters[0].email, world.reporter_refs[0].public_id, "筆名"):
        assert text not in stored, text


# ---- 結案為不成立 --------------------------------------------------------------------------------------


def test_dismissing_without_any_body_or_password_closes_everything_and_leaves_the_version_published(world):
    version = world.make("published")
    for who in range(3):
        world.report(version, who, note=TEXT)
    response = world.client("admin").post(reports_url(version, "dismiss"))
    assert response.status_code == 200
    body = response.get_json()["version"]
    assert (body["id"], body["status"], body["report_count"], body["red_line_reported"]) == (version, "published", 0, False)
    assert world.states(version) == ["dismissed"] * 3 and world.status(version) == "published"
    [audit] = world.audits()
    assert audit.action == "report.dismiss" and audit.detail_json["count"] == 3 and audit.target_user_public_id == world.author_ref.public_id
    assert TEXT not in response.get_data(as_text=True)


def test_dismissing_ignores_whatever_body_is_sent(world):
    version = reported_version(world)
    response = world.client("admin").post(reports_url(version, "dismiss"), json={"password": "wrong", "x": 1})
    assert response.status_code == 200 and world.states(version) == ["dismissed"]
    assert [a.action for a in world.audits()] == ["report.dismiss"]                # 沒有 reauth.failed：這個操作不看密碼


def test_dismissing_with_nothing_open_is_a_409_no_open_reports_and_leaves_no_audit(world):
    quiet = world.make("published")
    response = world.client("admin").post(reports_url(quiet, "dismiss"))
    assert response.status_code == 409 and error_of(response)["code"] == "no_open_reports" and world.audits() == []
    version = reported_version(world)
    assert world.client("admin").post(reports_url(version, "dismiss")).status_code == 200
    again = world.client("admin").post(reports_url(version, "dismiss"))
    assert again.status_code == 409 and error_of(again)["code"] == "no_open_reports" and len(world.audits()) == 1


def test_after_dismissal_the_version_leaves_the_reported_list_and_a_new_report_brings_it_back(world):
    version = reported_version(world)
    client = world.client("admin")
    assert [i["id"] for i in client.get(scripts_url() + "?status=reported").get_json()["versions"]] == [version]
    client.post(reports_url(version, "dismiss"))
    assert client.get(scripts_url() + "?status=reported").get_json()["versions"] == []
    world.report(version, 1)
    assert [i["id"] for i in client.get(scripts_url() + "?status=reported").get_json()["versions"]] == [version]
    closed = client.get(reports_url(version)).get_json()["reports"]
    assert [r["closed_result"] for r in closed] == ["dismissed", None]


# ---- 結案為下架（沿用密碼保護的下架）---------------------------------------------------------------------


def test_a_takedown_with_the_password_closes_the_reports_as_removed_and_they_leave_the_list(world):
    version = reported_version(world)
    client = world.client("admin")
    response = client.post(scripts_url(f"/versions/{version}/remove"), json={"category": "other_policy", **PASSWORD_BODY})
    assert response.status_code == 200 and response.get_json()["version"]["report_count"] == 0
    assert world.states(version) == ["removed"] and world.status(version) == "removed"
    assert client.get(scripts_url() + "?status=reported").get_json()["versions"] == []
    reports = client.get(reports_url(version)).get_json()["reports"]
    assert [r["closed_result"] for r in reports] == ["removed"]
    assert [a.action for a in world.audits()] == ["script.remove", "report.read"]


def test_a_takedown_with_a_wrong_password_leaves_the_reports_open(world):
    version = reported_version(world)
    response = world.client("admin").post(
        scripts_url(f"/versions/{version}/remove"), json={"category": "other_policy", "password": "definitely-not-the-password"},
    )
    assert response.status_code == 400 and "password" in error_of(response)["fields"]
    assert world.states(version) == [None] and world.status(version) == "published"
    assert [a.action for a in world.audits()] == ["reauth.failed"]


def test_there_is_no_password_free_takedown_endpoint(world):
    version = reported_version(world)
    for path in (reports_url(version, "remove"), reports_url(version, "takedown"), reports_url(version, "close")):
        assert world.client("admin").post(path).status_code in (404, 405), path
    assert world.status(version) == "published" and world.states(version) == [None]
