"""rpg-turn-revision 規格「開始前」的每個情境：`GameService.begin_revision`（驗證、擁有者、`revision`、可修訂、認領、免費與付費預扣、回饋列、
`RevisionPlan` 的內容），以及回合開始時保存回合前快照、`last_turn`／`turn_view`／`done` 帶 `revision`。

以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行。
"""
import dataclasses
import json
import logging

import pytest
from sqlalchemy import select

from app.models import MemoryLog, TurnRegeneration
from app.models.rpg_session import REGEN_CATEGORIES, REGEN_FAILED, REGEN_KIND_EDIT, REGEN_KIND_REGENERATE, REGEN_PENDING
from app.services.errors import (
    AgeConfirmationRequired, Conflict, InputBlocked, InsufficientCoins, NotFound, NotRevisable, RevisionConflict, RevisionInvalid, SystemBusy,
    ValidationError,
)
from app.services.game_service import (
    CATEGORIES, KIND_EDIT, KIND_REGENERATE, REASON_MAX, REASON_MIN, RevisionPlan, free_left_of, hold_key, world_from_snapshot, world_of,
)
from tests.support.game import MARKER, GameKit

ACTION = "我走進山門，向守門的弟子行禮。"
NEW_ACTION = "我繞到後山，沿著小徑往上走。"
REASON = "劇情和設定對不上，想換個寫法"
BAD_ACTION = "十歲的少女與他做愛。"                      # 規則層必擋（minor_sexual）
GHOST = "00000000-0000-4000-8000-000000000000"


@pytest.fixture()
def kit(member_session):
    return GameKit(member_session)


# ---- 輔助 -------------------------------------------------------------------------------------

def make_turn(kit, user, save, msg="m-1", action=ACTION, story="第一版劇情。"):
    """走完一個回合的開始階段並把它標為完成、結算預扣（模擬 `TurnRunner` 的收尾）；回傳 `TurnPlan`。"""
    plan = kit.game.begin_turn(user, save.public_id, client_msg_id=msg, action=action).plan
    assert plan is not None
    kit.sessions.complete_turn(user.id, save.public_id, msg, llm_raw_response=story, structured_result=None, now=kit.clock(), attempt=plan.attempt)
    kit.wallet.settle(user, plan.hold_key)
    return plan


def regenerate(kit, user, save, msg="m-1", *, revision=1, category="other", reason=REASON):
    return kit.game.begin_revision(
        user, save.public_id, msg, kind=KIND_REGENERATE, expected_revision=revision, category=category, reason=reason,
    )


def edit(kit, user, save, msg="m-1", *, revision=1, action=NEW_ACTION):
    return kit.game.begin_revision(user, save.public_id, msg, kind=KIND_EDIT, expected_revision=revision, action=action)


def succeed(kit, user, save, plan, story="新的劇情。"):
    """模擬 `RevisionRunner` 的成功收尾：替換內容（編輯時一併換行動並使免費次數歸零）並結算預扣。"""
    is_edit = plan.kind == KIND_EDIT
    kit.sessions.replace_turn(
        user.id, save.public_id, plan.client_msg_id, expected_revision=plan.expected_revision, attempt=plan.attempt, llm_raw_response=story,
        structured_result=None, now=kit.clock(), user_action=plan.action if is_edit else None, reset_regen_count=is_edit,
    )
    if plan.hold_key is not None:
        kit.wallet.settle(user, plan.hold_key)


def fail(kit, user, save, plan):
    """模擬 `RevisionRunner` 的失敗收尾：釋放認領、退回預扣。"""
    kit.sessions.release_revision(user.id, save.public_id, plan.client_msg_id, attempt=plan.attempt)
    if plan.hold_key is not None:
        kit.wallet.refund(user, plan.hold_key)


def turn_row(kit, save, msg="m-1"):
    kit.session.expire_all()
    return kit.session.scalar(select(MemoryLog).where(MemoryLog.session_id == save.id, MemoryLog.client_msg_id == msg))


def feedback(kit, save):
    kit.session.expire_all()
    return list(kit.session.scalars(select(TurnRegeneration).where(TurnRegeneration.session_id == save.id).order_by(TurnRegeneration.id)))


def assert_untouched(kit, user, save, total_before, msg="m-1"):
    """失敗的開始階段什麼都沒留下：沒有認領、嘗試序號不變、沒有回饋列、餘額不變。"""
    row = turn_row(kit, save, msg)
    assert row.revising_started_at is None and row.attempt == 1 and row.revision == 1 and row.regen_count == 0
    assert feedback(kit, save) == [] and kit.total(user) == total_before


def ready(kit):
    """一個玩家、一個存檔、一個已完成並有快照的第 1 輪；回傳（玩家, 存檔, 餘額）。"""
    user = kit.player()
    save = kit.open_save(user)
    make_turn(kit, user, save)
    return user, save, kit.total(user)


