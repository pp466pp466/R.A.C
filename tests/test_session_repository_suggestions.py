"""rpg-story-view 規格（`rpg-sessions` 的「存檔保存行動建議」與 `rpg-suggestions` 的錨點）：`SessionRepository` 的建議方法。

一般測試以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行；並行測試另用 `concurrent_engine`，以真實的執行緒與
獨立連線證明：並行的兩個寫入只保留一份、寫入與新回合並行時不會把舊錨點的建議存成有效。

錨點＝最後一個完成回合的 (`seq`, `revision`)，尚無完成回合（第 0 幕）為 (0, 0)；錨點相等才有效；寫入不改 `version`、世界狀態、記事簿、
最後活動時間與回合列。
"""
import threading
import time

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MemoryLog, RpgSession
from app.repositories import session_repository
from app.repositories.session_repository import (
    SUGGESTIONS_ENDED, SUGGESTIONS_REUSED, SUGGESTIONS_SAVED, SUGGESTIONS_STALE, SessionRepository, SuggestionSave, SuggestionState,
)
from tests.test_session_repository import NOW, finish, make_user, open_save, run_in_threads, seed_save, start
from tests.test_session_repository_revision import claim, replace
from tests.test_session_repository_revision import play as play_with_snapshots

MISSING = "00000000-0000-4000-8000-000000000000"
ITEMS = ("走向山門", "向守衛打聽消息 😀", "回頭看看來時的路")


@pytest.fixture()
def repo(member_session):
    return SessionRepository(member_session)


@pytest.fixture()
def world(repo, member_session):
    alice = make_user(member_session, 1)
    return alice, open_save(repo, alice)


def play(repo, user, save, index: int) -> None:
    start(repo, user, save, client_msg_id=f"m-{index}", action=f"行動{index}")
    finish(repo, user, save, client_msg_id=f"m-{index}", reply=f"劇情{index}")


def anchor(repo, user, save) -> tuple[int, int]:
    state = repo.suggestion_state(user.id, save.public_id)
    return state.seq, state.revision


def stored(member_session, save):
    member_session.expire_all()
    return save.suggestions_json


# ---- 錨點 ------------------------------------------------------------------------------------------


def test_a_new_save_has_no_suggestions_and_the_opening_anchor_is_zero_zero(repo, member_session, world):
    alice, save = world
    assert repo.suggestion_state(alice.id, save.public_id) == SuggestionState(0, 0, ())
    assert stored(member_session, save) is None


def test_the_anchor_is_the_last_complete_turns_seq_and_revision(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1)
    play(repo, alice, save, 2)
    state = repo.suggestion_state(alice.id, save.public_id)
    last = repo.get_turn(alice.id, save.public_id, "m-2")
    assert (state.seq, state.revision) == (last.seq, last.revision) == (2, 1)


def test_interrupted_blocked_and_streaming_attempts_do_not_move_the_anchor(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1)
    before = anchor(repo, alice, save)
    start(repo, alice, save, client_msg_id="gone", action="中斷的行動")
    repo.interrupt_turn(alice.id, save.public_id, "gone", partial_response="半截")
    start(repo, alice, save, client_msg_id="nope", action="被擋的行動")
    repo.block_turn(alice.id, save.public_id, "nope", partial_response="被擋的文字")
    start(repo, alice, save, client_msg_id="live", action="進行中的行動")
    assert anchor(repo, alice, save) == before


# ---- 讀取：錨點相等才有效 ----------------------------------------------------------------------------


def test_saved_suggestions_are_readable_only_while_the_anchor_is_equal(repo, member_session, world):
    alice, save = world
    seq, revision = anchor(repo, alice, save)
    saved = repo.save_suggestions(alice.id, save.public_id, seq=seq, revision=revision, items=ITEMS)
    assert saved == SuggestionSave(SUGGESTIONS_SAVED, ITEMS)
    assert repo.suggestion_state(alice.id, save.public_id) == SuggestionState(0, 0, ITEMS)
    assert stored(member_session, save) == {"seq": 0, "revision": 0, "items": list(ITEMS)}


