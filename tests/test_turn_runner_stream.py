"""rpg-turn-stream、rpg-turn-engine、rpg-output-review 規格：`TurnRunner` 的串流階段與每個出口。

以假的串流供應商（`tests/support/llm.py`）與假單調時鐘驅動，不連網、不真的等待；資料庫是真的（SQLite 與 SQL Server 各跑一次）。
每個出口都斷言：回合狀態、遊戲幣（預扣結算或退回）、帳本與輪數。
"""
import json

import pytest

from app.llm.types import LLMBlockedError, LLMConfigError, LLMTransientError
from app.models.llm_call_log import KIND_RPG_STATE, KIND_RPG_STREAM
from app.models.rpg_session import TURN_BLOCKED, TURN_COMPLETE, TURN_INTERRUPTED
from app.services.game_service import hold_key
from tests.support.game import BAD_SENTENCE, FALLBACK_MODEL, MODEL, STORY, STORY_SENTENCES, RunnerKit, story_script
from tests.support.llm import Chunk, Fail, Finish, Idle, Step

STATE_OK = json.dumps({"elapsed_months": 1, "current_location": "山門前"}, ensure_ascii=False)
BROKEN = "這不是 JSON"


@pytest.fixture()
def kit(member_session):
    return RunnerKit(member_session)


def setup(kit, **overrides):
    user = kit.player()
    save = kit.open_save(user, **overrides)
    plan = kit.start(user, save)
    return user, save, plan


def names(events):
    return [event.name for event in events]


def of(events, name):
    return [event.data for event in events if event.name == name]


def text_of(events):
    return "".join(data["text"] for data in of(events, "token"))


# ---- 成功 -------------------------------------------------------------------------------------

def test_a_normal_turn_streams_reviews_updates_state_and_settles(kit):
    user, save, plan = setup(kit)
    kit.provider.script_stream(*story_script()).script(Step(STATE_OK))
    events = kit.run(plan, user)
    order = names(events)
    assert order[0] == "meta" and order[-2:] == ["state", "done"] and order[-3] == "ping"
    assert set(order) <= {"meta", "token", "ping", "state", "done"} and order.count("token") >= 1
    assert text_of(events) == STORY                                         # 審查緩衝釋放後，文字完整
    assert of(events, "meta")[0] == {"turn_id": 1, "client_msg_id": "m-1", "attempt": 1, "replayed": False}
    assert of(events, "done")[0] == {"status": "complete", "turn_number": 1, "coins": 170}
    assert of(events, "state")[0]["updated"] is True and of(events, "state")[0]["location"] == "山門前"
    (turn,) = kit.turns(save)
    assert (turn.turn_status, turn.turn_number, turn.llm_raw_response) == (TURN_COMPLETE, 1, STORY)
    assert kit.total(user) == 170
    entry = kit.wallets.entry(user.id, hold_key(save.public_id, "m-1", 1))
    assert entry.resolution == "settled"
    assert [(row.kind, row.status, row.model) for row in kit.ledger()] == [(KIND_RPG_STREAM, "ok", MODEL), (KIND_RPG_STATE, "ok", MODEL)]      # 串流帳本先於狀態呼叫寫入
    assert kit.provider.handles[0].closed
    saved = kit.fresh(save)
    assert (saved.version, saved.elapsed_months, saved.current_location) == (2, 1, "山門前")


def test_the_stream_request_uses_the_frozen_plan(kit):
    user, save, plan = setup(kit)
    kit.provider.script_stream(*story_script()).script(Step(STATE_OK))
    kit.run(plan, user)
    request = kit.provider.calls[0]
    assert (request.model, request.system_instruction, request.contents, request.max_output_tokens) == (
        plan.model, plan.system_instruction, plan.contents, 1800,
    )
    assert request.response_schema is None


def test_a_ping_is_sent_when_the_upstream_is_silent_for_fifteen_seconds(kit):
    user, save, plan = setup(kit)
    kit.provider.script_stream(Idle(seconds=6), Idle(seconds=6), Idle(seconds=6), *story_script()).script(Step(STATE_OK))
    events = kit.run(plan, user)
    order = names(events)
    assert order[:2] == ["meta", "ping"]                                    # 18 秒沉默：第 15 秒之後的第一個 tick 送出 ping
    assert text_of(events) == STORY


def test_a_ping_precedes_the_state_call(kit):
    user, save, plan = setup(kit)
    kit.provider.script_stream(*story_script()).script(Step(STATE_OK))
    events = kit.run(plan, user)
    assert names(events)[-3:] == ["ping", "state", "done"]