# ---- 擁有者與回合：404 --------------------------------------------------------------------------

def test_a_non_owner_gets_the_same_404_as_a_missing_session_and_nothing_is_claimed(kit):
    owner, save, total = ready(kit)
    stranger = kit.player(2)
    with pytest.raises(NotFound):
        regenerate(kit, stranger, save)
    with pytest.raises(NotFound):
        edit(kit, stranger, save)
    with pytest.raises(NotFound):
        kit.game.begin_revision(owner, GHOST, "m-1", kind=KIND_REGENERATE, expected_revision=1, category="other", reason=REASON)
    assert_untouched(kit, owner, save, total)
    assert kit.total(stranger) == 0                                                    # 陌生人的錢包連建立都不需要


def test_an_unknown_client_msg_id_is_a_404(kit):
    user, save, total = ready(kit)
    with pytest.raises(NotFound):
        regenerate(kit, user, save, msg="no-such-turn")
    assert_untouched(kit, user, save, total)


def test_an_unconfirmed_account_cannot_revise(kit):
    user, save, total = ready(kit)
    unconfirmed = kit.player(2, age=None)
    with pytest.raises(AgeConfirmationRequired):
        regenerate(kit, unconfirmed, save)
    assert_untouched(kit, user, save, total)


# ---- 驗證本文 ----------------------------------------------------------------------------------

@pytest.mark.parametrize("value", [0, -1, "1", None, True, 1.0])
def test_expected_revision_must_be_a_positive_integer(kit, value):
    user, save, total = ready(kit)
    with pytest.raises(ValidationError) as caught:
        regenerate(kit, user, save, revision=value)
    assert set(caught.value.fields) == {"expected_revision"}
    assert_untouched(kit, user, save, total)


@pytest.mark.parametrize("value", ["", None, 5, "x" * 65])
def test_the_client_msg_id_is_validated(kit, value):
    user, save, total = ready(kit)
    with pytest.raises(ValidationError) as caught:
        regenerate(kit, user, save, msg=value)
    assert set(caught.value.fields) == {"client_msg_id"}
    assert_untouched(kit, user, save, total)


@pytest.mark.parametrize("reason", ["太短", "四個字啊", "", "   ", None, 12345, "a\x00b\x01c\x02d"])         # 最後一個去除控制字元後只有 4 字
def test_a_reason_below_the_minimum_after_cleaning_is_a_422_naming_the_field(kit, reason):
    user, save, total = ready(kit)
    with pytest.raises(RevisionInvalid) as caught:
        regenerate(kit, user, save, reason=reason)
    assert set(caught.value.fields) == {"reason"} and caught.value.categories == ()
    assert_untouched(kit, user, save, total)


def test_a_reason_over_the_maximum_is_rejected_and_the_boundaries_are_accepted(kit):
    user, save, total = ready(kit)
    with pytest.raises(RevisionInvalid) as caught:
        regenerate(kit, user, save, reason="字" * (REASON_MAX + 1))
    assert set(caught.value.fields) == {"reason"}
    assert_untouched(kit, user, save, total)
    shortest = regenerate(kit, user, save, reason="字" * REASON_MIN)
    fail(kit, user, save, shortest)
    longest = regenerate(kit, user, save, reason="字" * REASON_MAX)
    fail(kit, user, save, longest)
    assert [row.reason for row in feedback(kit, save)] == ["字" * REASON_MIN, "字" * REASON_MAX]


def test_the_reason_bounds_are_five_and_fifty_characters(kit):
    user, save, total = ready(kit)
    assert (REASON_MIN, REASON_MAX) == (5, 50)
    with pytest.raises(RevisionInvalid):
        regenerate(kit, user, save, reason="字" * 51)
    assert_untouched(kit, user, save, total)
    plan = regenerate(kit, user, save, reason="字" * 50)
    assert len(feedback(kit, save)[0].reason) == 50
    fail(kit, user, save, plan)


def test_the_reason_is_counted_after_nfc_and_stored_cleaned(kit):
    user, save, _ = ready(kit)
    decomposed = "é" * 5                                                         # 組合字：NFC 之後是 5 個字元（原本 10 個）
    plan = regenerate(kit, user, save, reason=decomposed)
    (row,) = feedback(kit, save)
    assert row.reason == "é" * 5 and plan.attempt == 2
    fail(kit, user, save, plan)
    with pytest.raises(RevisionInvalid):
        regenerate(kit, user, save, reason="é" * 4 + "\x00")                    # 去除控制字元後 4 字


@pytest.mark.parametrize("category", ["", "unknown", None, 3, "Other", "other "])
def test_a_category_outside_the_enumeration_is_a_422_naming_the_field(kit, category):
    user, save, total = ready(kit)
    with pytest.raises(RevisionInvalid) as caught:
        regenerate(kit, user, save, category=category)
    assert set(caught.value.fields) == {"category"}
    assert_untouched(kit, user, save, total)


