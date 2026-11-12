"""rpg-turn-revision 規格：`SessionRepository` 的修訂方法（回合前快照、認領、釋放、替換、不含目標回合的歷史、最後一輪的狀態）。

一般測試以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行；並行測試另用 `concurrent_engine`，
以真實的執行緒與獨立連線證明：兩個並行的認領只有一個成功、認領與新回合互相排斥。
"""
import ast
import inspect
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import MemoryLog, RpgSession
from app.models.rpg_session import SESSION_ENDED, TURN_COMPLETE
from app.repositories import session_repository
from app.repositories.errors import (
    InvalidTurnTransitionError, NotRevisableError, SessionEndedError, StaleSessionVersionError, TurnInProgressError,
)
from app.repositories.session_repository import (
    NOT_REVISABLE_ENDED, NOT_REVISABLE_IN_PROGRESS, NOT_REVISABLE_NO_SNAPSHOT, NOT_REVISABLE_NOT_LAST, NOT_REVISABLE_REASONS,
    REVISION_BUSY, REVISION_CLAIMED, REVISION_CONFLICT, REVISION_TAKEN_OVER, RevisionClaim, RevisionState, SessionRepository,
)
from tests.test_session_repository import NOW, finish, make_user, open_save, run_in_threads, seed_save, start

MISSING = "00000000-0000-4000-8000-000000000000"
STALE = 180                                    # 與 `DEFAULT_TURN_STALE_SECONDS` 相同：認領與預扣同時到期


@pytest.fixture()
def repo(member_session):
    return SessionRepository(member_session)


@pytest.fixture()
def world(repo, member_session):
    alice = make_user(member_session, 1)
    return alice, open_save(repo, alice)


def snap(location: str = "山門", months: int = 0) -> dict:
    """回合前快照的樣子（`WorldState.to_fields()` 的六欄）。"""
    return {
        "entities_json": {}, "goals_json": {"active": [], "archived": []}, "milestone_progress_json": {}, "elapsed_months": months,
        "current_location": location, "status_summary": "",
    }


def play(repo, user, save, count: int, first: int = 1, *, snapshots: bool = True) -> None:
    """依序完成 `count` 個回合；每個都帶回合前快照（`snapshots=False` 時不帶，像遷移之前的回合）。"""
    for index in range(first, first + count):
        repo.start_turn(
            user.id, save.public_id, client_msg_id=f"m-{index}", user_action=f"行動{index}", now=NOW,
            state_before=snap(f"地點{index}") if snapshots else None,
        )
        finish(repo, user, save, client_msg_id=f"m-{index}", reply=f"劇情{index}")


def claim(repo, user, save, client_msg_id="m-1", *, expected=1, now=NOW):
    return repo.claim_revision(
        user.id, save.public_id, client_msg_id, expected_revision=expected, now=now, stale_after_seconds=STALE,
    )


def replace(repo, user, save, client_msg_id="m-1", *, expected=1, attempt=2, reply="新的劇情", now=NOW, **extra):
    return repo.replace_turn(
        user.id, save.public_id, client_msg_id, expected_revision=expected, attempt=attempt, llm_raw_response=reply,
        structured_result={"revised": True}, now=now, **extra,
    )


def turn_of(repo, user, save, client_msg_id="m-1") -> MemoryLog:
    return repo.get_turn(user.id, save.public_id, client_msg_id)


def snapshot_is_sql_null(member_session, client_msg_id: str) -> bool:
    """快照是 SQL 的 NULL，不是 JSON 的 null（兩者讀回來都是 `None`，只有 SQL 條件分得出來）。"""
    member_session.expire_all()
    return member_session.scalar(
        select(func.count()).select_from(MemoryLog).where(MemoryLog.client_msg_id == client_msg_id, MemoryLog.state_before_json.is_(None))
    ) == 1


# ---- 2.1 回合前快照 --------------------------------------------------------------------------

