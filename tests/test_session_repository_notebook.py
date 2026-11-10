"""rpg-notebook 規格：`SessionRepository` 的記事簿方法（抓取的視窗、鎖、AI 追加、標記推進、玩家儲存）。

一般測試以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行；並行測試另用 `concurrent_engine`，
以真實的執行緒與獨立連線證明：重複的抓取只有一次生效、抓取的鎖讓追加建立在玩家剛儲存的文字上、AI 追加不會被舊版本的儲存覆蓋。
"""
import ast
import inspect
import threading
import time

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MemoryLog, RpgSession
from app.models.rpg_session import SESSION_ENDED
from app.repositories import session_repository
from app.repositories.errors import SessionEndedError, StaleNotebookVersionError
from app.repositories.session_repository import NotebookLock, SessionRepository
from tests.test_session_repository import NOW, finish, make_user, open_save, run_in_threads, seed_save, start

MISSING = "00000000-0000-4000-8000-000000000000"


@pytest.fixture()
def repo(member_session):
    return SessionRepository(member_session)


def complete(repo, user, save, count: int, first: int = 1) -> None:
    """依序完成 `count` 個回合（輪數接續目前已完成的）。"""
    for index in range(first, first + count):
        start(repo, user, save, client_msg_id=f"m-{index}", action=f"行動{index}")
        finish(repo, user, save, client_msg_id=f"m-{index}", reply=f"劇情{index}")


def state_of(member_session, save):
    member_session.expire_all()
    return save.notebook_text, save.notebook_version, save.notebook_upto_turn


@pytest.fixture()
def world(repo, member_session):
    alice = make_user(member_session, 1)
    return alice, open_save(repo, alice)


# ---- turns_in_range ---------------------------------------------------------------------------

def test_the_range_returns_only_complete_turns_in_turn_order_and_includes_both_ends(repo, member_session, world):
    alice, save = world
    complete(repo, alice, save, 12)
    turns = repo.turns_in_range(alice.id, save.public_id, 1, 10)
    assert [t.turn_number for t in turns] == list(range(1, 11))
    assert [t.llm_raw_response for t in turns][:2] == ["劇情1", "劇情2"] and [t.user_action for t in turns][-1] == "行動10"
    assert [t.turn_number for t in repo.turns_in_range(alice.id, save.public_id, 4, 6)] == [4, 5, 6]
    assert [t.turn_number for t in repo.turns_in_range(alice.id, save.public_id, 11, 20)] == [11, 12]
    assert repo.turns_in_range(alice.id, save.public_id, 13, 20) == [] and repo.turns_in_range(alice.id, save.public_id, 10, 1) == []


def test_interrupted_blocked_and_streaming_turns_are_not_in_the_window(repo, member_session, world):
    alice, save = world
    complete(repo, alice, save, 2)
    start(repo, alice, save, client_msg_id="gone", action="中斷的行動")
    repo.interrupt_turn(alice.id, save.public_id, "gone", partial_response="半截")
    start(repo, alice, save, client_msg_id="nope", action="被擋的行動")
    repo.block_turn(alice.id, save.public_id, "nope", partial_response="被擋的文字")
    complete(repo, alice, save, 1, first=3)
    start(repo, alice, save, client_msg_id="live", action="進行中的行動")            # 同一存檔同時只能有一個生成中：放最後
    turns = repo.turns_in_range(alice.id, save.public_id, 1, 10)
    assert [(t.turn_number, t.user_action) for t in turns] == [(1, "行動1"), (2, "行動2"), (3, "行動3")]
    assert member_session.scalar(select(MemoryLog.id).where(MemoryLog.client_msg_id == "gone")) is not None      # 它們還在，只是不在視窗內


def test_the_range_only_covers_the_one_save(repo, member_session, world):
    alice, save = world
    other = open_save(repo, alice)
    complete(repo, alice, save, 3)
    complete(repo, alice, other, 2)
    assert [t.user_action for t in repo.turns_in_range(alice.id, other.public_id, 1, 10)] == ["行動1", "行動2"]
    assert len(repo.turns_in_range(alice.id, save.public_id, 1, 10)) == 3


# ---- lock_notebook_for_capture ------------------------------------------------------------------