def test_both_invalid_fields_are_reported_together_without_echoing_the_input(kit):
    user, save, total = ready(kit)
    with pytest.raises(RevisionInvalid) as caught:
        regenerate(kit, user, save, category="nope", reason="短")
    assert set(caught.value.fields) == {"category", "reason"}
    assert "nope" not in json.dumps(caught.value.fields, ensure_ascii=False)
    assert_untouched(kit, user, save, total)


def test_every_listed_category_is_accepted(kit):
    user, save, _ = ready(kit)
    kit.set_limit("rpg.free_regenerations", 20)
    assert CATEGORIES == REGEN_CATEGORIES and len(CATEGORIES) == 5
    for index, category in enumerate(CATEGORIES):
        plan = regenerate(kit, user, save, revision=1 + index, category=category)
        succeed(kit, user, save, plan)
    assert [row.category for row in feedback(kit, save)] == list(CATEGORIES)


def test_an_edit_needs_no_reason_or_category_and_ignores_them(kit):
    user, save, _ = ready(kit)
    plan = kit.game.begin_revision(
        user, save.public_id, "m-1", kind=KIND_EDIT, expected_revision=1, action=NEW_ACTION, category="not-checked", reason="x",
    )
    (row,) = feedback(kit, save)
    assert (row.kind, row.category, row.reason) == (REGEN_KIND_EDIT, None, None) and plan.kind == KIND_EDIT


@pytest.mark.parametrize("action", ["", "   ", None, 5, "字" * 1001])
def test_an_edit_goes_through_the_same_cleaning_and_length_checks_as_a_new_action(kit, action):
    user, save, total = ready(kit)
    with pytest.raises(ValidationError) as caught:
        edit(kit, user, save, action=action)
    assert set(caught.value.fields) == {"action"}
    assert_untouched(kit, user, save, total)


def test_an_edit_equal_to_the_original_action_is_a_422_without_a_claim_or_a_hold(kit):
    user, save, total = ready(kit)
    with pytest.raises(RevisionInvalid) as caught:
        edit(kit, user, save, action=ACTION)
    assert set(caught.value.fields) == {"action"}
    with pytest.raises(RevisionInvalid):
        edit(kit, user, save, action="  " + ACTION + "\x00 ")                       # 清理之後相同
    assert_untouched(kit, user, save, total)


def test_an_edit_that_hits_a_must_refuse_category_is_input_blocked_with_categories_only(kit):
    user, save, total = ready(kit)
    with pytest.raises(InputBlocked) as caught:
        edit(kit, user, save, action=BAD_ACTION)
    assert caught.value.categories and BAD_ACTION not in str(caught.value)
    assert_untouched(kit, user, save, total)


def test_the_unknown_kind_is_a_programming_error(kit):
    user, save, _ = ready(kit)
    with pytest.raises(ValueError):
        kit.game.begin_revision(user, save.public_id, "m-1", kind="reroll", expected_revision=1)


# ---- revision 與可修訂 -------------------------------------------------------------------------

def test_a_stale_expected_revision_is_a_revision_conflict_and_changes_nothing(kit):
    user, save, total = ready(kit)
    with pytest.raises(RevisionConflict) as caught:
        regenerate(kit, user, save, revision=2)
    assert isinstance(caught.value, Conflict) and caught.value.code == "revision_conflict"
    assert_untouched(kit, user, save, total)


def test_a_device_that_lost_the_race_gets_a_revision_conflict_after_the_other_finished(kit):
    user, save, _ = ready(kit)
    succeed(kit, user, save, regenerate(kit, user, save))                              # 裝置甲完成（revision 變 2）
    with pytest.raises(RevisionConflict):
        regenerate(kit, user, save, revision=1)                                        # 裝置乙還以為是 1
    assert turn_row(kit, save).revision == 2


def test_only_the_last_complete_turn_can_be_revised(kit):
    user, save, _ = ready(kit)
    make_turn(kit, user, save, msg="m-2", action="第二個行動。")
    total = kit.total(user)
    with pytest.raises(NotRevisable) as caught:
        regenerate(kit, user, save, msg="m-1")
    assert (caught.value.code, caught.value.reason) == ("not_revisable", "not_last")
    assert turn_row(kit, save, "m-1").revising_started_at is None and feedback(kit, save) == [] and kit.total(user) == total


def test_a_finished_session_is_not_revisable_even_for_the_last_turn(kit):
    user, save, total = ready(kit)
    kit.sessions.end_session(user.id, save.public_id, now=kit.clock())
    for call in (regenerate, edit):
        with pytest.raises(NotRevisable) as caught:
            call(kit, user, save)
        assert caught.value.reason == "ended"
    assert turn_row(kit, save).revising_started_at is None and feedback(kit, save) == [] and kit.total(user) == total