def test_a_new_turn_stores_the_state_before_snapshot(repo, member_session, world):
    alice, save = world
    started = repo.start_turn(alice.id, save.public_id, client_msg_id="m-1", user_action="行動", now=NOW, state_before=snap("山門", 3))
    assert started.turn.state_before_json == snap("山門", 3)
    assert turn_of(repo, alice, save).state_before_json == snap("山門", 3)


def test_the_snapshot_is_a_copy_so_later_changes_to_the_argument_do_not_leak_in(repo, member_session, world):
    alice, save = world
    given = snap()
    repo.start_turn(alice.id, save.public_id, client_msg_id="m-1", user_action="行動", now=NOW, state_before=given)
    given["goals_json"]["active"].append({"title": "事後加的"})
    member_session.expire_all()
    assert turn_of(repo, alice, save).state_before_json["goals_json"]["active"] == []


def test_a_turn_started_without_a_snapshot_has_a_sql_null_not_a_json_null(repo, member_session, world):
    alice, save = world
    start(repo, alice, save)                                      # 既有的呼叫方式：不帶快照
    assert turn_of(repo, alice, save).state_before_json is None
    assert snapshot_is_sql_null(member_session, "m-1")


def test_completing_the_next_turn_clears_the_previous_turns_snapshot_only(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1)
    assert turn_of(repo, alice, save, "m-1").state_before_json == snap("地點1")
    play(repo, alice, save, 1, first=2)
    assert snapshot_is_sql_null(member_session, "m-1")
    assert turn_of(repo, alice, save, "m-2").state_before_json == snap("地點2")
    play(repo, alice, save, 1, first=3)
    assert snapshot_is_sql_null(member_session, "m-2") and turn_of(repo, alice, save, "m-3").state_before_json == snap("地點3")


def test_finish_turn_clears_the_previous_snapshot_too(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1)
    repo.start_turn(alice.id, save.public_id, client_msg_id="m-2", user_action="行動2", now=NOW, state_before=snap("地點2"))
    repo.finish_turn(alice.id, save.public_id, "m-2", llm_raw_response="劇情2", structured_result=None, now=NOW)
    assert snapshot_is_sql_null(member_session, "m-1") and turn_of(repo, alice, save, "m-2").state_before_json == snap("地點2")


def test_a_blocked_or_interrupted_next_turn_does_not_clear_the_previous_snapshot(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1)
    repo.start_turn(alice.id, save.public_id, client_msg_id="m-2", user_action="行動2", now=NOW, state_before=snap("地點2"))
    repo.block_turn(alice.id, save.public_id, "m-2", partial_response="被擋")
    repo.start_turn(alice.id, save.public_id, client_msg_id="m-3", user_action="行動3", now=NOW, state_before=snap("地點3"))
    repo.interrupt_turn(alice.id, save.public_id, "m-3", partial_response="半截")
    assert turn_of(repo, alice, save, "m-1").state_before_json == snap("地點1")


def test_resending_a_blocked_turn_overwrites_its_snapshot(repo, member_session, world):
    alice, save = world
    repo.start_turn(alice.id, save.public_id, client_msg_id="m-1", user_action="行動", now=NOW, state_before=snap("舊地點"))
    repo.block_turn(alice.id, save.public_id, "m-1", partial_response="被擋")
    resent = repo.start_turn(alice.id, save.public_id, client_msg_id="m-1", user_action="行動", now=NOW, state_before=snap("新地點", 2))
    assert resent.turn.state_before_json == snap("新地點", 2)
    member_session.expire_all()
    assert turn_of(repo, alice, save).state_before_json == snap("新地點", 2)


def test_resending_without_a_snapshot_clears_the_old_one(repo, member_session, world):
    alice, save = world
    repo.start_turn(alice.id, save.public_id, client_msg_id="m-1", user_action="行動", now=NOW, state_before=snap("舊地點"))
    repo.interrupt_turn(alice.id, save.public_id, "m-1", partial_response="半截")
    start(repo, alice, save)                                       # 重送，沒帶快照
    assert snapshot_is_sql_null(member_session, "m-1")


