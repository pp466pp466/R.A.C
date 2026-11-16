"""rpg-turn-revision 規格「串流後」的每個情境：`RevisionRunner` 的串流、逐句審查、狀態階段、原子替換與每個失敗出口。

以假的串流供應商（`tests/support/llm.py`）與假單調時鐘驅動，不連網、不真的等待；資料庫是真的（SQLite 與 SQL Server 各跑一次）。
每個出口都斷言：回合內容與 `revision`、世界狀態、認領是否釋放、遊戲幣（預扣結算或退回）、回饋列的結果與事件序列。
"""
import json
import logging

import pytest

from app.llm.types import LLMTransientError
from app.models.llm_call_log import KIND_RPG_STATE, KIND_RPG_STREAM
from app.models.rpg_session import (
    REGEN_APPLIED, REGEN_BLOCKED, REGEN_FAILED, REGEN_KIND_EDIT, REGEN_KIND_REGENERATE, SESSION_ACTIVE, SESSION_ENDED, TURN_COMPLETE,
)
from app.services.errors import NotRevisable, SessionEnded
from app.services.game_service import hold_key
from tests.support.game import BAD_SENTENCE, MARKER, RunnerKit, story_script
from tests.support.llm import Fail, Idle, Step
from tests.support.script_data import validated

OLD_STORY = ("清晨的海風吹過山門。", "守門的弟子向你點了點頭。", "你踏進了霧氣之中。")
NEW_STORY = ("夜裡的雨敲著屋簷。", "老僧為你點亮一盞油燈。", "你在燈下翻開了舊書。")
OLD = "".join(OLD_STORY)
NEW = "".join(NEW_STORY)
ACTION = "我走進山門。"
NEW_ACTION = "我繞到後山，沿著小徑往上走。"
REASON = "劇情和設定對不上，想換個寫法"


def state(**fields):
    return json.dumps(fields, ensure_ascii=False)


STATE_OLD = state(elapsed_months=3, current_location="古劍山莊", status_summary="你抵達了山莊。", goals_add=[{"text": "拜見莊主"}])
STATE_NEW = state(elapsed_months=1, status_summary="你在寺中避雨。")


@pytest.fixture()
def kit(member_session):
    return RunnerKit(member_session)


# ---- 輔助 -------------------------------------------------------------------------------------

def names(events):
    return [event.name for event in events]


def of(events, name):
    return [event.data for event in events if event.name == name]


def text_of(events):
    return "".join(data["text"] for data in of(events, "token"))


def first_turn(kit, *, content=None, free_limit=None, **overrides):
    """一個玩家、一個存檔、已完成並有快照的第 1 輪（劇情 `OLD`，世界狀態有地點與目標的變化）。回傳（玩家, 存檔）。"""
    if free_limit is not None:
        kit.set_limit("rpg.free_regenerations", free_limit)
    user = kit.player()
    save = kit.open_save(user, content=content, **overrides)
    plan = kit.start(user, save, action=ACTION)
    kit.provider.script_stream(*story_script(*OLD_STORY)).script(Step(STATE_OLD))
    events = kit.run(plan, user)
    assert events[-1].data["status"] == "complete"
    return user, save


def revise(kit, user, save, *replies, sentences=NEW_STORY, **kwargs):
    """修訂的開始階段加一次成功串流的腳本（`replies` 是狀態呼叫的回覆）；回傳（計畫, 事件）。"""
    plan = kit.revise(user, save, **kwargs)
    kit.provider.script_stream(*story_script(*sentences)).script(*replies)
    return plan, kit.run_revision(plan, user)


def turn_row(kit, save, msg="m-1"):
    (turn,) = [turn for turn in kit.turns(save) if turn.client_msg_id == msg]
    return turn


def holds(kit, user):
    return [entry for entry in kit.wallets.export_wallet(user.id)[1] if entry.kind == "turn_hold"]


def assert_old_content_kept(kit, user, save, *, total, revision=1, regen_count=0):
    """舊內容原樣保留：內容、輪數、`revision`、已用免費次數都沒變，認領已釋放，世界狀態仍是第 1 輪的結果。"""
    turn = turn_row(kit, save)
    assert (turn.turn_status, turn.turn_number, turn.seq, turn.llm_raw_response, turn.user_action) == (TURN_COMPLETE, 1, 1, OLD, ACTION)
    assert (turn.revision, turn.regen_count) == (revision, regen_count) and turn.revising_started_at is None
    assert kit.total(user) == total


