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
    OPENING_FALLBACK, OPENING_GENERATING, OPENING_PENDING, OPENING_READY, SESSION_ACTIVE, SESSION_ENDED, TURN_BLOCKED,
    TURN_COMPLETE, TURN_INTERRUPTED, TURN_STREAMING,
)
from app.repositories import session_repository
from app.repositories.errors import (
    InvalidTurnTransitionError, SessionEndedError, StaleSessionVersionError, TurnInProgressError,
)
from app.repositories.session_repository import (
    OPENING_BUSY, OPENING_CLAIMED, OPENING_DONE, OPENING_TAKEN_OVER, TURN_CREATED, TURN_REPLAYED, TURN_RESTARTED, SessionRepository,
)
from tests.support.script_data import validated
from tests.support.protagonist import protagonist

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
        source_version_id=None, snapshot=SNAPSHOT, snapshot_spec_version=1, protagonist=protagonist(),
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


# ---- 主角（rpg-protagonist：存放與姓名、年齡同步）-----------------------------------------------------------


def test_the_protagonist_is_stored_and_name_and_age_follow_the_card(repo, member_session):
    alice = make_user(member_session)
    anchor = {"kind": "location", "ref": "古劍山莊", "text": "出生的地方"}
    save = open_save(repo, alice, protagonist=protagonist("林舟", 19, anchor=anchor, personality="沉默愛思考"))
    member_session.commit()
    member_session.expire_all()
    stored = repo.get_owned(alice.id, save.public_id)
    assert (stored.player_name, stored.character_age) == ("林舟", 19)
    assert stored.protagonist_json["character"]["name"] == "林舟" and stored.protagonist_json["character"]["age"] == 19
    assert stored.protagonist_json["anchor"] == anchor
    assert (stored.player_name, stored.character_age) == (stored.protagonist_json["character"]["name"], stored.protagonist_json["character"]["age"])