def test_the_lock_returns_the_text_version_and_marker_and_changes_nothing(repo, member_session, world):
    alice, save = world
    lock = repo.lock_notebook_for_capture(alice.id, save.public_id, 10)
    assert lock == NotebookLock(text="", version=1, upto_turn=0)
    assert state_of(member_session, save) == ("", 1, 0) and save.last_active_at == NOW


def test_the_lock_reads_the_latest_text_after_the_player_saved(repo, member_session, world):
    alice, save = world
    repo.save_notebook(alice.id, save.public_id, text="玩家寫的設定", expected_version=1)
    assert repo.lock_notebook_for_capture(alice.id, save.public_id, 10) == NotebookLock("玩家寫的設定", 2, 0)


def test_the_lock_is_refused_when_the_marker_has_already_reached_the_boundary(repo, member_session, world):
    alice, save = world
    assert repo.advance_notebook_marker(alice.id, save.public_id, 10) is True
    assert repo.lock_notebook_for_capture(alice.id, save.public_id, 10) is None
    assert repo.lock_notebook_for_capture(alice.id, save.public_id, 5) is None
    assert repo.lock_notebook_for_capture(alice.id, save.public_id, 20) == NotebookLock("", 1, 10)


def test_the_lock_is_refused_for_an_ended_save_and_for_a_non_owner(repo, member_session, world):
    alice, save = world
    bob = make_user(member_session, 2)
    for public_id in (save.public_id, MISSING):
        assert repo.lock_notebook_for_capture(bob.id, public_id, 10) is None
    repo.end_session(alice.id, save.public_id, now=NOW)
    assert repo.lock_notebook_for_capture(alice.id, save.public_id, 10) is None


# ---- append_notebook --------------------------------------------------------------------------

def test_an_append_writes_the_text_bumps_the_version_and_sets_the_marker(repo, member_session, world):
    alice, save = world
    assert repo.append_notebook(alice.id, save.public_id, text="第一條\n第二條", boundary=10) is True
    assert state_of(member_session, save) == ("第一條\n第二條", 2, 10)
    assert repo.lock_notebook_for_capture(alice.id, save.public_id, 20) == NotebookLock("第一條\n第二條", 2, 10)


def test_two_consecutive_appends_for_the_same_boundary_take_effect_only_once(repo, member_session, world):
    alice, save = world
    assert repo.append_notebook(alice.id, save.public_id, text="只算一次", boundary=10) is True
    assert repo.append_notebook(alice.id, save.public_id, text="只算一次\n重複的", boundary=10) is False
    assert state_of(member_session, save) == ("只算一次", 2, 10)


def test_the_marker_never_moves_backwards(repo, member_session, world):
    alice, save = world
    repo.append_notebook(alice.id, save.public_id, text="第二個視窗", boundary=20)
    assert repo.append_notebook(alice.id, save.public_id, text="舊視窗的結果", boundary=10) is False
    assert state_of(member_session, save) == ("第二個視窗", 2, 20)


def test_an_append_to_an_ended_save_or_a_non_owners_save_changes_nothing(repo, member_session, world):
    alice, save = world
    bob = make_user(member_session, 2)
    for public_id in (save.public_id, MISSING):
        assert repo.append_notebook(bob.id, public_id, text="別人的", boundary=10) is None
    repo.end_session(alice.id, save.public_id, now=NOW)
    assert repo.append_notebook(alice.id, save.public_id, text="完結後", boundary=10) is False
    assert state_of(member_session, save) == ("", 1, 0) and save.status == SESSION_ENDED


# ---- advance_notebook_marker --------------------------------------------------------------------

def test_advancing_the_marker_changes_neither_the_text_nor_the_version(repo, member_session, world):
    alice, save = world
    repo.save_notebook(alice.id, save.public_id, text="玩家的設定", expected_version=1)
    assert repo.advance_notebook_marker(alice.id, save.public_id, 10) is True
    assert state_of(member_session, save) == ("玩家的設定", 2, 10)


def test_advancing_only_goes_forward(repo, member_session, world):
    alice, save = world
    assert repo.advance_notebook_marker(alice.id, save.public_id, 20) is True
    assert repo.advance_notebook_marker(alice.id, save.public_id, 20) is False
    assert repo.advance_notebook_marker(alice.id, save.public_id, 10) is False
    assert state_of(member_session, save)[2] == 20


