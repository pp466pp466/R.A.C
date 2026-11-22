"""service-observability 規格「檢舉與匿名保留事件只含白名單欄位」（rpg-script-reports 的 2.4）：整個檢舉流程走完後，日誌裡找不到檢舉的自寫文字、
理由陣列的內容、劇本標題與標籤與內容、筆名與短代號、版本的對外識別碼，而且每一行的欄位都在日誌的白名單內。

流程（全部走 HTTP 與 flask 指令）：玩家檢舉（含壞的理由、重複、被每日上限擋下、找不到、自己的劇本）→ 新版本核可，舊版本的檢舉自動結為「被取代」→
又有玩家檢舉新版本 → 作者自行下架，檢舉自動結為「下架」。管理員的讀取與結案（`report.read`、`report.dismiss`、管理員下架）的掃描在
`tests/test_admin_report_flow.py`（日誌、稽核與全部回應）；匿名保留的抹除在 `test_account_erasure_retain.py`。
"""
import json
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base, ScriptReport, User
from app.models.script import VERSION_PUBLISHED
from app.observability.json_logging import ALLOWED_FIELDS
from app.repositories.script_repository import ScriptRepository
from tests.support.auth import START, FakeClock, add_user, sign_in_as
from tests.support.llm import FakeLLMProvider
from tests.support.script_review import new_draft, put_in_status, script_public_id

SECRETS = {
    "text_b": "掃描檢舉文字乙SCAN-REPORT-TEXT-B",
    "text_c": "掃描檢舉文字丙SCAN-REPORT-TEXT-C",
    "text_d": "掃描檢舉文字丁SCAN-REPORT-TEXT-D",
    "bad_reason": "SCAN-BAD-REASON-VALUE",
    "title": "掃描劇本標題SCAN-TITLE",
    "title_two": "掃描第二版標題SCAN-TITLE-TWO",
    "tag": "掃描標籤SCANTAG",
    "background": "掃描背景SCAN-BACKGROUND-TEXT",
    "character": "掃描角色SCAN-CHAR",
    "nickname": "掃描筆名甲SCAN-NICK",
    "code": "ZQ91",
}
BASE_KEYS = {"time", "level", "logger", "message"}


@pytest.fixture()
def world():
    app = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock(), rpg_llm_provider=FakeLLMProvider())
    Base.metadata.create_all(app.extensions["db_engine"])
    with Session(app.extensions["db_engine"]) as s:
        author = add_user(s, 1, nickname=SECRETS["nickname"], nickname_code=SECRETS["code"])
        admin = add_user(s, 2, role="admin")
        reporters = [add_user(s, n) for n in (3, 4, 5)]
        ids = {user.email: (user.id, user.token_version) for user in [author, admin, *reporters]}
        repo = ScriptRepository(s)
        first = put_in_status(
            repo, author, VERSION_PUBLISHED, title=SECRETS["title"], tags=[SECRETS["tag"]], background=SECRETS["background"],
            characters=[{"name": SECRETS["character"], "personality": "冷靜", "age": 30, "appearance": "高瘦"}],
        )
        second = new_draft(repo, author, script_public_id(repo, first), title=SECRETS["title_two"], tags=[SECRETS["tag"]])
        repo.submit_for_review(author.id, second.public_id, now=START)
        s.commit()
        versions = {"first": first.public_id, "second": second.public_id}
    clients = {}
    for email, (uid, tv) in ids.items():
        clients[email] = app.test_client()
        sign_in_as(clients[email], uid, tv)
    return app, clients, versions


