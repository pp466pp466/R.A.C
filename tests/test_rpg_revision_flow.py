"""rpg-turn-revision 的端到端整合測試（任務 3.6）：以假的模型供應商（不連網）走過 HTTP、真實的 Service、Repository 與資料庫。

旅程（預設幣價 30、前 2 次重新生成免費）：
- 一輪 → 兩次免費重新生成（帳本沒有新列）→ 第三次付費（淨扣 30）→ 編輯（淨扣 30、已用次數歸零）→ 再重新生成又免費；
- 兩個請求以同一個 `expected_revision`：先到的認領、後到的得到 409；進行中送新行動也得到 409；完成後後到的得到 `revision_conflict`；
- 失敗的付費重新生成（退回）→ 再付費重新生成：鍵不重複、淨扣一次；
- 第 11 輪完成並抓取記事簿之後修訂第 11 輪：記事簿、`notebook_upto_turn` 與抓取呼叫都不變；邊界輪（第 10 輪）在下一輪之前被修訂，之後的抓取用的是修訂後的內容；
- 整個流程的日誌找不到理由、行動與劇情。

「進行中」以 `buffered=False` 的請求模擬：認領發生在請求內，事件串流要等呼叫端讀取回應才開始。同一組在 SQLite 與真實 SQL Server
（`project_test`，標記 sqlserver）上各跑一次。
"""
import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base, LlmCallLog, MemoryLog, RpgSession, TurnRegeneration
from app.models.llm_call_log import KIND_RPG_NOTEBOOK
from app.models.wallet import WalletEntry
from app.repositories.runtime_limit_repository import RuntimeLimitRepository
from app.repositories.wallet_repository import WalletRepository
from tests.conftest import require_sqlserver_test_url
from tests.support.auth import FakeClock, add_user, sign_in_as
from tests.support.config_env import AUTH_ENV
from tests.support.game import story_script
from tests.support.llm import FakeLLMProvider, Step
from tests.test_game_api import make_save
from tests.test_llm_providers_by_purpose import BOTH

STATE_OLD = json.dumps({"elapsed_months": 3, "current_location": "古劍山莊", "goals_add": [{"text": "拜見莊主"}]}, ensure_ascii=False)
STATE_NEW = json.dumps({"elapsed_months": 1, "status_summary": "你在寺中避雨。"}, ensure_ascii=False)
ENTRIES = ("主角在青雲宗山門前遇見守門弟子。", "守門弟子對主角抱持戒心。")
REASON = "獨特的理由只該在回饋表裡"
NEW_ACTION = "獨特的新行動只該在請求裡"
CHEAP = 5                                                                        # 第 11 輪的案例用便宜的幣價，不必簽到就夠用


def action(n: int) -> str:
    return f"修訂流程的第 {n} 個行動XYZ。"


def sentences(label: str) -> tuple[str, str]:
    return (f"獨特劇情{label}的第一句。", f"獨特劇情{label}的第二句。")


def reply(*entries: str) -> str:
    return json.dumps({"entries": list(entries)}, ensure_ascii=False)


def build_world(request, *, cost: int | None):
    environ = {**BOTH, **AUTH_ENV}
    if request.param == "sqlserver":
        environ["DATABASE_URL"] = require_sqlserver_test_url()
    provider = FakeLLMProvider()
    app = create_app(load_config(environ={"APP_ENV": "testing", **environ}), rpg_llm_provider=provider, clock=FakeClock())
    engine = app.extensions["db_engine"]
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        if cost is not None:
            RuntimeLimitRepository(s).set_value("coin.turn_cost.standard", cost, FakeClock()())
        user = add_user(s, 1, password=None)
        user_id, token_version = user.id, user.token_version
        s.commit()
    client = app.test_client()
    sign_in_as(client, user_id, token_version)
    return Play(app, provider, client, user_id, make_save(app, user_id)), engine


PARAMS = ["sqlite", pytest.param("sqlserver", marks=pytest.mark.sqlserver)]


@pytest.fixture(params=PARAMS)
def world(request):
    play, engine = build_world(request, cost=None)
    yield play
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture(params=PARAMS)
def cheap_world(request):
    play, engine = build_world(request, cost=CHEAP)
    yield play
    Base.metadata.drop_all(engine)
    engine.dispose()


