"""rpg-opening 規格：第 0 幕（開場）的狀態、生成、搶佔與接手、失敗與被擋時退回初始對話、斷線退回待生成。

以真的資料庫（SQLite 與 SQL Server）與假的串流供應商驗證。第 0 幕不碰錢包、不寫回合表。
"""
import pytest

from app.llm.types import LLMTransientError
from app.models.llm_call_log import KIND_RPG_OPENING
from app.models.rpg_session import OPENING_FALLBACK, OPENING_GENERATING, OPENING_PENDING, OPENING_READY
from app.services.errors import AgeConfirmationRequired, Conflict, NotFound, SystemBusy
from tests.support.game import BAD_SENTENCE, MARKER, MODEL, STORY, STORY_SENTENCES, RunnerKit, story_script
from tests.support.llm import Chunk, Fail
from tests.support.script_data import validated

ANCHOR = {"kind": "character", "ref": "林曉", "text": "她是我兒時的玩伴"}
SCRIPT_OPENING = "山門前，晨霧未散。"


@pytest.fixture()
def kit(member_session):
    return RunnerKit(member_session)


def of(events, name):
    return [event.data for event in events if event.name == name]


def text_of(events):
    return "".join(data["text"] for data in of(events, "token"))


def begin(kit, n=1, **overrides):
    user = kit.player(n)
    save = kit.pending_save(user, **overrides)
    return user, save, kit.begin_opening(user, save)


def reload(kit, save):
    kit.session.expire_all()
    return save


# ---- 狀態與生成 -------------------------------------------------------------------------------

def test_a_new_save_starts_pending_with_no_turn_rows_and_zero_completed_turns(kit):
    user = kit.player()
    save = kit.pending_save(user)
    assert (save.opening_state, save.opening_text) == (OPENING_PENDING, None)
    assert kit.turns(save) == [] and kit.sessions.count_complete_turns(user.id, save.public_id) == 0


def test_generating_streams_the_opening_stores_it_and_leaves_the_wallet_and_turn_table_alone(kit):
    user, save, begun = begin(kit)
    plan = begun.plan
    assert plan is not None and begun.done is None and plan.taken_over is False and plan.fallback_text == SCRIPT_OPENING
    assert reload(kit, save).opening_state == OPENING_GENERATING
    kit.provider.script_stream(*story_script())
    events = kit.run_opening(plan)
    names = [event.name for event in events]
    assert names[0] == "meta" and names[-1] == "done" and set(names) <= {"meta", "token", "ping", "done"}
    assert of(events, "meta")[0] == {"kind": "opening", "replayed": False}
    assert text_of(events) == STORY
    assert of(events, "done")[0] == {"status": "complete", "turn_number": None, "coins": 0, "opening_state": "ready"}
    saved = reload(kit, save)
    assert (saved.opening_state, saved.opening_text) == (OPENING_READY, STORY)
    assert kit.turns(save) == [] and kit.sessions.count_complete_turns(user.id, save.public_id) == 0      # 不是一輪
    assert kit.wallets.state(user.id) is None                                                          # 完全沒有碰錢包
    (row,) = kit.ledger()
    assert (row.kind, row.status, row.model, row.counted) == (KIND_RPG_OPENING, "ok", MODEL, False)
    assert kit.provider.handles[0].closed


def test_the_request_carries_the_protagonist_and_anchor_as_data_not_in_the_instruction(kit):
    user, save, begun = begin(kit, anchor=ANCHOR, personality="沉默愛思考")
    kit.provider.script_stream(*story_script())
    kit.run_opening(begun.plan)
    request = kit.provider.calls[0]
    data = "".join(part for content in request.contents for part in content.parts)
    assert "旅人" in data and "沉默愛思考" in data and "她是我兒時的玩伴" in data and "林曉" in data
    assert "旅人" not in request.system_instruction and "她是我兒時的玩伴" not in request.system_instruction
    assert MARKER in request.system_instruction and request.max_output_tokens == 1800


def test_a_transmigrator_gets_the_platform_rule_from_the_fixed_instruction(kit):
    user, save, begun = begin(kit, origin="transmigrator")
    assert "穿越" in begun.plan.system_instruction
    user2, save2, native = begin(kit, n=2)
    assert "穿越" not in native.plan.system_instruction