def test_an_older_turn_of_a_finished_session_is_reported_as_ended_not_as_not_last(kit):
    user, save, _ = ready(kit)
    make_turn(kit, user, save, msg="m-2", action="第二個行動。")
    kit.sessions.end_session(user.id, save.public_id, now=kit.clock())
    with pytest.raises(NotRevisable) as caught:
        regenerate(kit, user, save, msg="m-1")
    assert caught.value.reason == "ended"


def test_a_turn_without_a_snapshot_is_not_revisable(kit):
    user = kit.player()
    save = kit.open_save(user)
    kit.sessions.start_turn(user.id, save.public_id, client_msg_id="old", user_action=ACTION, now=kit.clock())          # 遷移之前的回合：沒有快照
    kit.sessions.complete_turn(user.id, save.public_id, "old", llm_raw_response="舊劇情。", structured_result=None, now=kit.clock())
    with pytest.raises(NotRevisable) as caught:
        regenerate(kit, user, save, msg="old")
    assert caught.value.reason == "no_snapshot"
    assert turn_row(kit, save, "old").revising_started_at is None and feedback(kit, save) == []


def test_the_next_turn_being_generated_makes_the_last_one_not_revisable(kit):
    user, save, _ = ready(kit)
    kit.game.begin_turn(user, save.public_id, client_msg_id="m-2", action="第二個行動。")             # 生成中且未逾時
    total = kit.total(user)
    with pytest.raises(NotRevisable) as caught:
        regenerate(kit, user, save)
    assert caught.value.reason == "in_progress"
    assert turn_row(kit, save).revising_started_at is None and feedback(kit, save) == [] and kit.total(user) == total


def test_a_blocked_or_interrupted_next_attempt_does_not_stop_the_previous_turn_from_being_revised(kit):
    user, save, _ = ready(kit)
    kit.game.begin_turn(user, save.public_id, client_msg_id="m-2", action="第二個行動。")
    kit.sessions.interrupt_turn(user.id, save.public_id, "m-2", partial_response="半截", attempt=1)
    kit.game.begin_turn(user, save.public_id, client_msg_id="m-3", action="第三個行動。")
    kit.sessions.block_turn(user.id, save.public_id, "m-3", partial_response="", attempt=1)
    plan = regenerate(kit, user, save)
    assert plan.client_msg_id == "m-1" and plan.attempt == 2 and plan.turn_number == 1


def test_a_stale_streaming_turn_does_not_block_revision(kit):
    user, save, _ = ready(kit)
    kit.game.begin_turn(user, save.public_id, client_msg_id="m-2", action="第二個行動。")
    kit.clock.advance(seconds=kit.limits.coin_hold_timeout() + 1)                      # 生成中的回合逾時，視同中斷
    assert regenerate(kit, user, save).attempt == 2


# ---- 認領：並行、逾時接手與新回合 ---------------------------------------------------------------

def test_a_second_request_while_a_claim_is_alive_is_turn_in_progress_with_a_single_hold(kit):
    user, save, _ = ready(kit)
    first = edit(kit, user, save)                                                       # 付費：預扣一筆
    total = kit.total(user)
    with pytest.raises(Conflict) as caught:
        regenerate(kit, user, save)
    assert caught.value.code == "turn_in_progress" and not isinstance(caught.value, NotRevisable)
    with pytest.raises(Conflict) as caught:
        edit(kit, user, save, action="另一個不同的行動。")
    assert caught.value.code == "turn_in_progress" and not isinstance(caught.value, NotRevisable)
    assert kit.total(user) == total and len(feedback(kit, save)) == 1
    assert turn_row(kit, save).attempt == first.attempt


def test_a_new_turn_while_a_revision_claim_is_alive_is_turn_in_progress_and_leaves_no_row(kit):
    user, save, _ = ready(kit)
    regenerate(kit, user, save)
    total = kit.total(user)
    with pytest.raises(Conflict) as caught:
        kit.game.begin_turn(user, save.public_id, client_msg_id="m-2", action="第二個行動。")
    assert caught.value.code == "turn_in_progress"
    assert turn_row(kit, save, "m-2") is None and kit.total(user) == total


def test_once_the_claim_has_expired_a_new_turn_can_start(kit):
    user, save, _ = ready(kit)
    regenerate(kit, user, save)
    kit.clock.advance(seconds=kit.limits.coin_hold_timeout() + 1)
    assert kit.game.begin_turn(user, save.public_id, client_msg_id="m-2", action="第二個行動。").plan is not None


def test_an_expired_claim_is_taken_over_with_a_newer_attempt_and_the_old_feedback_row_fails(kit):
    user, save, _ = ready(kit)
    old = edit(kit, user, save)
    kit.clock.advance(seconds=kit.limits.coin_hold_timeout() + 1)
    new = regenerate(kit, user, save)
    assert new.taken_over is True and new.attempt == old.attempt + 1
    first, second = feedback(kit, save)
    assert (first.attempt, first.result) == (old.attempt, REGEN_FAILED) and first.reason is None          # 編輯沒有理由
    assert (second.attempt, second.result, second.reason) == (new.attempt, REGEN_PENDING, REASON)
    assert old.hold_key != new.hold_key


