"""玩家主角的端對端流程（rpg-character-creation 的整合測試，假供應商，不打真實 API）。

從「讀創角資料」走到「開存檔」，涵蓋 AI 生成後修改再開、用盡次數後自創、AI 生成失敗後自創、被拒絕後修改再開、略過錨定事實；
每個失敗路徑結束時，存檔數量與剩餘次數都與預期一致，日誌裡沒有任何輸入文字。
"""
import json

import pytest
from sqlalchemy import select

from app.models import RpgSession, RuntimeLimit
from tests.support.auth import FakeClock
from tests.support.llm import FakeLLMProvider
from tests.test_session_api import ANCHOR, CARD, NPC, LIN, builtin, db, saves, signed_in, start_body, verdict
from tests.test_llm_providers_by_purpose import BOTH, make_app

INTRO = "我是個流浪的郎中FLOW-INTRO，想拜師學藝。"


@pytest.fixture()
def provider():
    return FakeLLMProvider()


@pytest.fixture()
def app(provider):
    return make_app({**BOTH}, rpg_llm_provider=provider)


def suggestion(**card):
    character = {**CARD, "occupation": "遊方郎中", "speech_style": None, "hidden_trait": None, **card}
    return json.dumps({"character": character, "anchor": ANCHOR}, ensure_ascii=False)


def stored_save(app) -> RpgSession:
    with db(app) as s:
        return s.scalar(select(RpgSession))


def test_generate_edit_and_start(app, provider, log_lines):
    client, version = signed_in(app), builtin(app, characters=[LIN, NPC])
    setup = client.get(f"/api/sessions/setup/{version}").get_json()
    assert setup["suggestions_remaining"] == 5 and [c["name"] for c in setup["characters"]] == ["林曉", "蘇長風"]

    provider.script(suggestion())
    generated = client.post("/api/sessions/protagonist/suggest", json={"version_id": version, "intro": INTRO}).get_json()
    assert generated["remaining"] == 4 and saves(app) == 0                          # 預覽不開存檔

    edited = {**generated["protagonist"], "name": "林舟改", "personality": "沉默而且愛思考"}      # 玩家先改再接受
    provider.script(verdict())
    started = client.post("/api/sessions", json={"version_id": version, "protagonist": edited, "anchor": generated["anchor"]})
    assert started.status_code == 201 and saves(app) == 1
    save = stored_save(app)
    assert (save.player_name, save.character_age) == ("林舟改", 19) and save.protagonist_json["character"]["personality"] == "沉默而且愛思考"
    assert save.protagonist_json["anchor"] == ANCHOR
    assert "FLOW-INTRO" not in log_lines.raw() and "林舟改" not in log_lines.raw()


def test_when_the_daily_count_is_used_up_the_player_creates_the_character_themselves(app, provider):
    client, version = signed_in(app), builtin(app)
    with db(app) as s:
        s.add(RuntimeLimit(key="protagonist_suggest_daily.free", int_value=0, updated_at=FakeClock()()))
        s.commit()
    assert client.get(f"/api/sessions/setup/{version}").get_json()["suggestions_remaining"] == 0
    refused = client.post("/api/sessions/protagonist/suggest", json={"version_id": version})
    assert refused.status_code == 429 and provider.calls == []
    provider.script(verdict())
    assert client.post("/api/sessions", json=start_body(version)).status_code == 201 and saves(app) == 1


def test_when_the_generation_fails_validation_twice_manual_creation_still_works(app, provider):
    client, version = signed_in(app), builtin(app)
    bad = json.dumps({"character": {**CARD, "occupation": None, "speech_style": None, "hidden_trait": None}, "anchor": {**ANCHOR, "ref": "不存在的人"}}, ensure_ascii=False)
    provider.script(bad, bad)
    failed = client.post("/api/sessions/protagonist/suggest", json={"version_id": version, "intro": INTRO})
    assert failed.status_code == 503 and failed.get_json()["error"]["code"] == "suggestion_unavailable" and saves(app) == 0
    provider.script(verdict())
    assert client.post("/api/sessions", json=start_body(version)).status_code == 201 and saves(app) == 1