def test_no_database_transaction_is_open_while_the_model_streams_or_the_state_call_runs(kit):
    user, save, plan = setup(kit)
    opened = []
    real_factory = kit.factory

    def tracking():
        session = real_factory()
        opened.append(session)
        return session

    kit.factory = tracking
    seen = []

    def hook(_request):
        seen.append([session.in_transaction() for session in (kit.session, *opened)])

    kit.provider.hooks.append(hook)
    kit.provider.script_stream(*story_script()).script(Step(STATE_OK))
    kit.run(plan, user)
    assert len(seen) == 2                                                   # 串流開始、狀態呼叫開始各一次
    assert not any(any(flags) for flags in seen)


def test_a_recently_unhealthy_primary_model_is_skipped_straight_to_the_fallback(kit):
    from app.repositories.llm_call_log_repository import LlmCallLogRepository

    user, save, plan = setup(kit)
    LlmCallLogRepository(kit.session).add(
        user_id=None, conversation_id=None, kind=KIND_RPG_STREAM, provider="fake", model=MODEL, attempt=1, status="timeout", counted=False,
        latency_ms=20000, created_at=kit.clock(),
    )
    kit.session.commit()
    kit.provider.script_stream(*story_script()).script(Step(STATE_OK))
    kit.run(plan, user)
    assert kit.provider.calls[0].model == FALLBACK_MODEL                    # 斷路由帳本判斷，不放行程內狀態


# ---- 模型失敗與逾時 ---------------------------------------------------------------------------

def test_when_both_models_fail_before_any_text_the_turn_is_interrupted_and_refunded(kit):
    user, save, plan = setup(kit)
    kit.provider.script_stream(Fail(LLMTransientError("server_error", http_status=503))).script_stream(Fail(LLMTransientError("server_error", http_status=503)))
    events = kit.run(plan, user)
    assert names(events) == ["meta", "error"] and events[-1].data["code"] == "generation_failed"
    assert "遊戲幣" in events[-1].data["message"]
    (turn,) = kit.turns(save)
    assert (turn.turn_status, turn.turn_number, turn.llm_raw_response) == (TURN_INTERRUPTED, None, "")
    assert kit.total(user) == 200
    assert kit.wallets.entry(user.id, hold_key(save.public_id, "m-1", 1)).resolution == "refunded"
    assert [(row.model, row.status) for row in kit.ledger(KIND_RPG_STREAM)] == [(MODEL, "server_error"), (FALLBACK_MODEL, "server_error")]


def test_the_fallback_model_takes_over_when_the_primary_fails_before_any_text(kit):
    user, save, plan = setup(kit)
    kit.provider.script_stream(Fail(LLMTransientError("rate_limited", http_status=429))).script_stream(*story_script()).script(Step(STATE_OK))
    events = kit.run(plan, user)
    assert names(events)[-1] == "done" and text_of(events) == STORY
    assert [call.model for call in kit.provider.calls[:2]] == [MODEL, FALLBACK_MODEL]
    assert kit.total(user) == 170


def test_a_first_chunk_timeout_on_both_models_ends_with_a_timeout_error(kit):
    user, save, plan = setup(kit)
    kit.provider.script_stream(Idle(seconds=21)).script_stream(Idle(seconds=21))
    events = kit.run(plan, user)
    assert events[-1].name == "error" and events[-1].data["code"] == "generation_timeout"
    assert kit.total(user) == 200 and kit.turns(save)[0].turn_status == TURN_INTERRUPTED


def test_a_failure_in_the_middle_of_the_text_keeps_the_partial_and_refunds(kit):
    user, save, plan = setup(kit)
    kit.provider.script_stream(Chunk(STORY_SENTENCES[0]), Chunk(STORY_SENTENCES[1]), Fail(LLMTransientError("network")))
    events = kit.run(plan, user)
    assert events[-1].name == "error" and "done" not in names(events)
    (turn,) = kit.turns(save)
    assert turn.turn_status == TURN_INTERRUPTED and turn.llm_raw_response == "".join(STORY_SENTENCES[:2])
    assert kit.total(user) == 200
    assert [row.model for row in kit.ledger(KIND_RPG_STREAM)] == [MODEL]    # 已送出文字之後不切換備援


@pytest.mark.parametrize("error", [LLMConfigError(http_status=401), LLMBlockedError("output")])
def test_provider_config_errors_and_provider_blocks_end_with_an_error_and_a_refund(kit, error):
    user, save, plan = setup(kit)
    kit.provider.script_stream(Fail(error))
    events = kit.run(plan, user)
    assert events[-1].name == "error" and kit.total(user) == 200
    assert kit.turns(save)[0].turn_status == TURN_INTERRUPTED
    assert len(kit.provider.handles) == 1                                   # 不切換備援