def test_the_old_executors_replace_fails_after_a_takeover(kit):
    from app.repositories.errors import InvalidTurnTransitionError

    user, save, _ = ready(kit)
    old = regenerate(kit, user, save)
    kit.clock.advance(seconds=kit.limits.coin_hold_timeout() + 1)
    regenerate(kit, user, save)
    with pytest.raises(InvalidTurnTransitionError):
        succeed(kit, user, save, old)
    assert turn_row(kit, save).llm_raw_response == "第一版劇情。" and turn_row(kit, save).revision == 1


def test_the_claim_timeout_is_read_from_the_settings_table_each_time(kit):
    user, save, _ = ready(kit)
    regenerate(kit, user, save)
    kit.set_limit("coin.hold_timeout_seconds", 60)                                      # 下限
    kit.clock.advance(seconds=61)
    assert regenerate(kit, user, save).taken_over is True


# ---- 費用：免費次數、付費預扣與鍵 ---------------------------------------------------------------

def test_the_first_two_regenerations_are_free_and_hold_nothing(kit):
    user, save, total = ready(kit)
    for expected in (1, 2):
        plan = regenerate(kit, user, save, revision=expected)
        assert (plan.is_free, plan.hold_key, plan.cost, plan.free_limit) == (True, None, 0, 2)
        assert kit.total(user) == total
        succeed(kit, user, save, plan)
    assert [row.is_free for row in feedback(kit, save)] == [True, True]
    assert kit.wallets.entry(user.id, hold_key(save.public_id, "m-1", 2)) is None                       # 帳本沒有修訂的預扣


def test_the_third_regeneration_is_paid_with_a_key_that_includes_the_new_attempt(kit):
    user, save, total = ready(kit)
    for revision in (1, 2):
        succeed(kit, user, save, regenerate(kit, user, save, revision=revision))
    plan = regenerate(kit, user, save, revision=3)
    assert (plan.is_free, plan.cost, plan.attempt) == (False, 30, 4)
    assert plan.hold_key == hold_key(save.public_id, "m-1", 4) != hold_key(save.public_id, "m-1", 1)
    assert kit.total(user) == total - 30
    assert feedback(kit, save)[-1].is_free is False


def test_a_failed_paid_revision_refunds_and_the_next_one_uses_a_new_key_and_nets_one_charge(kit):
    user, save, total = ready(kit)
    kit.set_limit("rpg.free_regenerations", 0)
    first = regenerate(kit, user, save)
    fail(kit, user, save, first)
    assert kit.total(user) == total
    second = regenerate(kit, user, save)
    assert second.hold_key != first.hold_key and second.attempt == first.attempt + 1
    assert kit.total(user) == total - 30
    succeed(kit, user, save, second)
    assert kit.total(user) == total - 30


def test_an_edit_is_always_paid_even_with_free_regenerations_left_and_resets_the_free_count(kit):
    user, save, total = ready(kit)
    succeed(kit, user, save, regenerate(kit, user, save, revision=1))
    succeed(kit, user, save, regenerate(kit, user, save, revision=2))
    assert free_left_of(2, turn_row(kit, save).regen_count) == 0
    plan = edit(kit, user, save, revision=3)
    assert (plan.is_free, plan.cost, plan.kind) == (False, 30, KIND_EDIT) and kit.total(user) == total - 30
    succeed(kit, user, save, plan)
    row = turn_row(kit, save)
    assert (row.revision, row.regen_count, row.user_action) == (4, 0, NEW_ACTION)
    again = regenerate(kit, user, save, revision=4)
    assert again.is_free is True and again.regen_count == 0                             # 編輯之後又有 N 次免費


def test_a_zero_free_setting_makes_every_regeneration_paid_and_the_setting_is_read_each_time(kit):
    user, save, total = ready(kit)
    kit.set_limit("rpg.free_regenerations", 0)
    plan = regenerate(kit, user, save)
    assert plan.is_free is False and plan.free_limit == 0
    fail(kit, user, save, plan)
    kit.set_limit("rpg.free_regenerations", 1)
    assert regenerate(kit, user, save).is_free is True


def test_insufficient_coins_for_a_paid_revision_leaves_no_claim_and_no_feedback_row(kit):
    user, save, _ = ready(kit)
    kit.wallet.adjust(user.id, bucket="free", delta=-(kit.total(user) - 20))            # 剩 20 幣，不足 30
    with pytest.raises(InsufficientCoins) as caught:
        edit(kit, user, save)
    assert (caught.value.balance, caught.value.needed) == (20, 30)
    kit.set_limit("rpg.free_regenerations", 0)
    with pytest.raises(InsufficientCoins):
        regenerate(kit, user, save)
    assert_untouched(kit, user, save, 20)