def test_a_rejected_protagonist_can_be_fixed_and_started_again(app, provider):
    client, version = signed_in(app), builtin(app)
    rejected = client.post("/api/sessions", json=start_body(version, protagonist={**CARD, "age": 15, "personality": "喜歡性愛的少年"}))
    assert rejected.status_code == 422 and rejected.get_json()["categories"] == ["minor_sexual"] and saves(app) == 0 and provider.calls == []
    provider.script(verdict("reject", ["minor_sexual"]))
    model_rejected = client.post("/api/sessions", json=start_body(version))
    assert model_rejected.status_code == 422 and saves(app) == 0
    provider.script(verdict())
    assert client.post("/api/sessions", json=start_body(version, protagonist={**CARD, "name": "改過的主角"})).status_code == 201
    assert saves(app) == 1 and stored_save(app).player_name == "改過的主角"


def test_skipping_the_anchor_opens_a_save_and_each_request_opens_its_own_save(app, provider):
    client, version = signed_in(app), builtin(app)
    provider.script(verdict(), verdict())
    skipped = client.post("/api/sessions", json={"version_id": version, "protagonist": CARD, "skip_anchor": True})
    anchored = client.post("/api/sessions", json=start_body(version, protagonist={**CARD, "name": "第二個"}))
    assert (skipped.status_code, anchored.status_code) == (201, 201) and saves(app) == 2
    assert skipped.get_json()["session"]["id"] != anchored.get_json()["session"]["id"]
    assert skipped.get_json()["session"]["protagonist"]["anchor"] is None


def test_a_minor_cannot_get_a_romantic_link_by_any_route(app, provider):
    """AI 生成的輸出、玩家直接送出的聯繫，兩條路都被擋：前者進入重試，後者是欄位錯誤。"""
    client, version = signed_in(app), builtin(app, characters=[LIN, NPC])
    romantic = {**ANCHOR, "ref": "林曉", "text": "舊日的戀人"}
    bad = json.dumps({"character": {**CARD, "age": 30, "occupation": None, "speech_style": None, "hidden_trait": None}, "anchor": romantic}, ensure_ascii=False)
    provider.script(bad, bad)
    assert client.post("/api/sessions/protagonist/suggest", json={"version_id": version}).status_code == 503
    direct = client.post("/api/sessions", json=start_body(version, protagonist={**CARD, "age": 30}, anchor=romantic))
    assert direct.status_code == 400 and "anchor.text" in direct.get_json()["error"]["fields"] and saves(app) == 0


def test_the_origin_the_player_chooses_is_stored_and_a_missing_one_means_a_native(app, provider):
    client, version = signed_in(app), builtin(app)
    provider.script(verdict(), verdict())
    transmigrator = client.post("/api/sessions", json=start_body(version, protagonist={**CARD, "origin": "transmigrator"}))
    plain = client.post("/api/sessions", json=start_body(version, protagonist={**CARD, "name": "本地人"}))
    assert (transmigrator.status_code, plain.status_code) == (201, 201)
    with db(app) as s:
        saves_by_name = {save.player_name: save.protagonist_json["character"] for save in s.scalars(select(RpgSession))}
    assert saves_by_name[CARD["name"]]["origin"] == "transmigrator" and saves_by_name["本地人"]["origin"] == "native"
    assert transmigrator.get_json()["session"]["protagonist"]["character"]["origin"] == "transmigrator"


def test_an_invalid_origin_is_a_400_on_protagonist_origin_and_opens_no_save(app, provider):
    client, version = signed_in(app), builtin(app)
    refused = client.post("/api/sessions", json=start_body(version, protagonist={**CARD, "origin": "ghost"}))
    assert refused.status_code == 400 and "protagonist.origin" in refused.get_json()["error"]["fields"]
    assert saves(app) == 0 and provider.calls == []                          # 在驗證階段就停下：不呼叫模型、不開存檔


def test_the_setup_api_lists_the_origin_options(app):
    client, version = signed_in(app), builtin(app)
    assert client.get(f"/api/sessions/setup/{version}").get_json()["origin_options"] == ["native", "transmigrator"]