# ---- 成功 -------------------------------------------------------------------------------------

def test_a_successful_regeneration_streams_replaces_in_place_and_keeps_the_number_and_seq(kit):
    user, save = first_turn(kit)
    plan, events = revise(kit, user, save, Step(STATE_NEW))
    order = names(events)
    assert order[0] == "meta" and order[-3:] == ["ping", "state", "done"]
    assert set(order) <= {"meta", "token", "ping", "state", "done"} and order.count("token") >= 1
    assert text_of(events) == NEW
    assert of(events, "meta")[0] == {
        "kind": "regenerate", "turn_id": 1, "client_msg_id": "m-1", "attempt": 2, "revision": 1, "replayed": False,
    }
    assert of(events, "done")[0] == {"status": "complete", "turn_number": 1, "coins": 170, "revision": 2, "free_left": 1}
    turn = turn_row(kit, save)
    assert (turn.turn_status, turn.turn_number, turn.seq, turn.llm_raw_response, turn.user_action) == (TURN_COMPLETE, 1, 1, NEW, ACTION)
    assert (turn.revision, turn.regen_count, turn.attempt) == (2, 1, 2) and turn.revising_started_at is None
    assert turn.structured_result == {"state": {"ok": True, "dropped": 0}}
    assert kit.sessions.count_complete_turns(user.id, save.public_id) == 1                    # 已完成輪數不變
    assert len(holds(kit, user)) == 1 and kit.total(user) == 170                               # 免費：沒有新的預扣
    (feedback,) = kit.feedback(save)
    assert (feedback.kind, feedback.result, feedback.is_free, feedback.turn_seq, feedback.attempt) == (REGEN_KIND_REGENERATE, REGEN_APPLIED, True, 1, 2)
    assert feedback.reason == REASON and feedback.category == "other"
    assert kit.provider.handles[-1].closed


def test_the_regenerated_turn_replays_with_the_new_text_and_revision(kit):
    user, save = first_turn(kit)
    revise(kit, user, save, Step(STATE_NEW))
    replay = kit.game.begin_turn(user, save.public_id, client_msg_id="m-1", action=ACTION).replay
    assert (replay.story, replay.revision, replay.free_left, replay.turn_number) == (NEW, 2, 1, 1)


def test_the_regeneration_restores_the_world_before_the_turn_then_applies_only_the_new_response(kit):
    user, save = first_turn(kit)
    saved = kit.fresh(save)
    assert (saved.current_location, saved.elapsed_months, saved.version) == ("古劍山莊", 3, 2)           # 第 1 輪造成的變化
    assert [goal["text"] for goal in saved.goals_json["active"]] == ["拜見莊主"]
    plan, events = revise(kit, user, save, Step(STATE_NEW))
    payload = of(events, "state")[0]
    assert payload["updated"] is True and payload["elapsed_months"] == 1 and payload["summary"] == "你在寺中避雨。"
    assert payload["goals"] == [] and payload["location"] == ""                                        # 新回應沒有改地點：回到第 1 輪開始之前（剛建立的存檔沒有地點）
    saved = kit.fresh(save)
    assert saved.current_location == ""                                                               # 原本那輪造成的地點被覆蓋
    assert saved.elapsed_months == 1 and saved.status_summary == "你在寺中避雨。"                    # 只有新回應的變化
    assert saved.goals_json["active"] == []                                                          # 原本新增的目標被覆蓋
    assert saved.version == 3                                                                        # 寫回時版本加 1


def test_the_stream_request_uses_the_frozen_plan_and_the_state_call_uses_the_target_turns_number(kit):
    user, save = first_turn(kit)
    plan, _events = revise(kit, user, save, Step(STATE_NEW))
    request = kit.provider.calls[-2]                                                                   # 倒數第二個呼叫是修訂的串流
    assert (request.model, request.system_instruction, request.contents, request.max_output_tokens) == (
        plan.model, plan.system_instruction, plan.contents, 1800,
    )
    assert request.response_schema is None and plan.turn_number == 1 and plan.history == ()
    assert ACTION in "".join(part for content in request.contents for part in content.parts)       # 重新生成沿用原行動
    state_call = kit.provider.calls[-1]
    assert state_call.response_schema is not None and state_call.model == plan.model


