"""rpg-turn-stream、rpg-notebook-capture 規格：`/api/game/sessions/<id>/turns/stream` 的記事簿抓取接線（HTTP 這一層）。

抓取以 `response.call_on_close` 在回應關閉之後執行：事件序列不變、關閉前不抓取、關閉後才抓取，且只有「`done` 已被取走的正常完成回合」才抓取。
專案的測試用戶端預設以 `buffered=True` 開啟請求（讀完本文就自動呼叫 `close()`，見 `tests/support/client.py`），看不出「關閉之前」與
「關閉之後」的差別；這裡一律以 `buffered=False` 送出，再以 `response.close()` 模擬 WSGI 伺服器的關閉。`NotebookCapturer.run` 換成記錄呼叫的替身
（抓取本身的行為由 `test_rpg_notebook_capture` 涵蓋，端到端由 `test_rpg_notebook_flow` 涵蓋）。
"""
import logging

import pytest

from app.services.rpg_notebook_capture import OUTCOME_NOTHING, CaptureResult, NotebookCapturer
from tests.support.game import BAD_SENTENCE, story_script
from tests.support.llm import Step
from tests.test_game_api import STATE_OK, app, make_save, names, parse, player, provider  # noqa: F401 — 固定組件（app 與 provider）

STATE_BROKEN = "這不是 JSON"


@pytest.fixture()
def runs(monkeypatch):
    """記錄 `NotebookCapturer.run` 收到的對象，並回傳 `nothing`（不真的抓取）。"""
    seen = []

    def spy(self, target):
        seen.append(target)
        return CaptureResult(OUTCOME_NOTHING)

    monkeypatch.setattr(NotebookCapturer, "run", spy)
    return seen


def turn(client, save_id, *, msg="m-1"):
    """不緩衝：回應在呼叫端 `close()` 之前保持開啟（像串流中的連線）。"""
    return client.post(
        f"/api/game/sessions/{save_id}/turns/stream", json={"client_msg_id": msg, "action": "我走進山門。"}, buffered=False,
    )


def opening(client, save_id):
    return client.post(f"/api/game/sessions/{save_id}/opening/stream", json={}, buffered=False)


def play(client, provider, save_id, *, msg="m-1", state=STATE_OK):
    provider.script_stream(*story_script()).script(Step(state))
    return turn(client, save_id, msg=msg)


# ---- 時機 ---------------------------------------------------------------------------------------

def test_the_capture_runs_only_after_the_response_is_closed(app, provider, runs):
    client, uid = player(app)
    save_id = make_save(app, uid)
    response = play(client, provider, save_id)
    events = parse(response)
    assert names(events)[-2:] == ["state", "done"] and runs == []               # 串流已完整讀完，但回應還沒有關閉
    response.close()
    assert len(runs) == 1


def test_the_target_identifies_the_player_the_save_and_the_effective_mode(app, provider, runs):
    client, uid = player(app, member_tier="plus")
    save_id = make_save(app, uid)
    response = play(client, provider, save_id)
    parse(response)
    response.close()
    (target,) = runs
    assert (target.user_id, target.session_public_id, target.member_tier, target.content_tier) == (uid, save_id, "plus", "safe")
    assert all(isinstance(value, (int, str)) for value in vars(target).values())


def test_the_events_are_the_same_as_without_a_capture(app, provider, runs):
    client, uid = player(app)
    save_id = make_save(app, uid)
    response = play(client, provider, save_id)
    events = parse(response)
    response.close()
    assert set(names(events)) <= {"meta", "token", "ping", "state", "done"} and names(events)[-1] == "done"
    assert response.headers["Content-Type"].startswith("text/event-stream")


# ---- 不抓取 -------------------------------------------------------------------------------------

def test_a_blocked_turn_is_not_captured(app, provider, runs):
    client, uid = player(app)
    save_id = make_save(app, uid)
    provider.script_stream(*story_script(BAD_SENTENCE)).script_stream(*story_script(BAD_SENTENCE))
    response = turn(client, save_id)
    assert parse(response)[-1][1]["status"] == "blocked"
    response.close()
    assert runs == []


def test_a_failed_generation_is_not_captured(app, provider, runs):
    from app.llm.types import LLMTransientError
    from tests.support.llm import Chunk, Fail

    client, uid = player(app)
    save_id = make_save(app, uid)
    provider.script_stream(Chunk("清晨的海風吹過山門。"), Fail(LLMTransientError("network")))
    response = turn(client, save_id)
    assert parse(response)[-1][0] == "error"
    response.close()
    assert runs == []


def test_a_client_that_leaves_before_done_does_not_trigger_the_capture(app, provider, runs):
    client, uid = player(app)
    save_id = make_save(app, uid)
    provider.script_stream(*story_script()).script(Step(STATE_OK))
    response = turn(client, save_id)                                            # 不讀本文：連線在第一個事件之後就被關閉
    response.close()
    assert runs == []


def test_a_resent_completed_turn_is_not_captured_again(app, provider, runs):
    client, uid = player(app)
    save_id = make_save(app, uid)
    first = play(client, provider, save_id)
    parse(first)
    first.close()
    again = turn(client, save_id)                                               # 同一個 client_msg_id：重送，不呼叫模型
    assert parse(again)[0][1]["replayed"] is True
    again.close()
    assert len(runs) == 1


def test_the_opening_stream_never_triggers_a_capture(app, provider, runs):
    client, uid = player(app)
    save_id = make_save(app, uid, opening="pending")
    provider.script_stream(*story_script("晨霧之中，山門緩緩開啟。"))
    response = opening(client, save_id)
    assert parse(response)[-1][0] == "done"
    response.close()
    assert runs == []


# ---- 不拋出 -------------------------------------------------------------------------------------

def test_a_capture_that_raises_never_breaks_the_close_and_logs_only_enumerations(app, provider, monkeypatch, caplog):
    def boom(self, target):
        raise RuntimeError("劇情與行動文字不該進日誌")

    monkeypatch.setattr(NotebookCapturer, "run", boom)
    client, uid = player(app)
    save_id = make_save(app, uid)
    response = play(client, provider, save_id)
    parse(response)
    with caplog.at_level(logging.DEBUG):
        response.close()                                                        # 不得拋出
    failed = [r for r in caplog.records if getattr(r, "event", "") == "rpg.notebook_failed"]
    assert len(failed) == 1 and failed[0].reason == "unavailable"
    assert "劇情與行動文字不該進日誌" not in caplog.text and "我走進山門" not in caplog.text


def test_a_failed_state_call_does_not_change_the_capture_decision(app, provider, runs):
    client, uid = player(app)
    save_id = make_save(app, uid)
    provider.script_stream(*story_script()).script(Step(STATE_BROKEN), Step(STATE_BROKEN))
    response = turn(client, save_id)
    events = parse(response)
    assert [data for name, data in events if name == "state"][0]["updated"] is False and events[-1][1]["status"] == "complete"
    response.close()
    assert len(runs) == 1