def test_advancing_is_refused_for_an_ended_save_and_a_non_owner(repo, member_session, world):
    alice, save = world
    bob = make_user(member_session, 2)
    assert repo.advance_notebook_marker(bob.id, save.public_id, 10) is None and repo.advance_notebook_marker(bob.id, MISSING, 10) is None
    repo.end_session(alice.id, save.public_id, now=NOW)
    assert repo.advance_notebook_marker(alice.id, save.public_id, 10) is False
    assert state_of(member_session, save)[2] == 0


# ---- save_notebook ----------------------------------------------------------------------------

def test_a_save_with_the_current_version_writes_the_text_and_bumps_the_version(repo, member_session, world):
    alice, save = world
    saved = repo.save_notebook(alice.id, save.public_id, text="師妹的父親是宗門長老。", expected_version=1)
    assert saved is save and state_of(member_session, save) == ("師妹的父親是宗門長老。", 2, 0)
    repo.save_notebook(alice.id, save.public_id, text="", expected_version=2)                  # 可以清空
    assert state_of(member_session, save) == ("", 3, 0)


def test_a_save_never_touches_the_marker(repo, member_session, world):
    alice, save = world
    repo.advance_notebook_marker(alice.id, save.public_id, 20)
    repo.save_notebook(alice.id, save.public_id, text="編輯", expected_version=1)
    assert state_of(member_session, save) == ("編輯", 2, 20)


def test_a_save_with_a_stale_version_is_refused_and_changes_nothing(repo, member_session, world):
    alice, save = world
    with pytest.raises(StaleNotebookVersionError):
        repo.save_notebook(alice.id, save.public_id, text="舊版本的編輯", expected_version=0)
    with pytest.raises(StaleNotebookVersionError):
        repo.save_notebook(alice.id, save.public_id, text="未來版本的編輯", expected_version=2)
    assert state_of(member_session, save) == ("", 1, 0)


def test_an_append_by_the_ai_bumps_the_version_so_the_players_old_save_is_refused_and_the_ai_text_stays(repo, member_session, world):
    alice, save = world
    repo.append_notebook(alice.id, save.public_id, text="AI 抓取的重點", boundary=10)
    with pytest.raises(StaleNotebookVersionError):
        repo.save_notebook(alice.id, save.public_id, text="玩家以舊版本編輯", expected_version=1)
    assert state_of(member_session, save) == ("AI 抓取的重點", 2, 10)


def test_saving_an_ended_save_raises_ended_even_when_the_version_is_also_stale(repo, member_session, world):
    alice, save = world
    repo.end_session(alice.id, save.public_id, now=NOW)
    for version in (1, 7):
        with pytest.raises(SessionEndedError):
            repo.save_notebook(alice.id, save.public_id, text="完結後的編輯", expected_version=version)
    assert state_of(member_session, save) == ("", 1, 0)


def test_saving_returns_nothing_for_a_non_owner_and_changes_nothing(repo, member_session, world):
    alice, save = world
    bob = make_user(member_session, 2)
    for public_id in (save.public_id, MISSING):
        assert repo.save_notebook(bob.id, public_id, text="別人的編輯", expected_version=1) is None
    assert state_of(member_session, save) == ("", 1, 0)


# ---- 隔離 --------------------------------------------------------------------------------------

def test_the_notebook_methods_never_touch_the_players_other_saves(repo, member_session, world):
    alice, save = world
    other = open_save(repo, alice)
    repo.append_notebook(alice.id, save.public_id, text="這個存檔的重點", boundary=10)
    repo.save_notebook(alice.id, save.public_id, text="玩家改過", expected_version=2)
    repo.advance_notebook_marker(alice.id, save.public_id, 20)
    assert state_of(member_session, other) == ("", 1, 0) and repo.lock_notebook_for_capture(alice.id, other.public_id, 10) == NotebookLock("", 1, 0)


def test_the_notebook_methods_leave_the_world_state_and_activity_time_alone(repo, member_session, world):
    alice, save = world
    repo.append_notebook(alice.id, save.public_id, text="重點", boundary=10)
    repo.save_notebook(alice.id, save.public_id, text="編輯", expected_version=2)
    member_session.expire_all()
    assert (save.version, save.status, save.elapsed_months, save.last_active_at) == (1, "active", 0, NOW)