def test_a_new_complete_turn_makes_the_old_suggestions_unreadable_but_leaves_them_stored(repo, member_session, world):
    alice, save = world
    repo.save_suggestions(alice.id, save.public_id, seq=0, revision=0, items=ITEMS)
    play(repo, alice, save, 1)
    state = repo.suggestion_state(alice.id, save.public_id)
    assert state.items == () and (state.seq, state.revision) == (1, 1)
    assert stored(member_session, save)["items"] == list(ITEMS)                    # 過期內容留到下次被覆寫或隨存檔刪除（個資定位與存檔相同）


def test_a_successful_revision_makes_the_suggestions_unreadable(repo, member_session, world):
    alice, save = world
    play_with_snapshots(repo, alice, save, 1)                                      # 修訂需要回合前快照
    seq, revision = anchor(repo, alice, save)
    repo.save_suggestions(alice.id, save.public_id, seq=seq, revision=revision, items=ITEMS)
    assert repo.suggestion_state(alice.id, save.public_id).items == ITEMS
    claimed = claim(repo, alice, save)
    assert claimed.outcome == session_repository.REVISION_CLAIMED
    assert replace(repo, alice, save, attempt=claimed.turn.attempt) is not None
    state = repo.suggestion_state(alice.id, save.public_id)
    assert (state.seq, state.revision, state.items) == (1, 2, ())


def test_a_failed_or_released_revision_leaves_the_suggestions_valid(repo, member_session, world):
    alice, save = world
    play_with_snapshots(repo, alice, save, 1)
    seq, revision = anchor(repo, alice, save)
    repo.save_suggestions(alice.id, save.public_id, seq=seq, revision=revision, items=ITEMS)
    claimed = claim(repo, alice, save)
    assert repo.release_revision(alice.id, save.public_id, "m-1", attempt=claimed.turn.attempt) is True
    assert repo.suggestion_state(alice.id, save.public_id).items == ITEMS


def test_a_second_save_for_the_same_anchor_after_expiry_overwrites_the_old_document(repo, member_session, world):
    alice, save = world
    repo.save_suggestions(alice.id, save.public_id, seq=0, revision=0, items=ITEMS)
    play(repo, alice, save, 1)
    again = repo.save_suggestions(alice.id, save.public_id, seq=1, revision=1, items=("新的建議",))
    assert again == SuggestionSave(SUGGESTIONS_SAVED, ("新的建議",))
    assert stored(member_session, save) == {"seq": 1, "revision": 1, "items": ["新的建議"]}


@pytest.mark.parametrize("bad", [
    None, [], "字串", {"seq": 0, "revision": 0}, {"seq": 0, "revision": 0, "items": "不是陣列"}, {"seq": "0", "revision": 0, "items": ["a"]},
    {"seq": True, "revision": False, "items": ["a"]}, {"seq": 5, "revision": 0, "items": ["a"]},
])
def test_a_malformed_or_foreign_document_is_treated_as_no_suggestions(repo, member_session, world, bad):
    alice, save = world
    save.suggestions_json = bad
    member_session.flush()
    member_session.expire_all()
    assert repo.suggestion_state(alice.id, save.public_id).items == ()


def test_non_string_items_in_a_stored_document_are_ignored(repo, member_session, world):
    alice, save = world
    save.suggestions_json = {"seq": 0, "revision": 0, "items": ["好的", 3, None, "也好"]}
    member_session.flush()
    member_session.expire_all()
    assert repo.suggestion_state(alice.id, save.public_id).items == ("好的", "也好")


# ---- 寫入：鎖內重新確認 ----------------------------------------------------------------------------


def test_a_second_save_for_the_same_valid_anchor_returns_the_stored_ones_and_does_not_overwrite(repo, member_session, world):
    alice, save = world
    repo.save_suggestions(alice.id, save.public_id, seq=0, revision=0, items=ITEMS)
    again = repo.save_suggestions(alice.id, save.public_id, seq=0, revision=0, items=("另一組",))
    assert again == SuggestionSave(SUGGESTIONS_REUSED, ITEMS)
    assert stored(member_session, save)["items"] == list(ITEMS)


