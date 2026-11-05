"""service-observability 規格「完整流程後掃描日誌」（rpg-character-creation 的修改）：整個主角流程走完後，日誌裡找不到自我介紹、
主角角色卡與錨定事實的任何文字、模型原文，而且每個主角事件只有白名單內的欄位。

流程：讀創角資料 -> 以自我介紹生成預覽（模型第一次回傳無法解析的原文、重試後合法）-> 自我介紹被規則層拒絕 -> 生成次數用盡 -> 開存檔被欄位驗證擋下 ->
被規則層拒絕 -> 被模型層拒絕 -> 模型層不可用 -> 成功開存檔 -> 略過錨定事實再開一個。
"""
import json

import pytest
from sqlalchemy.orm import Session

from app.models import Base, RuntimeLimit
from app.observability.json_logging import ALLOWED_FIELDS
from app.llm.types import LLMTransientError
from app.repositories.script_repository import ScriptRepository
from tests.support.auth import FakeClock, add_user, sign_in_as
from tests.support.llm import FakeLLMProvider, Step
from tests.support.script_data import validated
from tests.test_llm_providers_by_purpose import BOTH, make_app

SECRETS = {
    "intro": "掃描自我介紹SCAN-INTRO-TEXT",
    "name": "掃描主角姓名SCAN-HERO",
    "personality": "掃描個性SCAN-PERSONALITY",
    "appearance": "掃描外貌SCAN-APPEARANCE",
    "hidden": "掃描隱藏特質SCAN-HIDDEN",
    "anchor": "掃描聯繫文字SCAN-LINK-TEXT",
    "model_raw": "模型原文SCAN-MODEL-RAW-OUTPUT",
    "suggested_name": "掃描生成姓名SCAN-GENERATED",
}
BASE_KEYS = {"time", "level", "logger", "message"}
NPC = {"name": "蘇長風", "personality": "沉穩寡言", "age": 168, "appearance": "鬢髮微白", "occupation": "長老"}


def verdict(name="pass", categories=()):
    return json.dumps({"verdict": name, "categories": list(categories)})


def suggestion():
    return json.dumps({
        "character": {"name": SECRETS["suggested_name"], "personality": "沉默", "age": 19, "appearance": "高瘦", "occupation": None, "speech_style": None, "hidden_trait": None},
        "anchor": {"kind": "character", "ref": "蘇長風", "text": SECRETS["anchor"]},
    }, ensure_ascii=False)


def card(**changes):
    values = {"name": SECRETS["name"][:20], "personality": SECRETS["personality"], "age": 19, "appearance": SECRETS["appearance"], "hidden_trait": SECRETS["hidden"]}
    values.update(changes)
    return values


@pytest.fixture()
def world():
    provider = FakeLLMProvider()
    app = make_app({**BOTH}, rpg_llm_provider=provider)
    with Session(app.extensions["db_engine"]) as s:
        user = add_user(s, 1)
        uid, tv = user.id, user.token_version
        result = validated(characters=[NPC])
        version, _ = ScriptRepository(s).publish_builtin(
            "seed-scan", content=result.content, content_hash=result.content_hash, total_chars=result.total_chars, now=FakeClock()(),
        )
        s.commit()
        version_id = version.public_id
    client = app.test_client()
    sign_in_as(client, uid, tv)
    return app, client, provider, version_id


def test_a_complete_protagonist_tour_leaves_no_personal_text_in_the_logs(world, log_lines):
    app, client, provider, version = world
    anchor = {"kind": "character", "ref": "蘇長風", "text": SECRETS["anchor"]}

    assert client.get(f"/api/sessions/setup/{version}").status_code == 200

    provider.script(SECRETS["model_raw"], suggestion())                              # 第一次無法解析，重試後合法
    suggested = client.post("/api/sessions/protagonist/suggest", json={"version_id": version, "intro": SECRETS["intro"]})
    assert suggested.status_code == 200, suggested.get_data(as_text=True)

    refused = client.post("/api/sessions/protagonist/suggest", json={"version_id": version, "intro": "十歲的少女與他做愛。" + SECRETS["intro"]})
    assert refused.status_code == 422                                                # 規則層拒絕自我介紹

    with Session(app.extensions["db_engine"]) as s:                                  # 生成次數調成剛好用完
        s.add(RuntimeLimit(key="protagonist_suggest_daily.free", int_value=1, updated_at=FakeClock()()))
        s.commit()
    limited = client.post("/api/sessions/protagonist/suggest", json={"version_id": version, "intro": SECRETS["intro"]})
    assert limited.status_code == 429

    invalid = client.post("/api/sessions", json={"version_id": version, "protagonist": card(appearance=""), "anchor": anchor})
    assert invalid.status_code == 400                                                # 欄位驗證

    by_rules = client.post("/api/sessions", json={"version_id": version, "protagonist": card(age=15, personality="喜歡性愛的少年" + SECRETS["personality"]), "anchor": anchor})
    assert by_rules.status_code == 422                                               # 規則層

    provider.script(verdict("reject", ["minor_sexual"]))
    by_model = client.post("/api/sessions", json={"version_id": version, "protagonist": card(), "anchor": anchor})
    assert by_model.status_code == 422                                               # 模型層拒絕

    provider.script(verdict("flag", ["other_concern"]), verdict())
    assert client.post("/api/sessions", json={"version_id": version, "protagonist": card(), "anchor": anchor}).status_code == 201
    assert client.post("/api/sessions", json={"version_id": version, "protagonist": card(name="掃描主角姓名乙SCAN-HERO-TWO"), "skip_anchor": True}).status_code == 201

    # 模型層不可用放在最後：連續的暫時性錯誤會讓斷路暫時跳過主要模型，之後的呼叫都會降級
    provider.script(Step(LLMTransientError("timeout")), Step(LLMTransientError("server_error")))
    unavailable = client.post("/api/sessions", json={"version_id": version, "protagonist": card(), "anchor": anchor})
    assert unavailable.status_code == 503                                            # 模型層不可用

    raw = log_lines.raw()
    for name, secret in SECRETS.items():
        assert secret not in raw, f"日誌洩漏了：{name}"
    assert "SCAN-HERO-TWO" not in raw
    names = {line["event"] for line in log_lines() if str(line.get("event", "")).startswith(("protagonist.", "session."))}
    assert {
        "protagonist.suggested", "protagonist.suggest_limited", "protagonist.rejected", "protagonist.review", "protagonist.review_unavailable",
        "session.started",
    } <= names
    for line in log_lines():
        assert set(line) <= BASE_KEYS | set(ALLOWED_FIELDS), f"出現不允許的欄位：{set(line) - BASE_KEYS - set(ALLOWED_FIELDS)}"


def test_failure_paths_alone_also_leave_the_logs_clean(world, log_lines):
    app, client, provider, version = world
    provider.script(SECRETS["model_raw"], SECRETS["model_raw"])
    assert client.post("/api/sessions/protagonist/suggest", json={"version_id": version, "intro": SECRETS["intro"]}).status_code == 503
    assert client.post("/api/sessions", json={"version_id": version, "protagonist": card(), "anchor": {"kind": "character", "ref": "不存在" + SECRETS["anchor"], "text": SECRETS["anchor"]}}).status_code == 400
    raw = log_lines.raw()
    for name, secret in SECRETS.items():
        assert secret not in raw, f"日誌洩漏了：{name}"