def test_the_reason_and_the_category_never_reach_any_model_call_or_the_turn_row(kit):
    user, save = first_turn(kit)
    reason = "獨特的理由只該在回饋表"
    plan, _events = revise(kit, user, save, Step(STATE_NEW), reason=reason, category="repetitive_or_dull")
    blob = "".join(
        (call.system_instruction or "") + "".join(part for content in call.contents for part in content.parts) for call in kit.provider.calls
    )
    assert reason not in blob and "repetitive_or_dull" not in blob
    turn = turn_row(kit, save)
    assert reason not in json.dumps(turn.structured_result, ensure_ascii=False) and reason not in turn.llm_raw_response
    assert not hasattr(plan, "reason") and not hasattr(plan, "category")
    (feedback,) = kit.feedback(save)
    assert feedback.reason == reason and feedback.category == "repetitive_or_dull"                          # 只存在回饋表


def test_a_paid_regeneration_settles_its_hold_and_uses_the_new_attempts_key(kit):
    user, save = first_turn(kit)
    revise(kit, user, save, Step(STATE_NEW))
    revise(kit, user, save, Step(STATE_NEW), revision=2)
    assert kit.total(user) == 170 and len(holds(kit, user)) == 1                                     # 前兩次免費
    plan, events = revise(kit, user, save, Step(STATE_NEW), revision=3)
    assert plan.is_free is False and plan.attempt == 4 and plan.hold_key == hold_key(save.public_id, "m-1", 4)
    assert plan.hold_key != hold_key(save.public_id, "m-1", 1)                                       # 與原回合已結算的鍵不同
    assert of(events, "done")[0] == {"status": "complete", "turn_number": 1, "coins": 140, "revision": 4, "free_left": 0}
    assert kit.wallets.entry(user.id, plan.hold_key).resolution == "settled" and kit.total(user) == 140
    assert turn_row(kit, save).regen_count == 3 and len(holds(kit, user)) == 2


def test_a_free_edit_replaces_the_action_holds_nothing_and_uses_up_one_shared_free_revision(kit):
    user, save = first_turn(kit)
    plan, events = revise(kit, user, save, Step(STATE_NEW), kind="edit")
    assert plan.kind == "edit" and plan.is_free is True and plan.hold_key is None and plan.action == NEW_ACTION
    assert of(events, "meta")[0]["kind"] == "edit"
    assert of(events, "done")[0] == {"status": "complete", "turn_number": 1, "coins": 170, "revision": 2, "free_left": 1}
    turn = turn_row(kit, save)
    assert (turn.user_action, turn.llm_raw_response, turn.regen_count, turn.revision, turn.turn_number) == (NEW_ACTION, NEW, 1, 2, 1)
    assert len(holds(kit, user)) == 1 and kit.total(user) == 170                                       # 免費的編輯：沒有新的預扣、餘額不變
    *_older, last = kit.feedback(save)
    assert (last.kind, last.result, last.is_free, last.category, last.reason) == (REGEN_KIND_EDIT, REGEN_APPLIED, True, None, None)
    state_call = kit.provider.calls[-1]
    assert NEW_ACTION in "".join(part for content in state_call.contents for part in content.parts)  # 狀態呼叫看到的是新行動
    again = kit.revise(user, save, revision=2)
    assert again.is_free is True and again.regen_count == 1                                           # 還剩一次免費（編輯與重新生成共用）


def test_a_paid_edit_after_the_free_ones_settles_its_hold_and_the_count_keeps_growing(kit):
    user, save = first_turn(kit)
    revise(kit, user, save, Step(STATE_NEW))
    revise(kit, user, save, Step(STATE_NEW), revision=2, kind="edit")                                  # 兩種操作共用前兩次免費
    assert turn_row(kit, save).regen_count == 2 and kit.total(user) == 170 and len(holds(kit, user)) == 1
    plan, events = revise(kit, user, save, Step(STATE_NEW), revision=3, kind="edit", action=f"{NEW_ACTION}（再改）")
    assert plan.kind == "edit" and plan.is_free is False and plan.attempt == 4 and plan.hold_key == hold_key(save.public_id, "m-1", 4)
    assert of(events, "done")[0] == {"status": "complete", "turn_number": 1, "coins": 140, "revision": 4, "free_left": 0}
    turn = turn_row(kit, save)
    assert (turn.user_action, turn.regen_count, turn.revision) == (f"{NEW_ACTION}（再改）", 3, 4)         # 沒有歸零
    assert kit.wallets.entry(user.id, plan.hold_key).resolution == "settled" and kit.total(user) == 140
    *_older, last = kit.feedback(save)
    assert (last.kind, last.result, last.is_free) == (REGEN_KIND_EDIT, REGEN_APPLIED, False)
    assert kit.revise(user, save, revision=4).is_free is False                                         # 編輯沒有讓次數重來


