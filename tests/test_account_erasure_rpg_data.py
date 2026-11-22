"""script-store「刪除帳號時劇本一併消失」與 rpg-sessions「抹除一併刪除」：抹除一位作者或玩家時，RPG 資料的去留。

- 作者被抹除：他的劇本、版本、標籤與自己的存檔、回合都不存在；別的玩家用他的劇本開過的存檔仍在，內容複本不變，來源參照為空值，
  而且抹除沒有被擋下（`sessions.source_version_id` 是無動作的外鍵，抹除流程先把參照清為空值）。
- 條件不成立（使用者剛好登入）時整個交易回復，連清掉的參照也一起復原。
- 玩家被抹除：他的存檔與回合消失，作者的劇本不受影響。

以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行。
"""
from datetime import timedelta

from sqlalchemy import func, select

from app.models import DeletionTombstone, MemoryLog, RpgSession, Script, ScriptVersion, ScriptVersionTag, TurnRegeneration, User
from app.models.script import VERSION_REJECTED, VERSION_REMOVED
from app.repositories.deletion_tombstone_repository import DeletionTombstoneRepository
from app.repositories.script_repository import ScriptRepository
from app.repositories.session_repository import SessionRepository
from app.repositories.turn_regeneration_repository import TurnRegenerationRepository
from app.repositories.user_repository import UserRepository
from app.services.account_erasure_service import AccountErasureService
from app.services.deletion_replay_service import DeletionReplayService
from tests.support.auth import START, add_user
from tests.support.script_data import validated
from tests.support.script_review import put_in_status
from tests.support.protagonist import protagonist

CUTOFF = START + timedelta(days=1)


def erase_inactive(session, user_id: int) -> bool:
    service = AccountErasureService(
        users=UserRepository(session), scripts=ScriptRepository(session), tombstones=DeletionTombstoneRepository(session), commit=session.commit, rollback=session.rollback,
    )
    return service.erase_inactive(user_id, cutoff=CUTOFF, now=START)


def count(session, model) -> int:
    session.expire_all()
    return session.scalar(select(func.count()).select_from(model))


def old_user(session, n: int, **overrides) -> User:
    return add_user(session, n, last_login_at=START - timedelta(days=400), created_at=START - timedelta(days=500), **overrides)


def build_world(session):
    """作者有一個已發佈的劇本（含標籤）與一個新草稿，並有自己的存檔與回合；讀者用作者已發佈的版本開過存檔。"""
    author, reader = old_user(session, 1), old_user(session, 2)
    scripts, saves = ScriptRepository(session), SessionRepository(session)
    first = validated(title="作者的劇本")
    published = scripts.create_script(author.id, author_name="甲", author_code="aaaaaa", content=first.content, content_hash=first.content_hash, total_chars=first.total_chars)
    scripts.submit_for_review(author.id, published.public_id)
    scripts.approve_version(published.public_id, now=START)
    second = validated(title="作者的新草稿")
    scripts.create_draft_version(author.id, scripts.get_script_of(published).public_id, content=second.content, content_hash=second.content_hash, total_chars=second.total_chars)
    snapshot = scripts.content_of(published)
    reader_save = saves.create(
        reader.id, source_version_id=published.id, snapshot=snapshot, snapshot_spec_version=1, protagonist=protagonist("讀者", 18), content_tier="safe", now=START,
    )
    saves.start_turn(reader.id, reader_save.public_id, client_msg_id="r-1", user_action="讀者的行動", now=START)
    author_save = saves.create(
        author.id, source_version_id=published.id, snapshot=snapshot, snapshot_spec_version=1, protagonist=protagonist("作者自己", 18), content_tier="safe", now=START,
    )
    saves.start_turn(author.id, author_save.public_id, client_msg_id="a-1", user_action="作者的行動", now=START)
    session.commit()
    return author, reader, published, reader_save, snapshot


