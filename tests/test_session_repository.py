"""rpg-sessions 規格：存檔與回合的資料行為（初始值、擁有者隔離、回合的唯一性與冪等、中斷不計輪、世界狀態的版本號、已完結）。

一般測試以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行；並行測試另用 `concurrent_engine`，
以真實的執行緒與獨立連線證明「同一個編號最多一筆」「序號不重複」。
"""
import ast
import inspect
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import MemoryLog, RpgSession, User
from app.models.base import utcnow
from app.models.rpg_session import (
    SESSION_ACTIVE, SESSION_ENDED, TURN_BLOCKED, TURN_COMPLETE, TURN_INTERRUPTED, TURN_STREAMING,
)
from app.repositories import session_repository
from app.repositories.errors import (
    InvalidTurnTransitionError, SessionEndedError, StaleSessionVersionError, TurnInProgressError,
)
from app.repositories.session_repository import TURN_CREATED, TURN_REPLAYED, TURN_RESTARTED, SessionRepository
from tests.support.script_data import validated

NOW = utcnow()
SNAPSHOT = validated().content


def make_user(session, n: int = 1, **overrides) -> User:
    user = User(email=f"u{n}@example.com", email_canonical=f"u{n}@example.com", display_name=f"使用者{n}", status="active", **overrides)
    session.add(user)
    session.flush()
    return user


@pytest.fixture()
def repo(member_session):
    return SessionRepository(member_session)


def open_save(repo, user, **overrides) -> RpgSession:
    values = dict(
        source_version_id=None, snapshot=SNAPSHOT, snapshot_spec_version=1, player_name="旅人", character_age=18,
        content_tier="safe", now=NOW,
    )
    values.update(overrides)
    return repo.create(user.id, **values)


def start(repo, user, save, client_msg_id="m-1", action="我走進山門", now=NOW):
    return repo.start_turn(user.id, save.public_id, client_msg_id=client_msg_id, user_action=action, now=now)


def finish(repo, user, save, client_msg_id="m-1", reply="山門前的晨霧散開了。", now=NOW):
    return repo.complete_turn(user.id, save.public_id, client_msg_id, llm_raw_response=reply, structured_result={"ok": True}, now=now)


# ---- 開存檔：複本與初始值 ----------------------------------------------------------------------

def test_a_new_save_has_the_documented_initial_values(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice, content_tier="standard")
    member_session.commit()
    assert (save.status, save.version, save.elapsed_months, save.ended_at, save.is_archived) == (SESSION_ACTIVE, 1, 0, None, False)
    assert (save.notebook_text, save.notebook_version, save.notebook_upto_turn) == ("", 1, 0)
    assert (save.goals_json, save.milestone_progress_json, save.entities_json) == ({"active": [], "archived": []}, {}, {})
    assert (save.player_name, save.character_age, save.content_tier, save.snapshot_spec_version) == ("旅人", 18, "standard", 1)
    assert save.last_active_at == NOW and len(save.public_id) == 36


def test_the_snapshot_is_a_complete_copy_that_later_changes_to_the_source_cannot_touch(repo, member_session):
    alice = make_user(member_session)
    source = validated()
    save = open_save(repo, alice, snapshot=source.content)
    source.content["title"] = "之後被改過的標題"                      # 呼叫端手上的來源被改動，存檔裡的複本不受影響（記憶體中與資料庫中都是）
    source.content["characters"][0]["name"] = "被改的名字"
    assert save.script_snapshot_json["title"] == "青雲問道" and save.script_snapshot_json["characters"][0]["name"] == "林曉"
    member_session.commit()
    member_session.expire_all()
    stored = member_session.get(RpgSession, save.id).script_snapshot_json
    assert stored["title"] == "青雲問道" and stored["characters"][0]["name"] == "林曉"
    assert stored["spec_version"] == 1 and stored["milestones"][0]["trigger"] == {"kind": "age", "value": 20}


def test_a_new_save_does_not_inherit_anything_from_the_players_other_saves(repo, member_session):
    alice = make_user(member_session)
    first = open_save(repo, alice)
    first.notebook_text = "一段很長的記事簿內容"
    member_session.commit()
    second = open_save(repo, alice)
    assert second.notebook_text == "" and second.notebook_upto_turn == 0 and second.id != first.id


# ---- 擁有者隔離 -----------------------------------------------------------------------------