# ---- 狀態階段失敗與世界版本改變 -------------------------------------------------------------------

@pytest.mark.parametrize("free_limit", [2, 0])
def test_a_failed_state_call_keeps_the_old_content_and_world_and_releases_everything(kit, free_limit):
    user, save = first_turn(kit, free_limit=free_limit)
    before = kit.fresh(save)
    world_before = (before.version, before.current_location, before.elapsed_months, before.status_summary)
    plan, events = revise(kit, user, save, Step("不是 JSON"), Step("還是不是 JSON"))
    assert events[-1].name == "error" and events[-1].data["code"] == "generation_failed" and "遊戲幣" in events[-1].data["message"]
    assert "state" not in names(events) and "done" not in names(events)
    assert_old_content_kept(kit, user, save, total=170)                                              # 付費者的預扣已退回
    saved = kit.fresh(save)
    assert (saved.version, saved.current_location, saved.elapsed_months, saved.status_summary) == world_before
    (feedback,) = kit.feedback(save)
    assert (feedback.result, feedback.reason) == (REGEN_FAILED, REASON)                               # 失敗的理由仍保留
    if free_limit:
        assert plan.hold_key is None and len(holds(kit, user)) == 1
    else:
        assert kit.wallets.entry(user.id, plan.hold_key).resolution == "refunded"
    again = kit.revise(user, save)                                                                    # 可以再次修訂；免費次數沒有被消耗
    assert again.attempt == 3 and again.is_free is bool(free_limit)


def test_without_a_state_provider_a_revision_fails_and_keeps_the_old_content(kit):
    user, save = first_turn(kit)
    kit.state_available = False
    plan = kit.revise(user, save)
    kit.provider.script_stream(*story_script(*NEW_STORY))
    events = kit.run_revision(plan, user)
    assert events[-1].name == "error" and events[-1].data["code"] == "generation_failed"
    assert_old_content_kept(kit, user, save, total=170)


def test_a_world_version_change_during_the_revision_fails_it_and_refunds(kit):
    user, save = first_turn(kit, free_limit=0)
    plan = kit.revise(user, save)

    def bump(request):
        if request.response_schema is not None:                                                       # 狀態呼叫開始時，另一個地方改了世界狀態
            kit.sessions.update_world_state(user.id, save.public_id, expected_version=plan.version, current_location="別處")
            kit.session.commit()

    kit.provider.hooks.append(bump)
    kit.provider.script_stream(*story_script(*NEW_STORY)).script(Step(STATE_NEW))
    events = kit.run_revision(plan, user)
    assert events[-1].name == "error" and events[-1].data["code"] == "generation_failed" and "done" not in names(events)
    assert_old_content_kept(kit, user, save, total=170)
    saved = kit.fresh(save)
    assert (saved.version, saved.current_location, saved.elapsed_months) == (3, "別處", 3)            # 以後端為準：只有那次改動
    assert kit.wallets.entry(user.id, plan.hold_key).resolution == "refunded"
    assert [row.result for row in kit.feedback(save)] == [REGEN_FAILED]


# ---- 審查：被擋與自動重生 ---------------------------------------------------------------------------