def test_a_complete_reporting_tour_leaves_no_text_title_or_identifier_in_the_logs(world, log_lines):
    app, clients, versions = world
    author, admin = clients["user1@example.com"], clients["user2@example.com"]
    reporter_b, reporter_c, reporter_d = (clients[f"user{n}@example.com"] for n in (3, 4, 5))
    first, second = versions["first"], versions["second"]
    path = "/api/sessions/catalog/{}/report"

    # 玩家檢舉：成功、壞的理由、重複、找不到、自己的劇本
    assert reporter_b.post(path.format(first), json={"reasons": ["minor_sexual", "other"], "text": SECRETS["text_b"]}).status_code == 201
    assert reporter_c.post(path.format(first), json={"reasons": [SECRETS["bad_reason"]], "text": SECRETS["text_c"]}).status_code == 400
    assert reporter_c.post(path.format(first), json={"reasons": ["plagiarism"], "text": SECRETS["text_c"]}).status_code == 201
    assert reporter_c.post(path.format(first), json={"reasons": ["other"], "text": SECRETS["text_c"]}).status_code == 409
    assert reporter_c.post(path.format("00000000-0000-4000-8000-000000000000"), json={"reasons": ["other"], "text": SECRETS["text_c"]}).status_code == 404
    assert author.post(path.format(first), json={"reasons": ["other"], "text": SECRETS["text_b"]}).status_code == 403

    # 部署者調整每日上限，被擋下的檢舉記一筆事件
    runner = app.test_cli_runner()
    assert runner.invoke(args=["set-script-limit", "script_report_daily", "1"]).exit_code == 0
    assert reporter_b.get("/api/sessions/catalog/" + first).get_json()["script"]["report_state"] == "reported"
    assert reporter_d.post(path.format(first), json={"reasons": ["other"], "text": SECRETS["text_d"]}).status_code == 201
    other = put_other_script(app)
    assert reporter_d.post(path.format(other), json={"reasons": ["other"], "text": SECRETS["text_d"]}).status_code == 429
    assert runner.invoke(args=["set-script-limit", "script_report_daily", "5"]).exit_code == 0

    # 新版本核可：舊版本三筆未結案的檢舉由系統結為「被取代」（管理員核可不需要密碼）
    assert admin.post(f"/api/admin/scripts/versions/{second}/approve", json={}).status_code == 200
    with Session(app.extensions["db_engine"]) as s:
        assert sorted(r.closed_result for r in s.scalars(select(ScriptReport))) == ["superseded"] * 3

    # 新版本又被檢舉，作者自行下架：檢舉由系統結為「下架」
    assert reporter_b.post(path.format(second), json={"reasons": ["tier_mismatch"], "text": SECRETS["text_b"]}).status_code == 201
    assert author.post(f"/api/scripts/versions/{second}/remove").status_code == 200
    with Session(app.extensions["db_engine"]) as s:
        assert sorted(r.closed_result for r in s.scalars(select(ScriptReport))) == ["removed"] + ["superseded"] * 3

    raw = log_lines.raw()
    for name, secret in SECRETS.items():
        assert secret not in raw, f"日誌洩漏了：{name}"
    for identifier in (first, second, other):
        assert identifier not in raw, "日誌含有版本的對外識別碼"
    for reason in ("minor_sexual", "plagiarism", "tier_mismatch"):
        assert f'"{reason}"' not in raw and f"'{reason}'" not in raw, f"日誌含有理由內容：{reason}"

    lines = log_lines()
    submitted = [line for line in lines if line.get("event") == "script.report_submitted"]
    limited = [line for line in lines if line.get("event") == "script.report_limited"]
    closed = [line for line in lines if line.get("event") == "script.reports_auto_closed"]
    assert [(line["count"], line["chars"]) for line in submitted] == [
        (2, len(SECRETS["text_b"])), (1, len(SECRETS["text_c"])), (1, len(SECRETS["text_d"])), (1, len(SECRETS["text_b"])),
    ]
    assert len(limited) == 1
    assert [(line["count"], line["outcome"]) for line in closed] == [(3, "superseded"), (1, "removed")]      # 沒有未結案的檢舉就沒有事件
    for line in lines:
        assert set(line) <= BASE_KEYS | set(ALLOWED_FIELDS), f"出現不允許的欄位：{set(line) - BASE_KEYS - set(ALLOWED_FIELDS)}"


def put_other_script(app) -> str:
    """另一份上架中的玩家劇本（讓同一位玩家在今日上限為 1 時有第二個可檢舉的目標）；回傳版本的對外識別碼。"""
    with Session(app.extensions["db_engine"]) as s:
        owner = s.scalar(select(User).where(User.email == "user1@example.com"))
        version = put_in_status(ScriptRepository(s), owner, VERSION_PUBLISHED, title="另一份劇本")
        s.commit()
        return version.public_id


def test_a_removal_without_open_reports_leaves_no_auto_close_event(world, log_lines):
    """沒有未結案的檢舉時，下架不產生自動結案事件（只在筆數大於 0 時記錄）。"""
    _app, clients, versions = world
    author, admin = clients["user1@example.com"], clients["user2@example.com"]
    assert admin.post(f"/api/admin/scripts/versions/{versions['second']}/approve", json={}).status_code == 200
    assert author.post(f"/api/scripts/versions/{versions['second']}/remove").status_code == 200
    assert [line for line in log_lines() if line.get("event") == "script.reports_auto_closed"] == []
    assert any(line.get("event") == "script.removed" for line in log_lines())