def test_a_non_owner_gets_the_same_nothing_as_for_a_missing_save_for_every_method(repo, member_session):
    alice, bob = make_user(member_session, 1), make_user(member_session, 2)
    save = open_save(repo, alice)
    start(repo, alice, save)
    missing = "00000000-0000-4000-8000-000000000000"
    for public_id in (save.public_id, missing):
        assert repo.get_owned(bob.id, public_id) is None
        assert repo.list_turns(bob.id, public_id) is None
        assert repo.get_turn(bob.id, public_id, "m-1") is None
        assert repo.count_complete_turns(bob.id, public_id) is None
        assert repo.start_turn(bob.id, public_id, client_msg_id="x", user_action="a", now=NOW) is None
        assert repo.complete_turn(bob.id, public_id, "m-1", llm_raw_response="r", structured_result=None, now=NOW) is None
        assert repo.interrupt_turn(bob.id, public_id, "m-1", partial_response="r") is None
        assert repo.block_turn(bob.id, public_id, "m-1") is None
        assert repo.update_world_state(bob.id, public_id, expected_version=1, current_location="x") is None
        assert repo.end_session(bob.id, public_id, now=NOW) is None
        assert repo.set_archived(bob.id, public_id, True) is None
    member_session.expire_all()
    assert (save.status, save.version, save.is_archived, save.current_location) == (SESSION_ACTIVE, 1, False, "")
    assert member_session.scalar(select(func.count()).select_from(MemoryLog)) == 1 and repo.get_turn(alice.id, save.public_id, "m-1").turn_status == TURN_STREAMING


def test_listing_shows_only_the_players_own_saves_newest_activity_first(repo, member_session):
    alice, bob = make_user(member_session, 1), make_user(member_session, 2)
    old = open_save(repo, alice, now=NOW - timedelta(days=2))
    recent = open_save(repo, alice, now=NOW)
    open_save(repo, bob)
    assert [s.id for s in repo.list_owned(alice.id)] == [recent.id, old.id]
    assert len(repo.list_owned(bob.id)) == 1
    repo.set_archived(alice.id, old.public_id, True)
    assert [s.id for s in repo.list_owned(alice.id, include_archived=False)] == [recent.id]


def test_an_admin_account_has_no_special_path_to_other_players_saves(repo, member_session):
    admin = make_user(member_session, 1, role="admin")
    player = make_user(member_session, 2)
    save = open_save(repo, player)
    assert repo.get_owned(admin.id, save.public_id) is None and repo.list_owned(admin.id) == []
    public_methods = [name for name, _ in inspect.getmembers(SessionRepository, inspect.isfunction) if not name.startswith("_")]
    inherited = {"add", "get", "get_by_public_id", "list", "delete"}                # 基底類別的通用方法不在 API 路徑使用
    for name in set(public_methods) - inherited:
        parameters = list(inspect.signature(getattr(SessionRepository, name)).parameters)
        assert parameters[1] == "user_id", f"{name} 的第一個參數必須是 user_id"


# ---- 回合的唯一性與冪等 -----------------------------------------------------------------------