def test_a_free_regeneration_needs_no_balance_at_all(kit):
    user, save, _ = ready(kit)
    kit.wallet.adjust(user.id, bucket="free", delta=-kit.total(user))
    plan = regenerate(kit, user, save)
    assert plan.is_free is True and kit.total(user) == 0


def test_the_global_token_budget_is_checked_before_anything_is_claimed(kit):
    user, save, total = ready(kit)
    kit.budget_exhausted = True
    for call in (regenerate, edit):
        with pytest.raises(SystemBusy):
            call(kit, user, save)
    assert_untouched(kit, user, save, total)


# ---- 計畫的內容 ---------------------------------------------------------------------------------

def test_the_plan_uses_the_snapshot_world_the_history_without_the_target_and_the_original_turn_number(kit):
    user = kit.player()
    save = kit.open_save(user)
    start_world = world_of(save).to_fields()
    make_turn(kit, user, save, msg="m-1", action="第一個行動。", story="第一輪的劇情。")
    snapshot_two = world_of(save).to_fields()
    plan_two = make_turn(kit, user, save, msg="m-2", action=ACTION, story="第二輪的劇情。")
    kit.session.expire_all()
    kit.sessions.update_world_state(user.id, save.public_id, expected_version=save.version, current_location="第二輪造成的新地點", elapsed_months=6)
    kit.session.expire_all()
    assert world_of(save).current_location == "第二輪造成的新地點"

    plan = regenerate(kit, user, save, msg="m-2")
    assert isinstance(plan, RevisionPlan) and plan.turn_number == 2 == plan_two.turn_number
    assert plan.world == world_from_snapshot(snapshot_two) and plan.world.current_location != "第二輪造成的新地點"
    assert plan.world.elapsed_months == 0 and start_world["elapsed_months"] == 0
    assert [turn.turn_number for turn in plan.history] == [1] and plan.history[0].narration == "第一輪的劇情。"
    assert plan.version == save.version and plan.version > plan_two.version             # 寫回時以認領當下的存檔版本比對
    assert (plan.seq, plan.client_msg_id, plan.expected_revision, plan.regen_count, plan.action) == (2, "m-2", 1, 0, ACTION)
    assert (plan.user_id, plan.session_id, plan.session_public_id, plan.tier, plan.taken_over) == (user.id, save.id, save.public_id, "safe", False)
    assert plan.ending_due is False and plan.max_months == kit.limits.rpg_max_months_per_turn() and plan.characters
    assert MARKER in plan.system_instruction


def test_a_regeneration_keeps_the_original_action_and_an_edit_uses_the_new_one_in_the_context(kit):
    user, save, _ = ready(kit)
    same = regenerate(kit, user, save)
    assert same.action == ACTION and ACTION in same.contents[-1].parts[-1]
    fail(kit, user, save, same)
    changed = edit(kit, user, save)
    assert changed.action == NEW_ACTION and NEW_ACTION in changed.contents[-1].parts[-1]
    assert ACTION not in "".join(part for content in changed.contents for part in content.parts)
    assert turn_row(kit, save).user_action == ACTION                                    # 資料庫裡的行動要到替換成功才改


def test_the_first_turn_context_keeps_the_opening_scene_and_a_late_turn_does_not_duplicate_the_target(kit):
    user, save, _ = ready(kit)
    plan = regenerate(kit, user, save)
    assert plan.history == () and "開場的劇情。" in "".join(part for content in plan.contents for part in content.parts)
    assert "第一版劇情。" not in "".join(part for content in plan.contents for part in content.parts)     # 目標回合自己的舊劇情不在上下文


def test_the_reason_and_category_are_not_in_the_plan_at_all_and_never_reach_the_model(kit):
    user, save, _ = ready(kit)
    reason = "獨特理由甲乙丙丁戊"
    plan = regenerate(kit, user, save, category="repetitive_or_dull", reason=reason)
    names = {field.name for field in dataclasses.fields(RevisionPlan)}
    assert not names & {"reason", "category", "reason_text"}
    everything = repr(plan) + plan.system_instruction + "".join(part for content in plan.contents for part in content.parts)
    assert reason not in everything and "repetitive_or_dull" not in everything
    (row,) = feedback(kit, save)
    assert (row.reason, row.category, row.kind) == (reason, "repetitive_or_dull", REGEN_KIND_REGENERATE)


def test_the_claim_and_feedback_row_are_written_in_one_step(kit):
    user, save, _ = ready(kit)
    plan = regenerate(kit, user, save)
    row = turn_row(kit, save)
    assert row.revising_started_at is not None and row.attempt == plan.attempt == 2 and row.revision == 1
    (feedback_row,) = feedback(kit, save)
    assert (feedback_row.turn_seq, feedback_row.attempt, feedback_row.result, feedback_row.is_free) == (plan.seq, plan.attempt, REGEN_PENDING, True)


