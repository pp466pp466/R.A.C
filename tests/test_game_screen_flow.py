"""rpg-game-ui 的整合測試（任務 4.4）：後端對遊戲主畫面前端的契約。

依 `game-session.js` 實際呼叫的順序走一遍（假的模型供應商，不連網；真實的 Service、Repository 與資料庫）：
玩家（Google 登入、年齡確認）開存檔 → `GET /game/<存檔>` 為 200 的頁面外殼 → 快照 → 第 0 幕串流 → `turns?since_seq=0` → `GET /api/wallet` →
送一輪（以與前端 `sse-parser.js` 相同的規則解析串流，並刻意把位元組切成很小的塊，含切在多位元組字元中間）→ 事件順序與 `done.coins` 等於 `GET /api/wallet` 的合計 →
以相同 `client_msg_id` 重送得到 `replayed` 且餘額不變、不呼叫模型 → 補齊 `since_seq` 只回新回合 → 被擋的回合沒有文字 →
另一位玩家開同一個網址與端點得到與不存在逐字相同的 404 → 全程日誌掃描沒有行動、劇情、第 0 幕與角色卡。

前端的純邏輯在 `tests/js/game-*.test.js`；這裡證明的是那些前端程式依賴的**後端行為**確實如此（欄位、事件順序、冪等與隔離）。
同一組在 SQLite 與真實 SQL Server（`project_test`，標記 sqlserver）上各跑一次。
"""
import codecs
import json
import re

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base, MemoryLog, Wallet, WalletEntry
from app.observability.json_logging import ALLOWED_FIELDS
from app.repositories.script_repository import ScriptRepository
from app.services.google_oidc import GoogleProfile
from tests.conftest import require_sqlserver_test_url
from tests.support.age import age_fields
from tests.support.auth import FakeClock, add_user
from tests.support.config_env import AUTH_ENV
from tests.support.game import BAD_SENTENCE, STORY, story_script
from tests.support.google import FakeProvider, google_sign_in
from tests.support.idor import logged_in_client
from tests.support.llm import FakeLLMProvider
from tests.support.script_data import validated
from tests.test_llm_providers_by_purpose import BOTH

GOOGLE_EMAIL = "game.screen@example.com"
OPENING = "晨霧之中，山門緩緩開啟ＳＣＲ０。"
ACTION_ONE = "我走進藏經閣翻找古籍ＳＣＲ１。"
ACTION_TWO = "我向守閣的長老請教ＳＣＲ２。"
ACTION_BLOCKED = "我做了一件會被擋下的事ＳＣＲ３。"
SECRETS = {"opening": OPENING, "action_one": ACTION_ONE, "action_two": ACTION_TWO, "action_blocked": ACTION_BLOCKED, "story": STORY, "protagonist": "林舟ＳＣＲ名"}
CARD = {"name": SECRETS["protagonist"], "personality": "沉默愛思考", "age": 19, "appearance": "高瘦，常穿灰衣"}
NPC_NAME = "蘇長風"
ANCHOR = {"kind": "character", "ref": NPC_NAME, "text": "他的舊識之後"}
NPC = {"name": NPC_NAME, "personality": "沉穩寡言", "age": 168, "appearance": "鬢髮微白", "occupation": "長老", "hidden_trait": "暗中守護青雲宗"}
STATE = json.dumps({"elapsed_months": 1, "current_location": "藏經閣"}, ensure_ascii=False)
PASS = json.dumps({"verdict": "pass", "categories": []})
GHOST = "00000000-0000-4000-8000-000000000000"
BASE_KEYS = {"time", "level", "logger", "message"}

# 前端 game-state.js 的 worldView／turn_view 白名單：後端回的欄位必須剛好是這些（多了或少了，前端的對照都會悄悄失效）
WORLD_KEYS = {"elapsed_months", "location", "summary", "goals", "places", "protagonist_age"}
TURN_KEYS = {"seq", "client_msg_id", "turn_number", "status", "action", "story"}
SNAPSHOT_KEYS = {"id", "title", "content_tier", "status", "ended", "protagonist", "version", "completed_turns", "opening", "world", "notebook"}
NOTEBOOK_SUMMARY_KEYS = {"version", "length", "capacity", "full"}               # 快照的摘要（全文與預備空間只在 GET …/notebook）


