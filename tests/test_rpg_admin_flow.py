"""admin-unlimited-coins 的端到端整合測試（任務 2.2、2.3）：以假的模型供應商（不連網）走過 HTTP、真實的 Service、Repository 與資料庫。

管理員（角色為 admin 且啟用中）不消耗遊戲幣：預扣寫入金額 0 的帳本列、餘額不足也不被拒；其餘（免費次數的計數、全站每日 token 上限、
`llm_call_logs`）與一般玩家相同。

- 餘額為 0 的管理員：一輪 → 兩次免費修訂 → 付費的重新生成與編輯（預扣 0）→ `done.coins` 一律是 0、帳本每一列都是 0 且已結算、
  `last_turn` 的費用是 0、`/api/wallet` 回 `coin_exempt: true`；
- 同一個世界（幣價調高到玩家付不起）裡的一般玩家仍得到 409 `insufficient_coins`、沒有回合列；
- 串流中途失敗 → 退回（金額 0）→ 以同一編號重送，`attempt` 加 1、新的鍵、仍是 0；
- 全站每日 token 上限已達：管理員也得到 503 `system_busy`，沒有回合列與預扣；
- 模型呼叫照常寫入 `llm_call_logs`，日誌找不到行動與劇情。

同一組在 SQLite 與真實 SQL Server（`project_test`，標記 sqlserver）上各跑一次。
"""
import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.llm.types import LLMTransientError
from app.models import Base, LlmCallLog, MemoryLog
from app.models.llm_call_log import KIND_RPG_STATE, KIND_RPG_STREAM
from app.repositories.runtime_limit_repository import RuntimeLimitRepository
from tests.conftest import require_sqlserver_test_url
from tests.support.auth import FakeClock, add_user, sign_in_as
from tests.support.config_env import AUTH_ENV
from tests.support.game import story_script
from tests.support.llm import Chunk, Fail, FakeLLMProvider, Step
from tests.test_game_api import make_save
from tests.test_llm_providers_by_purpose import BOTH
from tests.test_rpg_revision_flow import NEW_ACTION, REASON, SECOND_EDIT, Play, action, done_of, parse, sentences

PARAMS = ["sqlite", pytest.param("sqlserver", marks=pytest.mark.sqlserver)]
STATE = json.dumps({"elapsed_months": 1, "current_location": "青雲宗山門"}, ensure_ascii=False)
PRICE = 500                                              # 高於註冊贈幣 200：一般玩家付不起一輪，管理員不受影響


def build(request, *, cost: int | None = None, extra_env: dict | None = None):
    """一個世界：管理員（編號 90）與一般玩家（編號 1）各自有一個存檔與登入後的 client。"""
    environ = {**BOTH, **AUTH_ENV, **(extra_env or {})}
    if request.param == "sqlserver":
        environ["DATABASE_URL"] = require_sqlserver_test_url()
    provider = FakeLLMProvider()
    app = create_app(load_config(environ={"APP_ENV": "testing", **environ}), rpg_llm_provider=provider, clock=FakeClock())
    engine = app.extensions["db_engine"]
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    plays = {}
    with Session(engine) as s:
        if cost is not None:
            RuntimeLimitRepository(s).set_value("coin.turn_cost.standard", cost, FakeClock()())
        users = {"admin": add_user(s, 90, role="admin", age=30), "player": add_user(s, 1, password=None)}
        ids = {name: (user.id, user.token_version) for name, user in users.items()}
        s.commit()
    for name, (user_id, token_version) in ids.items():
        client = app.test_client()
        sign_in_as(client, user_id, token_version)
        plays[name] = Play(app, provider, client, user_id, make_save(app, user_id))
    return plays, engine


@pytest.fixture(params=PARAMS)
def world(request):
    plays, engine = build(request)
    yield plays
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture(params=PARAMS)
def pricey_world(request):
    plays, engine = build(request, cost=PRICE)
    yield plays
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture(params=PARAMS)
def budget_world(request):
    plays, engine = build(request, extra_env={"USAGE_DAILY_TOKEN_LIMIT": "1000"})
    yield plays
    Base.metadata.drop_all(engine)
    engine.dispose()


def llm_rows(play: Play, kind: str, user_id: int) -> int:
    with Session(play.app.extensions["db_engine"]) as s:
        return s.scalar(select(func.count()).select_from(LlmCallLog).where(LlmCallLog.kind == kind, LlmCallLog.user_id == user_id))


def wallet_of(play: Play) -> dict:
    response = play.client.get("/api/wallet")
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["wallet"]


# ---- 旅程：回合與付費的修訂都是 0 幣 ---------------------------------------------------------------------