def test_a_ready_opening_is_returned_without_calling_the_model_again(kit):
    user, save, begun = begin(kit)
    kit.provider.script_stream(*story_script())
    kit.run_opening(begun.plan)
    calls = len(kit.provider.calls)
    again = kit.begin_opening(user, save)
    assert again.plan is None and (again.done.state, again.done.text) == (OPENING_READY, STORY)
    assert len(kit.provider.calls) == calls


def test_a_fallback_opening_is_also_returned_as_it_is(kit):
    user = kit.player()
    save = kit.open_save(user)
    kit.session.expire_all()
    done = kit.begin_opening(user, kit.sessions.get_owned(user.id, save.public_id)).done
    assert done.state == OPENING_READY and done.text == "開場的劇情。"


def test_a_save_from_before_the_migration_gets_the_scripts_opening_and_is_never_generated(kit):
    user = kit.player()
    save = kit.pending_save(user)
    save.opening_state = None
    kit.session.commit()
    done = kit.begin_opening(user, save).done
    assert (done.state, done.text) == (OPENING_FALLBACK, SCRIPT_OPENING) and kit.provider.calls == []


# ---- 同時只有一個裝置生成 -----------------------------------------------------------------------

def test_a_second_request_while_generating_is_opening_in_progress(kit):
    user, save, begun = begin(kit)
    with pytest.raises(Conflict) as caught:
        kit.begin_opening(user, save)
    assert caught.value.code == "opening_in_progress"
    assert reload(kit, save).opening_state == OPENING_GENERATING


def test_a_stale_generation_can_be_taken_over_and_the_old_runner_cannot_overwrite_it(kit):
    user, save, first = begin(kit)
    kit.clock.advance(seconds=91)
    second = kit.begin_opening(user, save)
    assert second.plan is not None and second.plan.taken_over is True and second.plan.claimed_at != first.plan.claimed_at
    kit.provider.script_stream(*story_script("新的開場。", "第二句。", "第三句。"))
    new_events = kit.run_opening(second.plan)
    assert new_events[-1].data["opening_state"] == "ready"
    kit.provider.script_stream(*story_script("舊的開場。", "舊的第二句。", "舊的第三句。"))
    old_events = kit.run_opening(first.plan)                                 # 過時的產生器
    assert old_events[-1].name == "error" and old_events[-1].data["code"] == "opening_superseded"
    saved = reload(kit, save)
    assert (saved.opening_state, saved.opening_text) == (OPENING_READY, "新的開場。第二句。第三句。")


def test_an_unfinished_generation_within_ninety_seconds_is_not_taken_over(kit):
    user, save, _first = begin(kit)
    kit.clock.advance(seconds=89)
    with pytest.raises(Conflict):
        kit.begin_opening(user, save)


# ---- 失敗與被擋：退回初始對話 ---------------------------------------------------------------------

def test_a_blocked_first_attempt_regenerates_once(kit):
    user, save, begun = begin(kit)
    kit.provider.script_stream(*story_script("開場。", BAD_SENTENCE)).script_stream(*story_script())
    events = kit.run_opening(begun.plan)
    transitions = of(events, "transition")
    assert [t["will_retry"] for t in transitions] == [True] and "少女" not in "".join(e.encode() for e in events)
    assert events[-1].data["opening_state"] == "ready" and reload(kit, save).opening_text == STORY
    assert [row.status for row in kit.ledger()] == ["blocked", "ok"]


def test_two_blocked_attempts_fall_back_to_the_scripts_opening_dialogue(kit):
    user, save, begun = begin(kit)
    kit.provider.script_stream(*story_script(BAD_SENTENCE)).script_stream(*story_script("路上。", BAD_SENTENCE))
    events = kit.run_opening(begun.plan)
    assert [t["will_retry"] for t in of(events, "transition")] == [True, False]
    assert "少女" not in "".join(e.encode() for e in events)
    assert text_of(events) == SCRIPT_OPENING and events[-1].data["opening_state"] == "fallback"
    saved = reload(kit, save)
    assert (saved.opening_state, saved.opening_text) == (OPENING_FALLBACK, SCRIPT_OPENING)
    assert kit.wallets.state(user.id) is None


