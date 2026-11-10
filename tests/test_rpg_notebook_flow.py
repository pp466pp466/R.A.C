"""rpg-notebook 的端到端整合測試（任務 3.4）：以假的模型供應商（不連網）走過 HTTP、真實的 Service、Repository 與資料庫。

旅程：同一位玩家的存檔連續玩 22 輪——
- 第 1 至 10 輪：記事簿仍是空的，沒有任何抓取呼叫（第 10 輪完成時還沒有待抓取的視窗）；
- 第 11 輪：串流結束之後（回應關閉）抓取第 1 至 10 輪，追加條目、`notebook_upto_turn` 為 10、版本加 1（新存檔的版本從 1 開始）；事件序列不變、不另外扣遊戲幣；
- 第 12 輪：回合的上下文帶著這些條目；沒有新的抓取；
- 第 21 輪：抓取失敗（主要與備援模型都暫時性失敗）→ 標記不動；
- 第 22 輪：下一個回合補做同一個視窗（第 11 至 20 輪），接在既有條目之後追加；
最後掃描日誌：找不到任何記事簿條目、行動或劇情文字。

專案的測試用戶端以 `buffered=True` 開啟請求，讀完本文就自動呼叫 `close()`，所以回合的 `client.post` 返回時抓取已經完成；
另有一個案例以 `buffered=False` 驗證「串流先結束、關閉之後才抓取」。同一組在 SQLite 與真實 SQL Server（`project_test`，標記 sqlserver）上各跑一次。
"""
import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.llm.types import TRANSIENT_RATE_LIMITED, LLMTransientError
from app.models import Base, LlmCallLog, RpgSession
from app.models.llm_call_log import KIND_RPG_NOTEBOOK, KIND_RPG_STATE, KIND_RPG_STREAM, STATUS_OK
from app.models.wallet import WalletEntry
from app.repositories.runtime_limit_repository import RuntimeLimitRepository
from tests.conftest import require_sqlserver_test_url
from tests.support.auth import FakeClock, add_user, sign_in_as
from tests.support.config_env import AUTH_ENV
from tests.support.game import STORY, story_script
from tests.support.llm import FakeLLMProvider, Step
from tests.test_game_api import make_save
from tests.test_llm_providers_by_purpose import BOTH, RPG_MODEL

STATE_OK = json.dumps({"elapsed_months": 1, "current_location": "山門前"}, ensure_ascii=False)
FIRST_ENTRIES = ("主角在青雲宗山門前遇見守門弟子。", "守門弟子對主角抱持戒心。")
SECOND_ENTRIES = ("主角通過了宗門的入門試煉。",)
TURN_COST = 5                                                                    # 22 輪共 110 幣，不必簽到就夠用


def action(n: int) -> str:
    return f"記事簿流程的第 {n} 個獨特行動XYZ。"


def reply(*entries: str) -> str:
    return json.dumps({"entries": list(entries)}, ensure_ascii=False)


@pytest.fixture(params=["sqlite", pytest.param("sqlserver", marks=pytest.mark.sqlserver)])
def world(request):
    environ = {**BOTH, **AUTH_ENV}
    if request.param == "sqlserver":
        environ["DATABASE_URL"] = require_sqlserver_test_url()
    provider = FakeLLMProvider()
    app = create_app(load_config(environ={"APP_ENV": "testing", **environ}), rpg_llm_provider=provider, clock=FakeClock())
    engine = app.extensions["db_engine"]
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        RuntimeLimitRepository(s).set_value("coin.turn_cost.standard", TURN_COST, FakeClock()())
        user = add_user(s, 1, password=None)
        user_id, token_version = user.id, user.token_version
        s.commit()
    client = app.test_client()
    sign_in_as(client, user_id, token_version)
    save_id = make_save(app, user_id)
    yield Play(app, provider, client, user_id, save_id)
    Base.metadata.drop_all(engine)
    engine.dispose()