@pytest.fixture(params=["sqlite", pytest.param("sqlserver", marks=pytest.mark.sqlserver)])
def world(request):
    environ = {**BOTH, **AUTH_ENV}
    if request.param == "sqlserver":
        environ["DATABASE_URL"] = require_sqlserver_test_url()
    rpg, google = FakeLLMProvider(), FakeProvider()
    google.profile = GoogleProfile("sub-game-screen-0001", GOOGLE_EMAIL, True, "畫面測試玩家")
    app = create_app(load_config(environ={"APP_ENV": "testing", **environ}), rpg_llm_provider=rpg, identity_provider=google, clock=FakeClock())
    engine = app.extensions["db_engine"]
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield app, rpg
    Base.metadata.drop_all(engine)
    engine.dispose()


def db(app) -> Session:
    return Session(app.extensions["db_engine"])


# ---- 與前端 sse-parser.js 相同的解析規則（Python 版）---------------------------------------------------------------


class BrowserSseParser:
    """`createParser().feed(text)` 的 Python 版：事件以空行分隔（容許 \\r\\n 與單獨的 \\r）、只取 event 與 data（多行以換行串接）、`:` 開頭是註解、
    data 必須是 JSON 物件，否則整個事件丟棄；不完整的尾端留在緩衝。"""

    def __init__(self) -> None:
        self.buffer = ""
        self.held = False

    @staticmethod
    def block(text: str):
        name, data_lines = "", []
        for line in text.split("\n"):
            if line == "" or line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "event":
                name = value
            elif field == "data":
                data_lines.append(value)
        if not name or not data_lines:
            return None
        try:
            data = json.loads("\n".join(data_lines))
        except ValueError:
            return None
        return (name, data) if isinstance(data, dict) else None

    def feed(self, text: str):
        if self.held:
            text, self.held = "\r" + text, False
        if text.endswith("\r"):
            text, self.held = text[:-1], True
        self.buffer += re.sub(r"\r\n|\r", "\n", text)
        events = []
        while "\n\n" in self.buffer:
            raw, self.buffer = self.buffer.split("\n\n", 1)
            parsed = self.block(raw)
            if parsed:
                events.append(parsed)
        return events


def stream_events(response, *, slice_size=7):
    """依瀏覽器的做法讀串流：位元組切成小塊（刻意含切在多位元組字元中間）→ 串流式 UTF-8 解碼 → 解析器。"""
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.mimetype == "text/event-stream"
    decoder = codecs.getincrementaldecoder("utf-8")()
    parser = BrowserSseParser()
    raw = response.get_data()
    events = []
    for start in range(0, len(raw), slice_size):
        events.extend(parser.feed(decoder.decode(raw[start:start + slice_size])))
    events.extend(parser.feed(decoder.decode(b"", final=True)))
    assert parser.buffer == "" and not parser.held, "串流結尾不應有殘餘的半個事件"
    return events


def names(events):
    return [name for name, _ in events]


def of(events, name):
    return [data for event, data in events if event == name]


def text_of(events) -> str:
    return "".join(data["text"] for data in of(events, "token"))


# ---- 前端會呼叫的端點 --------------------------------------------------------------------------------------------


def snapshot(client, save_id):
    response = client.get(f"/api/game/sessions/{save_id}")
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["session"]


def turns_since(client, save_id, since):
    response = client.get(f"/api/game/sessions/{save_id}/turns?since_seq={since}")
    assert response.status_code == 200
    return response.get_json()


def wallet_total(client) -> int:
    return client.get("/api/wallet").get_json()["wallet"]["total"]


def play(client, save_id, msg, action):
    return client.post(f"/api/game/sessions/{save_id}/turns/stream", json={"client_msg_id": msg, "action": action})