class Play:
    def __init__(self, app, provider, client, user_id, save_id) -> None:
        self.app, self.provider, self.client, self.user_id, self.save_id = app, provider, client, user_id, save_id

    # ---- 動作 ---------------------------------------------------------------------------------

    def post(self, n: int, **options):
        return self.client.post(
            f"/api/game/sessions/{self.save_id}/turns/stream", json={"client_msg_id": f"m-{n}", "action": action(n)}, **options,
        )

    def turn(self, n: int, *capture_steps: Step, story: tuple[str, ...] = ()) -> list[tuple[str, dict]]:
        """排好腳本（串流、狀態呼叫，加上抓取呼叫）後送出第 n 輪；回應關閉時抓取已完成。回傳解析後的事件。"""
        self.provider.script_stream(*story_script(*story)).script(Step(STATE_OLD), *capture_steps)
        return parse(self.post(n))

    def revision_response(self, kind: str, msg: str, revision: int, **options):
        body = {"expected_revision": revision}
        body.update({"category": "other", "reason": REASON} if kind == "regenerate" else {"action": NEW_ACTION})
        return self.client.post(f"/api/game/sessions/{self.save_id}/turns/{msg}/{kind}/stream", json=body, **options)

    def script_revision(self, label: str, *state_steps: Step, story: tuple[str, ...] | None = None) -> None:
        self.provider.script_stream(*story_script(*(story or sentences(label)))).script(*(state_steps or (Step(STATE_NEW),)))

    def revise(self, kind: str, msg: str, revision: int, label: str = "新", **kwargs) -> list[tuple[str, dict]]:
        self.script_revision(label, **kwargs)
        return parse(self.revision_response(kind, msg, revision))

    # ---- 觀察（每次都讀最新的資料）----------------------------------------------------------------

    def coins(self) -> int:
        with Session(self.app.extensions["db_engine"]) as s:
            state = WalletRepository(s).state(self.user_id)
            return 0 if state is None else state.free_balance + state.paid_balance

    def entries(self) -> list[WalletEntry]:
        with Session(self.app.extensions["db_engine"]) as s:
            return list(s.scalars(select(WalletEntry).order_by(WalletEntry.seq)))

    def holds(self) -> list[WalletEntry]:
        return [entry for entry in self.entries() if entry.kind == "turn_hold"]

    def save(self) -> tuple[str, int, int]:
        with Session(self.app.extensions["db_engine"]) as s:
            row = s.scalar(select(RpgSession).where(RpgSession.public_id == self.save_id))
            return row.notebook_text, row.notebook_version, row.notebook_upto_turn

    def turn_row(self, msg: str) -> MemoryLog:
        with Session(self.app.extensions["db_engine"]) as s:
            return s.scalar(select(MemoryLog).where(MemoryLog.client_msg_id == msg))

    def notebook_calls(self) -> int:
        with Session(self.app.extensions["db_engine"]) as s:
            return s.scalar(select(func.count()).select_from(LlmCallLog).where(LlmCallLog.kind == KIND_RPG_NOTEBOOK))

    def feedback(self) -> list[TurnRegeneration]:
        with Session(self.app.extensions["db_engine"]) as s:
            return list(s.scalars(select(TurnRegeneration).order_by(TurnRegeneration.id)))

    def prompt(self, index: int) -> str:
        return "".join(part for content in self.provider.calls[index].contents for part in content.parts)

    def last_turn(self) -> dict:
        return self.client.get(f"/api/game/sessions/{self.save_id}").get_json()["session"]["last_turn"]


def parse(response) -> list[tuple[str, dict]]:
    assert response.status_code == 200, response.get_data(as_text=True)
    events = []
    for block in response.get_data(as_text=True)[:-2].split("\n\n"):
        event, data = block.split("\n")
        events.append((event[len("event: "):], json.loads(data[len("data: "):])))
    return events


def done_of(events) -> dict:
    assert events[-1][0] == "done", events[-1]
    return events[-1][1]


def set_free_regenerations(play: Play, value: int) -> None:
    with Session(play.app.extensions["db_engine"]) as s:
        RuntimeLimitRepository(s).set_value("rpg.free_regenerations", value, FakeClock()())
        s.commit()


# ---- 費用旅程 ----------------------------------------------------------------------------------