def test_replaying_a_complete_turn_leaves_its_snapshot_alone(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1)
    again = repo.start_turn(alice.id, save.public_id, client_msg_id="m-1", user_action="行動1", now=NOW, state_before=snap("別的地點"))
    assert again.outcome == session_repository.TURN_REPLAYED
    member_session.expire_all()
    assert turn_of(repo, alice, save).state_before_json == snap("地點1")


def test_a_live_revision_claim_blocks_starting_a_new_turn_until_it_expires(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1)
    assert claim(repo, alice, save).outcome == REVISION_CLAIMED
    with pytest.raises(TurnInProgressError):
        start(repo, alice, save, "m-2", now=NOW + timedelta(seconds=STALE))                 # 剛好滿期那一秒仍算未逾時
    assert repo.get_turn(alice.id, save.public_id, "m-2") is None                           # 沒有留下回合列
    started = repo.start_turn(
        alice.id, save.public_id, client_msg_id="m-2", user_action="行動2", now=NOW + timedelta(seconds=STALE + 1), stale_after_seconds=STALE,
    )
    assert started.outcome == session_repository.TURN_CREATED


def test_a_live_revision_claim_also_blocks_resending_an_old_blocked_turn(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1)
    start(repo, alice, save, "m-2", now=NOW)
    repo.block_turn(alice.id, save.public_id, "m-2", partial_response="被擋")
    claim(repo, alice, save)
    with pytest.raises(TurnInProgressError):
        start(repo, alice, save, "m-2", now=NOW + timedelta(seconds=5))
    assert repo.get_turn(alice.id, save.public_id, "m-2").turn_status == "blocked"          # 沒有被重設


def test_a_revision_claim_does_not_block_starting_a_turn_in_another_save(repo, member_session, world):
    alice, save = world
    other = open_save(repo, alice)
    play(repo, alice, save, 1)
    claim(repo, alice, save)
    assert start(repo, alice, other, "m-9").outcome == session_repository.TURN_CREATED


# ---- 2.2 認領 --------------------------------------------------------------------------------

def test_claiming_sets_the_claim_time_and_adds_one_attempt(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1)
    before = turn_of(repo, alice, save)
    assert (before.attempt, before.revision, before.regen_count, before.revising_started_at) == (1, 1, 0, None)
    claimed = claim(repo, alice, save)
    assert isinstance(claimed, RevisionClaim) and claimed.outcome == REVISION_CLAIMED
    turn = turn_of(repo, alice, save)
    assert (turn.attempt, turn.revision, turn.regen_count, turn.revising_started_at, turn.turn_status) == (2, 1, 0, NOW, TURN_COMPLETE)
    assert (claimed.turn.id, claimed.turn.attempt) == (turn.id, 2)
    assert (turn.llm_raw_response, turn.user_action, turn.turn_number) == ("劇情1", "行動1", 1)          # 舊內容原樣可讀


def test_a_wrong_expected_revision_is_a_conflict_and_changes_nothing(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1)
    result = claim(repo, alice, save, expected=2)
    assert result.outcome == REVISION_CONFLICT and result.turn.revision == 1
    turn = turn_of(repo, alice, save)
    assert (turn.attempt, turn.revising_started_at) == (1, None)


def test_a_second_claim_on_a_live_claim_is_busy_and_does_not_add_an_attempt(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1)
    claim(repo, alice, save)
    again = claim(repo, alice, save, now=NOW + timedelta(seconds=5))
    assert again.outcome == REVISION_BUSY
    turn = turn_of(repo, alice, save)
    assert (turn.attempt, turn.revising_started_at) == (2, NOW)