def test_erasing_an_author_removes_their_scripts_versions_tags_saves_and_turns_but_keeps_the_readers_save(member_session):
    s = member_session
    author, reader, published, reader_save, snapshot = build_world(s)
    author_id, reader_id, reader_save_id = author.id, reader.id, reader_save.id
    assert (count(s, Script), count(s, ScriptVersion), count(s, RpgSession), count(s, MemoryLog)) == (1, 2, 2, 2)
    assert count(s, ScriptVersionTag) == 4                                             # 兩個版本各兩個標籤

    assert erase_inactive(s, author_id) is True          # 沒有被別人的存檔擋下

    assert s.get(User, author_id) is None
    assert (count(s, Script), count(s, ScriptVersion), count(s, ScriptVersionTag)) == (0, 0, 0)
    assert count(s, RpgSession) == 1 and count(s, MemoryLog) == 1                     # 作者自己的存檔與回合消失
    kept = s.get(RpgSession, reader_save_id)
    assert kept.user_id == reader_id and kept.source_version_id is None             # 讀者的存檔保留，來源參照為空值
    assert kept.script_snapshot_json == snapshot and kept.script_snapshot_json["title"] == "作者的劇本"
    assert s.scalar(select(MemoryLog.user_action)) == "讀者的行動"
    (tombstone,) = s.scalars(select(DeletionTombstone))
    assert tombstone.user_id == author_id


def test_erasing_an_author_also_removes_rejected_and_removed_versions_together_with_their_admin_notes(member_session):
    """rpg-script-publishing：被退回、被下架（含管理員下架並附備註）的版本隨作者一起刪除，備註在同一列、沒有殘留；
    讀者用該劇本開過的存檔保留，來源參照清為空值。"""
    s = member_session
    author = old_user(s, 1)
    scripts, saves = ScriptRepository(s), SessionRepository(s)
    rejected = put_in_status(scripts, author, VERSION_REJECTED)
    removed = put_in_status(scripts, author, VERSION_REMOVED, removed_by="admin")
    reader = old_user(s, 2)
    snapshot = scripts.content_of(removed)
    kept = saves.create(
        reader.id, source_version_id=removed.id, snapshot=snapshot, snapshot_spec_version=1, protagonist=protagonist("讀者", 18),
        content_tier="safe", now=START,
    )
    s.commit()
    assert count(s, ScriptVersion) == 2 and s.scalars(select(ScriptVersion.decision_note).where(ScriptVersion.decision_note.is_not(None))).all()
    author_id, kept_id, reader_id = author.id, kept.id, reader.id
    assert erase_inactive(s, author_id) is True
    assert (count(s, Script), count(s, ScriptVersion), count(s, ScriptVersionTag)) == (0, 0, 0)
    assert s.scalars(select(ScriptVersion.decision_note)).all() == []
    survivor = s.get(RpgSession, kept_id)
    assert survivor.user_id == reader_id and survivor.source_version_id is None and survivor.script_snapshot_json == snapshot


def test_when_the_erasure_condition_no_longer_holds_nothing_changes_including_the_cleared_references(member_session):
    s = member_session
    author, reader, published, reader_save, _snapshot = build_world(s)
    author.last_login_at = START + timedelta(days=2)      # 剛好又登入了（晚於期限的截止時間）：條件在刪除當下再確認一次，不成立
    s.commit()
    author_id = author.id
    assert erase_inactive(s, author_id) is False
    s.expire_all()
    assert s.get(User, author_id) is not None
    assert count(s, Script) == 1 and count(s, ScriptVersion) == 2 and count(s, RpgSession) == 2
    assert s.get(RpgSession, reader_save.id).source_version_id == published.id          # 清掉的參照一起復原
    assert count(s, DeletionTombstone) == 0


def test_erasing_a_reader_removes_their_saves_and_turns_and_leaves_the_authors_scripts_alone(member_session):
    s = member_session
    author, reader, published, reader_save, _snapshot = build_world(s)
    author_id, reader_id = author.id, reader.id
    assert erase_inactive(s, reader_id) is True
    assert s.get(User, reader_id) is None
    assert (count(s, Script), count(s, ScriptVersion), count(s, ScriptVersionTag)) == (1, 2, 4)
    assert count(s, RpgSession) == 1 and s.scalar(select(RpgSession.user_id)) == author_id
    assert count(s, MemoryLog) == 1 and s.scalar(select(MemoryLog.user_action)) == "作者的行動"


def test_a_players_own_saves_from_their_own_scripts_do_not_block_the_erasure(member_session):
    s = member_session
    author = old_user(s, 1)
    scripts, saves = ScriptRepository(s), SessionRepository(s)
    content = validated()
    version = scripts.create_script(author.id, author_name="甲", author_code="aaaaaa", content=content.content, content_hash=content.content_hash, total_chars=content.total_chars)
    saves.create(author.id, source_version_id=version.id, snapshot=content.content, snapshot_spec_version=1, protagonist=protagonist("自己", 18), content_tier="safe", now=START)
    s.commit()
    author_id = author.id
    assert erase_inactive(s, author_id) is True
    assert (count(s, Script), count(s, ScriptVersion), count(s, RpgSession)) == (0, 0, 0)