@pytest.mark.parametrize("free_limit", [2, 0])
def test_a_second_block_ends_with_done_blocked_and_keeps_the_old_content(kit, free_limit):
    user, save = first_turn(kit, free_limit=free_limit)
    plan = kit.revise(user, save)
    kit.provider.script_stream(*story_script(BAD_SENTENCE)).script_stream(*story_script("路上。", BAD_SENTENCE))
    events = kit.run_revision(plan, user)
    assert names(events).count("transition") == 2
    first, second = of(events, "transition")
    assert (first["will_retry"], second["will_retry"]) == (True, False) and first["text"] != second["text"]
    assert events[-1].name == "done" and events[-1].data == {"status": "blocked", "turn_number": 1, "coins": 170, "revision": 1, "free_left": 2 if free_limit else 0}
    assert "state" not in names(events) and "少女" not in "".join(event.encode() for event in events)          # 被擋的文字不會送到前端
    assert_old_content_kept(kit, user, save, total=170)
    (feedback,) = kit.feedback(save)
    assert feedback.result == REGEN_BLOCKED and feedback.reason == REASON
    assert kit.provider.remaining_streams == 0 and kit.provider.remaining == 0                         # 沒有狀態呼叫
    assert "少女" not in turn_row(kit, save).llm_raw_response                                          # 被擋的文字沒有保存
    if not free_limit:
        assert kit.wallets.entry(user.id, plan.hold_key).resolution == "refunded"


def test_a_blocked_first_attempt_regenerates_once_on_the_same_hold_and_then_succeeds(kit):
    user, save = first_turn(kit, free_limit=0)
    plan = kit.revise(user, save)
    kit.provider.script_stream(*story_script("山門前很安靜。", BAD_SENTENCE, "後面的文字。")).script_stream(*story_script(*NEW_STORY)).script(Step(STATE_NEW))
    events = kit.run_revision(plan, user)
    transition = of(events, "transition")[0]
    assert transition["will_retry"] is True and transition["text"] and "少女" not in transition["text"]
    assert "少女" not in "".join(event.encode() for event in events)
    assert events[-1].data == {"status": "complete", "turn_number": 1, "coins": 140, "revision": 2, "free_left": 0}
    assert text_of(events).endswith(NEW) and turn_row(kit, save).llm_raw_response == NEW
    assert len(holds(kit, user)) == 2 and kit.total(user) == 140                                       # 原回合一筆、修訂一筆：重生沒有再預扣
    assert len(kit.provider.handles) == 3 and all(handle.closed for handle in kit.provider.handles)
    statuses = [(row.kind, row.status) for row in kit.ledger()][2:]                                    # 略過第 1 輪的串流與狀態
    assert statuses[0] == (KIND_RPG_STREAM, "blocked") and statuses[1] == (KIND_RPG_STREAM, "ok")


# ---- 模型失敗與逾時 ---------------------------------------------------------------------------

@pytest.mark.parametrize(("script", "code"), [
    ((Fail(LLMTransientError("server_error", http_status=503)), Fail(LLMTransientError("server_error", http_status=503))), "generation_failed"),
    ((Idle(seconds=21), Idle(seconds=21)), "generation_timeout"),
])
def test_when_both_models_fail_before_any_text_the_old_content_is_kept_and_the_hold_refunded(kit, script, code):
    user, save = first_turn(kit, free_limit=0)
    plan = kit.revise(user, save)
    for item in script:
        kit.provider.script_stream(item)
    events = kit.run_revision(plan, user)
    assert names(events) == ["meta", "error"] and events[-1].data["code"] == code
    assert_old_content_kept(kit, user, save, total=170)
    assert kit.wallets.entry(user.id, plan.hold_key).resolution == "refunded"
    assert [row.result for row in kit.feedback(save)] == [REGEN_FAILED]


def test_a_failure_in_the_middle_of_the_text_keeps_the_old_content(kit):
    user, save = first_turn(kit)
    plan = kit.revise(user, save)
    kit.provider.script_stream(*story_script(*NEW_STORY[:2], finish=False), Fail(LLMTransientError("network")))
    events = kit.run_revision(plan, user)
    assert events[-1].name == "error" and "done" not in names(events)
    assert_old_content_kept(kit, user, save, total=170)
    assert [row.result for row in kit.feedback(save)] == [REGEN_FAILED]


# ---- 斷線 -------------------------------------------------------------------------------------

def advance_to_first_token(generator):
    seen = []
    while not seen or seen[-1].name != "token":
        seen.append(next(generator))
    return seen