def test_a_save_with_a_stale_anchor_is_refused_and_writes_nothing(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1)                                                      # 呼叫期間有新回合完成：產生建議時讀到的錨點是 (0, 0)
    result = repo.save_suggestions(alice.id, save.public_id, seq=0, revision=0, items=ITEMS)
    assert result == SuggestionSave(SUGGESTIONS_STALE, ())
    assert stored(member_session, save) is None
    revised_anchor = anchor(repo, alice, save)
    assert repo.save_suggestions(alice.id, save.public_id, seq=revised_anchor[0], revision=revised_anchor[1] + 1, items=ITEMS).outcome == SUGGESTIONS_STALE


def test_a_save_on_an_ended_session_is_refused(repo, member_session, world):
    alice, save = world
    assert repo.end_session(alice.id, save.public_id, now=NOW) is True
    result = repo.save_suggestions(alice.id, save.public_id, seq=0, revision=0, items=ITEMS)
    assert result == SuggestionSave(SUGGESTIONS_ENDED, ())
    assert stored(member_session, save) is None


def test_saving_changes_nothing_but_the_suggestions_column(repo, member_session, world):
    alice, save = world
    play(repo, alice, save, 1)
    repo.update_world_state(alice.id, save.public_id, expected_version=1, current_location="山門前", status_summary="下雨", elapsed_months=2)
    repo.save_notebook(alice.id, save.public_id, text="記事簿內容", expected_version=1)
    member_session.commit()
    member_session.expire_all()
    # `updated_at` 由資料表的 onupdate 在任何 UPDATE（含只為取得列鎖的那一次）時更新，是簿記時間戳記，不是世界狀態，所以不在比對之列
    columns = [c.name for c in RpgSession.__table__.columns if c.name not in ("suggestions_json", "updated_at")]
    before = {name: getattr(save, name) for name in columns}
    turns_before = [(t.id, t.revision, t.attempt, t.turn_status, t.llm_raw_response, t.updated_at) for t in member_session.scalars(select(MemoryLog))]
    seq, revision = anchor(repo, alice, save)
    assert repo.save_suggestions(alice.id, save.public_id, seq=seq, revision=revision, items=ITEMS).outcome == SUGGESTIONS_SAVED
    member_session.commit()
    member_session.expire_all()
    assert {name: getattr(save, name) for name in columns} == before                # version、世界狀態、記事簿、最後活動時間、更新時間都沒變
    assert before["version"] == 2 and save.version == 2
    assert [(t.id, t.revision, t.attempt, t.turn_status, t.llm_raw_response, t.updated_at) for t in member_session.scalars(select(MemoryLog))] == turns_before


def test_the_stored_items_are_plain_strings_in_order(repo, member_session, world):
    alice, save = world
    repo.save_suggestions(alice.id, save.public_id, seq=0, revision=0, items=["一", "二", "三"])
    assert stored(member_session, save)["items"] == ["一", "二", "三"]
    assert all(type(item) is str for item in stored(member_session, save)["items"])


# ---- 擁有者隔離 ------------------------------------------------------------------------------------


def test_another_players_or_a_missing_save_gives_no_data_and_writes_nothing(repo, member_session, world):
    alice, save = world
    mallory = make_user(member_session, 2)
    repo.save_suggestions(alice.id, save.public_id, seq=0, revision=0, items=ITEMS)
    assert repo.suggestion_state(mallory.id, save.public_id) is None
    assert repo.suggestion_state(alice.id, MISSING) is None
    assert repo.save_suggestions(mallory.id, save.public_id, seq=0, revision=0, items=("入侵",)) is None
    assert repo.save_suggestions(alice.id, MISSING, seq=0, revision=0, items=("入侵",)) is None
    assert stored(member_session, save)["items"] == list(ITEMS)