# ---- 日誌 --------------------------------------------------------------------------------------

def test_the_logs_of_a_revision_never_contain_the_reason_the_action_or_the_story(kit, caplog):
    user = kit.player()
    save = kit.open_save(user)
    reason, new_action = "獨特理由只該在回饋表裡", "獨特的新行動只該在請求裡"
    with caplog.at_level(logging.DEBUG):
        make_turn(kit, user, save, action="獨特的舊行動", story="獨特的舊劇情。")
        plan = regenerate(kit, user, save, reason=reason)
        fail(kit, user, save, plan)
        edit(kit, user, save, action=new_action)
    blob = "\n".join(record.getMessage() + " " + json.dumps(record.__dict__, default=str, ensure_ascii=False) for record in caplog.records)
    for secret in (reason, new_action, "獨特的舊行動", "獨特的舊劇情", "repetitive", "other"):
        assert secret not in blob, secret
    started = [record for record in caplog.records if getattr(record, "event", "") == "rpg.revision_started"]
    assert [(record.kind, record.outcome, record.coins) for record in started] == [(REGEN_KIND_REGENERATE, "free", 0), (REGEN_KIND_EDIT, "paid", 30)]


# ---- 回合前快照（begin_turn）--------------------------------------------------------------------

def test_a_new_turn_stores_the_world_at_that_moment_as_its_snapshot_and_the_plan_uses_the_same_world(kit):
    user = kit.player()
    save = kit.open_save(user)
    before = world_of(save).to_fields()
    plan = kit.game.begin_turn(user, save.public_id, client_msg_id="m-1", action=ACTION).plan
    row = turn_row(kit, save)
    assert row.state_before_json == before and plan.world == world_from_snapshot(row.state_before_json)


def test_the_snapshot_is_the_world_after_earlier_turns_and_the_next_completion_clears_the_previous_one(kit):
    user = kit.player()
    save = kit.open_save(user)
    make_turn(kit, user, save, msg="m-1")
    kit.sessions.update_world_state(user.id, save.public_id, expected_version=save.version, current_location="山莊", status_summary="抵達山莊。")
    kit.session.expire_all()
    plan = kit.game.begin_turn(user, save.public_id, client_msg_id="m-2", action="第二個行動。").plan
    row = turn_row(kit, save, "m-2")
    assert row.state_before_json["current_location"] == "山莊" and plan.world.current_location == "山莊"
    assert turn_row(kit, save, "m-1").state_before_json is not None                    # 第 2 輪還沒完成：第 1 輪仍可修訂
    kit.sessions.complete_turn(user.id, save.public_id, "m-2", llm_raw_response="劇情。", structured_result=None, now=kit.clock(), attempt=1)
    assert turn_row(kit, save, "m-1").state_before_json is None and turn_row(kit, save, "m-2").state_before_json is not None


def test_resending_an_interrupted_turn_overwrites_the_snapshot_with_the_current_world(kit):
    user = kit.player()
    save = kit.open_save(user)
    kit.game.begin_turn(user, save.public_id, client_msg_id="m-1", action=ACTION)
    first = turn_row(kit, save).state_before_json
    kit.sessions.interrupt_turn(user.id, save.public_id, "m-1", partial_response="半截", attempt=1)
    kit.wallet.refund(user, hold_key(save.public_id, "m-1", 1))
    kit.session.expire_all()
    kit.sessions.update_world_state(user.id, save.public_id, expected_version=save.version, current_location="別處")
    kit.session.expire_all()
    plan = kit.game.begin_turn(user, save.public_id, client_msg_id="m-1", action=ACTION).plan
    assert turn_row(kit, save).state_before_json != first and turn_row(kit, save).state_before_json["current_location"] == "別處"
    assert plan.attempt == 2 and plan.world.current_location == "別處"


def test_replaying_a_completed_turn_does_not_touch_its_snapshot(kit):
    user, save, _ = ready(kit)
    snapshot = turn_row(kit, save).state_before_json
    assert kit.game.begin_turn(user, save.public_id, client_msg_id="m-1", action=ACTION).replay is not None
    assert turn_row(kit, save).state_before_json == snapshot


# ---- 玩家端的資料：revision、free_left、last_turn ------------------------------------------------

LAST_TURN_KEYS = {"client_msg_id", "seq", "revision", "revisable", "reason", "free_left", "regenerate_cost", "edit_cost"}


def test_a_new_plan_carries_the_revision_and_the_configured_free_count_for_the_done_event(kit):
    user = kit.player()
    save = kit.open_save(user)
    plan = kit.game.begin_turn(user, save.public_id, client_msg_id="m-1", action=ACTION).plan
    assert (plan.revision, plan.free_left) == (1, 2)
    kit.sessions.interrupt_turn(user.id, save.public_id, "m-1", partial_response="", attempt=1)
    kit.wallet.refund(user, plan.hold_key)
    kit.set_limit("rpg.free_regenerations", 5)
    again = kit.game.begin_turn(user, save.public_id, client_msg_id="m-1", action=ACTION).plan
    assert (again.revision, again.free_left) == (1, 5)