def test_a_disconnect_in_the_middle_of_the_text_releases_the_claim_refunds_and_closes_upstream(kit):
    user, save = first_turn(kit, free_limit=0)
    plan = kit.revise(user, save)
    kit.provider.script_stream(*story_script(*NEW_STORY))
    generator = kit.revision_runner(plan, user).events()
    advance_to_first_token(generator)
    generator.close()                                                                                 # 客戶端斷線：WSGI 伺服器關閉產生器
    assert_old_content_kept(kit, user, save, total=170)
    assert kit.wallets.entry(user.id, plan.hold_key).resolution == "refunded"
    assert kit.provider.handles[-1].closed
    assert [row.result for row in kit.feedback(save)] == [REGEN_FAILED]
    again = kit.revise(user, save)                                                                    # 回合仍可再次修訂
    assert again.attempt == 3


def test_a_disconnect_right_after_the_first_event_still_releases_cleanly(kit):
    user, save = first_turn(kit)
    plan = kit.revise(user, save)
    kit.provider.script_stream(*story_script(*NEW_STORY))
    generator = kit.revision_runner(plan, user).events()
    assert next(generator).name == "meta"
    generator.close()
    assert_old_content_kept(kit, user, save, total=170)
    assert [row.result for row in kit.feedback(save)] == [REGEN_FAILED]


@pytest.mark.parametrize("free_limit", [2, 0])
def test_a_disconnect_after_the_text_is_complete_still_replaces_and_settles(kit, free_limit):
    user, save = first_turn(kit, free_limit=free_limit)
    plan = kit.revise(user, save)
    kit.provider.script_stream(*story_script(*NEW_STORY)).script(Step(STATE_NEW))
    generator = kit.revision_runner(plan, user).events()
    seen = []
    while not seen or seen[-1].name != "ping":
        seen.append(next(generator))
    generator.close()                                                                                 # 文字已完整、正要開始狀態呼叫時斷線
    turn = turn_row(kit, save)
    assert (turn.llm_raw_response, turn.revision, turn.regen_count, turn.revising_started_at) == (NEW, 2, 1, None)
    assert kit.fresh(save).elapsed_months == 1                                                        # 狀態呼叫也完成了
    assert kit.total(user) == (170 if free_limit else 140)
    if not free_limit:
        assert kit.wallets.entry(user.id, plan.hold_key).resolution == "settled"
    assert [row.result for row in kit.feedback(save)] == [REGEN_APPLIED]                              # 成功即算用掉


def test_an_unexpected_error_inside_the_generator_releases_the_claim_and_refunds(kit, monkeypatch):
    user, save = first_turn(kit, free_limit=0)
    plan = kit.revise(user, save)
    kit.provider.script_stream(*story_script(*NEW_STORY))
    runner = kit.revision_runner(plan, user)

    def boom(_text):
        raise RuntimeError("意外")

    monkeypatch.setattr("app.services.rpg_runner_base.OutputGuard.feed", lambda self, text: boom(text))
    events = list(runner.events())
    assert events[-1].name == "error" and events[-1].data["code"] == "internal_error" and "意外" not in events[-1].encode()
    assert_old_content_kept(kit, user, save, total=170)
    assert kit.wallets.entry(user.id, plan.hold_key).resolution == "refunded"
    assert kit.provider.handles[-1].closed and [row.result for row in kit.feedback(save)] == [REGEN_FAILED]


# ---- 被接手與逾時的預扣 -----------------------------------------------------------------------------

def test_a_stale_executor_ends_with_a_superseded_error_and_does_not_touch_the_new_claim(kit):
    user, save = first_turn(kit, free_limit=0)
    old = kit.revise(user, save)                                                                      # 認領（嘗試 2），付費預扣
    kit.clock.advance(seconds=181)                                                                    # 認領逾時（與預扣相同的逾時時間）
    new = kit.revise(user, save)                                                                      # 接手（嘗試 3）
    assert (old.attempt, new.attempt, new.taken_over) == (2, 3, True)
    kit.provider.script_stream(*story_script(*NEW_STORY)).script(Step(STATE_NEW))
    events = kit.run_revision(old, user)                                                              # 過時的執行者
    assert events[-1].name == "error" and events[-1].data["code"] == "turn_superseded" and "done" not in names(events)
    turn = turn_row(kit, save)
    assert (turn.llm_raw_response, turn.revision, turn.attempt) == (OLD, 1, 3) and turn.revising_started_at is not None      # 新的認領不受影響
    assert kit.wallets.entry(user.id, old.hold_key).resolution == "refunded"
    assert kit.wallets.entry(user.id, new.hold_key).resolution not in ("settled", "refunded")
    results = sorted((row.attempt, row.result) for row in kit.feedback(save))
    assert results == [(2, REGEN_FAILED), (3, "pending")]


