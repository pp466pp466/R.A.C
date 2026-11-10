"""rpg-world-state、rpg-turn-engine 規格：`TurnRunner` 的狀態階段（狀態呼叫、驗證、短交易寫回、結局）。

文字已完整送達之後才進入狀態階段：狀態呼叫失敗、版本衝突都不影響文字與結算；結局由 Service 判定。
以真的資料庫（SQLite 與 SQL Server）與假的供應商驗證每個出口。
"""
import json

import pytest

from app.models.llm_call_log import KIND_RPG_STATE
from app.models.rpg_session import SESSION_ACTIVE, SESSION_ENDED, TURN_COMPLETE
from app.services.errors import SessionEnded
from app.services.game_service import hold_key
from tests.support.game import RunnerKit, story_script
from tests.support.llm import Step
from tests.support.script_data import validated

NAMED_STORY = ("清晨的海風吹過山門。", "林曉笑著向你揮手。", "你踏進了霧氣之中。")


def state(**fields):
    return json.dumps(fields, ensure_ascii=False)


@pytest.fixture()
def kit(member_session):
    return RunnerKit(member_session)


def setup(kit, content=None, **overrides):
    user = kit.player()
    save = kit.open_save(user, content=content, **overrides)
    plan = kit.start(user, save)
    return user, save, plan


def of(events, name):
    return [event.data for event in events if event.name == name]


def play(kit, user, plan, *replies, sentences=NAMED_STORY):
    kit.provider.script_stream(*story_script(*sentences))
    kit.provider.script(*replies)
    return kit.run(plan, user)


# ---- 成功寫回 ---------------------------------------------------------------------------------

def test_a_valid_proposal_updates_the_world_in_the_same_transaction_as_completion_and_settlement(kit):
    user, save, plan = setup(kit)
    proposal = state(
        elapsed_months=3, current_location="古劍山莊", status_summary="你抵達了山莊。", goals_add=[{"text": "拜見莊主"}],
        npc_updates=[{"name": "林曉", "traits": [{"label": "開朗", "layer": "surface"}], "age": None}],
    )
    events = play(kit, user, plan, Step(proposal))
    payload = of(events, "state")[0]
    assert payload["updated"] is True and payload["elapsed_months"] == 3 and payload["location"] == "古劍山莊"
    assert payload["goals"] == ["拜見莊主"] and payload["summary"] == "你抵達了山莊。" and payload["notices"] == []
    saved = kit.fresh(save)
    assert (saved.version, saved.elapsed_months, saved.current_location, saved.status_summary) == (2, 3, "古劍山莊", "你抵達了山莊。")
    assert saved.goals_json["active"][0]["text"] == "拜見莊主"
    card = saved.entities_json["npcs"]["林曉"]
    assert card["last_seen_turn"] == 1 and card["stage"] == "surface" and card["origin"] == "script"
    assert "開朗" in [t["label"] for t in card["traits"]]
    (turn,) = kit.turns(save)
    assert (turn.turn_status, turn.turn_number) == (TURN_COMPLETE, 1)
    assert turn.structured_result == {"state": {"ok": True, "dropped": 0}}
    assert kit.total(user) == 170 and kit.wallets.entry(user.id, hold_key(save.public_id, "m-1", 1)).resolution == "settled"


def test_the_state_call_runs_as_an_rpg_state_ledger_row_with_the_frozen_primary_model(kit):
    user, save, plan = setup(kit)
    play(kit, user, plan, Step(state(elapsed_months=0)))
    (row,) = kit.ledger(KIND_RPG_STATE)
    assert row.status == "ok" and row.counted is False and row.user_id == user.id
    call = kit.provider.calls[-1]
    assert call.response_schema is not None and call.model == plan.model
    assert "林曉" in "".join(part for content in call.contents for part in content.parts)          # 本回合劇情與在場人物是資料區塊
    assert plan.action not in call.system_instruction