def test_the_whole_revision_journey_costs_exactly_what_the_rules_say(world, log_lines):
    play = world
    play.turn(1, story=sentences("初版"))
    assert play.coins() == 170 and len(play.holds()) == 1
    ledger_rows = len(play.entries())

    # 兩次免費的重新生成：帳本沒有新列
    first = play.revise("regenerate", "m-1", 1, "甲")
    assert done_of(first) == {"status": "complete", "turn_number": 1, "coins": 170, "revision": 2, "free_left": 1}
    second = play.revise("regenerate", "m-1", 2, "乙")
    assert done_of(second) == {"status": "complete", "turn_number": 1, "coins": 170, "revision": 3, "free_left": 0}
    assert play.coins() == 170 and len(play.entries()) == ledger_rows

    # 第三次：付費，淨扣一輪的幣價
    third = play.revise("regenerate", "m-1", 3, "丙")
    assert done_of(third) == {"status": "complete", "turn_number": 1, "coins": 140, "revision": 4, "free_left": 0}
    assert play.coins() == 140 and len(play.holds()) == 2

    # 編輯：再淨扣一輪的幣價，已用次數歸零
    edited = play.revise("edit", "m-1", 4, "丁")
    assert done_of(edited) == {"status": "complete", "turn_number": 1, "coins": 110, "revision": 5, "free_left": 2}
    assert play.coins() == 110 and len(play.holds()) == 3
    assert play.turn_row("m-1").regen_count == 0

    # 編輯之後又有免費的重新生成
    again = play.revise("regenerate", "m-1", 5, "戊")
    assert done_of(again) == {"status": "complete", "turn_number": 1, "coins": 110, "revision": 6, "free_left": 1}
    assert play.coins() == 110 and len(play.holds()) == 3

    # 三筆預扣都已結算，沒有退回；鍵不重複
    holds = play.holds()
    assert [(entry.resolution, entry.free_delta + entry.paid_delta) for entry in holds] == [("settled", -30)] * 3
    assert len({entry.idempotency_key for entry in holds}) == 3
    assert [entry.idempotency_key.rsplit(":", 1)[1] for entry in holds] == ["1", "4", "5"]                 # 原回合是 1、付費修訂用認領後的新序號

    # 讀取的是最後一版內容與新的 revision、免費次數
    turns = play.client.get(f"/api/game/sessions/{play.save_id}/turns").get_json()["turns"]
    assert [(item["story"], item["action"], item["revision"], item["turn_number"]) for item in turns] == [
        ("".join(sentences("戊")), NEW_ACTION, 6, 1),
    ]
    assert play.last_turn() == {
        "client_msg_id": "m-1", "seq": 1, "revision": 6, "revisable": True, "reason": None, "free_left": 1, "regenerate_cost": 0, "edit_cost": 30,
    }
    resent = parse(play.client.post(f"/api/game/sessions/{play.save_id}/turns/stream", json={"client_msg_id": "m-1", "action": "不同的文字也一樣"}))
    assert [data["text"] for name, data in resent if name == "token"] == ["".join(sentences("戊"))]     # 重送得到最新版本
    assert [(row.kind, row.result, row.is_free) for row in play.feedback()] == [
        ("regenerate", "applied", True), ("regenerate", "applied", True), ("regenerate", "applied", False), ("edit", "applied", False),
        ("regenerate", "applied", True),
    ]

    # 整個流程的日誌：沒有理由、行動與劇情
    raw = log_lines.raw()
    for secret in (REASON, NEW_ACTION, action(1), *(text for label in ("初版", "甲", "乙", "丙", "丁", "戊") for text in sentences(label))):
        assert secret not in raw, secret
    events = [line.get("event") for line in log_lines() if str(line.get("event", "")).startswith("rpg.revision_")]
    assert events.count("rpg.revision_started") == 5 and events.count("rpg.revision_completed") == 5 and "rpg.revision_failed" not in events


# ---- 並行與進行中 --------------------------------------------------------------------------------

def test_two_requests_with_the_same_expected_revision_only_one_wins_and_a_new_action_is_refused_meanwhile(world):
    play = world
    play.turn(1)
    play.script_revision("先到")
    first = play.revision_response("regenerate", "m-1", 1, buffered=False)             # 認領已在請求內完成，事件串流還沒開始
    assert first.status_code == 200

    # 另一個裝置同時操作
    second = play.revision_response("regenerate", "m-1", 1)
    assert second.status_code == 409 and second.get_json()["error"]["code"] == "turn_in_progress"
    third = play.revision_response("edit", "m-1", 1)
    assert third.status_code == 409 and third.get_json()["error"]["code"] == "turn_in_progress"
    new_turn = play.post(2)                                                            # 重新生成進行中送新行動
    assert new_turn.status_code == 409 and new_turn.get_json()["error"]["code"] == "turn_in_progress"
    assert play.turn_row("m-2") is None and len(play.holds()) == 1 and len(play.feedback()) == 1 and play.coins() == 170

    events = parse(first)                                                              # 先到的完成
    assert done_of(events) == {"status": "complete", "turn_number": 1, "coins": 170, "revision": 2, "free_left": 1}
    late = play.revision_response("regenerate", "m-1", 1)                              # 後到的帶著舊的 revision
    assert late.status_code == 409 and late.get_json()["error"]["code"] == "revision_conflict"
    assert play.turn_row("m-1").llm_raw_response == "".join(sentences("先到")) and len(play.feedback()) == 1

    play.turn(2)                                                                       # 完成後可以送新行動
    after = play.revision_response("regenerate", "m-1", 2)
    assert after.status_code == 409 and after.get_json()["reason"] == "not_last"       # 下一輪開始之後，上一輪不可再動


