"""rpg-turn-engine 規格：回合的開始階段（閘門、輸入檢查、預扣、接手、單一進行中、重送）。

以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行。
"""
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import MemoryLog
from app.models.rpg_session import OPENING_FALLBACK, OPENING_GENERATING, OPENING_PENDING, TURN_COMPLETE, TURN_INTERRUPTED, TURN_STREAMING
from app.observability import rpg_events
from app.services.errors import (
    AgeConfirmationRequired, Conflict, InputBlocked, InsufficientCoins, NotFound, OpeningPending, SessionEnded, SystemBusy, ValidationError,
)
from app.services.game_service import HOLD_KEY_PREFIX, hold_key
from app.services.wallet_service import HOLD_KEY_MAX
from tests.support.game import MARKER, MODEL, GameKit

ACTION = "我走進山門，向守門的弟子行禮。"
GHOST = "00000000-0000-4000-8000-000000000000"


@pytest.fixture()
def kit(member_session):
    return GameKit(member_session)


def begin(kit, user, save, msg="m-1", action=ACTION):
    return kit.game.begin_turn(user, save.public_id, client_msg_id=msg, action=action)


def turn_rows(kit, save):
    kit.session.expire_all()
    return list(kit.session.scalars(select(MemoryLog).where(MemoryLog.session_id == save.id).order_by(MemoryLog.seq)))


def finish(kit, user, save, msg="m-1", attempt=None, story="劇情。"):
    """把一個生成中的回合直接標為完成（模擬回合引擎的收尾）。"""
    kit.sessions.complete_turn(user.id, save.public_id, msg, llm_raw_response=story, structured_result=None, now=kit.clock(), attempt=attempt)
    kit.wallet.settle(user, hold_key(save.public_id, msg, attempt or 1))


# ---- 開始、預扣與計畫 -------------------------------------------------------------------------

def test_a_new_turn_holds_one_turn_of_coins_and_freezes_a_plan(kit):
    user = kit.player()
    save = kit.open_save(user)
    assert kit.total(user) == 0                                                # 錢包尚未建立
    result = begin(kit, user, save)
    plan = result.plan
    assert result.replay is None and plan is not None
    assert (plan.attempt, plan.turn_number, plan.cost, plan.seq, plan.start) == (1, 1, 30, 1, rpg_events.START_NEW)
    assert plan.hold_key == f"{HOLD_KEY_PREFIX}:{save.public_id}:m-1:1"
    assert (plan.model, plan.tier, plan.version, plan.ending_due) == (MODEL, "safe", save.version, False)
    assert (plan.user_id, plan.session_id, plan.session_public_id) == (user.id, save.id, save.public_id)
    assert plan.action == ACTION and plan.history == ()
    assert kit.total(user) == 200 - 30                                         # 首次存取時補發 200 幣，預扣 30
    (row,) = turn_rows(kit, save)
    assert (row.turn_status, row.attempt, row.user_action) == (TURN_STREAMING, 1, ACTION)


def test_the_plan_carries_a_context_with_the_action_only_in_the_data_part(kit):
    user = kit.player()
    save = kit.open_save(user)
    plan = begin(kit, user, save).plan
    assert ACTION not in plan.system_instruction and MARKER in plan.system_instruction
    assert ACTION in plan.contents[-1].parts[-1]
    assert plan.contents[-1].role == "user"
    assert "開場的劇情。" in "".join(part for content in plan.contents for part in content.parts)           # 第 0 幕在歷史最前面


def test_the_plan_freezes_the_recent_ten_complete_turns_and_the_expected_turn_number(kit):
    user = kit.player()
    save = kit.open_save(user)
    kit.wallet.view(user)
    kit.wallet.adjust(user.id, bucket="paid", delta=1000)                          # 13 輪需要的幣
    for index in range(1, 13):
        begin(kit, user, save, msg=f"m-{index}", action=f"行動{index}")
        finish(kit, user, save, msg=f"m-{index}", story=f"劇情{index}")
    plan = begin(kit, user, save, msg="m-13", action="行動13").plan
    assert plan.turn_number == 13
    assert [turn.turn_number for turn in plan.history] == list(range(3, 13))
    assert all(turn.status == TURN_COMPLETE for turn in plan.history)