def test_an_admin_with_an_empty_wallet_plays_and_revises_past_the_free_count_without_paying(world, log_lines):
    play = world["admin"]
    assert wallet_of(play) | {"checkin": None} == {
        "free_balance": 0, "paid_balance": 0, "total": 0, "free_cap": 1000, "turn_cost": {"standard": 0}, "coin_exempt": True, "checkin": None,
    }

    first = play.turn(1, story=sentences("初版"))
    assert done_of(first) == {"status": "complete", "turn_number": 1, "coins": 0, "revision": 1, "free_left": 2}

    assert done_of(play.revise("regenerate", "m-1", 1, "甲"))["free_left"] == 1                       # 兩次免費：不留預扣列
    assert done_of(play.revise("edit", "m-1", 2, "乙"))["free_left"] == 0
    assert len(play.holds()) == 1

    third = play.revise("regenerate", "m-1", 3, "丙")                                                  # 付費的修訂：預扣 0 幣、不被拒
    assert done_of(third) == {"status": "complete", "turn_number": 1, "coins": 0, "revision": 4, "free_left": 0}
    fourth = play.revise("edit", "m-1", 4, "丁", new_action=SECOND_EDIT)
    assert done_of(fourth) == {"status": "complete", "turn_number": 1, "coins": 0, "revision": 5, "free_left": 0}

    holds = play.holds()
    assert len(holds) == 3 and len({entry.idempotency_key for entry in holds}) == 3
    assert [(entry.resolution, entry.free_delta, entry.paid_delta) for entry in holds] == [("settled", 0, 0)] * 3
    assert all(entry.free_after == 0 and entry.paid_after == 0 for entry in play.entries())             # 每一列的餘額都還是 0
    assert play.coins() == 0 and (play.turn_row("m-1").regen_count, play.turn_row("m-1").user_action) == (4, SECOND_EDIT)
    assert play.last_turn() == {
        "client_msg_id": "m-1", "seq": 1, "revision": 5, "revisable": True, "reason": None, "free_left": 0, "regenerate_cost": 0, "edit_cost": 0,
    }

    # 模型呼叫照常記錄；日誌沒有行動、理由與劇情
    assert llm_rows(play, KIND_RPG_STREAM, play.user_id) >= 4 and llm_rows(play, KIND_RPG_STATE, play.user_id) >= 4
    raw = log_lines.raw()
    for secret in (action(1), NEW_ACTION, SECOND_EDIT, REASON, *sentences("初版"), *sentences("丁")):
        assert secret not in raw


def test_an_ordinary_player_who_cannot_afford_a_turn_gets_a_409_while_the_admin_plays_in_the_same_world(pricey_world):
    admin, player = pricey_world["admin"], pricey_world["player"]
    refused = player.post(1)
    assert refused.status_code == 409 and refused.get_json()["error"]["code"] == "insufficient_coins"
    with Session(player.app.extensions["db_engine"]) as s:
        assert s.scalar(select(func.count()).select_from(MemoryLog).where(MemoryLog.session_id.is_not(None), MemoryLog.client_msg_id == "m-1")) == 0
    assert len(player.holds()) == 0                                          # 沒有預扣（被拒絕的請求整筆回滾，連當次補發的贈幣也一起回滾）

    assert done_of(admin.turn(1, story=sentences("管理員")))["coins"] == 0
    assert [(entry.free_delta, entry.paid_delta) for entry in admin.holds()] == [(0, 0)]


def test_the_wallet_of_a_player_does_not_claim_an_exemption_and_keeps_the_configured_price(pricey_world):
    body = wallet_of(pricey_world["player"])
    assert body["coin_exempt"] is False and body["turn_cost"] == {"standard": PRICE} and body["total"] == 200


# ---- 中斷與重送 ------------------------------------------------------------------------------------------

def test_an_admin_turn_that_breaks_mid_stream_is_refunded_for_zero_and_the_resend_gets_a_new_attempt_and_key(world):
    play = world["admin"]
    play.provider.script_stream(Chunk("清晨的海風吹過山門。"), Fail(LLMTransientError("network")))
    broken = parse(play.post(1))
    assert broken[-1][0] == "error" and all(name != "done" for name, _ in broken)
    (first,) = play.holds()
    assert (first.resolution, first.free_delta, first.paid_delta) == ("refunded", 0, 0)
    assert play.coins() == 0

    resent = play.turn(1, story=sentences("重送"))
    meta = next(data for name, data in resent if name == "meta")
    assert meta["attempt"] == 2 and done_of(resent)["coins"] == 0
    keys = [entry.idempotency_key for entry in play.holds()]
    assert len(keys) == 2 and keys[0] != keys[1] and keys[1].endswith(":2")
    assert [entry.resolution for entry in play.holds()] == ["refunded", "settled"]


# ---- 全站 token 上限不因管理員而放寬 ------------------------------------------------------------------------

def test_when_the_site_token_limit_is_reached_an_admin_gets_system_busy_and_nothing_is_written(budget_world):
    play = budget_world["admin"]
    with Session(play.app.extensions["db_engine"]) as s:
        s.add(LlmCallLog(
            user_id=None, conversation_id=None, kind="chat", provider="fake", model="m", attempt=1, status="ok", counted=False,
            prompt_tokens=1000, latency_ms=10, created_at=FakeClock()(),
        ))
        s.commit()
    response = play.post(1)
    assert response.status_code == 503 and response.get_json()["error"]["code"] == "system_busy"
    with Session(play.app.extensions["db_engine"]) as s:
        assert s.scalar(select(func.count()).select_from(MemoryLog).where(MemoryLog.client_msg_id == "m-1")) == 0
    assert play.holds() == [] and play.provider.calls == []