class Play:
    def __init__(self, app, provider, client, user_id, save_id) -> None:
        self.app, self.provider, self.client, self.user_id, self.save_id = app, provider, client, user_id, save_id

    def post(self, n: int, **options):
        return self.client.post(
            f"/api/game/sessions/{self.save_id}/turns/stream", json={"client_msg_id": f"m-{n}", "action": action(n)}, **options,
        )

    def turn(self, n: int, *capture_steps: Step) -> list[tuple[str, dict]]:
        """排好腳本（串流、狀態呼叫，加上抓取呼叫）後送出第 n 輪；回應關閉時抓取已完成。回傳解析後的事件。"""
        self.provider.script_stream(*story_script()).script(Step(STATE_OK), *capture_steps)
        return parse(self.post(n))

    # ---- 觀察（每次都讀最新的資料）----------------------------------------------------------------

    def save(self) -> tuple[str, int, int]:
        """（記事簿文字, 版本, 已處理到的輪次）。"""
        with Session(self.app.extensions["db_engine"]) as s:
            row = s.scalar(select(RpgSession).where(RpgSession.public_id == self.save_id))
            return row.notebook_text, row.notebook_version, row.notebook_upto_turn

    def ledger(self, kind: str | None = None) -> list[LlmCallLog]:
        with Session(self.app.extensions["db_engine"]) as s:
            statement = select(LlmCallLog).order_by(LlmCallLog.id)
            if kind is not None:
                statement = statement.where(LlmCallLog.kind == kind)
            return list(s.scalars(statement))

    def wallet_rows(self) -> int:
        with Session(self.app.extensions["db_engine"]) as s:
            return s.scalar(select(func.count()).select_from(WalletEntry))

    def prompt(self, index: int) -> str:
        return "".join(part for content in self.provider.calls[index].contents for part in content.parts)


def parse(response) -> list[tuple[str, dict]]:
    assert response.status_code == 200, response.get_data(as_text=True)
    events = []
    for block in response.get_data(as_text=True)[:-2].split("\n\n"):
        event, data = block.split("\n")
        events.append((event[len("event: "):], json.loads(data[len("data: "):])))
    return events


def names(events):
    return [name for name, _ in events]


# ---- 旅程 ---------------------------------------------------------------------------------------

def test_the_notebook_is_captured_after_the_eleventh_turn_used_by_the_next_and_redone_after_a_failure(world, log_lines):
    play = world
    provider = play.provider

    # ---- 第 1 至 10 輪：沒有抓取 -----------------------------------------------------------------
    for n in range(1, 11):
        events = play.turn(n)
        assert events[-1][1]["status"] == "complete" and events[-1][1]["turn_number"] == n
    assert play.save() == ("", 1, 0) and play.ledger(KIND_RPG_NOTEBOOK) == []         # 第 10 輪完成時還沒有待抓取的視窗
    assert provider.remaining == 0 and provider.remaining_streams == 0
    assert (len(play.ledger(KIND_RPG_STREAM)), len(play.ledger(KIND_RPG_STATE))) == (10, 10)
    entries_before = play.wallet_rows()

    # ---- 第 11 輪：串流結束之後抓取第 1 至 10 輪 -----------------------------------------------------
    calls_before = len(provider.calls)
    events = play.turn(11, Step(reply(*FIRST_ENTRIES)))
    assert names(events)[0] == "meta" and names(events)[-2:] == ["state", "done"]
    assert set(names(events)) <= {"meta", "token", "ping", "state", "done"}            # 沒有任何記事簿相關的事件
    text, version, upto = play.save()
    assert text == "\n".join(FIRST_ENTRIES) and (version, upto) == (2, 10)
    assert provider.remaining == 0
    capture_prompt = play.prompt(calls_before + 2)                                      # 這一輪的呼叫順序：串流、狀態、抓取
    assert all(action(n) in capture_prompt for n in range(1, 11)) and action(11) not in capture_prompt
    (captured,) = play.ledger(KIND_RPG_NOTEBOOK)
    assert captured.status == STATUS_OK and captured.model == RPG_MODEL and captured.user_id == play.user_id
    assert play.wallet_rows() == entries_before + 1                                     # 第 11 輪只多一筆預扣；抓取不碰錢包

    # ---- 第 12 輪：回合的上下文帶著記事簿，沒有新的抓取 ---------------------------------------------
    calls_before = len(provider.calls)
    play.turn(12)
    narration_prompt = play.prompt(calls_before)
    assert all(entry in narration_prompt for entry in FIRST_ENTRIES)
    assert play.save() == ("\n".join(FIRST_ENTRIES), 2, 10) and len(play.ledger(KIND_RPG_NOTEBOOK)) == 1

    # ---- 第 13 至 20 輪：同一個視窗已處理，不再抓取 ---------------------------------------------------
    for n in range(13, 21):
        play.turn(n)
    assert play.save() == ("\n".join(FIRST_ENTRIES), 2, 10) and len(play.ledger(KIND_RPG_NOTEBOOK)) == 1

    # ---- 第 21 輪：抓取失敗，標記不動 -----------------------------------------------------------------
    events = play.turn(21, Step(LLMTransientError(TRANSIENT_RATE_LIMITED)))             # 這個應用程式沒有設定備援模型：一次失敗就結束
    assert events[-1][1]["status"] == "complete"                                        # 回合不受抓取影響
    assert play.save() == ("\n".join(FIRST_ENTRIES), 2, 10) and provider.remaining == 0
    assert [row.status == STATUS_OK for row in play.ledger(KIND_RPG_NOTEBOOK)] == [True, False]          # 第 11 輪成功，第 21 輪失敗

    # ---- 第 22 輪：下一個回合補做同一個視窗（第 11 至 20 輪）-------------------------------------------
    calls_before = len(provider.calls)
    play.turn(22, Step(reply(*SECOND_ENTRIES)))
    text, version, upto = play.save()
    assert text == "\n".join((*FIRST_ENTRIES, *SECOND_ENTRIES)) and (version, upto) == (3, 20)
    redo_prompt = play.prompt(calls_before + 2)
    assert all(action(n) in redo_prompt for n in range(11, 21)) and action(10) not in redo_prompt and action(21) not in redo_prompt
    assert all(entry in redo_prompt for entry in FIRST_ENTRIES)                         # 現有的記事簿也在資料區塊裡，讓模型不重複

    # ---- 日誌：只有整數與列舉 ------------------------------------------------------------------------
    raw = log_lines.raw()
    for secret in (*FIRST_ENTRIES, *SECOND_ENTRIES, STORY, "清晨的海風", *(action(n) for n in range(1, 23))):
        assert secret not in raw, secret
    events = [line.get("event") for line in log_lines() if str(line.get("event", "")).startswith("rpg.notebook_")]
    assert events.count("rpg.notebook_captured") == 2 and events.count("rpg.notebook_failed") == 1