def test_interrupted_and_blocked_turns_are_not_in_the_frozen_history(kit):
    user = kit.player()
    save = kit.open_save(user)
    begin(kit, user, save, msg="m-1")
    kit.sessions.interrupt_turn(user.id, save.public_id, "m-1", partial_response="半截", attempt=1)
    kit.wallet.refund(user, hold_key(save.public_id, "m-1", 1))
    plan = begin(kit, user, save, msg="m-2").plan
    assert plan.history == () and plan.turn_number == 1


def test_the_hold_key_with_the_longest_inputs_fits_the_wallet_limit():
    key = hold_key("00000000-0000-4000-8000-000000000000", "x" * 64, 999999)
    assert len(key) <= HOLD_KEY_MAX


# ---- 閘門與輸入 -------------------------------------------------------------------------------

def test_an_unconfirmed_account_gets_age_confirmation_required_and_nothing_is_written(kit):
    user = kit.player(age=None)
    save = kit.open_save(kit.player(2))
    with pytest.raises(AgeConfirmationRequired):
        begin(kit, user, save)
    assert turn_rows(kit, save) == []


@pytest.mark.parametrize("action", ["", "   \n\t ", "\u0007\u0008"])
def test_a_blank_action_is_a_field_error_with_no_hold_and_no_turn(kit, action):
    user = kit.player()
    save = kit.open_save(user)
    with pytest.raises(ValidationError) as caught:
        begin(kit, user, save, action=action)
    assert "action" in caught.value.fields
    assert turn_rows(kit, save) == [] and kit.total(user) == 0


@pytest.mark.parametrize("action", [None, 5, ["a"]])
def test_a_non_text_action_is_a_field_error(kit, action):
    user = kit.player()
    save = kit.open_save(user)
    with pytest.raises(ValidationError) as caught:
        begin(kit, user, save, action=action)
    assert "action" in caught.value.fields


def test_the_action_limit_is_one_thousand_characters_after_cleaning(kit):
    user = kit.player()
    save = kit.open_save(user)
    begin(kit, user, save, msg="ok", action="字" * 1000)
    with pytest.raises(ValidationError):
        begin(kit, user, save, msg="too-long", action="字" * 1001)


@pytest.mark.parametrize("msg", ["", "m" * 65, None, 7])
def test_a_bad_client_msg_id_is_a_field_error(kit, msg):
    user = kit.player()
    save = kit.open_save(user)
    with pytest.raises(ValidationError) as caught:
        begin(kit, user, save, msg=msg)
    assert "client_msg_id" in caught.value.fields


def test_the_action_is_cleaned_before_it_is_stored(kit):
    user = kit.player()
    save = kit.open_save(user)
    plan = begin(kit, user, save, action="  向\u0007前　走  \n").plan
    assert plan.action == "向前　走"
    assert turn_rows(kit, save)[0].user_action == "向前　走"


def test_another_players_save_and_an_unknown_save_are_the_same_404(kit):
    owner, other = kit.player(1), kit.player(2)
    save = kit.open_save(owner)
    with pytest.raises(NotFound):
        begin(kit, other, save)
    with pytest.raises(NotFound):
        kit.game.begin_turn(owner, GHOST, client_msg_id="m-1", action=ACTION)
    assert turn_rows(kit, save) == []


def test_a_rejected_action_is_input_blocked_with_categories_only(kit):
    user = kit.player()
    save = kit.open_save(user)
    with pytest.raises(InputBlocked) as caught:
        begin(kit, user, save, action="十歲的少女與他做愛。")
    assert "minor_sexual" in caught.value.categories
    assert "少女" not in str(caught.value) and "少女" not in caught.value.message
    assert turn_rows(kit, save) == [] and kit.total(user) == 0                  # 沒有預扣、沒有回合列（錢包也沒有被建立）


def test_a_sexual_action_in_safe_mode_is_not_blocked_and_the_tier_is_not_raised(kit):
    user = kit.player()
    save = kit.open_save(user, tier="safe")
    plan = begin(kit, user, save, action="他們做愛。").plan
    assert plan is not None and plan.tier == "safe"


def test_the_effective_tier_is_the_most_conservative_of_the_save_and_the_players_cap(kit):
    adult = kit.player(1)
    save = kit.open_save(adult, tier="standard")
    assert begin(kit, adult, save).plan.tier == "standard"
    teen = kit.player(2, age=15)
    teen_save = kit.open_save(teen, tier="standard")                             # 存檔記錄較寬鬆，但玩家未滿 18 歲
    assert begin(kit, teen, teen_save).plan.tier == "safe"