def test_starting_a_turn_creates_a_streaming_turn_without_a_turn_number(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    started = start(repo, alice, save)
    assert started.outcome == TURN_CREATED
    turn = started.turn
    assert (turn.turn_status, turn.turn_number, turn.seq, turn.user_action, turn.llm_raw_response) == (TURN_STREAMING, None, 1, "我走進山門", "")


def test_sequence_numbers_strictly_increase(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    seqs = []
    for index in range(3):
        started = start(repo, alice, save, client_msg_id=f"m-{index}")
        seqs.append(started.turn.seq)
        finish(repo, alice, save, client_msg_id=f"m-{index}")
    assert seqs == [1, 2, 3]
    assert [t.seq for t in repo.list_turns(alice.id, save.public_id)] == [1, 2, 3]
    assert [t.seq for t in repo.list_turns(alice.id, save.public_id, since_seq=1)] == [2, 3]


def test_resending_a_completed_turn_returns_it_and_adds_nothing(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    start(repo, alice, save)
    finish(repo, alice, save)
    again = start(repo, alice, save, action="不同的文字也不會覆蓋")
    assert again.outcome == TURN_REPLAYED and again.turn.turn_status == TURN_COMPLETE and again.turn.user_action == "我走進山門"
    assert member_session.scalar(select(func.count()).select_from(MemoryLog)) == 1


@pytest.mark.parametrize("interrupt", ["interrupt", "block"])
def test_resending_an_interrupted_or_blocked_turn_resets_that_row_instead_of_adding_one(repo, member_session, interrupt):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    start(repo, alice, save)
    if interrupt == "interrupt":
        repo.interrupt_turn(alice.id, save.public_id, "m-1", partial_response="山門前的晨")
    else:
        repo.block_turn(alice.id, save.public_id, "m-1")
    again = start(repo, alice, save, action="重新送出的行動")
    assert again.outcome == TURN_RESTARTED
    turn = again.turn
    assert (turn.turn_status, turn.user_action, turn.llm_raw_response, turn.turn_number, turn.seq) == (TURN_STREAMING, "重新送出的行動", "", None, 1)
    assert member_session.scalar(select(func.count()).select_from(MemoryLog)) == 1


def test_resending_a_turn_that_is_still_streaming_is_a_conflict(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    start(repo, alice, save)
    with pytest.raises(TurnInProgressError):
        start(repo, alice, save, action="又送一次")
    assert repo.get_turn(alice.id, save.public_id, "m-1").user_action == "我走進山門"


def test_the_same_client_message_id_in_different_saves_is_independent(repo, member_session):
    alice = make_user(member_session)
    first, second = open_save(repo, alice), open_save(repo, alice)
    first_turn, second_turn = start(repo, alice, first), start(repo, alice, second)       # 兩個存檔都用預設的 "m-1"
    assert (first_turn.outcome, second_turn.outcome) == (TURN_CREATED, TURN_CREATED)
    assert (first_turn.turn.seq, second_turn.turn.seq) == (1, 1) and first_turn.turn.id != second_turn.turn.id


def run_in_threads(engine, worker, count: int):
    barrier = Barrier(count)

    def task(index: int):
        with Session(engine) as s:
            barrier.wait(timeout=30)
            outcome = worker(s, index)
            s.commit()
            return outcome

    with ThreadPoolExecutor(max_workers=count) as pool:
        return [f.result(timeout=90) for f in [pool.submit(task, i) for i in range(count)]]


def seed_save(engine) -> tuple[int, str]:
    with Session(engine) as s:
        alice = make_user(s)
        save = open_save(SessionRepository(s), alice)
        s.commit()
        return alice.id, save.public_id


def test_two_requests_with_the_same_client_message_id_produce_exactly_one_turn(concurrent_engine):
    user_id, public_id = seed_save(concurrent_engine)

    def worker(s: Session, _index: int) -> str:
        try:
            started = SessionRepository(s).start_turn(user_id, public_id, client_msg_id="same", user_action="行動", now=NOW)
            return started.outcome
        except TurnInProgressError:
            return "conflict"

    outcomes = run_in_threads(concurrent_engine, worker, 2)
    assert sorted(outcomes) == ["conflict", "created"], outcomes
    with Session(concurrent_engine) as s:
        assert s.scalar(select(func.count()).select_from(MemoryLog)) == 1


def test_parallel_turns_with_different_ids_get_distinct_sequence_numbers(concurrent_engine):
    user_id, public_id = seed_save(concurrent_engine)

    def worker(s: Session, index: int) -> int:
        return SessionRepository(s).start_turn(user_id, public_id, client_msg_id=f"m-{index}", user_action="行動", now=NOW).turn.seq

    seqs = run_in_threads(concurrent_engine, worker, 4)
    assert sorted(seqs) == [1, 2, 3, 4]
    with Session(concurrent_engine) as s:
        assert sorted(s.scalars(select(MemoryLog.seq))) == [1, 2, 3, 4]


# ---- 中斷的回合不計輪 -------------------------------------------------------------------------

def test_a_turn_number_is_assigned_only_when_the_turn_completes(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    turn = start(repo, alice, save).turn
    assert turn.turn_number is None
    done = finish(repo, alice, save)
    assert (done.turn_status, done.turn_number, done.llm_raw_response, done.structured_result) == (TURN_COMPLETE, 1, "山門前的晨霧散開了。", {"ok": True})


def test_an_interrupted_turn_takes_no_turn_number_and_the_next_completed_one_gets_two(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    start(repo, alice, save, "m-1")
    finish(repo, alice, save, "m-1")
    start(repo, alice, save, "m-2")
    interrupted = repo.interrupt_turn(alice.id, save.public_id, "m-2", partial_response="半截的回應")
    assert (interrupted.turn_status, interrupted.turn_number, interrupted.llm_raw_response) == (TURN_INTERRUPTED, None, "半截的回應")
    start(repo, alice, save, "m-3")
    third = finish(repo, alice, save, "m-3")
    assert third.turn_number == 2 and third.seq == 3
    assert repo.count_complete_turns(alice.id, save.public_id) == 2


def test_a_blocked_turn_takes_no_turn_number_and_stays_out_of_the_count(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    start(repo, alice, save, "m-1")
    blocked = repo.block_turn(alice.id, save.public_id, "m-1")
    assert (blocked.turn_status, blocked.turn_number) == (TURN_BLOCKED, None)
    assert repo.count_complete_turns(alice.id, save.public_id) == 0


@pytest.mark.parametrize("finisher", ["complete", "interrupt", "block"])
@pytest.mark.parametrize("first", ["complete", "interrupt", "block"])
def test_only_a_streaming_turn_can_be_finished_and_a_refusal_changes_nothing(repo, member_session, first, finisher):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    start(repo, alice, save)

    def apply(kind):
        if kind == "complete":
            return finish(repo, alice, save)
        if kind == "interrupt":
            return repo.interrupt_turn(alice.id, save.public_id, "m-1", partial_response="片段")
        return repo.block_turn(alice.id, save.public_id, "m-1")

    settled = apply(first)
    snapshot = (settled.turn_status, settled.turn_number, settled.llm_raw_response)
    with pytest.raises(InvalidTurnTransitionError):
        apply(finisher)
    member_session.refresh(settled)
    assert (settled.turn_status, settled.turn_number, settled.llm_raw_response) == snapshot


def test_finishing_a_turn_that_does_not_exist_returns_nothing(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    assert finish(repo, alice, save, "never-started") is None
    assert repo.interrupt_turn(alice.id, save.public_id, "never-started", partial_response="") is None


def test_starting_and_completing_a_turn_updates_the_last_activity_time(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice, now=NOW - timedelta(days=30))
    start(repo, alice, save, now=NOW - timedelta(days=1))
    member_session.refresh(save)
    assert save.last_active_at == NOW - timedelta(days=1)
    finish(repo, alice, save, now=NOW)
    member_session.refresh(save)
    assert save.last_active_at == NOW


# ---- 世界狀態的版本號 -------------------------------------------------------------------------

def test_updating_the_world_state_with_the_current_version_succeeds_and_bumps_it(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    updated = repo.update_world_state(
        alice.id, save.public_id, expected_version=1, current_location="古劍山莊", status_summary="剛入山門", elapsed_months=3,
        entities_json={"npc": {"stage": "surface"}}, goals_json={"active": ["通過大測"], "archived": []}, milestone_progress_json={"m1": "done"},
    )
    assert (updated.version, updated.current_location, updated.status_summary, updated.elapsed_months) == (2, "古劍山莊", "剛入山門", 3)
    assert updated.entities_json == {"npc": {"stage": "surface"}} and updated.goals_json["active"] == ["通過大測"]
    assert repo.update_world_state(alice.id, save.public_id, expected_version=2, current_location="金寶閣").version == 3


def test_updating_with_a_stale_version_is_refused_and_changes_nothing(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    repo.update_world_state(alice.id, save.public_id, expected_version=1, current_location="古劍山莊")
    with pytest.raises(StaleSessionVersionError):
        repo.update_world_state(alice.id, save.public_id, expected_version=1, current_location="被擋下的地點", elapsed_months=99)
    member_session.refresh(save)
    assert (save.version, save.current_location, save.elapsed_months) == (2, "古劍山莊", 0)


def test_only_world_state_fields_can_be_written_through_the_update(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    for forbidden in ("status", "script_snapshot_json", "notebook_text", "content_tier", "version", "ended_at", "player_name", "is_archived"):
        with pytest.raises(ValueError):
            repo.update_world_state(alice.id, save.public_id, expected_version=1, **{forbidden: "x"})
    member_session.refresh(save)
    assert (save.version, save.status) == (1, SESSION_ACTIVE)


def test_two_parallel_updates_with_the_same_version_let_exactly_one_win(concurrent_engine):
    user_id, public_id = seed_save(concurrent_engine)

    def worker(s: Session, index: int) -> str:
        try:
            SessionRepository(s).update_world_state(user_id, public_id, expected_version=1, current_location=f"地點{index}")
            return "won"
        except StaleSessionVersionError:
            return "stale"

    assert sorted(run_in_threads(concurrent_engine, worker, 2)) == ["stale", "won"]
    with Session(concurrent_engine) as s:
        assert s.scalar(select(RpgSession.version)) == 2


# ---- 已完結的存檔 ---------------------------------------------------------------------------

def test_ending_a_save_marks_it_ended_with_the_time(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    assert repo.end_session(alice.id, save.public_id, now=NOW) is True
    assert (save.status, save.ended_at) == (SESSION_ENDED, NOW)


def test_ending_again_changes_nothing(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    repo.end_session(alice.id, save.public_id, now=NOW)
    assert repo.end_session(alice.id, save.public_id, now=NOW + timedelta(days=1)) is False
    assert save.ended_at == NOW


def test_starting_a_turn_in_an_ended_save_is_refused_with_no_new_data(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    start(repo, alice, save, "m-1")
    finish(repo, alice, save, "m-1")
    repo.end_session(alice.id, save.public_id, now=NOW)
    for client_msg_id in ("m-2", "m-1"):                           # 新的編號，與已完成的舊編號都不行
        with pytest.raises(SessionEndedError):
            start(repo, alice, save, client_msg_id)
    assert member_session.scalar(select(func.count()).select_from(MemoryLog)) == 1


def test_an_ended_save_stays_readable_with_its_history_state_and_notebook(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    start(repo, alice, save)
    finish(repo, alice, save)
    repo.update_world_state(alice.id, save.public_id, expected_version=1, current_location="終點")
    save.notebook_text = "記事簿的內容"
    repo.end_session(alice.id, save.public_id, now=NOW)
    member_session.commit()
    again = repo.get_owned(alice.id, save.public_id)
    assert (again.status, again.current_location, again.notebook_text) == (SESSION_ENDED, "終點", "記事簿的內容")
    assert [t.turn_status for t in repo.list_turns(alice.id, save.public_id)] == [TURN_COMPLETE]
    assert [s.id for s in repo.list_owned(alice.id)] == [save.id]


def test_an_ended_save_cannot_be_changed_back_to_active_or_have_its_world_state_written(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    repo.end_session(alice.id, save.public_id, now=NOW)
    with pytest.raises(SessionEndedError):
        repo.update_world_state(alice.id, save.public_id, expected_version=1, current_location="不該寫入")
    member_session.refresh(save)
    assert (save.status, save.current_location, save.version) == (SESSION_ENDED, "", 1)


def test_no_method_of_the_repository_can_set_a_save_back_to_active():
    """不可恢復：除了建立存檔的初始值，沒有任何程式碼把 status 寫成進行中。"""
    tree = ast.parse(inspect.getsource(session_repository))
    writes = [
        ast.unparse(node) for node in ast.walk(tree)
        if isinstance(node, ast.keyword) and node.arg == "status" and "SESSION_ACTIVE" in ast.unparse(node.value)
    ]
    assert writes == []
    assert any(isinstance(n, ast.keyword) and n.arg == "status" and "SESSION_ENDED" in ast.unparse(n.value) for n in ast.walk(tree))


def test_archiving_is_independent_of_the_story_ending(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    repo.set_archived(alice.id, save.public_id, True)
    assert (save.is_archived, save.status) == (True, SESSION_ACTIVE)
    repo.end_session(alice.id, save.public_id, now=NOW)
    assert save.is_archived is True
    repo.set_archived(alice.id, save.public_id, False)                 # 已完結的存檔也可以取消封存
    assert (save.is_archived, save.status) == (False, SESSION_ENDED)


def test_a_new_save_can_be_opened_from_the_same_script_after_the_old_one_ended(repo, member_session):
    alice = make_user(member_session)
    first = open_save(repo, alice)
    repo.end_session(alice.id, first.public_id, now=NOW)
    second = open_save(repo, alice)
    assert (second.status, second.id != first.id) == (SESSION_ACTIVE, True)
    assert start(repo, alice, second).outcome == TURN_CREATED