def test_erasing_an_author_with_many_readers_saves_clears_every_reference(member_session):
    s = member_session
    author, _reader, published, _save, _snapshot = build_world(s)
    author_id = author.id
    saves = SessionRepository(s)
    for n in range(3, 6):
        other = old_user(s, n)
        saves.create(other.id, source_version_id=published.id, snapshot={"title": f"複本{n}"}, snapshot_spec_version=1, protagonist=protagonist("旅人", 18), content_tier="safe", now=START)
    s.commit()
    assert erase_inactive(s, author_id) is True
    s.expire_all()
    remaining = list(s.scalars(select(RpgSession)))
    assert len(remaining) == 4 and all(save.source_version_id is None for save in remaining)
    assert sorted(save.script_snapshot_json["title"] for save in remaining) == ["作者的劇本", "複本3", "複本4", "複本5"]


# ---- 重新生成與編輯的回饋（rpg-turn-revision）：隨存檔與帳號串聯刪除 ----------------------------------------

REASON = "劇情和設定對不上，想換個寫法"


def add_feedback(session, user: User, save: RpgSession, *, count_rows: int = 2) -> None:
    """為某個存檔加上幾筆回饋（理由是玩家文字，視為個資）。"""
    feedback = TurnRegenerationRepository(session)
    for attempt in range(2, 2 + count_rows):
        assert feedback.add_pending(
            user.id, save.public_id, turn_seq=1, attempt=attempt, kind="regenerate", category="other", reason=REASON, is_free=True,
        )
    session.commit()


def test_erasing_a_player_removes_their_feedback_rows_and_keeps_everyone_elses(member_session):
    s = member_session
    author, reader, _published, reader_save, _snapshot = build_world(s)
    author_save = s.scalar(select(RpgSession).where(RpgSession.user_id == author.id))
    add_feedback(s, reader, reader_save, count_rows=3)
    add_feedback(s, author, author_save, count_rows=2)
    reader_id = reader.id
    assert count(s, TurnRegeneration) == 5

    assert erase_inactive(s, reader_id) is True

    assert s.get(User, reader_id) is None
    assert count(s, TurnRegeneration) == 2                                               # 讀者的 3 筆消失
    assert {row.session_id for row in s.scalars(select(TurnRegeneration))} == {author_save.id}


def test_erasing_an_author_removes_their_own_saves_feedback_but_not_a_readers_feedback_on_a_copy_of_their_script(member_session):
    s = member_session
    author, reader, _published, reader_save, _snapshot = build_world(s)
    author_save = s.scalar(select(RpgSession).where(RpgSession.user_id == author.id))
    add_feedback(s, reader, reader_save, count_rows=2)
    add_feedback(s, author, author_save, count_rows=2)
    reader_save_id = reader_save.id

    assert erase_inactive(s, author.id) is True

    assert count(s, TurnRegeneration) == 2
    assert {row.session_id for row in s.scalars(select(TurnRegeneration))} == {reader_save_id}


def test_the_feedback_rows_of_a_restored_account_are_erased_again_by_the_tombstone_replay(member_session):
    s = member_session
    author, reader, _published, reader_save, _snapshot = build_world(s)
    author_save = s.scalar(select(RpgSession).where(RpgSession.user_id == author.id))
    add_feedback(s, reader, reader_save, count_rows=2)
    add_feedback(s, author, author_save, count_rows=1)
    reader_id, author_save_id = reader.id, author_save.id
    DeletionTombstoneRepository(s).record(user_id=reader_id, reason="user_requested", deleted_at=START + timedelta(days=2))   # 還原後備份裡又出現已刪除的帳號
    s.commit()
    users, tombstones = UserRepository(s), DeletionTombstoneRepository(s)
    erasure = AccountErasureService(users=users, scripts=ScriptRepository(s), tombstones=tombstones, commit=s.commit, rollback=s.rollback)

    report = DeletionReplayService(users=users, tombstones=tombstones, erasure=erasure).run()

    assert report.erased == 1
    s.expire_all()
    assert s.get(User, reader_id) is None
    assert count(s, TurnRegeneration) == 1
    assert s.scalar(select(TurnRegeneration.session_id)) == author_save_id