def test_a_claim_older_than_the_timeout_is_taken_over_with_another_attempt(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1)
    claim(repo, alice, save)
    assert claim(repo, alice, save, now=NOW + timedelta(seconds=STALE)).outcome == REVISION_BUSY          # 滿期當秒仍有效
    later = NOW + timedelta(seconds=STALE + 1)
    taken = claim(repo, alice, save, now=later)
    assert taken.outcome == REVISION_TAKEN_OVER
    turn = turn_of(repo, alice, save)
    assert (turn.attempt, turn.revising_started_at, turn.revision) == (3, later, 1)


def test_an_expired_holder_cannot_replace_or_release_after_a_takeover(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1)
    claim(repo, alice, save)                                                   # 舊執行者：attempt 2
    claim(repo, alice, save, now=NOW + timedelta(seconds=STALE + 1))           # 新的請求接手：attempt 3
    with pytest.raises(InvalidTurnTransitionError):
        replace(repo, alice, save, attempt=2)
    assert repo.release_revision(alice.id, save.public_id, "m-1", attempt=2) is False
    turn = turn_of(repo, alice, save)
    assert (turn.llm_raw_response, turn.revision, turn.attempt) == ("劇情1", 1, 3)
    assert turn.revising_started_at is not None                                # 新的認領沒有被舊執行者清掉


def test_claiming_a_turn_whose_session_was_never_the_callers_returns_none_without_changes(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1)
    mallory = make_user(member_session, 2)
    assert repo.claim_revision(mallory.id, save.public_id, "m-1", expected_revision=1, now=NOW) is None
    assert repo.claim_revision(alice.id, MISSING, "m-1", expected_revision=1, now=NOW) is None
    assert repo.claim_revision(alice.id, save.public_id, "不存在", expected_revision=1, now=NOW) is None
    turn = turn_of(repo, alice, save)
    assert (turn.attempt, turn.revising_started_at) == (1, None)


def test_the_same_client_message_id_in_another_save_is_not_claimable_through_this_one(repo, member_session, world):
    alice, save = world
    other = open_save(repo, alice)
    play(repo, alice, other, 1)
    assert repo.claim_revision(alice.id, save.public_id, "m-1", expected_revision=1, now=NOW) is None


# ---- 2.2 不可修訂的原因 -----------------------------------------------------------------------------

def reason_of(repo, user, save, client_msg_id="m-1", *, expected=1, now=NOW) -> str:
    with pytest.raises(NotRevisableError) as caught:
        claim(repo, user, save, client_msg_id, expected=expected, now=now)
    return caught.value.reason


def test_an_older_turn_is_not_the_last_turn(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 2)
    assert reason_of(repo, alice, save, "m-1") == NOT_REVISABLE_NOT_LAST
    assert claim(repo, alice, save, "m-2").outcome == REVISION_CLAIMED
    assert turn_of(repo, alice, save, "m-1").attempt == 1                     # 沒有任何寫入


def test_a_blocked_or_interrupted_attempt_after_the_last_complete_turn_does_not_make_it_not_last(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1)
    start(repo, alice, save, "m-2")
    repo.block_turn(alice.id, save.public_id, "m-2", partial_response="被擋")
    start(repo, alice, save, "m-3")
    repo.interrupt_turn(alice.id, save.public_id, "m-3", partial_response="半截")
    assert claim(repo, alice, save).outcome == REVISION_CLAIMED
    assert reason_of(repo, alice, save, "m-2") == NOT_REVISABLE_NOT_LAST        # 被擋或中斷的嘗試自己不能被修訂
    assert reason_of(repo, alice, save, "m-3") == NOT_REVISABLE_NOT_LAST


def test_a_live_streaming_turn_after_the_last_complete_turn_is_in_progress_but_a_stale_one_is_not(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1)
    start(repo, alice, save, "m-2", now=NOW)
    assert reason_of(repo, alice, save, now=NOW + timedelta(seconds=STALE)) == NOT_REVISABLE_IN_PROGRESS
    assert claim(repo, alice, save, now=NOW + timedelta(seconds=STALE + 1)).outcome == REVISION_CLAIMED