def wallet_state(app):
    with db(app) as s:
        wallets = [(w.user_id, w.free_balance, w.paid_balance, w.version) for w in s.scalars(select(Wallet).order_by(Wallet.id))]
        entries = s.scalar(select(func.count()).select_from(WalletEntry))
    return wallets, entries


def make_save(app, rpg, client) -> str:
    result = validated(characters=[{"name": "林曉", "personality": "活潑好動", "age": 16, "appearance": "瘦小", "occupation": "學徒"}, NPC])
    with db(app) as s:
        version, _ = ScriptRepository(s).publish_builtin(
            "seed-screen", content=result.content, content_hash=result.content_hash, total_chars=result.total_chars, now=FakeClock()(),
        )
        s.commit()
        version_id = version.public_id
    rpg.script(PASS)                                                            # 主角的模型層審查
    started = client.post("/api/sessions", json={"version_id": version_id, "protagonist": CARD, "anchor": ANCHOR})
    assert started.status_code == 201, started.get_data(as_text=True)
    return started.get_json()["session"]["id"]


def assert_logs_are_clean(log_lines):
    raw = log_lines.raw()
    for name, secret in SECRETS.items():
        assert secret not in raw, f"日誌洩漏了：{name}"
    for line in log_lines():
        assert set(line) <= BASE_KEYS | set(ALLOWED_FIELDS), f"出現不允許的欄位：{set(line) - BASE_KEYS - set(ALLOWED_FIELDS)}"


# ---- 測試 ------------------------------------------------------------------------------------------------------


def test_the_parser_used_here_follows_the_frontend_rules():
    """測試用的解析器本身要和前端一致，否則整個流程測試證明不了什麼。"""
    parser = BrowserSseParser()
    chunks = ["event: meta\r\ndata: {\"a\": 1}\r", "\n\r\n: ping\n\nevent: token\ndata: {\"text\"", ": \"字\"}\n\nevent: bad\ndata: [1]\n\nevent: x\ndata: nope\n\n", "event: tail\ndata: {\"z\": 1}"]
    got = [event for chunk in chunks for event in parser.feed(chunk)]
    assert got == [("meta", {"a": 1}), ("token", {"text": "字"})]                 # 註解、陣列、壞 JSON 被丟棄；沒結尾的事件留在緩衝
    assert parser.buffer.startswith("event: tail")
    split = BrowserSseParser()
    one = [event for part in ["event: a\ndata: {\"v\": 1}\n", "\nevent: b\ndata: {\"v\": 2}\n\n"] for event in split.feed(part)]
    assert one == [("a", {"v": 1}), ("b", {"v": 2})]