def test_a_hold_already_refunded_by_the_timeout_rule_does_not_stop_a_finished_revision(kit):
    user, save = first_turn(kit, free_limit=0)
    plan = kit.revise(user, save)
    kit.wallet.refund(user, plan.hold_key)                                                            # 逾時規則已先退回
    kit.session.commit()
    kit.provider.script_stream(*story_script(*NEW_STORY)).script(Step(STATE_NEW))
    events = kit.run_revision(plan, user)
    assert events[-1].name == "done" and turn_row(kit, save).llm_raw_response == NEW                  # 新內容已生效，不追討
    assert kit.wallets.entry(user.id, plan.hold_key).resolution == "refunded"


# ---- 結局 -------------------------------------------------------------------------------------

def custom_ending(**changes):
    return validated(ending_mode="custom", ending_text="主角在山中安然離世。", **changes).content


def test_a_revision_that_proposes_the_ending_ends_the_session(kit):
    user, save = first_turn(kit, content=custom_ending())
    assert kit.fresh(save).status == SESSION_ACTIVE
    plan, events = revise(kit, user, save, Step(state(ending_proposed=True)))
    assert events[-1].data == {"status": "ended", "turn_number": 1, "coins": 170, "revision": 2, "free_left": 1}
    assert text_of(events) == NEW
    saved = kit.fresh(save)
    assert saved.status == SESSION_ENDED and saved.ended_at is not None
    assert turn_row(kit, save).llm_raw_response == NEW
    with pytest.raises(SessionEnded):
        kit.game.begin_turn(user, save.public_id, client_msg_id="m-2", action="再來一次")
    with pytest.raises(NotRevisable) as excinfo:                                                      # 結局那一輪不可再動
        kit.game.begin_revision(user, save.public_id, "m-1", kind="regenerate", expected_revision=2, category="other", reason=REASON)
    assert excinfo.value.reason == "ended"


def test_a_proposed_ending_is_ignored_for_a_script_without_an_ending(kit):
    user, save = first_turn(kit)
    _plan, events = revise(kit, user, save, Step(state(ending_proposed=True)))
    assert events[-1].data["status"] == "complete" and kit.fresh(save).status == SESSION_ACTIVE


# ---- 記事簿 -----------------------------------------------------------------------------------

def test_a_revision_never_reads_or_writes_the_notebook_and_triggers_no_capture(kit):
    user, save = first_turn(kit)
    kit.sessions.save_notebook(user.id, save.public_id, text="玩家寫下的設定：山門守衛姓陳。", expected_version=kit.fresh(save).notebook_version)
    kit.session.commit()
    before = kit.fresh(save)
    notebook = (before.notebook_text, before.notebook_version, before.notebook_upto_turn)
    calls_before = len(kit.provider.calls)
    runner_plan = kit.revise(user, save)
    runner = kit.revision_runner(runner_plan, user)
    kit.provider.script_stream(*story_script(*NEW_STORY)).script(Step(STATE_NEW))
    events = list(runner.events())
    assert events[-1].data["status"] == "complete"
    after = kit.fresh(save)
    assert (after.notebook_text, after.notebook_version, after.notebook_upto_turn) == notebook
    assert len(kit.provider.calls) - calls_before == 2                                                # 只有串流與狀態呼叫，沒有抓取
    assert not hasattr(runner, "capture_target")
    assert "山門守衛姓陳" in "".join(part for content in kit.provider.calls[-2].contents for part in content.parts)   # 記事簿只讀、照常進上下文


# ---- 事件內容、交易與日誌 ----------------------------------------------------------------------