def test_a_finished_save_is_not_revisable_even_for_the_last_turn(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1)
    repo.end_session(alice.id, save.public_id, now=NOW)
    assert reason_of(repo, alice, save) == NOT_REVISABLE_ENDED
    assert turn_of(repo, alice, save).attempt == 1


def test_a_turn_without_a_snapshot_is_not_revisable(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1, snapshots=False)
    assert reason_of(repo, alice, save) == NOT_REVISABLE_NO_SNAPSHOT


def test_a_revision_conflict_is_reported_before_any_not_revisable_reason(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 2)
    repo.end_session(alice.id, save.public_id, now=NOW)
    assert claim(repo, alice, save, "m-1", expected=7).outcome == REVISION_CONFLICT


def test_the_reasons_are_the_documented_ones_in_the_documented_order():
    assert NOT_REVISABLE_REASONS == ("ended", "not_last", "in_progress", "no_snapshot")


# ---- 2.2 釋放 --------------------------------------------------------------------------------

def test_releasing_clears_only_the_callers_own_claim_and_leaves_the_content_alone(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1)
    claim(repo, alice, save)
    assert repo.release_revision(alice.id, save.public_id, "m-1", attempt=2) is True
    turn = turn_of(repo, alice, save)
    assert (turn.revising_started_at, turn.llm_raw_response, turn.revision, turn.regen_count, turn.attempt) == (None, "劇情1", 1, 0, 2)
    assert repo.release_revision(alice.id, save.public_id, "m-1", attempt=2) is False        # 已經沒有認領
    again = claim(repo, alice, save, now=NOW + timedelta(seconds=1))                         # 釋放後可以再修訂，嘗試序號繼續往上
    assert again.outcome == REVISION_CLAIMED and turn_of(repo, alice, save).attempt == 3


def test_releasing_with_the_wrong_attempt_does_nothing(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1)
    claim(repo, alice, save)
    assert repo.release_revision(alice.id, save.public_id, "m-1", attempt=1) is False
    assert turn_of(repo, alice, save).revising_started_at == NOW


# ---- 2.2 替換 --------------------------------------------------------------------------------

def test_replacing_swaps_the_content_keeps_number_and_seq_and_clears_the_claim(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 2)
    before = turn_of(repo, alice, save, "m-2")
    claim(repo, alice, save, "m-2")
    done = replace(repo, alice, save, "m-2")
    assert (done.world_updated, done.ended) == (False, False)
    turn = turn_of(repo, alice, save, "m-2")
    assert (turn.llm_raw_response, turn.structured_result, turn.user_action) == ("新的劇情", {"revised": True}, "行動2")
    assert (turn.turn_number, turn.seq, turn.turn_status, turn.client_msg_id) == (before.turn_number, before.seq, TURN_COMPLETE, "m-2")
    assert (turn.revision, turn.regen_count, turn.revising_started_at, turn.attempt) == (2, 1, None, 2)
    assert turn.state_before_json == snap("地點2")                                      # 快照不動：仍可再修訂
    assert repo.count_complete_turns(alice.id, save.public_id) == 2


def test_replacing_with_a_new_action_replaces_the_player_input_and_can_reset_the_regeneration_count(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1)
    claim(repo, alice, save)
    replace(repo, alice, save)                                                           # 一次成功的重新生成：regen_count 1、revision 2
    claim(repo, alice, save, expected=2, now=NOW + timedelta(seconds=1))
    replace(repo, alice, save, expected=2, attempt=3, user_action="我改變主意了", reset_regen_count=True)
    turn = turn_of(repo, alice, save)
    assert (turn.user_action, turn.regen_count, turn.revision) == ("我改變主意了", 0, 3)


def test_each_successful_regeneration_adds_one_to_the_count(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1)
    for index in range(3):
        claim(repo, alice, save, expected=1 + index, now=NOW + timedelta(seconds=index))
        replace(repo, alice, save, expected=1 + index, attempt=2 + index)
    turn = turn_of(repo, alice, save)
    assert (turn.regen_count, turn.revision, turn.attempt) == (3, 4, 4)