def test_a_bad_item_is_dropped_but_the_rest_of_the_proposal_is_applied(kit):
    user, save, plan = setup(kit)
    proposal = state(
        elapsed_months=2, current_location="山門",
        npc_updates=[{"name": "林曉", "traits": [{"label": "過長的特質標籤超過八個字上限", "layer": "surface"}, {"label": "沉穩", "layer": "surface"}]}],
    )
    events = play(kit, user, plan, Step(proposal))
    assert of(events, "state")[0]["updated"] is True
    saved = kit.fresh(save)
    assert saved.elapsed_months == 2 and saved.current_location == "山門"
    labels = [t["label"] for t in saved.entities_json["npcs"]["林曉"]["traits"]]
    assert "沉穩" in labels and all(len(label) <= 8 for label in labels)
    assert kit.turns(save)[0].structured_result["state"]["dropped"] >= 1


def test_an_age_correction_is_a_notice_in_the_state_event_with_the_previous_age(kit):
    user, save, plan = setup(kit)
    events = play(kit, user, plan, Step(state(npc_updates=[{"name": "林曉", "age": 17}])))
    notices = of(events, "state")[0]["notices"]
    assert len(notices) == 1 and notices[0]["kind"] == "age_corrected" and notices[0]["name"] == "林曉"
    assert (notices[0]["before"], notices[0]["after"]) == (16, 17) and "16" in notices[0]["text"] and "17" in notices[0]["text"]
    assert kit.fresh(save).entities_json["npcs"]["林曉"]["age"] == 17


def test_state_events_never_contain_the_models_raw_json(kit):
    user, save, plan = setup(kit)
    raw = state(elapsed_months=1, status_summary="摘要", npc_updates=[{"name": "林曉", "anchor_fact": "與主角是舊識"}])
    events = play(kit, user, plan, Step(raw))
    encoded = "".join(event.encode() for event in events)
    assert "npc_updates" not in encoded and "anchor_fact" not in encoded and "ending_proposed" not in encoded
    assert set(of(events, "state")[0]) == {"updated", "elapsed_months", "location", "summary", "goals", "notices"}


# ---- 狀態呼叫失敗與版本衝突 -----------------------------------------------------------------------

def test_two_failed_state_calls_still_complete_the_turn_and_settle(kit):
    user, save, plan = setup(kit)
    events = play(kit, user, plan, Step("不是 JSON"), Step("還是不是 JSON"))
    assert of(events, "state")[0] == {"updated": False, "notices": []}
    assert events[-1].data["status"] == "complete" and events[-1].data["turn_number"] == 1
    (turn,) = kit.turns(save)
    assert (turn.turn_status, turn.llm_raw_response, turn.structured_result) == (TURN_COMPLETE, "".join(NAMED_STORY), {"state": {"ok": False, "dropped": 0}})
    saved = kit.fresh(save)
    assert (saved.version, saved.elapsed_months, saved.entities_json) == (1, 0, {})          # 世界狀態不變
    assert kit.total(user) == 170
    # 預扣必須真的「結算」（不只是餘額被扣）：沒有結算的預扣會被逾時規則退回，等於狀態呼叫失敗的回合免費
    assert kit.wallets.entry(user.id, hold_key(save.public_id, "m-1", 1)).resolution == "settled"
    assert [row.status for row in kit.ledger(KIND_RPG_STATE)] == ["validation_failed", "validation_failed"]


def test_a_state_call_that_raises_still_completes_the_turn(kit):
    user, save, plan = setup(kit)
    events = play(kit, user, plan, Step(RuntimeError("意外")))
    assert events[-1].data["status"] == "complete" and kit.total(user) == 170
    assert kit.wallets.entry(user.id, hold_key(save.public_id, "m-1", 1)).resolution == "settled"


def test_a_version_conflict_keeps_the_turn_but_not_the_world_update(kit):
    user, save, plan = setup(kit)
    kit.sessions.update_world_state(user.id, save.public_id, expected_version=plan.version, current_location="別處")      # 另一個裝置先改了
    kit.session.commit()
    events = play(kit, user, plan, Step(state(elapsed_months=5, current_location="山門", npc_updates=[{"name": "林曉", "age": 20}])))
    assert of(events, "state")[0] == {"updated": False, "notices": []}                      # 沒有寫入的更正不通知
    saved = kit.fresh(save)
    assert (saved.version, saved.current_location, saved.elapsed_months, saved.entities_json) == (2, "別處", 0, {})       # 以後端為準
    (turn,) = kit.turns(save)
    assert (turn.turn_status, turn.turn_number) == (TURN_COMPLETE, 1) and kit.total(user) == 170