def test_a_save_without_an_anchor_keeps_the_card_and_an_empty_anchor(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    member_session.commit()
    assert save.protagonist_json["anchor"] is None and save.protagonist_json["character"]["name"] == "旅人"


def test_the_stored_protagonist_is_a_copy_so_later_changes_to_the_input_do_not_touch_it(repo, member_session):
    alice = make_user(member_session)
    given = protagonist("林舟", 19)
    save = open_save(repo, alice, protagonist=given)
    given["character"]["name"] = "被改的名字"
    member_session.commit()
    assert save.protagonist_json["character"]["name"] == "林舟" and save.player_name == "林舟"


def test_a_save_created_before_the_column_existed_reads_normally(repo, member_session):
    """遷移之前建立的存檔沒有主角資料：欄位為空，姓名與年齡照舊可讀。"""
    alice = make_user(member_session)
    legacy = RpgSession(user_id=alice.id, script_snapshot_json={"title": "舊"}, player_name="舊存檔", character_age=30)
    member_session.add(legacy)
    member_session.commit()
    member_session.expire_all()
    stored = repo.get_owned(alice.id, legacy.public_id)
    assert stored.protagonist_json is None and (stored.player_name, stored.character_age) == ("舊存檔", 30)


# ---- rpg-turn-engine：嘗試序號、逾時接手、同一存檔只有一個進行中的回合 ------------------------------------

def test_a_new_save_starts_with_the_opening_pending_and_empty(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    assert (save.opening_state, save.opening_text, save.opening_started_at) == (OPENING_PENDING, None, None)
    assert member_session.scalar(select(func.count()).select_from(MemoryLog)) == 0                     # 第 0 幕不是回合


def test_the_attempt_starts_at_one_and_each_reset_adds_one_but_a_replay_changes_nothing(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    assert start(repo, alice, save).turn.attempt == 1
    repo.interrupt_turn(alice.id, save.public_id, "m-1", partial_response="片段")
    assert start(repo, alice, save).turn.attempt == 2                                                   # 中斷後重送
    repo.block_turn(alice.id, save.public_id, "m-1")
    assert start(repo, alice, save).turn.attempt == 3                                                   # 被擋後重送
    finish(repo, alice, save)
    replayed = start(repo, alice, save)
    assert replayed.outcome == TURN_REPLAYED and replayed.turn.attempt == 3                             # 已完成的重送：不變


def test_a_streaming_turn_that_is_not_stale_conflicts_but_a_stale_one_is_taken_over_with_a_new_attempt(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    start(repo, alice, save, now=NOW)
    with pytest.raises(TurnInProgressError):
        start(repo, alice, save, now=NOW + timedelta(seconds=179))                                      # 剛好未逾時（預設 180 秒）
    taken = start(repo, alice, save, action="接手後的行動", now=NOW + timedelta(seconds=181))
    assert taken.outcome == TURN_RESTARTED
    assert (taken.turn.attempt, taken.turn.turn_status, taken.turn.user_action, taken.turn.seq) == (2, TURN_STREAMING, "接手後的行動", 1)
    assert member_session.scalar(select(func.count()).select_from(MemoryLog)) == 1


def test_the_stale_threshold_is_the_one_the_caller_passes(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    start(repo, alice, save, now=NOW)
    with pytest.raises(TurnInProgressError):
        repo.start_turn(alice.id, save.public_id, client_msg_id="m-1", user_action="a", now=NOW + timedelta(seconds=100), stale_after_seconds=120)
    taken = repo.start_turn(alice.id, save.public_id, client_msg_id="m-1", user_action="a", now=NOW + timedelta(seconds=100), stale_after_seconds=60)
    assert taken.outcome == TURN_RESTARTED and taken.turn.attempt == 2


def test_another_message_id_conflicts_while_a_turn_is_streaming_and_adds_nothing(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    start(repo, alice, save, "m-1", now=NOW)
    with pytest.raises(TurnInProgressError):
        start(repo, alice, save, "m-2", now=NOW + timedelta(seconds=10))
    assert member_session.scalar(select(func.count()).select_from(MemoryLog)) == 1
    finish(repo, alice, save, "m-1")
    assert start(repo, alice, save, "m-2", now=NOW + timedelta(seconds=20)).outcome == TURN_CREATED         # 上一個離開生成中之後就可以


def test_resending_an_interrupted_turn_conflicts_while_another_turn_is_streaming(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    start(repo, alice, save, "m-1", now=NOW)
    repo.interrupt_turn(alice.id, save.public_id, "m-1", partial_response="片段")
    start(repo, alice, save, "m-2", now=NOW + timedelta(seconds=5))
    with pytest.raises(TurnInProgressError):
        start(repo, alice, save, "m-1", now=NOW + timedelta(seconds=10))
    assert repo.get_turn(alice.id, save.public_id, "m-1").turn_status == TURN_INTERRUPTED


def test_a_stale_streaming_turn_with_another_id_is_marked_interrupted_when_a_new_turn_starts(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    start(repo, alice, save, "m-1", now=NOW)
    started = start(repo, alice, save, "m-2", now=NOW + timedelta(seconds=181))
    assert started.outcome == TURN_CREATED and started.turn.seq == 2
    old = repo.get_turn(alice.id, save.public_id, "m-1")
    assert (old.turn_status, old.turn_number, old.attempt) == (TURN_INTERRUPTED, None, 1)
    assert repo.count_complete_turns(alice.id, save.public_id) == 0


def test_parallel_turns_with_different_ids_let_exactly_one_start_and_the_rest_conflict(concurrent_engine):
    user_id, public_id = seed_save(concurrent_engine)

    def worker(s: Session, index: int) -> str:
        try:
            return SessionRepository(s).start_turn(user_id, public_id, client_msg_id=f"m-{index}", user_action="行動", now=NOW).outcome
        except TurnInProgressError:
            return "conflict"

    outcomes = run_in_threads(concurrent_engine, worker, 4)
    assert sorted(outcomes) == ["conflict", "conflict", "conflict", "created"], outcomes
    with Session(concurrent_engine) as s:
        assert list(s.scalars(select(MemoryLog.seq))) == [1]


def test_parallel_takeovers_of_a_stale_turn_let_exactly_one_win_and_add_one_attempt(concurrent_engine):
    user_id, public_id = seed_save(concurrent_engine)
    with Session(concurrent_engine) as s:
        SessionRepository(s).start_turn(user_id, public_id, client_msg_id="same", user_action="行動", now=NOW)
        s.commit()
    later = NOW + timedelta(seconds=300)

    def worker(s: Session, _index: int) -> str:
        try:
            return SessionRepository(s).start_turn(user_id, public_id, client_msg_id="same", user_action="行動", now=later).outcome
        except TurnInProgressError:
            return "conflict"

    outcomes = run_in_threads(concurrent_engine, worker, 3)
    assert sorted(outcomes) == ["conflict", "conflict", "restarted"], outcomes
    with Session(concurrent_engine) as s:
        assert s.scalar(select(MemoryLog.attempt)) == 2


# ---- 收尾與帶 attempt 的轉移 ---------------------------------------------------------------------------

def test_finish_turn_completes_the_turn_writes_the_world_state_and_can_end_the_story_in_one_call(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    start(repo, alice, save)
    done = repo.finish_turn(
        alice.id, save.public_id, "m-1", llm_raw_response="劇情", structured_result={"state": "ok"}, now=NOW,
        world_changes={"current_location": "山門", "elapsed_months": 3}, expected_version=1, end_story=True,
    )
    assert (done.world_updated, done.ended) == (True, True)
    assert (done.turn.turn_status, done.turn.turn_number, done.turn.llm_raw_response) == (TURN_COMPLETE, 1, "劇情")
    member_session.refresh(save)
    assert (save.current_location, save.elapsed_months, save.version, save.status) == ("山門", 3, 2, SESSION_ENDED)


def test_finish_turn_without_world_changes_only_completes_the_turn(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    start(repo, alice, save)
    done = repo.finish_turn(alice.id, save.public_id, "m-1", llm_raw_response="劇情", structured_result=None, now=NOW)
    assert (done.world_updated, done.ended, done.turn.turn_number) == (False, False, 1)
    member_session.refresh(save)
    assert (save.version, save.status) == (1, SESSION_ACTIVE)


def test_finish_turn_with_a_stale_version_still_completes_the_turn_but_leaves_the_world_state_alone(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    start(repo, alice, save)
    repo.update_world_state(alice.id, save.public_id, expected_version=1, current_location="別處")          # 版本變成 2
    done = repo.finish_turn(
        alice.id, save.public_id, "m-1", llm_raw_response="劇情", structured_result=None, now=NOW,
        world_changes={"current_location": "山門"}, expected_version=1,
    )
    assert done.world_updated is False and done.turn.turn_status == TURN_COMPLETE
    member_session.refresh(save)
    assert (save.current_location, save.version) == ("別處", 2)


def test_finish_turn_with_a_wrong_attempt_changes_nothing_at_all(repo, member_session):
    """被接手而過時的產生器（attempt 1）不能完成新的嘗試（attempt 2）：回合、世界狀態、存檔狀態都不動。"""
    alice = make_user(member_session)
    save = open_save(repo, alice)
    start(repo, alice, save, now=NOW)
    start(repo, alice, save, now=NOW + timedelta(seconds=300))                                          # 接手：attempt 2
    member_session.commit()
    with pytest.raises(InvalidTurnTransitionError):
        repo.finish_turn(
            alice.id, save.public_id, "m-1", llm_raw_response="過時的劇情", structured_result=None, now=NOW, attempt=1,
            world_changes={"current_location": "不該寫入"}, expected_version=1, end_story=True,
        )
    member_session.rollback()                                       # 呼叫端遇到例外會回復交易：這裡也確認沒有半套寫入
    turn = repo.get_turn(alice.id, save.public_id, "m-1")
    assert (turn.turn_status, turn.llm_raw_response, turn.attempt) == (TURN_STREAMING, "", 2)
    member_session.refresh(save)
    assert (save.current_location, save.version, save.status) == ("", 1, SESSION_ACTIVE)
    done = repo.finish_turn(alice.id, save.public_id, "m-1", llm_raw_response="新的劇情", structured_result=None, now=NOW, attempt=2)
    assert done.turn.turn_status == TURN_COMPLETE


def test_a_stale_producer_cannot_interrupt_or_block_the_new_attempt(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    start(repo, alice, save, now=NOW)
    start(repo, alice, save, now=NOW + timedelta(seconds=300))
    with pytest.raises(InvalidTurnTransitionError):
        repo.interrupt_turn(alice.id, save.public_id, "m-1", partial_response="舊的片段", attempt=1)
    with pytest.raises(InvalidTurnTransitionError):
        repo.block_turn(alice.id, save.public_id, "m-1", attempt=1)
    assert repo.get_turn(alice.id, save.public_id, "m-1").turn_status == TURN_STREAMING
    assert repo.interrupt_turn(alice.id, save.public_id, "m-1", partial_response="新的片段", attempt=2).turn_status == TURN_INTERRUPTED


def test_blocked_text_is_kept_on_the_streaming_turn_and_the_final_block_records_the_categories(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    start(repo, alice, save)
    first = repo.save_blocked_text(
        alice.id, save.public_id, "m-1", text="被擋的第一段", structured_result={"blocked": {"categories": ["tier_mismatch"], "auto_retry": True}},
    )
    assert (first.turn_status, first.llm_raw_response) == (TURN_STREAMING, "被擋的第一段")              # 自動重生期間仍在生成中
    blocked = repo.block_turn(
        alice.id, save.public_id, "m-1", partial_response="被擋的第二段",
        structured_result={"blocked": {"categories": ["minor_sexual"], "auto_retry": True}},
    )
    assert (blocked.turn_status, blocked.turn_number, blocked.llm_raw_response) == (TURN_BLOCKED, None, "被擋的第二段")
    assert blocked.structured_result == {"blocked": {"categories": ["minor_sexual"], "auto_retry": True}}
    with pytest.raises(InvalidTurnTransitionError):
        repo.save_blocked_text(alice.id, save.public_id, "m-1", text="x", structured_result={"blocked": {}})      # 已不在生成中


def test_the_new_turn_methods_return_nothing_for_a_non_owner(repo, member_session):
    alice, bob = make_user(member_session, 1), make_user(member_session, 2)
    save = open_save(repo, alice)
    start(repo, alice, save)
    assert repo.finish_turn(bob.id, save.public_id, "m-1", llm_raw_response="r", structured_result=None, now=NOW) is None
    assert repo.save_blocked_text(bob.id, save.public_id, "m-1", text="x", structured_result={}) is None
    assert repo.claim_opening(bob.id, save.public_id, now=NOW) is None
    assert repo.finish_opening(bob.id, save.public_id, claimed_at=NOW, state=OPENING_READY, text="x") is None
    assert repo.reset_opening(bob.id, save.public_id, claimed_at=NOW) is None
    member_session.expire_all()
    assert (save.opening_state, repo.get_turn(alice.id, save.public_id, "m-1").turn_status) == (OPENING_PENDING, TURN_STREAMING)


# ---- 第 0 幕：搶佔、接手、完成、退回 -----------------------------------------------------------------------

def test_claiming_the_opening_moves_pending_to_generating_and_records_the_start_time(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    outcome, claimed = repo.claim_opening(alice.id, save.public_id, now=NOW)
    assert outcome == OPENING_CLAIMED and claimed.opening_state == OPENING_GENERATING and claimed.opening_started_at == NOW


def test_a_second_claim_is_busy_until_the_first_is_stale_and_then_takes_over(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    repo.claim_opening(alice.id, save.public_id, now=NOW)
    assert repo.claim_opening(alice.id, save.public_id, now=NOW + timedelta(seconds=89))[0] == OPENING_BUSY
    outcome, taken = repo.claim_opening(alice.id, save.public_id, now=NOW + timedelta(seconds=91))
    assert outcome == OPENING_TAKEN_OVER and taken.opening_started_at == NOW + timedelta(seconds=91)


def test_a_producer_that_was_taken_over_cannot_overwrite_the_new_result(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    repo.claim_opening(alice.id, save.public_id, now=NOW)
    second_start = NOW + timedelta(seconds=120)
    repo.claim_opening(alice.id, save.public_id, now=second_start)                                      # 接手
    assert repo.finish_opening(alice.id, save.public_id, claimed_at=NOW, state=OPENING_READY, text="過時的開場") is False
    assert repo.reset_opening(alice.id, save.public_id, claimed_at=NOW) is False
    member_session.refresh(save)
    assert (save.opening_state, save.opening_text) == (OPENING_GENERATING, None)
    assert repo.finish_opening(alice.id, save.public_id, claimed_at=second_start, state=OPENING_READY, text="新的開場") is True
    member_session.refresh(save)
    assert (save.opening_state, save.opening_text) == (OPENING_READY, "新的開場")


@pytest.mark.parametrize("state, text", [(OPENING_READY, "生成的開場"), (OPENING_FALLBACK, "劇本的初始對話"), (OPENING_FALLBACK, "")])
def test_a_finished_opening_is_never_claimed_again(repo, member_session, state, text):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    repo.claim_opening(alice.id, save.public_id, now=NOW)
    assert repo.finish_opening(alice.id, save.public_id, claimed_at=NOW, state=state, text=text) is True
    for later in (NOW + timedelta(seconds=1), NOW + timedelta(days=1)):
        outcome, again = repo.claim_opening(alice.id, save.public_id, now=later)
        assert outcome == OPENING_DONE and (again.opening_state, again.opening_text) == (state, text)
    assert repo.finish_opening(alice.id, save.public_id, claimed_at=NOW, state=OPENING_READY, text="不該覆蓋") is False


def test_finishing_the_opening_only_accepts_ready_or_fallback(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    repo.claim_opening(alice.id, save.public_id, now=NOW)
    for state in (OPENING_PENDING, OPENING_GENERATING, "bogus"):
        with pytest.raises(ValueError):
            repo.finish_opening(alice.id, save.public_id, claimed_at=NOW, state=state, text="x")


def test_resetting_the_opening_returns_to_pending_and_discards_the_partial_text(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    repo.claim_opening(alice.id, save.public_id, now=NOW)
    assert repo.reset_opening(alice.id, save.public_id, claimed_at=NOW) is True
    member_session.refresh(save)
    assert (save.opening_state, save.opening_text, save.opening_started_at) == (OPENING_PENDING, None, None)
    assert repo.claim_opening(alice.id, save.public_id, now=NOW + timedelta(seconds=5))[0] == OPENING_CLAIMED       # 下次進入再生成


def test_a_save_from_before_the_columns_existed_is_treated_as_done_and_never_generated(repo, member_session):
    alice = make_user(member_session)
    legacy = RpgSession(user_id=alice.id, script_snapshot_json={"title": "舊"}, player_name="舊存檔", character_age=30)
    member_session.add(legacy)
    member_session.commit()
    outcome, found = repo.claim_opening(alice.id, legacy.public_id, now=NOW)
    assert outcome == OPENING_DONE and (found.opening_state, found.opening_text) == (None, None)


def test_two_claims_at_once_let_exactly_one_generate(concurrent_engine):
    user_id, public_id = seed_save(concurrent_engine)

    def worker(s: Session, _index: int) -> str:
        return SessionRepository(s).claim_opening(user_id, public_id, now=NOW)[0]

    outcomes = run_in_threads(concurrent_engine, worker, 4)
    assert sorted(outcomes) == [OPENING_BUSY, OPENING_BUSY, OPENING_BUSY, OPENING_CLAIMED], outcomes


def test_the_export_carries_the_opening_state_and_text_but_not_the_start_time(repo, member_session):
    alice = make_user(member_session)
    save = open_save(repo, alice)
    repo.claim_opening(alice.id, save.public_id, now=NOW)
    repo.finish_opening(alice.id, save.public_id, claimed_at=NOW, state=OPENING_READY, text="霧中的山門")
    (exported,) = list(repo.export_sessions(alice.id))
    assert (exported["opening_state"], exported["opening_text"]) == (OPENING_READY, "霧中的山門")
    assert "opening_started_at" not in exported