def test_events_carry_only_whitelisted_fields_and_never_the_models_raw_json_or_the_marker(kit):
    user, save = first_turn(kit)
    raw = state(elapsed_months=1, status_summary="摘要", npc_updates=[{"name": "林曉", "anchor_fact": "與主角是舊識"}])
    _plan, events = revise(kit, user, save, Step(raw))
    encoded = "".join(event.encode() for event in events)
    assert "npc_updates" not in encoded and "anchor_fact" not in encoded and MARKER not in encoded
    assert set(of(events, "meta")[0]) == {"kind", "turn_id", "client_msg_id", "attempt", "revision", "replayed"}
    assert set(of(events, "state")[0]) == {"updated", "elapsed_months", "location", "summary", "goals", "notices"}
    assert set(of(events, "done")[0]) == {"status", "turn_number", "coins", "revision", "free_left"}
    assert of(events, "done")[0]["revision"] == of(events, "meta")[0]["revision"] + 1
    for event in events:
        lines = event.encode().split("\n")
        assert lines[0] == f"event: {event.name}" and lines[1].startswith("data: ") and lines[2:] == ["", ""]
        assert json.loads(lines[1][6:]) == event.data


def test_no_database_transaction_is_open_while_the_model_streams_or_the_state_call_runs(kit):
    user, save = first_turn(kit)
    plan = kit.revise(user, save)
    opened = []
    real_factory = kit.factory

    def tracking():
        session = real_factory()
        opened.append(session)
        return session

    kit.factory = tracking
    seen = []
    kit.provider.hooks.append(lambda _request: seen.append([session.in_transaction() for session in (kit.session, *opened)]))
    kit.provider.script_stream(*story_script(*NEW_STORY)).script(Step(STATE_NEW))
    kit.run_revision(plan, user)
    assert len(seen) == 2                                                                             # 串流開始、狀態呼叫開始各一次
    assert not any(any(flags) for flags in seen)


def test_the_ledger_records_the_revision_stream_and_state_call(kit):
    user, save = first_turn(kit)
    revise(kit, user, save, Step(STATE_NEW))
    assert [(row.kind, row.status) for row in kit.ledger()] == [
        (KIND_RPG_STREAM, "ok"), (KIND_RPG_STATE, "ok"), (KIND_RPG_STREAM, "ok"), (KIND_RPG_STATE, "ok"),
    ]


def test_the_logs_of_a_whole_revision_flow_never_contain_the_reason_the_action_or_the_story(kit, caplog):
    secrets = {"reason": "獨特的理由只該在回饋表裡", "action": "獨特的新行動只該在請求裡", "story": "獨特的新劇情只該在串流裡。"}
    with caplog.at_level(logging.DEBUG):
        user, save = first_turn(kit)
        plan = kit.revise(user, save, reason=secrets["reason"], category="typo_or_quality")
        kit.provider.script_stream(*story_script(secrets["story"])).script(Step("壞掉"), Step("壞掉"))
        kit.run_revision(plan, user)                                                                  # 失敗
        plan = kit.revise(user, save, kind="edit", action=secrets["action"])
        kit.provider.script_stream(*story_script(secrets["story"], BAD_SENTENCE)).script_stream(*story_script(BAD_SENTENCE))
        kit.run_revision(plan, user)                                                                  # 被擋
        plan = kit.revise(user, save, kind="edit", action=secrets["action"])
        kit.provider.script_stream(*story_script(secrets["story"])).script(Step(STATE_NEW))
        kit.run_revision(plan, user)                                                                  # 成功
    blob = "\n".join(record.getMessage() + " " + json.dumps(record.__dict__, default=str, ensure_ascii=False) for record in caplog.records)
    for secret in (*secrets.values(), "少女", ACTION, OLD, "typo_or_quality"):
        assert secret not in blob, secret
    events = [record for record in caplog.records if getattr(record, "event", "").startswith("rpg.revision_")]
    assert [(record.event, record.kind) for record in events] == [
        ("rpg.revision_started", REGEN_KIND_REGENERATE), ("rpg.revision_failed", REGEN_KIND_REGENERATE),
        ("rpg.revision_started", REGEN_KIND_EDIT), ("rpg.revision_failed", REGEN_KIND_EDIT),
        ("rpg.revision_started", REGEN_KIND_EDIT), ("rpg.revision_completed", REGEN_KIND_EDIT),
    ]
    failed = [record for record in events if record.event == "rpg.revision_failed"]
    assert [record.reason for record in failed] == ["state_failed", "blocked"]
    completed = events[-1]
    assert (completed.outcome, completed.reason, completed.attempt) == ("complete", "world_updated", 4)