# ---- 輸出審查：被擋與自動重生 -------------------------------------------------------------------

def test_a_blocked_first_attempt_regenerates_on_the_same_hold(kit):
    user, save, plan = setup(kit)
    kit.provider.script_stream(*story_script("山門前很安靜。", BAD_SENTENCE, "後面的文字。")).script_stream(*story_script()).script(Step(STATE_OK))
    events = kit.run(plan, user)
    order = names(events)
    assert "transition" in order and order.index("transition") < order.index("state")
    transition = of(events, "transition")[0]
    assert transition["will_retry"] is True and transition["text"] and "少女" not in transition["text"]
    assert "少女" not in "".join(event.encode() for event in events)        # 被擋的文字不會送到前端
    assert events[-1].data == {"status": "complete", "turn_number": 1, "coins": 170}
    assert text_of(events).endswith(STORY)                                  # 轉場之後的新回應完整
    assert len(kit.provider.handles) == 2 and all(handle.closed for handle in kit.provider.handles)
    (turn,) = kit.turns(save)
    assert (turn.turn_status, turn.turn_number, turn.llm_raw_response) == (TURN_COMPLETE, 1, STORY)
    assert kit.total(user) == 170                                           # 只有一筆預扣
    holds = [e for e in kit.wallets.export_wallet(user.id)[1] if e.kind == "turn_hold"]
    assert len(holds) == 1
    statuses = [(row.kind, row.status) for row in kit.ledger()]
    assert statuses[0] == (KIND_RPG_STREAM, "blocked") and statuses[1] == (KIND_RPG_STREAM, "ok")
    assert "output_blocked minor_sexual" in kit.ledger()[0].error_detail


def test_a_second_block_ends_the_turn_as_blocked_and_refunds(kit):
    user, save, plan = setup(kit)
    kit.provider.script_stream(*story_script(BAD_SENTENCE)).script_stream(*story_script("路上。", BAD_SENTENCE))
    events = kit.run(plan, user)
    assert names(events).count("transition") == 2
    first, second = of(events, "transition")
    assert (first["will_retry"], second["will_retry"]) == (True, False) and first["text"] != second["text"]
    assert events[-1].name == "done" and events[-1].data == {"status": "blocked", "turn_number": None, "coins": 200}
    assert "state" not in names(events) and "少女" not in "".join(event.encode() for event in events)
    (turn,) = kit.turns(save)
    assert (turn.turn_status, turn.turn_number) == (TURN_BLOCKED, None)
    assert turn.structured_result == {"blocked": {"categories": ["minor_sexual"], "auto_retry": True}}
    assert "少女" in turn.llm_raw_response                                  # 被擋時收到的文字暫存（14 天後清除），不進上下文
    assert kit.total(user) == 200 and kit.wallets.entry(user.id, hold_key(save.public_id, "m-1", 1)).resolution == "refunded"
    assert kit.provider.remaining_streams == 0 and kit.provider.remaining == 0     # 沒有呼叫狀態


def test_a_blocked_turn_can_be_resent_with_a_new_attempt_and_a_new_hold(kit):
    user, save, plan = setup(kit)
    kit.provider.script_stream(*story_script(BAD_SENTENCE)).script_stream(*story_script(BAD_SENTENCE))
    kit.run(plan, user)
    again = kit.start(user, save)
    assert (again.attempt, again.hold_key) == (2, hold_key(save.public_id, "m-1", 2))
    kit.provider.script_stream(*story_script()).script(Step(STATE_OK))
    events = kit.run(again, user)
    assert events[-1].data["status"] == "complete" and kit.total(user) == 170


def test_a_leaked_marker_is_blocked_like_any_other_violation(kit):
    user, save, plan = setup(kit)
    from tests.support.game import MARKER

    kit.provider.script_stream(*story_script(f"旁白說：{MARKER}。")).script_stream(*story_script()).script(Step(STATE_OK))
    events = kit.run(plan, user)
    assert MARKER not in "".join(event.encode() for event in events)
    assert of(events, "transition")[0]["will_retry"] is True


# ---- 斷線 -------------------------------------------------------------------------------------