def test_the_game_screen_flow_in_the_order_the_frontend_calls_the_backend(world, log_lines):
    app, rpg = world
    client = app.test_client()

    # ---- 玩家：Google 登入、年齡確認、開存檔 ----------------------------------------------------------------
    assert google_sign_in(app, client).status_code == 302
    assert client.post("/api/auth/verify-age", json=age_fields(30)).status_code == 200
    assert wallet_total(client) == 200                                            # 首次登入的免費幣於年齡確認通過時發放
    save_id = make_save(app, rpg, client)
    wallet_before = wallet_state(app)

    # ---- 1) 頁面外殼：初始狀態沒有任何劇情 -----------------------------------------------------------------------
    page = client.get(f"/game/{save_id}")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert 'id="game-app"' in html and OPENING not in html and SECRETS["protagonist"] in html   # 主角姓名是存檔摘要（初始狀態），劇情不是
    assert "user1@example.com" not in html and GOOGLE_EMAIL not in html

    # ---- 2) 快照（前端載入的第一步）------------------------------------------------------------------------------
    first = snapshot(client, save_id)
    assert set(first) == SNAPSHOT_KEYS
    assert set(first["world"]) == WORLD_KEYS                                      # 前端 worldView 的白名單
    assert set(first["notebook"]) == NOTEBOOK_SUMMARY_KEYS                        # 快照只有記事簿的用量摘要，沒有全文
    assert first["opening"] == {"state": "pending", "text": None} and first["completed_turns"] == 0 and first["ended"] is False

    # ---- 3) 第 0 幕串流（pending -> 呼叫開場串流）；此時還不能送第一輪 ---------------------------------------------
    early = play(client, save_id, "too-early", ACTION_ONE)
    assert early.status_code == 409 and early.get_json()["error"]["code"] == "opening_pending"
    rpg.script_stream(*story_script(OPENING))
    opening = stream_events(client.post(f"/api/game/sessions/{save_id}/opening/stream"))
    assert names(opening)[0] == "meta" and of(opening, "meta")[0]["kind"] == "opening"
    assert names(opening)[-1] == "done" and of(opening, "done")[0]["opening_state"] == "ready" and of(opening, "done")[0]["coins"] == 200
    assert text_of(opening) == OPENING
    assert snapshot(client, save_id)["opening"] == {"state": "ready", "text": OPENING}     # 前端在 done 後重讀快照取得文字
    assert wallet_state(app) == wallet_before                                     # 第 0 幕不是一輪：不扣幣、沒有帳本

    # ---- 4) 歷史補齊與錢包（與第 0 幕並行載入）---------------------------------------------------------------------
    assert turns_since(client, save_id, 0) == {"turns": [], "has_more": False}
    assert wallet_total(client) == 200

    # ---- 5) 送一輪：事件順序與 done.coins -------------------------------------------------------------------------
    rpg.script_stream(*story_script()).script(STATE)
    one = stream_events(play(client, save_id, "msg-1", ACTION_ONE))
    assert names(one)[0] == "meta" and names(one)[-2:] == ["state", "done"] and set(names(one)) <= {"meta", "token", "ping", "state", "done"}
    assert of(one, "meta")[0]["turn_id"] == 1 and of(one, "meta")[0]["replayed"] is False        # meta.turn_id 就是回合的 seq
    assert text_of(one) == STORY
    state = of(one, "state")[0]
    assert state["updated"] is True and state["location"] == "藏經閣" and state["elapsed_months"] == 1
    done = of(one, "done")[0]
    assert done == {"status": "complete", "turn_number": 1, "coins": 170}
    assert done["coins"] == wallet_total(client) == 170                           # done.coins 就是 GET /api/wallet 的合計

    # ---- 6) 補齊 since_seq：只有 complete 的回合有文字，欄位剛好是前端的白名單 ---------------------------------------------
    caught_up = turns_since(client, save_id, 0)
    assert caught_up["has_more"] is False and len(caught_up["turns"]) == 1
    turn = caught_up["turns"][0]
    assert set(turn) == TURN_KEYS
    assert turn == {"seq": 1, "client_msg_id": "msg-1", "turn_number": 1, "status": "complete", "action": ACTION_ONE, "story": STORY}
    assert turns_since(client, save_id, 1) == {"turns": [], "has_more": False}   # 前端以本機最大 seq 補齊：沒有新的就是空
    after = snapshot(client, save_id)
    assert after["world"]["location"] == "藏經閣" and after["completed_turns"] == 1     # done 之後重讀快照：世界狀態已更新

    # ---- 7) 以相同 client_msg_id 重送：replayed、不呼叫模型、餘額不變 -----------------------------------------------------
    calls = len(rpg.calls)
    replay = stream_events(play(client, save_id, "msg-1", ACTION_ONE))
    assert names(replay) == ["meta", "token", "done"] and of(replay, "meta")[0]["replayed"] is True
    assert text_of(replay) == STORY and of(replay, "done")[0] == {"status": "complete", "turn_number": 1, "coins": 170}
    assert len(rpg.calls) == calls and wallet_total(client) == 170
    other_text = stream_events(play(client, save_id, "msg-1", "不同的行動內容"))     # 同一個編號、不同的行動：RPG 仍重播原本的結果（以已存的回合為準），不另外扣款
    assert of(other_text, "meta")[0]["replayed"] is True and text_of(other_text) == STORY and wallet_total(client) == 170
    assert turns_since(client, save_id, 0)["turns"][0]["action"] == ACTION_ONE

    # ---- 8) 第二輪之後，補齊只回新回合 ---------------------------------------------------------------------------
    rpg.script_stream(*story_script()).script(STATE)
    two = stream_events(play(client, save_id, "msg-2", ACTION_TWO))
    assert of(two, "done")[0] == {"status": "complete", "turn_number": 2, "coins": 140} and of(two, "meta")[0]["turn_id"] == 2
    newer = turns_since(client, save_id, 1)["turns"]
    assert [t["seq"] for t in newer] == [2] and newer[0]["action"] == ACTION_TWO and newer[0]["story"] == STORY

    # ---- 9) 被擋的回合：轉場、done=blocked、沒有扣款，歷史只有狀態沒有文字 --------------------------------------------------
    rpg.script_stream(*story_script(BAD_SENTENCE)).script_stream(*story_script(BAD_SENTENCE))
    blocked = stream_events(play(client, save_id, "msg-3", ACTION_BLOCKED))
    assert [t["will_retry"] for t in of(blocked, "transition")] == [True, False]
    assert of(blocked, "done")[0] == {"status": "blocked", "turn_number": None, "coins": 140}
    assert BAD_SENTENCE not in json.dumps(blocked, ensure_ascii=False) and wallet_total(client) == 140
    ghost = turns_since(client, save_id, 2)["turns"]
    assert len(ghost) == 1 and ghost[0]["status"] == "blocked" and ghost[0]["story"] is None and ghost[0]["turn_number"] is None
    assert BAD_SENTENCE not in json.dumps(ghost, ensure_ascii=False)             # 被擋的輸出永遠不離開伺服器；玩家自己的行動文字仍在欄位裡，前端的 normalizeTurn 對非完整回合一律丟掉

    # ---- 10) 另一位玩家：頁面與端點都得到與不存在逐字相同的 404 ---------------------------------------------------------
    with db(app) as s:
        other = add_user(s, 2)
        s.refresh(other)
        s.expunge(other)
    stranger = logged_in_client(app, other)
    for path in ("", "/turns", "/notebook"):
        theirs = stranger.get(f"/api/game/sessions/{save_id}{path}")
        missing = stranger.get(f"/api/game/sessions/{GHOST}{path}")
        assert theirs.status_code == missing.status_code == 404 and theirs.get_data() == missing.get_data(), path
    stolen, missing_page = stranger.get(f"/game/{save_id}"), stranger.get(f"/game/{GHOST}")
    assert stolen.status_code == missing_page.status_code == 404 and stolen.get_data() == missing_page.get_data()
    assert SECRETS["protagonist"] not in stolen.get_data(as_text=True)
    post = stranger.post(f"/api/game/sessions/{save_id}/turns/stream", json={"client_msg_id": "x", "action": "偷看"})
    assert post.status_code == 404

    # ---- 11) 整個流程的帳與日誌 ------------------------------------------------------------------------------------
    with db(app) as s:
        assert s.scalar(select(func.count()).select_from(MemoryLog).where(MemoryLog.client_msg_id == "too-early")) == 0       # 被拒的請求沒有回合列
    assert rpg.remaining_streams == 0
    assert_logs_are_clean(log_lines)


def test_the_page_shell_and_the_snapshot_never_show_the_opening_or_the_notebook_text(world):
    app, rpg = world
    client = app.test_client()
    assert google_sign_in(app, client).status_code == 302
    assert client.post("/api/auth/verify-age", json=age_fields(30)).status_code == 200
    save_id = make_save(app, rpg, client)
    rpg.script_stream(*story_script(OPENING))
    stream_events(client.post(f"/api/game/sessions/{save_id}/opening/stream"))
    html = client.get(f"/game/{save_id}").get_data(as_text=True)
    assert OPENING not in html                                                    # 第 0 幕一律由前端呼叫快照取得
    assert "notebook_text" not in json.dumps(snapshot(client, save_id))