def test_replacing_writes_the_world_state_with_the_version_and_can_end_the_story(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1)
    claim(repo, alice, save)
    done = replace(repo, alice, save, world_changes=snap("新山門", 5), expected_version=1, end_story=True)
    assert (done.world_updated, done.ended) == (True, True)
    member_session.refresh(save)
    assert (save.current_location, save.elapsed_months, save.version, save.status) == ("新山門", 5, 2, SESSION_ENDED)
    assert turn_of(repo, alice, save).llm_raw_response == "新的劇情"


def test_a_stale_world_version_rolls_everything_back_and_keeps_the_claim(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1)
    claim(repo, alice, save)
    repo.update_world_state(alice.id, save.public_id, expected_version=1, current_location="別人改的")      # 版本變 2
    with pytest.raises(StaleSessionVersionError):
        replace(repo, alice, save, world_changes=snap("新山門"), expected_version=1)
    turn = turn_of(repo, alice, save)
    assert (turn.llm_raw_response, turn.revision, turn.regen_count, turn.attempt) == ("劇情1", 1, 0, 2)
    assert turn.revising_started_at == NOW                                      # 整個復原：認領還在，由 Runner 釋放
    member_session.refresh(save)
    assert (save.current_location, save.version) == ("別人改的", 2)


def test_a_failed_replacement_does_not_leave_a_world_state_write_behind(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1)
    claim(repo, alice, save)
    with pytest.raises(InvalidTurnTransitionError):
        replace(repo, alice, save, attempt=1, world_changes=snap("不該寫入"), expected_version=1)
    member_session.refresh(save)
    assert (save.current_location, save.version) == ("", 1)


def test_replacing_without_a_claim_or_with_the_wrong_revision_or_attempt_fails_and_changes_nothing(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1)
    with pytest.raises(InvalidTurnTransitionError):                              # 沒有認領
        replace(repo, alice, save, attempt=1)
    claim(repo, alice, save)
    for wrong in (dict(expected=2), dict(attempt=1), dict(attempt=3)):
        with pytest.raises(InvalidTurnTransitionError):
            replace(repo, alice, save, **wrong)
    turn = turn_of(repo, alice, save)
    assert (turn.llm_raw_response, turn.revision, turn.regen_count, turn.revising_started_at) == ("劇情1", 1, 0, NOW)


def test_replacing_twice_with_the_same_claim_only_works_once(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1)
    claim(repo, alice, save)
    replace(repo, alice, save)
    with pytest.raises(InvalidTurnTransitionError):
        replace(repo, alice, save, reply="第二次")
    assert turn_of(repo, alice, save).llm_raw_response == "新的劇情"


def test_replacing_in_a_finished_save_fails_and_changes_nothing(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1)
    claim(repo, alice, save)
    repo.end_session(alice.id, save.public_id, now=NOW)
    with pytest.raises(SessionEndedError):
        replace(repo, alice, save)
    assert turn_of(repo, alice, save).llm_raw_response == "劇情1"


def test_replacing_with_world_changes_requires_the_expected_version(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1)
    claim(repo, alice, save)
    with pytest.raises(ValueError):
        replace(repo, alice, save, world_changes=snap())


# ---- 2.2 不含目標回合的歷史 -------------------------------------------------------------------------

def test_complete_turns_before_returns_the_recent_turns_below_the_number_oldest_first(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 14)
    turns = repo.complete_turns_before(alice.id, save.public_id, 14)
    assert [t.turn_number for t in turns] == list(range(4, 14))                  # 最近 10 個，不含第 14 輪
    assert [t.turn_number for t in repo.complete_turns_before(alice.id, save.public_id, 14, limit=3)] == [11, 12, 13]
    assert [t.turn_number for t in repo.complete_turns_before(alice.id, save.public_id, 3)] == [1, 2]
    assert repo.complete_turns_before(alice.id, save.public_id, 1) == []