def test_a_spent_global_budget_is_system_busy_without_a_hold(kit):
    user = kit.player()
    save = kit.open_save(user)
    kit.budget_exhausted = True
    with pytest.raises(SystemBusy):
        begin(kit, user, save)
    assert turn_rows(kit, save) == [] and kit.total(user) == 0


# ---- 第 0 幕閘門 ------------------------------------------------------------------------------

def test_a_pending_or_generating_opening_blocks_the_turn(kit):
    user = kit.player()
    save = kit.sessions.create(
        user.id, source_version_id=None, snapshot=kit.open_save(user).script_snapshot_json, snapshot_spec_version=1,
        protagonist={"character": {"name": "旅人", "age": 18}, "anchor": None}, content_tier="safe", now=kit.clock(),
    )
    assert save.opening_state == OPENING_PENDING
    with pytest.raises(OpeningPending):
        begin(kit, user, save)
    kit.sessions.claim_opening(user.id, save.public_id, now=kit.clock())
    kit.session.expire_all()
    assert save.opening_state == OPENING_GENERATING
    with pytest.raises(OpeningPending):
        begin(kit, user, save)
    assert turn_rows(kit, save) == [] and kit.total(user) == 0


def test_a_fallback_opening_does_not_block_the_turn(kit):
    user = kit.player()
    save = kit.open_save(user)
    save.opening_state = OPENING_FALLBACK
    kit.session.flush()
    assert begin(kit, user, save).plan is not None


def test_a_save_from_before_the_migration_has_no_opening_state_and_is_not_blocked(kit):
    user = kit.player()
    save = kit.open_save(user)
    save.opening_state = None
    save.opening_text = None
    kit.session.flush()
    plan = begin(kit, user, save).plan
    assert plan is not None
    assert "開場的劇情。" not in "".join(part for content in plan.contents for part in content.parts)


# ---- 預扣與餘額 -------------------------------------------------------------------------------

def test_insufficient_coins_is_a_conflict_and_leaves_no_turn_row(kit):
    user = kit.player()
    save = kit.open_save(user)
    kit.wallet.view(user)                                                         # 建立錢包並補發贈幣
    kit.wallet.adjust(user.id, bucket="free", delta=-180)                          # 剩 20 幣，不足一輪的 30
    with pytest.raises(InsufficientCoins) as caught:
        begin(kit, user, save)
    assert (caught.value.balance, caught.value.needed) == (20, 30)
    assert turn_rows(kit, save) == [] and kit.total(user) == 20


def test_insufficient_coins_on_a_resend_restores_the_interrupted_turn_untouched(kit):
    user = kit.player()
    save = kit.open_save(user)
    begin(kit, user, save)
    kit.sessions.interrupt_turn(user.id, save.public_id, "m-1", partial_response="半截", attempt=1)
    kit.wallet.refund(user, hold_key(save.public_id, "m-1", 1))
    kit.wallet.adjust(user.id, bucket="free", delta=-190)                          # 剩 10
    with pytest.raises(InsufficientCoins):
        begin(kit, user, save)
    (row,) = turn_rows(kit, save)
    assert (row.turn_status, row.attempt, row.llm_raw_response) == (TURN_INTERRUPTED, 1, "半截")


def test_the_price_comes_from_the_settings_table_each_time(kit):
    user = kit.player()
    save = kit.open_save(user)
    kit.set_limit("coin.turn_cost.standard", 45)
    assert begin(kit, user, save).plan.cost == 45
    assert kit.total(user) == 200 - 45


# ---- 重送、接手與單一進行中 -------------------------------------------------------------------

def test_resending_a_completed_turn_replays_it_without_touching_the_wallet(kit):
    user = kit.player()
    save = kit.open_save(user)
    begin(kit, user, save)
    finish(kit, user, save, story="完整的劇情。")
    before = kit.total(user)
    result = begin(kit, user, save)
    assert result.plan is None and result.replay.story == "完整的劇情。"
    assert (result.replay.turn_number, result.replay.attempt, result.replay.ended) == (1, 1, False)
    assert kit.total(user) == before and len(turn_rows(kit, save)) == 1