def test_a_disconnect_in_the_middle_of_the_text_interrupts_refunds_and_closes_upstream(kit):
    user, save, plan = setup(kit)
    kit.provider.script_stream(*story_script())
    generator = kit.runner(plan, user).events()
    seen = []
    while not seen or seen[-1].name != "token":
        seen.append(next(generator))
    generator.close()                                                       # 客戶端斷線：WSGI 伺服器關閉產生器
    (turn,) = kit.turns(save)
    assert turn.turn_status == TURN_INTERRUPTED and turn.turn_number is None and turn.llm_raw_response
    assert turn.llm_raw_response.startswith(STORY_SENTENCES[0])
    assert kit.total(user) == 200 and kit.wallets.entry(user.id, hold_key(save.public_id, "m-1", 1)).resolution == "refunded"
    assert kit.provider.handles[0].closed
    (row,) = kit.ledger(KIND_RPG_STREAM)
    assert row.error_detail == "disconnected" and row.finish_reason == "other" and row.completion_tokens > 0     # 以字數估算用量


def test_a_disconnect_right_after_the_first_event_still_closes_cleanly(kit):
    user, save, plan = setup(kit)
    kit.provider.script_stream(*story_script())
    generator = kit.runner(plan, user).events()
    assert next(generator).name == "meta"
    generator.close()
    assert kit.turns(save)[0].turn_status == TURN_INTERRUPTED and kit.total(user) == 200
    assert kit.provider.handles == [] or kit.provider.handles[0].closed


def test_a_disconnect_after_the_text_is_complete_still_finishes_and_settles(kit):
    user, save, plan = setup(kit)
    kit.provider.script_stream(*story_script()).script(Step(STATE_OK))
    generator = kit.runner(plan, user).events()
    events = []
    while not events or events[-1].name != "ping":
        events.append(next(generator))
    generator.close()                                                       # 文字已完整、正要開始狀態呼叫時斷線
    (turn,) = kit.turns(save)
    assert (turn.turn_status, turn.turn_number, turn.llm_raw_response) == (TURN_COMPLETE, 1, STORY)
    assert kit.total(user) == 170 and kit.wallets.entry(user.id, hold_key(save.public_id, "m-1", 1)).resolution == "settled"
    assert kit.fresh(save).current_location == "山門前"                      # 狀態呼叫也完成了
    replay = kit.game.begin_turn(user, save.public_id, client_msg_id="m-1", action="我走進山門。")
    assert replay.replay.story == STORY and kit.total(user) == 170          # 重送相同編號得到既有結果


# ---- 收尾的其他情形 -----------------------------------------------------------------------------

def test_a_turn_taken_over_by_a_newer_attempt_ends_with_a_superseded_error_and_no_settle(kit):
    user, save, plan = setup(kit)
    kit.sessions.interrupt_turn(user.id, save.public_id, "m-1", partial_response="", attempt=1)
    kit.wallet.refund(user, plan.hold_key)
    newer = kit.start(user, save)                                           # 新的嘗試接手同一筆
    assert newer.attempt == 2
    kit.provider.script_stream(*story_script()).script(Step(STATE_OK))
    events = kit.run(plan, user)                                            # 過時的產生器（嘗試 1）
    assert events[-1].name == "error" and events[-1].data["code"] == "turn_superseded"
    (turn,) = kit.turns(save)
    assert (turn.turn_status, turn.attempt) == ("streaming", 2)             # 新的嘗試不受影響
    assert kit.total(user) == 170


def test_an_unexpected_error_inside_the_generator_interrupts_and_refunds(kit, monkeypatch):
    user, save, plan = setup(kit)
    kit.provider.script_stream(*story_script())
    runner = kit.runner(plan, user)

    def boom(_text):
        raise RuntimeError("意外")

    monkeypatch.setattr("app.services.rpg_runner_base.OutputGuard.feed", lambda self, text: boom(text))
    events = list(runner.events())
    assert events[-1].name == "error" and events[-1].data["code"] == "internal_error"
    assert "意外" not in events[-1].encode()
    assert kit.turns(save)[0].turn_status == TURN_INTERRUPTED and kit.total(user) == 200
    assert kit.provider.handles[0].closed


def test_events_are_single_line_json_blocks(kit):
    user, save, plan = setup(kit)
    kit.provider.script_stream(*story_script("第一行\n第二行。", "第三句。")).script(Step(STATE_OK))
    for event in kit.run(plan, user):
        lines = event.encode().split("\n")
        assert lines[0] == f"event: {event.name}" and lines[1].startswith("data: ") and lines[2:] == ["", ""]
        assert json.loads(lines[1][6:]) == event.data


def test_the_state_call_is_skipped_into_a_non_updated_state_when_it_is_not_configured(kit):
    user, save, plan = setup(kit)
    kit.state_available = False
    kit.provider.script_stream(*story_script())
    events = kit.run(plan, user)
    assert of(events, "state")[0] == {"updated": False, "notices": []}
    assert events[-1].data["status"] == "complete" and kit.total(user) == 170