def test_a_script_without_an_opening_dialogue_falls_back_to_empty_text(kit):
    content = validated(opening=...).content
    user, save, begun = begin(kit, content=content)
    assert begun.plan.fallback_text == ""
    kit.provider.script_stream(Fail(LLMTransientError("server_error"))).script_stream(Fail(LLMTransientError("server_error")))
    events = kit.run_opening(begun.plan)
    assert [e.name for e in events] == ["meta", "done"] and events[-1].data["opening_state"] == "fallback"
    saved = reload(kit, save)
    assert (saved.opening_state, saved.opening_text) == (OPENING_FALLBACK, "")


def test_when_both_models_fail_the_opening_falls_back(kit):
    user, save, begun = begin(kit)
    kit.provider.script_stream(Fail(LLMTransientError("rate_limited", http_status=429))).script_stream(Fail(LLMTransientError("server_error", http_status=500)))
    events = kit.run_opening(begun.plan)
    assert text_of(events) == SCRIPT_OPENING and "transition" not in [e.name for e in events]       # 沒有送出任何文字，不需要轉場
    assert reload(kit, save).opening_state == OPENING_FALLBACK


def test_a_failure_after_part_of_the_text_discards_it_and_falls_back(kit):
    user, save, begun = begin(kit)
    kit.provider.script_stream(Chunk(STORY_SENTENCES[0]), Chunk(STORY_SENTENCES[1]), Fail(LLMTransientError("network")))
    events = kit.run_opening(begun.plan)
    assert [t["will_retry"] for t in of(events, "transition")] == [False]
    assert reload(kit, save).opening_text == SCRIPT_OPENING and events[-1].data["opening_state"] == "fallback"


# ---- 斷線 -------------------------------------------------------------------------------------

def test_a_disconnect_mid_stream_resets_to_pending_and_drops_the_partial_text(kit):
    user, save, begun = begin(kit)
    kit.provider.script_stream(*story_script())
    generator = kit.opening_runner(begun.plan).events()
    seen = []
    while not seen or seen[-1].name != "token":
        seen.append(next(generator))
    generator.close()
    saved = reload(kit, save)
    assert (saved.opening_state, saved.opening_text, saved.opening_started_at) == (OPENING_PENDING, None, None)
    assert kit.provider.handles[0].closed
    again = kit.begin_opening(user, save)                                     # 下次進入再生成
    assert again.plan is not None and again.plan.taken_over is False


def test_a_disconnect_right_after_meta_also_resets(kit):
    user, save, begun = begin(kit)
    generator = kit.opening_runner(begun.plan).events()
    assert next(generator).name == "meta"
    generator.close()
    assert reload(kit, save).opening_state == OPENING_PENDING


# ---- 閘門與預算 -------------------------------------------------------------------------------

def test_a_spent_global_budget_refuses_generation_and_leaves_the_opening_pending(kit):
    user = kit.player()
    save = kit.pending_save(user)
    kit.budget_exhausted = True
    with pytest.raises(SystemBusy):
        kit.begin_opening(user, save)
    assert reload(kit, save).opening_state == OPENING_PENDING


def test_a_spent_budget_does_not_stop_reading_a_finished_opening(kit):
    user = kit.player()
    save = kit.open_save(user)
    kit.budget_exhausted = True
    assert kit.begin_opening(user, save).done.text == "開場的劇情。"


def test_a_stale_takeover_without_budget_hands_the_claim_back(kit):
    user, save, _first = begin(kit)
    kit.clock.advance(seconds=91)
    kit.budget_exhausted = True
    with pytest.raises(SystemBusy):
        kit.begin_opening(user, save)
    assert reload(kit, save).opening_state == OPENING_PENDING


def test_another_players_save_is_a_404_and_an_unconfirmed_account_is_refused(kit):
    owner, other = kit.player(1), kit.player(2)
    save = kit.pending_save(owner)
    with pytest.raises(NotFound):
        kit.begin_opening(other, save)
    with pytest.raises(AgeConfirmationRequired):
        kit.begin_opening(kit.player(3, age=None), save)
    assert reload(kit, save).opening_state == OPENING_PENDING


def test_a_turn_can_start_once_the_opening_is_ready_and_the_opening_enters_its_context(kit):
    user, save, begun = begin(kit)
    kit.provider.script_stream(*story_script("唯一的開場。", "第二句。", "第三句。"))
    kit.run_opening(begun.plan)
    plan = kit.game.begin_turn(user, save.public_id, client_msg_id="m-1", action="我往前走。").plan
    assert plan.turn_number == 1
    assert "唯一的開場。第二句。第三句。" in "".join(part for content in plan.contents for part in content.parts)
