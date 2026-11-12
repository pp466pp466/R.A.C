"""rpg-entry-ui 的整合測試（任務 4.3）：後端對前端契約。

依「頁面實際呼叫 API 的順序」走一遍（假的模型供應商，不連網）：`/game` 頁面 → `GET /api/sessions/catalog` 取得內建劇本的 `id` →
`/game/start/<id>` 精靈頁 → `GET /api/sessions/setup/<id>` → `POST /api/sessions/protagonist/suggest`（預覽）→
`POST /api/sessions`（用預覽的角色卡與錨定事實）→ `GET /api/sessions` 看到新存檔 → `/game/<新存檔>` 為 200。
並確認：預覽不開存檔、整個流程沒有扣遊戲幣也沒有寫帳本、日誌裡沒有劇本文字、角色卡、自我介紹、聯繫與暱稱、
換一個玩家看不到也開不了這個存檔（與不存在的存檔逐字相同的 404）。

同一組在 SQLite 與真實 SQL Server（`project_test`，標記 sqlserver）上各跑一次。
"""
import json

import pytest
from sqlalchemy import func, select

from app import create_app
from app.config import load_config
from app.models import Base, RpgSession, Wallet, WalletEntry
from app.observability.json_logging import ALLOWED_FIELDS
from tests.conftest import require_sqlserver_test_url
from tests.support.auth import FakeClock
from tests.support.llm import FakeLLMProvider
from tests.test_llm_providers_by_purpose import BOTH
from tests.test_session_api import ANCHOR, GHOST, LIN, NPC, builtin, db, saves, signed_in, verdict

BASE_KEYS = {"time", "level", "logger", "message"}
SECRETS = {
    "title": "入口流程劇本ENTRY-TITLE",
    "summary": "入口流程簡介ENTRY-SUMMARY",
    "background": "入口流程背景ENTRY-BACKGROUND，宗門林立。",
    "opening": "入口流程開場ENTRY-OPENING。",
    "npc_trait": "沉穩寡言ENTRY-NPC-TRAIT",
    "npc_hidden": "暗中守護ENTRY-NPC-HIDDEN",
    "nickname": "暱稱ENTRY-NICK",
    "intro": "我是個流浪的郎中ENTRY-INTRO，想拜師學藝。",
    "name": "生成姓名ENTRY-GEN-NAME",
    "personality": "生成個性ENTRY-GEN-TRAIT",
    "appearance": "生成外貌ENTRY-GEN-LOOK",
    "occupation": "生成職業ENTRY-GEN-JOB",
    "hidden": "生成隱藏ENTRY-GEN-HIDDEN",
    "anchor": "生成聯繫ENTRY-GEN-LINK",
}
SCRIPT = {
    "title": SECRETS["title"], "summary": SECRETS["summary"], "background": SECRETS["background"], "opening": SECRETS["opening"],
    "characters": [LIN, {**NPC, "personality": SECRETS["npc_trait"], "hidden_trait": SECRETS["npc_hidden"]}],
}


@pytest.fixture(params=["sqlite", pytest.param("sqlserver", marks=pytest.mark.sqlserver)])
def world(request):
    environ = {**BOTH}
    if request.param == "sqlserver":
        environ["DATABASE_URL"] = require_sqlserver_test_url()
    provider = FakeLLMProvider()
    app = create_app(load_config(environ={"APP_ENV": "testing", **environ}), rpg_llm_provider=provider, clock=FakeClock())
    engine = app.extensions["db_engine"]
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield app, provider
    Base.metadata.drop_all(engine)
    engine.dispose()


def suggestion() -> str:
    character = {
        "name": SECRETS["name"], "personality": SECRETS["personality"], "age": 19, "appearance": SECRETS["appearance"],
        "occupation": SECRETS["occupation"], "speech_style": None, "hidden_trait": SECRETS["hidden"],
    }
    return json.dumps({"character": character, "anchor": {**ANCHOR, "text": SECRETS["anchor"]}}, ensure_ascii=False)


def wallet_state(app):
    """錢包與帳本的完整狀態：入口流程不扣幣、不寫帳，前後必須相同。"""
    with db(app) as s:
        wallets = [(w.user_id, w.free_balance, w.paid_balance, w.version) for w in s.scalars(select(Wallet).order_by(Wallet.id))]
        entries = s.scalar(select(func.count()).select_from(WalletEntry))
    return wallets, entries