def test_a_replay_carries_the_turns_current_revision_and_remaining_free_count(kit):
    user, save, _ = ready(kit)
    first = kit.game.begin_turn(user, save.public_id, client_msg_id="m-1", action=ACTION).replay
    assert (first.revision, first.free_left) == (1, 2)
    succeed(kit, user, save, regenerate(kit, user, save))
    after = kit.game.begin_turn(user, save.public_id, client_msg_id="m-1", action=ACTION).replay
    assert (after.revision, after.free_left) == (2, 1)


def test_turn_views_carry_revision_and_nothing_internal(kit):
    user, save, _ = ready(kit)
    succeed(kit, user, save, regenerate(kit, user, save))
    kit.game.begin_turn(user, save.public_id, client_msg_id="m-2", action="第二個行動。")
    views, _ = kit.game.turn_views(user, save.public_id)
    assert [(view["client_msg_id"], view["revision"]) for view in views] == [("m-1", 2), ("m-2", 1)]
    assert not any("state_before" in key or "regen" in key or "revising" in key for view in views for key in view)


def test_last_turn_is_empty_without_a_complete_turn_and_ignores_blocked_attempts(kit):
    user = kit.player()
    save = kit.open_save(user)
    assert kit.game.session_snapshot(user, save.public_id)["last_turn"] is None
    kit.game.begin_turn(user, save.public_id, client_msg_id="m-1", action=ACTION)
    kit.sessions.block_turn(user.id, save.public_id, "m-1", partial_response="", attempt=1)
    assert kit.game.session_snapshot(user, save.public_id)["last_turn"] is None


def test_last_turn_describes_the_revisable_last_turn_and_tracks_the_free_count_and_prices(kit):
    user, save, _ = ready(kit)
    view = kit.game.session_snapshot(user, save.public_id)["last_turn"]
    assert set(view) == LAST_TURN_KEYS
    assert view == {
        "client_msg_id": "m-1", "seq": 1, "revision": 1, "revisable": True, "reason": None, "free_left": 2, "regenerate_cost": 0, "edit_cost": 30,
    }
    succeed(kit, user, save, regenerate(kit, user, save, revision=1))
    view = kit.game.session_snapshot(user, save.public_id)["last_turn"]
    assert (view["revision"], view["free_left"], view["regenerate_cost"], view["edit_cost"], view["revisable"]) == (2, 1, 0, 30, True)
    succeed(kit, user, save, regenerate(kit, user, save, revision=2))
    view = kit.game.session_snapshot(user, save.public_id)["last_turn"]
    assert (view["revision"], view["free_left"], view["regenerate_cost"]) == (3, 0, 30)
    succeed(kit, user, save, edit(kit, user, save, revision=3))
    view = kit.game.session_snapshot(user, save.public_id)["last_turn"]
    assert (view["revision"], view["free_left"], view["regenerate_cost"]) == (4, 2, 0)


def test_last_turn_reports_why_it_is_not_revisable(kit):
    user, save, _ = ready(kit)
    plan = regenerate(kit, user, save)                                                  # 認領中
    view = kit.game.session_snapshot(user, save.public_id)["last_turn"]
    assert (view["revisable"], view["reason"]) == (False, "in_progress")
    fail(kit, user, save, plan)
    kit.game.begin_turn(user, save.public_id, client_msg_id="m-2", action="第二個行動。")          # 下一輪生成中
    assert kit.game.session_snapshot(user, save.public_id)["last_turn"]["reason"] == "in_progress"
    kit.sessions.interrupt_turn(user.id, save.public_id, "m-2", partial_response="", attempt=1)
    assert kit.game.session_snapshot(user, save.public_id)["last_turn"]["revisable"] is True
    kit.sessions.end_session(user.id, save.public_id, now=kit.clock())
    view = kit.game.session_snapshot(user, save.public_id)["last_turn"]
    assert (view["revisable"], view["reason"]) == (False, "ended")
    assert "state_before" not in json.dumps(kit.game.session_snapshot(user, save.public_id), ensure_ascii=False)


def test_last_turn_for_a_turn_without_a_snapshot_says_no_snapshot(kit):
    user = kit.player()
    save = kit.open_save(user)
    kit.sessions.start_turn(user.id, save.public_id, client_msg_id="old", user_action=ACTION, now=kit.clock())
    kit.sessions.complete_turn(user.id, save.public_id, "old", llm_raw_response="舊劇情。", structured_result=None, now=kit.clock())
    view = kit.game.session_snapshot(user, save.public_id)["last_turn"]
    assert (view["revisable"], view["reason"]) == (False, "no_snapshot")