def test_complete_turns_before_skips_blocked_and_interrupted_attempts(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 2)
    start(repo, alice, save, "gone")
    repo.interrupt_turn(alice.id, save.public_id, "gone", partial_response="半截")
    start(repo, alice, save, "nope")
    repo.block_turn(alice.id, save.public_id, "nope", partial_response="被擋")
    play(repo, alice, save, 1, first=3)
    assert [t.llm_raw_response for t in repo.complete_turns_before(alice.id, save.public_id, 3)] == ["劇情1", "劇情2"]


def test_the_turn_list_carries_the_revision_so_a_view_can_show_it(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 2)
    claim(repo, alice, save, "m-2")
    replace(repo, alice, save, "m-2")
    assert [(t.client_msg_id, t.revision) for t in repo.list_turns(alice.id, save.public_id)] == [("m-1", 1), ("m-2", 2)]


# ---- 2.2 最後一輪的狀態 -----------------------------------------------------------------------------

def state(repo, user, save, *, now=NOW) -> RevisionState | None:
    return repo.revision_state(user.id, save.public_id, now=now, stale_after_seconds=STALE)


def test_the_state_of_a_save_without_complete_turns_is_empty(repo, member_session, world):
    alice, save = world
    assert state(repo, alice, save) == RevisionState(None, None, None, None, None, False, None)
    start(repo, alice, save)                                                      # 生成中的回合還不是最後一輪
    assert state(repo, alice, save).client_msg_id is None


def test_the_state_describes_the_last_complete_turn_and_whether_it_can_be_revised(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 2)
    last = turn_of(repo, alice, save, "m-2")
    assert state(repo, alice, save) == RevisionState("m-2", last.seq, 2, 1, 0, True, None)
    claim(repo, alice, save, "m-2")
    replace(repo, alice, save, "m-2")
    assert state(repo, alice, save) == RevisionState("m-2", last.seq, 2, 2, 1, True, None)


def test_the_state_gives_the_same_answers_as_claiming_for_every_reason(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1, snapshots=False)
    assert state(repo, alice, save).reason == NOT_REVISABLE_NO_SNAPSHOT
    play(repo, alice, save, 1, first=2)
    assert state(repo, alice, save).revisable is True
    start(repo, alice, save, "m-3", now=NOW)
    assert state(repo, alice, save, now=NOW + timedelta(seconds=5)).reason == NOT_REVISABLE_IN_PROGRESS
    assert state(repo, alice, save, now=NOW + timedelta(seconds=STALE + 1)).reason is None      # 逾時的生成中回合不算
    repo.end_session(alice.id, save.public_id, now=NOW)
    assert state(repo, alice, save).reason == NOT_REVISABLE_ENDED


def test_a_live_claim_shows_up_as_in_progress_until_it_expires(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1)
    claim(repo, alice, save)
    assert state(repo, alice, save, now=NOW + timedelta(seconds=STALE)).reason == NOT_REVISABLE_IN_PROGRESS
    assert state(repo, alice, save, now=NOW + timedelta(seconds=STALE + 1)).revisable is True


# ---- 擁有者隔離 ------------------------------------------------------------------------------

def test_every_revision_method_returns_none_for_someone_elses_save_and_changes_nothing(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 2)
    claim(repo, alice, save, "m-2")
    mallory = make_user(member_session, 2)
    snapshot_before = member_session.execute(select(MemoryLog.client_msg_id, MemoryLog.revision, MemoryLog.attempt, MemoryLog.revising_started_at)).all()
    assert repo.revision_state(mallory.id, save.public_id, now=NOW) is None
    assert repo.claim_revision(mallory.id, save.public_id, "m-2", expected_revision=1, now=NOW) is None
    assert repo.release_revision(mallory.id, save.public_id, "m-2", attempt=2) is None
    assert replace(repo, mallory, save, "m-2") is None
    assert repo.complete_turns_before(mallory.id, save.public_id, 2) is None
    member_session.expire_all()
    assert member_session.execute(select(MemoryLog.client_msg_id, MemoryLog.revision, MemoryLog.attempt, MemoryLog.revising_started_at)).all() == snapshot_before
    assert turn_of(repo, alice, save, "m-2").llm_raw_response == "劇情2"