def assert_logs_are_clean(log_lines):
    raw = log_lines.raw()
    for name, secret in SECRETS.items():
        assert secret not in raw, f"日誌洩漏了：{name}"
    for line in log_lines():
        assert set(line) <= BASE_KEYS | set(ALLOWED_FIELDS), f"出現不允許的欄位：{set(line) - BASE_KEYS - set(ALLOWED_FIELDS)}"


def test_the_whole_entry_flow_in_the_order_the_pages_call_the_apis(world, log_lines):
    app, provider = world
    client = signed_in(app, 1, nickname=SECRETS["nickname"], nickname_code="abc123")
    version = builtin(app, **SCRIPT)
    assert client.get("/api/wallet").status_code == 200                       # 先讓註冊贈幣發放：之後比對錢包才有東西可比
    wallet_before = wallet_state(app)
    assert wallet_before[0] and wallet_before[1] >= 1

    home = client.get("/game")                                                # 1) 入口頁只有外殼，清單內容由 API 取得
    assert home.status_code == 200 and SECRETS["title"] not in home.get_data(as_text=True)

    catalog = client.get("/api/sessions/catalog")                             # 2) 內建劇本的卡片，其 id 就是版本識別碼
    assert catalog.status_code == 200
    cards = [card for card in catalog.get_json()["scripts"] if card["id"] == version]
    assert len(cards) == 1 and cards[0]["title"] == SECRETS["title"] and cards[0]["author_name"] == "官方"
    script_id = cards[0]["id"]

    wizard = client.get(f"/game/start/{script_id}")                           # 3) 精靈頁的外殼不含劇本內容，也不含版本識別碼
    assert wizard.status_code == 200
    wizard_html = wizard.get_data(as_text=True)
    assert SECRETS["title"] not in wizard_html and script_id not in wizard_html

    setup = client.get(f"/api/sessions/setup/{script_id}")                    # 4) 序章與創角需要的資料
    assert setup.status_code == 200
    body = setup.get_json()
    assert body["title"] == SECRETS["title"] and body["background"] == SECRETS["background"]
    assert body["suggestions_remaining"] == 5 and body["suggestions_available"] is True
    assert [c["name"] for c in body["characters"]] == ["林曉", "蘇長風"]
    assert all(set(c) == {"name", "occupation"} for c in body["characters"])  # 聯繫的人物選項只有姓名與職業

    provider.script(suggestion())                                             # 5) AI 生成預覽：不開存檔、不扣幣
    preview = client.post("/api/sessions/protagonist/suggest", json={"version_id": script_id, "intro": SECRETS["intro"]})
    assert preview.status_code == 200
    generated = preview.get_json()
    assert generated["remaining"] == 4 and saves(app) == 0
    assert generated["protagonist"]["name"] == SECRETS["name"] and "origin" not in generated["protagonist"]   # 預覽不決定來歷
    assert generated["anchor"]["text"] == SECRETS["anchor"]
    assert wallet_state(app) == wallet_before

    provider.script(verdict())                                                # 6) 玩家選來歷後，用預覽的角色卡與錨定事實開存檔
    started = client.post("/api/sessions", json={
        "version_id": script_id, "protagonist": {**generated["protagonist"], "origin": "transmigrator"}, "anchor": generated["anchor"],
    })
    assert started.status_code == 201
    session_id = started.get_json()["session"]["id"]
    assert saves(app) == 1

    mine = client.get("/api/sessions")                                        # 7) 「我的存檔」看到新存檔，輪數為 0
    assert mine.status_code == 200
    rows = mine.get_json()["sessions"]
    assert [row["id"] for row in rows] == [session_id]
    assert (rows[0]["title"], rows[0]["status"], rows[0]["completed_turns"]) == (SECRETS["title"], "active", 0)
    assert rows[0]["player_name"] == SECRETS["name"]

    page = client.get(f"/game/{session_id}")                                  # 8) 存檔頁是本人的 200：遊戲主畫面的外殼（rpg-game-ui 之後不再是「建置中」的空殼）
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert SECRETS["title"] in html and SECRETS["name"] in html               # 標題與主角姓名在初始狀態裡（由 Vue 以插值顯示）
    assert 'id="game-app"' in html and "game.js" in html and "建置中" not in html

    assert wallet_state(app) == wallet_before                                 # 整個入口流程沒有扣遊戲幣、沒有寫帳本
    assert len(provider.calls) == 2                                           # 只有生成預覽與主角審查兩次模型呼叫（內建劇本視為已審查）
    events = {line.get("event") for line in log_lines()}
    assert {"protagonist.suggested", "protagonist.review", "session.started"} <= events     # 日誌擷取是有效的（不是因為什麼都沒記才乾淨）
    assert_logs_are_clean(log_lines)


