"""service-observability 規格「完整流程後掃描日誌」（rpg-script-authoring 的修改）：整個創作與送審流程走完後，日誌裡找不到
暱稱、短代號、筆名、劇本的任何文字（標題、標籤、背景、角色、里程碑）、申訴內容與模型原文，而且每個劇本事件只有白名單內的欄位。

流程：更改暱稱 -> 建立、編輯、帶入範例的草稿 -> 規則層在儲存時拒絕 -> 送審（模型第一次回傳無法解析的原文、重試後通過）-> 撤回與新版本、
重新取用筆名 -> 以 flask 指令調整與查看限制 -> 連續被模型層拒絕而被暫停發佈 -> 被暫停的作者送出申訴 -> 管理員檢視申訴並解除暫停。
"""
import json

import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base
from app.observability.json_logging import ALLOWED_FIELDS
from tests.support.auth import GOOD_PASSWORD, FakeClock, add_user, sign_in_as
from tests.support.llm import FakeLLMProvider, Step
from tests.support.script_data import valid

SECRETS = {
    "nickname": "掃描暱稱甲SCAN-NICK-A",
    "nickname_new": "掃描暱稱乙SCAN-NICK-B",
    "code": "ZQ91",
    "title": "掃描劇本標題SCAN-TITLE",
    "title_two": "掃描第二個標題SCAN-TITLE-TWO",
    "tag": "掃描標籤SCANTAG",
    "background": "掃描背景SCAN-BACKGROUND-TEXT",
    "character": "掃描角色SCAN-CHAR",
    "milestone": "掃描里程碑SCAN-MILESTONE",
    "location": "掃描地點SCAN-PLACE",
    "appeal": "掃描申訴內容SCAN-APPEAL-TEXT",
    "model_raw": "模型原文SCAN-MODEL-RAW-OUTPUT",
}
BASE_KEYS = {"time", "level", "logger", "message"}
PASS = json.dumps({"verdict": "pass", "categories": []})
REJECT = json.dumps({"verdict": "reject", "categories": ["non_consensual"]})


def content(**changes) -> dict:
    fields = {
        "title": SECRETS["title"], "tags": [SECRETS["tag"]], "background": SECRETS["background"], "locations": [SECRETS["location"]],
        "characters": [{"name": SECRETS["character"], "personality": "冷靜", "age": 30, "appearance": "高瘦"}],
        "milestones": [{"title": SECRETS["milestone"], "trigger": {"kind": "age", "value": 20}}],
    }
    return valid(**{**fields, **changes})


@pytest.fixture()
def world():
    provider = FakeLLMProvider(default=Step(REJECT)).script(SECRETS["model_raw"], PASS)
    app = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock(), rpg_llm_provider=provider)
    Base.metadata.create_all(app.extensions["db_engine"])
    with Session(app.extensions["db_engine"]) as s:
        author = add_user(s, 1, nickname=SECRETS["nickname"], nickname_code=SECRETS["code"])
        admin = add_user(s, 2, role="admin")
        ids = {"author": (author.id, author.token_version, author.public_id), "admin": (admin.id, admin.token_version, admin.public_id)}
    clients = {}
    for name in ("author", "admin"):
        clients[name] = app.test_client()
        sign_in_as(clients[name], ids[name][0], ids[name][1])
    return app, clients, ids["author"][2]


def test_a_complete_authoring_and_submission_tour_leaves_no_personal_text_in_the_logs(world, log_lines):
    app, clients, author_public_id = world
    author, admin = clients["author"], clients["admin"]

    assert author.put("/api/account/nickname", json={"nickname": SECRETS["nickname_new"]}).status_code == 200
    assert author.get("/api/scripts/example").status_code == 404                  # 測試資料庫沒有種子劇本，所以沒有範例
    assert author.get("/api/scripts/limits").status_code == 200

    created = author.post("/api/scripts", json={"content": content()})
    assert created.status_code == 201
    version = created.get_json()["version"]
    edited = author.put(
        f"/api/scripts/versions/{version['id']}", json={"content": content(rules="境界分為煉氣、築基。"), "base_hash": version["content_hash"]}
    )
    assert edited.status_code == 200
    version = edited.get_json()["version"]

    refused = author.post("/api/scripts", json={"content": content(title=SECRETS["title_two"], background="他迷暈她後發生性交。" + SECRETS["background"])})
    assert refused.status_code == 422                                              # 規則層在儲存時拒絕，內容沒有寫入

    sent = author.post(f"/api/scripts/versions/{version['id']}/submit")           # 模型先回傳無法解析的原文、重試後通過
    assert sent.status_code == 200 and sent.get_json()["version"]["status"] == "pending_review"
    assert author.post(f"/api/scripts/versions/{version['id']}/withdraw").status_code == 200
    assert author.post(f"/api/scripts/{version['script']['id']}/author-name/refresh").status_code == 200

    runner = app.test_cli_runner()
    for key in ("script_submit_daily.free", "script_limit.free"):                  # 要多寫幾份草稿並多次送審，先調高限制
        assert runner.invoke(args=["set-script-limit", key, "50"]).exit_code == 0
    assert runner.invoke(args=["show-script-limits"]).exit_code == 0

    for number in range(3):                                                        # 連續被模型層拒絕 -> 暫停發佈
        draft = author.post("/api/scripts", json={"content": content(title=f"{SECRETS['title']}{number}")})
        assert draft.status_code == 201, draft.get_data(as_text=True)
        rejected = author.post(f"/api/scripts/versions/{draft.get_json()['version']['id']}/submit")
        assert rejected.status_code == 422
    assert author.post(f"/api/scripts/versions/{version['id']}/submit").status_code == 403           # 已被暫停

    assert author.post("/api/appeals", json={"content": SECRETS["appeal"]}).status_code == 201
    listed = admin.get("/api/admin/appeals")
    assert listed.status_code == 200 and listed.get_json()["appeals"][0]["restriction"] == "suspended"
    lifted = admin.post(f"/api/admin/users/{author_public_id}/lift-publish-suspension", json={"password": GOOD_PASSWORD})
    assert lifted.status_code == 200

    raw = log_lines.raw()
    for name, secret in SECRETS.items():
        assert secret not in raw, f"日誌洩漏了：{name}"
    events = [line for line in log_lines() if str(line.get("event", "")).startswith(("script.", "account.nickname", "limits."))]
    names = {line["event"] for line in events}
    assert {
        "account.nickname_set", "script.created", "script.updated", "script.auto_rejected", "script.review", "script.submitted",
        "script.withdrawn", "script.publish_suspended", "script.publish_lifted", "limits.changed",
    } <= names
    for line in log_lines():
        assert set(line) <= BASE_KEYS | set(ALLOWED_FIELDS), f"出現不允許的欄位：{set(line) - BASE_KEYS - set(ALLOWED_FIELDS)}"