# ---- 串流先結束、關閉之後才抓取 -------------------------------------------------------------------------

def test_the_stream_ends_before_the_capture_and_the_capture_runs_when_the_response_closes(world):
    play = world
    for n in range(1, 11):
        play.turn(n)
    play.provider.script_stream(*story_script()).script(Step(STATE_OK), Step(reply(*FIRST_ENTRIES)))
    response = play.post(11, buffered=False)                                           # 不緩衝：連線保持開啟，直到呼叫端關閉
    events = parse(response)
    assert names(events)[-1] == "done" and play.save() == ("", 1, 0)                   # 串流已讀完（done 已送達），抓取還沒開始
    assert play.provider.remaining == 1                                                 # 抓取的腳本還在
    response.close()                                                                    # WSGI 伺服器在送完回應之後關閉
    assert play.save() == ("\n".join(FIRST_ENTRIES), 2, 10) and play.provider.remaining == 0


def test_a_client_that_disconnects_right_after_done_still_gets_the_capture(world):
    play = world
    for n in range(1, 11):
        play.turn(n)
    play.provider.script_stream(*story_script()).script(Step(STATE_OK), Step(reply(*FIRST_ENTRIES)))
    response = play.post(11, buffered=False)
    parse(response)                                                                    # done 已被取走
    response.close()                                                                   # 客戶端隨即斷線：關閉照樣呼叫回呼
    assert play.save()[1:] == (2, 10)


def test_a_capture_failure_never_changes_the_completed_turn_or_the_wallet(world):
    play = world
    for n in range(1, 11):
        play.turn(n)
    rows = play.wallet_rows()
    events = play.turn(11, Step("這不是 JSON"), Step("這也不是 JSON"))                  # 抓取的 JSON 驗證失敗（重試一次後仍失敗）
    assert events[-1][1] == {"status": "complete", "turn_number": 11, "coins": events[-1][1]["coins"]}
    assert play.save() == ("", 1, 0)                                                   # 驗證失敗不推進，由下個回合補做
    assert play.wallet_rows() == rows + 1