def test_another_player_cannot_see_or_open_the_save_and_gets_the_same_404_as_for_a_missing_one(world, log_lines):
    app, provider = world
    owner = signed_in(app, 1)
    version = builtin(app, **SCRIPT)
    provider.script(verdict())
    started = owner.post("/api/sessions", json={"version_id": version, "protagonist": {"name": SECRETS["name"], "personality": SECRETS["personality"], "age": 19, "appearance": SECRETS["appearance"]}, "anchor": ANCHOR})
    assert started.status_code == 201
    session_id = started.get_json()["session"]["id"]

    other = signed_in(app, 2)
    assert other.get("/api/sessions").get_json()["sessions"] == []            # 清單裡沒有別人的存檔
    seen = [card["id"] for card in other.get("/api/sessions/catalog").get_json()["scripts"]]
    assert seen == [version]                                                  # 劇本本身是公開的：別人看得到同一張卡片
    for path in ("/game/{}", "/api/game/sessions/{}"):
        theirs, missing = other.get(path.format(session_id)), other.get(path.format(GHOST))
        assert theirs.status_code == missing.status_code == 404, path
        assert theirs.get_data() == missing.get_data(), path                  # 逐字相同：不洩漏存檔是否存在
    assert SECRETS["name"] not in other.get(f"/game/{session_id}").get_data(as_text=True)
    assert [row["id"] for row in owner.get("/api/sessions").get_json()["sessions"]] == [session_id]      # 本人不受影響
    assert owner.get(f"/game/{session_id}").status_code == 200
    assert_logs_are_clean(log_lines)


def test_when_the_suggestion_is_unavailable_the_player_creates_the_character_themselves_and_both_saves_are_listed_newest_first(world, log_lines):
    app, provider = world
    client = signed_in(app, 1)
    version = builtin(app, **SCRIPT)
    bad = json.dumps({"character": {"name": "壞", "personality": "x", "age": 19, "appearance": "x", "occupation": None, "speech_style": None, "hidden_trait": None}, "anchor": {**ANCHOR, "ref": "不存在的人"}}, ensure_ascii=False)
    provider.script(bad, bad)
    unavailable = client.post("/api/sessions/protagonist/suggest", json={"version_id": version, "intro": SECRETS["intro"]})
    assert unavailable.status_code == 503 and unavailable.get_json()["error"]["code"] == "suggestion_unavailable" and saves(app) == 0

    card = {"name": "自創主角甲", "personality": "沉默愛思考", "age": 19, "appearance": "高瘦"}
    provider.script(verdict(), verdict())
    first = client.post("/api/sessions", json={"version_id": version, "protagonist": card, "skip_anchor": True})        # 略過聯繫
    second = client.post("/api/sessions", json={"version_id": version, "protagonist": {**card, "name": "自創主角乙"}, "anchor": ANCHOR})
    assert (first.status_code, second.status_code) == (201, 201) and saves(app) == 2
    listed = client.get("/api/sessions").get_json()["sessions"]
    assert [row["id"] for row in listed] == [second.get_json()["session"]["id"], first.get_json()["session"]["id"]]
    assert [row["player_name"] for row in listed] == ["自創主角乙", "自創主角甲"]
    assert all(row["completed_turns"] == 0 for row in listed)
    with db(app) as s:
        stored = {save.player_name: save.protagonist_json["anchor"] for save in s.scalars(select(RpgSession))}
    assert stored["自創主角甲"] is None and stored["自創主角乙"] == ANCHOR
    assert_logs_are_clean(log_lines)