def test_only_the_save_conditions_decide_what_the_notebook_methods_write():
    """沒有任何記事簿方法能把存檔改回進行中，也沒有能不帶使用者條件的寫入：每個 UPDATE 都經 `_update_owned`。"""
    tree = ast.parse(inspect.getsource(session_repository))
    for name in ("lock_notebook_for_capture", "append_notebook", "advance_notebook_marker", "save_notebook"):
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
        source = ast.unparse(method)
        assert "_update_owned" in source and "get_owned" in source and "SESSION_ACTIVE" in source and "update(" not in source, name
        assert next(a for a in method.args.args if a.arg != "self").arg == "user_id"


# ---- 並行（真實的執行緒與獨立連線）-----------------------------------------------------------------

def test_two_captures_for_the_same_boundary_apply_exactly_once(concurrent_engine):
    user_id, public_id = seed_save(concurrent_engine)

    def worker(s: Session, index: int) -> str:
        repository = SessionRepository(s)
        lock = repository.lock_notebook_for_capture(user_id, public_id, 10)
        if lock is None:
            return "skipped"
        repository.append_notebook(user_id, public_id, text=f"{lock.text}抓取{index}\n", boundary=10)
        return "applied"

    assert sorted(run_in_threads(concurrent_engine, worker, 3)) == ["applied", "skipped", "skipped"]
    with Session(concurrent_engine) as s:
        save = s.scalar(select(RpgSession))
        assert save.notebook_version == 2 and save.notebook_upto_turn == 10 and save.notebook_text.count("抓取") == 1


def test_a_capture_waits_for_a_player_save_in_progress_and_appends_to_the_players_text(concurrent_engine):
    """玩家的儲存先取得列鎖（交易還沒提交）；抓取的鎖必須等它，之後讀到的是玩家剛存的文字，不是舊文字。"""
    user_id, public_id = seed_save(concurrent_engine)
    saved_but_not_committed = threading.Event()
    result: dict[str, object] = {}

    def player() -> None:
        with Session(concurrent_engine) as s:
            SessionRepository(s).save_notebook(user_id, public_id, text="玩家剛存的設定", expected_version=1)
            saved_but_not_committed.set()
            time.sleep(0.8)                                      # 交易保持開啟，抓取在這段時間內啟動
            s.commit()

    def capture() -> None:
        saved_but_not_committed.wait(timeout=30)
        with Session(concurrent_engine) as s:
            repository = SessionRepository(s)
            lock = repository.lock_notebook_for_capture(user_id, public_id, 10)
            result["lock"] = lock
            repository.append_notebook(user_id, public_id, text=f"{lock.text}\nAI 的重點", boundary=10)
            s.commit()

    threads = [threading.Thread(target=player), threading.Thread(target=capture)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert result["lock"] == NotebookLock("玩家剛存的設定", 2, 0)
    with Session(concurrent_engine) as s:
        save = s.scalar(select(RpgSession))
        assert (save.notebook_text, save.notebook_version, save.notebook_upto_turn) == ("玩家剛存的設定\nAI 的重點", 3, 10)


def test_a_player_save_during_a_capture_is_refused_as_stale_and_the_ai_text_survives(concurrent_engine):
    user_id, public_id = seed_save(concurrent_engine)
    locked = threading.Event()
    outcome: dict[str, str] = {}

    def capture() -> None:
        with Session(concurrent_engine) as s:
            repository = SessionRepository(s)
            lock = repository.lock_notebook_for_capture(user_id, public_id, 10)
            locked.set()
            time.sleep(0.8)                                      # 持有鎖的期間玩家按下儲存
            repository.append_notebook(user_id, public_id, text=f"{lock.text}AI 的重點", boundary=10)
            s.commit()

    def player() -> None:
        locked.wait(timeout=30)
        with Session(concurrent_engine) as s:
            try:
                SessionRepository(s).save_notebook(user_id, public_id, text="玩家以舊版本編輯", expected_version=1)
                outcome["player"] = "saved"
            except StaleNotebookVersionError:
                outcome["player"] = "stale"
            s.commit()

    threads = [threading.Thread(target=capture), threading.Thread(target=player)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert outcome["player"] == "stale"
    with Session(concurrent_engine) as s:
        save = s.scalar(select(RpgSession))
        assert (save.notebook_text, save.notebook_version, save.notebook_upto_turn) == ("AI 的重點", 2, 10)
