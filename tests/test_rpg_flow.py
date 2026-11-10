"""rpg-turn-engine 的端對端整合測試（任務 5.3）：以假的模型供應商（不連網）走完一位玩家的整段旅程，再檢查錢包、帳本、日誌與資料一致。

旅程（全部經過 HTTP 與真實的 Service、Repository 與資料庫）：
Google 首次登入（註冊）→ 年齡確認（發放 200 幣）→ 開存檔 → 第 0 幕 → 七個回合：
1 正常、2 被擋一次後自動重生、3 串流中斷後以同一編號重送、4 至 6 正常把餘額用到不足、7 簽到後以模型提議的結局完結，
其間穿插：餘額不足（409，沒有回合列）、已完成回合的重送、完結後不得再送、完結後仍可讀；
最後匯出個人資料、申請刪除並抹除，確認 RPG 資料與錢包消失、用量帳本匿名化、日誌裡找不到任何劇情或行動文字。

同一組在 SQLite 與真實 SQL Server（`project_test`，標記 sqlserver）上各跑一次。
"""
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.llm.types import LLMTransientError
from app.models import Base, DeletionTombstone, LlmCallLog, MemoryLog, RpgSession, User, Wallet, WalletEntry
from app.models.llm_call_log import KIND_RPG_OPENING, KIND_RPG_STATE, KIND_RPG_STREAM
from app.repositories.deletion_tombstone_repository import DeletionTombstoneRepository
from app.repositories.script_repository import ScriptRepository
from app.repositories.user_repository import UserRepository
from app.services.account_deletion_service import CONFIRM_TEXT
from app.services.account_erasure_service import AccountErasureService
from app.services.google_oidc import GoogleProfile
from tests.conftest import require_sqlserver_test_url
from tests.support.age import age_fields
from tests.support.auth import FakeClock
from tests.support.config_env import AUTH_ENV
from tests.support.game import BAD_SENTENCE, STORY, story_script
from tests.support.google import FakeProvider, google_sign_in
from tests.support.llm import Chunk, Fail, FakeLLMProvider
from tests.support.script_data import validated
from tests.test_llm_providers_by_purpose import BOTH

GOOGLE_EMAIL = "rpg.flow@example.com"
OPENING = "晨霧之中，山門緩緩開啟。"
ACTIONS = {n: f"流程測試的第 {n} 個獨特行動XYZ。" for n in range(1, 8)}
NPC_NAME = "蘇長風"
CARD = {"name": "林舟", "personality": "沉默愛思考", "age": 19, "appearance": "高瘦，常穿灰衣"}
ANCHOR = {"kind": "character", "ref": NPC_NAME, "text": "他的舊識之後"}
NPC = {"name": NPC_NAME, "personality": "沉穩寡言", "age": 168, "appearance": "鬢髮微白", "occupation": "長老", "hidden_trait": "暗中守護青雲宗"}
STATE = json.dumps({"elapsed_months": 1, "current_location": "青雲宗山門"}, ensure_ascii=False)
STATE_ENDING = json.dumps({"elapsed_months": 2, "ending_proposed": True}, ensure_ascii=False)
PASS = json.dumps({"verdict": "pass", "categories": []})


@pytest.fixture(params=["sqlite", pytest.param("sqlserver", marks=pytest.mark.sqlserver)])
def world(request):
    environ = {**BOTH, **AUTH_ENV}
    if request.param == "sqlserver":
        environ["DATABASE_URL"] = require_sqlserver_test_url()
    rpg, google = FakeLLMProvider(), FakeProvider()
    google.profile = GoogleProfile("sub-rpg-flow-0001", GOOGLE_EMAIL, True, "流程測試玩家")
    clock = FakeClock()
    app = create_app(load_config(environ={"APP_ENV": "testing", **environ}), rpg_llm_provider=rpg, identity_provider=google, clock=clock)
    engine = app.extensions["db_engine"]
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield app, rpg
    Base.metadata.drop_all(engine)
    engine.dispose()


def db(app) -> Session:
    return Session(app.extensions["db_engine"])


def parse(response):
    assert response.status_code == 200, response.get_data(as_text=True)
    events = []
    for block in response.get_data(as_text=True)[:-2].split("\n\n"):
        event, data = block.split("\n")
        assert event.startswith("event: ") and data.startswith("data: ")
        events.append((event[7:], json.loads(data[6:])))
    return events


def names(events):
    return [name for name, _ in events]


def of(events, name):
    return [data for event, data in events if event == name]


def coins(client) -> int:
    return client.get("/api/wallet").get_json()["wallet"]["total"]


def play(client, rpg, save_id, n, *, msg=None):
    """送出第 n 個行動（模型腳本由呼叫端事先排好）。"""
    return client.post(f"/api/game/sessions/{save_id}/turns/stream", json={"client_msg_id": msg or f"m-{n}", "action": ACTIONS[n]})


def stream_and_state(rpg, state=STATE, *items):
    rpg.script_stream(*(items or story_script())).script(state)