def test_every_new_method_takes_the_user_id_first():
    import inspect

    for name in ("suggestion_state", "save_suggestions"):
        parameters = list(inspect.signature(getattr(SessionRepository, name)).parameters)
        assert parameters[:3] == ["self", "user_id", "public_id"], name


# ---- 匯出（目前保存的文字，不管錨點是否有效）--------------------------------------------------------------


def exported(repo, user):
    return list(repo.export_sessions(user.id))


def test_the_export_lists_the_stored_suggestions_without_the_anchor_even_when_expired(repo, member_session, world):
    alice, save = world
    assert exported(repo, alice)[0]["suggestions"] == []                            # 沒有建議：空陣列
    repo.save_suggestions(alice.id, save.public_id, seq=0, revision=0, items=ITEMS)
    member_session.expire_all()
    row = exported(repo, alice)[0]
    assert row["suggestions"] == list(ITEMS) and "seq" not in row and "revision" not in row
    play(repo, alice, save, 1)                                                      # 錨點改變：快照看不到，但資料庫裡還存著，所以仍匯出
    member_session.expire_all()
    assert exported(repo, alice)[0]["suggestions"] == list(ITEMS)
    assert repo.suggestion_state(alice.id, save.public_id).items == ()


def test_a_malformed_stored_document_exports_as_an_empty_list(repo, member_session, world):
    alice, save = world
    save.suggestions_json = {"items": "不是陣列"}
    member_session.flush()
    member_session.expire_all()
    assert exported(repo, alice)[0]["suggestions"] == []


# ---- 並行（真實的執行緒與獨立連線）-----------------------------------------------------------------------


def test_two_concurrent_saves_for_the_same_anchor_keep_exactly_one_document(concurrent_engine):
    user_id, public_id = seed_save(concurrent_engine)

    def worker(s: Session, index: int):
        result = SessionRepository(s).save_suggestions(user_id, public_id, seq=0, revision=0, items=(f"第{index}組",))
        return result.outcome

    outcomes = run_in_threads(concurrent_engine, worker, 3)
    assert sorted(outcomes) == [SUGGESTIONS_REUSED, SUGGESTIONS_REUSED, SUGGESTIONS_SAVED]
    with Session(concurrent_engine) as s:
        save = s.scalar(select(RpgSession))
        assert save.suggestions_json["items"] in (["第0組"], ["第1組"], ["第2組"]) and save.version == 1
        # 沒有被後到者覆蓋：留下的就是第一個寫入者的那一組，而後到者回傳的也是它
        assert len(save.suggestions_json["items"]) == 1


def test_a_save_waits_for_a_turn_being_completed_and_then_sees_the_new_anchor(concurrent_engine):
    """新回合先取得列鎖（交易還沒提交）；建議的寫入必須等它，之後讀到的是新的錨點 → `stale`，不會把舊錨點的建議存成有效。"""
    user_id, public_id = seed_save(concurrent_engine)
    turn_started = threading.Event()
    result: dict[str, object] = {}

    def new_turn() -> None:
        with Session(concurrent_engine) as s:
            repository = SessionRepository(s)
            repository.start_turn(user_id, public_id, client_msg_id="m-1", user_action="行動1", now=NOW)
            repository.complete_turn(user_id, public_id, "m-1", llm_raw_response="劇情1", structured_result=None, now=NOW)
            turn_started.set()
            time.sleep(0.8)                                       # 交易保持開啟，建議的寫入在這段時間內啟動
            s.commit()

    def suggest() -> None:
        turn_started.wait(timeout=30)
        with Session(concurrent_engine) as s:
            result["outcome"] = SessionRepository(s).save_suggestions(user_id, public_id, seq=0, revision=0, items=ITEMS).outcome
            s.commit()

    threads = [threading.Thread(target=new_turn), threading.Thread(target=suggest)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert result["outcome"] == SUGGESTIONS_STALE
    with Session(concurrent_engine) as s:
        assert s.scalar(select(RpgSession)).suggestions_json is None