def test_a_completed_turn_can_still_be_replayed_after_the_session_ended(kit):
    user = kit.player()
    save = kit.open_save(user)
    begin(kit, user, save)
    finish(kit, user, save, story="結局的劇情。")
    kit.sessions.end_session(user.id, save.public_id, now=kit.clock())
    result = begin(kit, user, save)
    assert result.replay.story == "結局的劇情。" and result.replay.ended is True
    with pytest.raises(SessionEnded):
        begin(kit, user, save, msg="m-2")


def test_a_replay_does_not_need_the_budget_the_opening_or_the_rules(kit):
    user = kit.player()
    save = kit.open_save(user)
    begin(kit, user, save)
    finish(kit, user, save)
    kit.budget_exhausted = True
    assert begin(kit, user, save).replay is not None


def test_resending_an_interrupted_turn_bumps_the_attempt_and_holds_again_once(kit):
    user = kit.player()
    save = kit.open_save(user)
    begin(kit, user, save)
    kit.sessions.interrupt_turn(user.id, save.public_id, "m-1", partial_response="半截", attempt=1)
    kit.wallet.refund(user, hold_key(save.public_id, "m-1", 1))
    assert kit.total(user) == 200
    plan = begin(kit, user, save).plan
    assert (plan.attempt, plan.start) == (2, rpg_events.START_RESTARTED)
    assert plan.hold_key.endswith(":m-1:2") and kit.total(user) == 170
    (row,) = turn_rows(kit, save)
    assert (row.turn_status, row.attempt, row.llm_raw_response) == (TURN_STREAMING, 2, "")


def test_a_streaming_turn_that_is_not_stale_is_turn_in_progress(kit):
    user = kit.player()
    save = kit.open_save(user)
    begin(kit, user, save)
    with pytest.raises(Conflict) as caught:
        begin(kit, user, save)
    assert caught.value.code == "turn_in_progress"
    assert kit.total(user) == 170 and len(turn_rows(kit, save)) == 1


def test_only_one_unfinished_turn_per_save_even_with_another_client_msg_id(kit):
    user = kit.player()
    save = kit.open_save(user)
    begin(kit, user, save, msg="m-1")
    with pytest.raises(Conflict) as caught:
        begin(kit, user, save, msg="m-2")
    assert caught.value.code == "turn_in_progress"
    assert len(turn_rows(kit, save)) == 1 and kit.total(user) == 170


def test_a_stale_streaming_turn_is_taken_over_and_the_player_pays_for_one_turn_only(kit):
    user = kit.player()
    save = kit.open_save(user)
    begin(kit, user, save)
    assert kit.total(user) == 170
    kit.clock.advance(seconds=kit.limits.coin_hold_timeout() + 1)                 # worker 重啟：舊的預扣與回合同時逾時
    plan = begin(kit, user, save).plan
    assert (plan.attempt, plan.start) == (2, rpg_events.START_RESTARTED)
    assert kit.total(user) == 170                                                 # 舊預扣被退回，新預扣再扣 30
    (row,) = turn_rows(kit, save)
    assert (row.turn_status, row.attempt) == (TURN_STREAMING, 2)


def test_a_stale_turn_with_another_id_is_interrupted_and_a_new_turn_starts(kit):
    user = kit.player()
    save = kit.open_save(user)
    begin(kit, user, save, msg="m-1")
    kit.clock.advance(seconds=kit.limits.coin_hold_timeout() + 1)
    plan = begin(kit, user, save, msg="m-2").plan
    assert plan.seq == 2 and plan.turn_number == 1
    assert [row.turn_status for row in turn_rows(kit, save)] == [TURN_INTERRUPTED, TURN_STREAMING]


def test_an_ended_session_rejects_a_new_turn(kit):
    user = kit.player()
    save = kit.open_save(user)
    kit.sessions.end_session(user.id, save.public_id, now=kit.clock())
    with pytest.raises(SessionEnded):
        begin(kit, user, save)
    assert turn_rows(kit, save) == [] and kit.total(user) == 0


def test_the_started_event_has_only_whitelisted_fields(kit, caplog):
    import logging
    user = kit.player()
    save = kit.open_save(user)
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        begin(kit, user, save)
    (record,) = [item for item in caplog.records if getattr(item, "event", None) == rpg_events.EVENT_TURN_STARTED]
    assert (record.user_id, record.session_id, record.turn_number, record.attempt) == (user.id, save.id, 1, 1)
    text = str(record.__dict__)
    assert ACTION not in text and save.public_id not in text and MARKER not in text and "m-1" not in text