def test_unknown_saves_and_turns_return_none_from_the_revision_methods(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1)
    assert repo.revision_state(alice.id, MISSING, now=NOW) is None
    assert repo.release_revision(alice.id, save.public_id, "不存在", attempt=1) is None
    assert replace(repo, alice, save, "不存在") is None
    assert repo.complete_turns_before(alice.id, MISSING, 2) is None


def test_every_public_method_of_the_session_repository_still_starts_with_the_user_id():
    tree = ast.parse(inspect.getsource(session_repository))
    klass = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SessionRepository")
    public = {node.name: [a.arg for a in node.args.args] for node in klass.body if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")}
    for name in ("revision_state", "claim_revision", "release_revision", "replace_turn", "complete_turns_before"):
        assert public[name][:2] == ["self", "user_id"], name
    assert all(args[:2] == ["self", "user_id"] for name, args in public.items() if name not in ("savepoint",)), public


# ---- 並行（真實的執行緒與獨立連線）---------------------------------------------------------------------

def seed_revisable(engine) -> tuple[int, str]:
    user_id, public_id = seed_save(engine)
    with Session(engine) as s:
        repository = SessionRepository(s)
        repository.start_turn(user_id, public_id, client_msg_id="m-1", user_action="行動", now=NOW, state_before=snap())
        repository.finish_turn(user_id, public_id, "m-1", llm_raw_response="劇情", structured_result=None, now=NOW)
        s.commit()
    return user_id, public_id


def test_two_parallel_claims_with_the_same_expected_revision_let_exactly_one_win(concurrent_engine):
    user_id, public_id = seed_revisable(concurrent_engine)

    def worker(s: Session, _index: int) -> str:
        result = SessionRepository(s).claim_revision(user_id, public_id, "m-1", expected_revision=1, now=NOW, stale_after_seconds=STALE)
        return result.outcome

    outcomes = run_in_threads(concurrent_engine, worker, 3)
    assert sorted(outcomes) == [REVISION_BUSY, REVISION_BUSY, REVISION_CLAIMED], outcomes
    with Session(concurrent_engine) as s:
        assert s.scalar(select(MemoryLog.attempt)) == 2                           # 只加了一次


def test_a_claim_and_a_new_turn_at_the_same_time_exclude_each_other(concurrent_engine):
    user_id, public_id = seed_revisable(concurrent_engine)

    def worker(s: Session, index: int) -> str:
        repository = SessionRepository(s)
        try:
            if index == 0:
                return "claim:" + repository.claim_revision(user_id, public_id, "m-1", expected_revision=1, now=NOW, stale_after_seconds=STALE).outcome
            return "turn:" + repository.start_turn(user_id, public_id, client_msg_id="m-2", user_action="行動2", now=NOW).outcome
        except NotRevisableError as error:
            return "claim:" + error.reason
        except TurnInProgressError:
            return "turn:conflict"

    outcomes = sorted(run_in_threads(concurrent_engine, worker, 2))
    assert outcomes in (["claim:claimed", "turn:conflict"], ["claim:in_progress", "turn:created"]), outcomes
    with Session(concurrent_engine) as s:
        claimed = s.scalar(select(func.count()).select_from(MemoryLog).where(MemoryLog.revising_started_at.is_not(None)))
        streaming = s.scalar(select(func.count()).select_from(MemoryLog).where(MemoryLog.turn_status == "streaming"))
        assert claimed + streaming == 1                                           # 修訂與新回合不會同時進行
        assert s.scalar(select(func.count()).select_from(RpgSession)) == 1