def test_a_new_turn_being_generated_blocks_a_revision_of_the_previous_one(world):
    play = world
    play.turn(1)
    play.provider.script_stream(*story_script()).script(Step(STATE_OLD))
    pending = play.post(2, buffered=False)                                             # 第 2 輪已開始（回合列與預扣在請求內），串流還沒開始
    assert pending.status_code == 200
    blocked = play.revision_response("regenerate", "m-1", 1)
    assert blocked.status_code == 409 and blocked.get_json()["error"]["code"] == "not_revisable" and blocked.get_json()["reason"] == "in_progress"
    assert play.feedback() == []
    assert done_of(parse(pending))["turn_number"] == 2


# ---- 失敗與再試 ----------------------------------------------------------------------------------

def test_a_failed_paid_regeneration_refunds_and_the_next_one_uses_a_new_key_and_charges_once(world):
    play = world
    set_free_regenerations(play, 0)                                                    # 一開始就收費
    play.turn(1)
    assert play.coins() == 170

    play.script_revision("失敗", Step("壞掉"), Step("還是壞掉"))                      # 新文字通過審查，但狀態呼叫兩次都失敗
    failed = parse(play.revision_response("regenerate", "m-1", 1))
    assert failed[-1][0] == "error" and failed[-1][1]["code"] == "generation_failed" and "done" not in [name for name, _ in failed]
    assert play.coins() == 170 and play.turn_row("m-1").revision == 1                  # 已退回、舊內容不變
    assert play.turn_row("m-1").revising_started_at is None
    assert [row.result for row in play.feedback()] == ["failed"]

    ok = play.revise("regenerate", "m-1", 1, "成功")
    assert done_of(ok) == {"status": "complete", "turn_number": 1, "coins": 140, "revision": 2, "free_left": 0}
    assert play.coins() == 140                                                         # 淨扣一次
    holds = play.holds()
    assert [entry.resolution for entry in holds] == ["settled", "refunded", "settled"]
    assert [entry.idempotency_key.rsplit(":", 1)[1] for entry in holds] == ["1", "2", "3"]       # 退回過的鍵不會被撿回
    assert len({entry.idempotency_key for entry in holds}) == 3
    assert [row.result for row in play.feedback()] == ["failed", "applied"]


# ---- 記事簿 ------------------------------------------------------------------------------------

def test_revising_the_eleventh_turn_after_the_capture_leaves_the_notebook_and_the_capture_calls_alone(cheap_world):
    play = cheap_world
    for n in range(1, 11):
        play.turn(n)
    play.turn(11, Step(reply(*ENTRIES)))                                               # 第 11 輪完成後抓取第 1 至 10 輪
    before = play.save()
    assert before == ("\n".join(ENTRIES), 2, 10) and play.notebook_calls() == 1

    calls = len(play.provider.calls)
    events = play.revise("regenerate", "m-11", 1, "第十一輪的修訂版")
    assert done_of(events)["status"] == "complete" and done_of(events)["revision"] == 2
    assert len(play.provider.calls) - calls == 2                                       # 只有串流與狀態呼叫
    assert play.save() == before and play.notebook_calls() == 1                        # 記事簿、版本、標記與抓取呼叫都不變

    play.turn(12)                                                                      # 下一輪也不補抓：同一個視窗已處理
    assert play.save() == before and play.notebook_calls() == 1


def test_a_boundary_turn_revised_before_the_next_turn_is_captured_with_its_final_content(cheap_world):
    play = cheap_world
    for n in range(1, 10):
        play.turn(n)
    old, new = ("第十輪原本的獨特劇情。",), ("第十輪修訂後的獨特劇情。",)
    play.turn(10, story=old)
    assert play.save() == ("", 1, 0)                                                   # 第 10 輪完成時還沒有待抓取的視窗

    events = play.revise("regenerate", "m-10", 1, story=new)
    assert done_of(events)["revision"] == 2 and play.save() == ("", 1, 0) and play.notebook_calls() == 0     # 修訂不抓取

    calls = len(play.provider.calls)
    play.turn(11, Step(reply(*ENTRIES)))
    capture_prompt = play.prompt(calls + 2)                                            # 這一輪的呼叫順序：串流、狀態、抓取
    assert new[0] in capture_prompt and old[0] not in capture_prompt                   # 抓取看到的是第 10 輪的最終內容
    assert play.save() == ("\n".join(ENTRIES), 2, 10)