def test_a_players_whole_journey_leaves_wallet_ledger_logs_and_data_consistent(world, log_lines):
    app, rpg = world
    client = app.test_client()

    # ---- 註冊與年齡確認 -----------------------------------------------------------------------
    assert google_sign_in(app, client).status_code == 302
    assert client.post("/api/auth/verify-age", json=age_fields(30)).status_code == 200
    assert coins(client) == 200                                                   # 首次登入的免費幣於年齡確認通過時發放

    # ---- 開存檔 ---------------------------------------------------------------------------------
    result = validated(characters=[{"name": "林曉", "personality": "活潑好動", "age": 16, "appearance": "瘦小", "occupation": "學徒"}, NPC],
                       ending_mode="custom", ending_text="主角在山中安然離世。")
    with db(app) as s:
        version, _ = ScriptRepository(s).publish_builtin(
            "seed-flow", content=result.content, content_hash=result.content_hash, total_chars=result.total_chars, now=FakeClock()(),
        )
        s.commit()
        version_id = version.public_id
    rpg.script(PASS)                                                              # 主角的模型層審查
    started = client.post("/api/sessions", json={"version_id": version_id, "protagonist": CARD, "anchor": ANCHOR})
    assert started.status_code == 201
    save_id = started.get_json()["session"]["id"]
    assert coins(client) == 200                                                   # 開存檔不扣幣

    # ---- 第 0 幕：不是一輪，不扣幣；重新請求不重抽 --------------------------------------------------
    early = client.post(f"/api/game/sessions/{save_id}/turns/stream", json={"client_msg_id": "too-early", "action": ACTIONS[1]})
    assert early.status_code == 409 and early.get_json()["error"]["code"] == "opening_pending"
    rpg.script_stream(*story_script(OPENING))
    opening = parse(client.post(f"/api/game/sessions/{save_id}/opening/stream", json={}))
    assert names(opening)[0] == "meta" and names(opening)[-1] == "done" and of(opening, "done")[0]["opening_state"] == "ready"
    assert "".join(d["text"] for d in of(opening, "token")) == OPENING
    again = parse(client.post(f"/api/game/sessions/{save_id}/opening/stream", json={}))
    assert of(again, "meta")[0]["replayed"] is True and "".join(d["text"] for d in of(again, "token")) == OPENING
    assert coins(client) == 200 and rpg.remaining_streams == 0

    # ---- 回合 1：正常 -----------------------------------------------------------------------------
    stream_and_state(rpg)
    one = parse(play(client, rpg, save_id, 1))
    assert names(one)[0] == "meta" and names(one)[-2:] == ["state", "done"]
    assert of(one, "done")[0] == {"status": "complete", "turn_number": 1, "coins": 170}
    assert "".join(d["text"] for d in of(one, "token")) == STORY and coins(client) == 170

    # ---- 回合 2：被擋一次後自動重生（只扣一次）-----------------------------------------------------
    rpg.script_stream(*story_script(BAD_SENTENCE))
    stream_and_state(rpg)
    two = parse(play(client, rpg, save_id, 2))
    assert [d["will_retry"] for d in of(two, "transition")] == [True] and of(two, "done")[0]["status"] == "complete"
    assert BAD_SENTENCE not in json.dumps(two, ensure_ascii=False) and coins(client) == 140

    # ---- 回合 3：串流中途失敗 → 退回；以同一編號重送 → 成功 -----------------------------------------
    rpg.script_stream(Chunk("清晨的海風吹過山門。"), Fail(LLMTransientError("network")))
    broken = parse(play(client, rpg, save_id, 3))
    assert names(broken)[-1] == "error" and "done" not in names(broken) and coins(client) == 140
    stream_and_state(rpg)
    three = parse(play(client, rpg, save_id, 3))
    assert of(three, "meta")[0]["attempt"] == 2 and of(three, "done")[0] == {"status": "complete", "turn_number": 3, "coins": 110}

    # ---- 已完成回合的重送：不呼叫模型、不扣幣 ------------------------------------------------------------
    calls = len(rpg.calls)
    replay = parse(play(client, rpg, save_id, 3))
    assert names(replay) == ["meta", "token", "done"] and of(replay, "meta")[0]["replayed"] is True
    assert len(rpg.calls) == calls and coins(client) == 110

    # ---- 回合 4 至 6：把餘額用到不足 ---------------------------------------------------------------
    for n in (4, 5, 6):
        stream_and_state(rpg)
        assert of(parse(play(client, rpg, save_id, n)), "done")[0]["status"] == "complete"
    assert coins(client) == 20
    broke = play(client, rpg, save_id, 7)
    assert broke.status_code == 409 and broke.get_json()["error"]["code"] == "insufficient_coins" and broke.mimetype == "application/json"
    with db(app) as s:
        assert s.scalar(select(func.count()).select_from(MemoryLog).where(MemoryLog.client_msg_id == "m-7")) == 0     # 沒有留下回合列

    # ---- 簽到補幣後，回合 7 由模型提議結局 → 存檔完結 --------------------------------------------------
    assert client.post("/api/wallet/checkin", json={}).status_code == 200 and coins(client) == 220
    stream_and_state(rpg, STATE_ENDING)
    seven = parse(play(client, rpg, save_id, 7))
    assert of(seven, "done")[0] == {"status": "ended", "turn_number": 7, "coins": 190}
    over = play(client, rpg, save_id, 7, msg="after-the-end")
    assert over.status_code == 409 and over.get_json()["error"]["code"] == "session_ended"
    assert of(parse(play(client, rpg, save_id, 7)), "done")[0]["status"] == "ended"      # 結局回合的重送仍拿得到結果
    assert coins(client) == 190 and rpg.remaining_streams == 0 and rpg.remaining == 0

    # ---- 完結後仍可讀 ---------------------------------------------------------------------------------
    snapshot = client.get(f"/api/game/sessions/{save_id}").get_json()["session"]
    assert snapshot["ended"] is True and snapshot["completed_turns"] == 7 and snapshot["opening"]["text"] == OPENING
    assert snapshot["world"]["location"] == "青雲宗山門" and snapshot["world"]["elapsed_months"] >= 2
    listing = client.get(f"/api/game/sessions/{save_id}/turns").get_json()
    complete = [t for t in listing["turns"] if t["status"] == "complete"]
    assert [t["turn_number"] for t in complete] == [1, 2, 3, 4, 5, 6, 7] and all(t["story"] == STORY for t in complete)
    assert [t["action"] for t in complete] == [ACTIONS[n] for n in range(1, 8)]
    assert all(t["story"] is None for t in listing["turns"] if t["status"] != "complete")
    assert BAD_SENTENCE not in json.dumps(listing, ensure_ascii=False)

    # ---- 錢包與帳本一致 -------------------------------------------------------------------------------
    with db(app) as s:
        wallet = s.scalar(select(Wallet))
        entries = list(s.scalars(select(WalletEntry).order_by(WalletEntry.seq)))
        assert wallet.free_balance + wallet.paid_balance == 190
        assert sum(e.free_delta + e.paid_delta for e in entries) == 190                          # 每一筆異動加總就是餘額
        assert (entries[-1].free_after, entries[-1].paid_after) == (wallet.free_balance, wallet.paid_balance)
        holds = [e for e in entries if e.resolution is not None]
        assert sorted(e.resolution for e in holds).count("settled") == 7                         # 七個完成的回合各結算一次
        refunded = [e for e in entries if e.ref_key is not None]
        assert len(refunded) == len([e for e in holds if e.resolution == "refunded"]) == 1       # 只有失敗的那一次被退回
        # 模型呼叫的帳本：第 0 幕 1 次；串流 = 回合 1、2（被擋＋重生）、3（失敗＋重送）、4 至 7 → 1+2+2+4 = 9；狀態呼叫 7 次
        kinds = [row.kind for row in s.scalars(select(LlmCallLog))]
        assert (kinds.count(KIND_RPG_OPENING), kinds.count(KIND_RPG_STREAM), kinds.count(KIND_RPG_STATE)) == (1, 9, 7), kinds

    # ---- 日誌裡沒有劇情、行動與角色名稱 ---------------------------------------------------------------------
    raw = log_lines.raw()
    for secret in (STORY, "清晨的海風", OPENING, BAD_SENTENCE, NPC_NAME, "林舟", "暗中守護", GOOGLE_EMAIL, *ACTIONS.values()):
        assert secret not in raw, secret
    events = [line.get("event") for line in log_lines() if str(line.get("event", "")).startswith("rpg.")]
    assert events.count("rpg.turn_completed") == 7 and events.count("rpg.session_ended") == 1
    assert events.count("rpg.turn_blocked") == 1 and events.count("rpg.turn_interrupted") == 1
    assert events.count("rpg.opening_started") == 1 and events.count("rpg.opening_ready") == 1

    # ---- 匯出 ----------------------------------------------------------------------------------------
    exported = client.post("/api/account/export", json={"format": "json"})
    assert exported.status_code == 200
    text = exported.get_data(as_text=True)
    assert ACTIONS[1] in text and STORY in text and OPENING in text

    # ---- 申請刪除並抹除 -----------------------------------------------------------------------------
    assert client.post("/api/account/delete", json={"confirm_text": CONFIRM_TEXT}).status_code == 200
    with db(app) as s:
        user_id = s.scalar(select(User.id))
        service = AccountErasureService(
            users=UserRepository(s), tombstones=DeletionTombstoneRepository(s), commit=s.commit, rollback=s.rollback,
        )
        assert service.erase_pending_due(user_id, cutoff=FakeClock()() + timedelta(days=8), now=FakeClock()() + timedelta(days=8))
    with db(app) as s:
        for model in (User, RpgSession, MemoryLog, Wallet, WalletEntry):
            assert s.scalar(select(func.count()).select_from(model)) == 0, model.__name__
        assert s.scalar(select(func.count()).select_from(DeletionTombstone)) == 1
        remaining = list(s.scalars(select(LlmCallLog)))
        assert len(remaining) >= 17 and all(row.user_id is None for row in remaining)        # 用量帳本保留，但已匿名化