def test_without_a_state_provider_the_turn_completes_and_no_state_call_is_made(kit):
    user, save, plan = setup(kit)
    kit.state_available = False
    kit.provider.script_stream(*story_script(*NAMED_STORY))
    events = kit.run(plan, user)
    assert events[-1].data["status"] == "complete" and kit.provider.remaining == 0


# ---- 結局 -------------------------------------------------------------------------------------

def custom_ending(**changes):
    return validated(ending_mode="custom", ending_text="主角在山中安然離世。", **changes).content


def test_a_proposed_ending_is_ignored_for_a_script_without_an_ending(kit):
    user, save, plan = setup(kit)
    events = play(kit, user, plan, Step(state(ending_proposed=True)))
    assert events[-1].data["status"] == "complete"
    assert kit.fresh(save).status == SESSION_ACTIVE


def test_a_proposed_ending_ends_the_session_for_a_custom_ending_script(kit):
    user, save, plan = setup(kit, content=custom_ending())
    events = play(kit, user, plan, Step(state(ending_proposed=True)))
    assert events[-1].name == "done" and events[-1].data == {"status": "ended", "turn_number": 1, "coins": 170}
    assert "".join(of(events, "token")[i]["text"] for i in range(len(of(events, "token")))) == "".join(NAMED_STORY)       # 結局回合的文字照常送完
    saved = kit.fresh(save)
    assert saved.status == SESSION_ENDED and saved.ended_at is not None
    with pytest.raises(SessionEnded):
        kit.game.begin_turn(user, save.public_id, client_msg_id="m-2", action="再來一次")
    assert kit.game.begin_turn(user, save.public_id, client_msg_id="m-1", action="我走進山門。").replay.ended is True


def test_the_age_of_death_makes_the_turn_an_ending_turn_even_when_the_state_call_fails(kit):
    user, save, plan = setup(kit, content=custom_ending(ending_age=18))              # 主角 18 歲，已達壽終年齡
    assert plan.ending_due is True
    events = play(kit, user, plan, Step("壞掉"), Step("壞掉"))
    assert events[-1].data["status"] == "ended"
    assert kit.fresh(save).status == SESSION_ENDED and kit.total(user) == 170


def test_the_age_of_death_alone_ends_the_session_without_the_model_proposing_it(kit):
    user, save, plan = setup(kit, content=custom_ending(ending_age=18))
    events = play(kit, user, plan, Step(state(elapsed_months=1)))
    assert events[-1].data["status"] == "ended" and of(events, "state")[0]["updated"] is True
    assert kit.fresh(save).status == SESSION_ENDED


def test_a_young_protagonist_does_not_reach_the_age_of_death(kit):
    user, save, plan = setup(kit, content=custom_ending(ending_age=60))
    assert plan.ending_due is False
    events = play(kit, user, plan, Step(state(elapsed_months=1)))
    assert events[-1].data["status"] == "complete" and kit.fresh(save).status == SESSION_ACTIVE


# ---- 斷線於狀態階段 -----------------------------------------------------------------------------

def test_disconnecting_during_the_state_phase_still_updates_the_world_and_settles(kit):
    user, save, plan = setup(kit)
    kit.provider.script_stream(*story_script(*NAMED_STORY)).script(Step(state(elapsed_months=4, current_location="山門前")))
    generator = kit.runner(plan, user).events()
    seen = []
    while not seen or seen[-1].name != "ping":
        seen.append(next(generator))
    generator.close()
    saved = kit.fresh(save)
    assert (saved.elapsed_months, saved.current_location, saved.version) == (4, "山門前", 2)
    (turn,) = kit.turns(save)
    assert turn.turn_status == TURN_COMPLETE and kit.total(user) == 170
    assert turn.llm_raw_response == "".join(NAMED_STORY)
