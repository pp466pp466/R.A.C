"""rpg-data-and-script 的資料模型約束（SQLite 與 SQL Server 兩軌各跑一次）：

版本編號唯一、每個劇本至多一個上架版本與一個進行中的版本（部分唯一索引，多個 NULL 並存）、種子鍵唯一、回合的唯一性、
刪除使用者時的串聯，以及「來源版本被刪除前必須先清空參照」（SQL Server 不允許多重串聯路徑，所以是無動作的外鍵）。
"""
import pytest
from sqlalchemy import JSON, Unicode, UnicodeText, delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.models import Base, MemoryLog, RpgSession, Script, ScriptVersion, ScriptVersionTag, User
from app.models.base import UTCDateTime
from app.models.rpg_session import SESSION_ACTIVE, TURN_STREAMING
from app.models.script import ENDING_NONE, VERSION_DRAFT, VERSION_PUBLISHED


def table(name: str):
    return Base.metadata.tables[name]


# ---- 欄位型別（不需要資料庫）-----------------------------------------------------------

def test_text_columns_are_unicode_json_columns_are_json_and_times_are_utc():
    versions, sessions, turns = table("script_versions"), table("sessions"), table("memory_logs")
    for name in ("title", "summary", "world_name", "content_tier", "status"):
        assert isinstance(versions.c[name].type, Unicode), name
    for name in ("background", "opening", "rules", "ending_text"):
        assert isinstance(versions.c[name].type, UnicodeText), name
    for name in ("characters_json", "locations_json", "milestones_json"):
        assert isinstance(versions.c[name].type, JSON), name
    for name in ("script_snapshot_json", "entities_json", "goals_json", "milestone_progress_json"):
        assert isinstance(sessions.c[name].type, JSON), name
    for name in ("notebook_text", "status_summary"):
        assert isinstance(sessions.c[name].type, UnicodeText), name
    assert isinstance(turns.c.user_action.type, UnicodeText) and isinstance(turns.c.llm_raw_response.type, UnicodeText)
    for t, name in ((versions, "published_at"), (sessions, "last_active_at"), (sessions, "ended_at"), (turns, "created_at"), (turns, "updated_at")):
        assert isinstance(t.c[name].type, UTCDateTime), (t.name, name)


def test_the_documented_columns_exist_and_the_optional_ones_are_nullable():
    versions, sessions, scripts = table("script_versions"), table("sessions"), table("scripts")
    assert {c.name for c in scripts.columns} == {
        "id", "public_id", "creator_id", "seed_key", "author_name", "author_code", "author_locked_at", "created_at", "updated_at",
    }
    for name in ("opening", "rules", "ending_text", "ending_age", "calendar_name", "calendar_start_year", "live_script_id", "open_script_id", "published_at"):
        assert versions.c[name].nullable, name
    for name in ("title", "summary", "world_name", "background", "content_hash", "ending_mode", "characters_json"):
        assert not versions.c[name].nullable, name
    assert sessions.c.source_version_id.nullable and sessions.c.ended_at.nullable and not sessions.c.script_snapshot_json.nullable
    assert table("memory_logs").c.turn_number.nullable and not table("memory_logs").c.seq.nullable


def test_foreign_key_actions_prefer_deletion_and_the_source_reference_has_no_action():
    def action(table_name, column):
        foreign_key = next(iter(table(table_name).c[column].foreign_keys))
        return foreign_key.column.table.name, foreign_key.ondelete

    assert action("scripts", "creator_id") == ("users", "CASCADE")
    assert action("script_versions", "script_id") == ("scripts", "CASCADE")
    assert action("script_version_tags", "version_id") == ("script_versions", "CASCADE")
    assert action("sessions", "user_id") == ("users", "CASCADE")
    assert action("memory_logs", "session_id") == ("sessions", "CASCADE")
    assert action("sessions", "source_version_id") == ("script_versions", None)     # SQL Server 錯誤 1785：不能是 SET NULL


# ---- 資料庫約束（雙軌）---------------------------------------------------------------

def make_user(session, number: int) -> User:
    user = User(
        email=f"u{number}@example.com", email_canonical=f"u{number}@example.com", display_name=f"使用者{number}", status="active",
    )
    session.add(user)
    session.flush()
    return user


def make_script(session, creator: User | None = None, seed_key: str | None = None) -> Script:
    script = Script(creator_id=creator.id if creator else None, seed_key=seed_key)
    session.add(script)
    session.flush()
    return script


def make_version(session, script: Script, version_no: int = 1, *, status: str = VERSION_DRAFT, **extra) -> ScriptVersion:
    version = ScriptVersion(
        script_id=script.id, version_no=version_no, status=status, content_hash="a" * 64, title="劇本", summary="簡介", world_name="古風仙俠",
        background="背景", ending_mode=ENDING_NONE, **extra,
    )
    session.add(version)
    session.flush()
    return version


def make_rpg_session(session, user: User, version: ScriptVersion | None = None) -> RpgSession:
    save = RpgSession(
        user_id=user.id, source_version_id=version.id if version else None, script_snapshot_json={"title": "劇本"},
        player_name="旅人", character_age=18,
    )
    session.add(save)
    session.flush()
    return save


def expect_integrity_error(session, action) -> None:
    """執行會違反約束的動作，確認被資料庫擋下，並還原交易（先前的資料已提交）。"""
    with pytest.raises(IntegrityError):
        action()
        session.flush()
    session.rollback()


def test_new_rows_get_public_ids_and_the_documented_defaults(member_session):
    user = make_user(member_session, 1)
    script = make_script(member_session, user)
    version = make_version(member_session, script)
    save = make_rpg_session(member_session, user, version)
    turn = MemoryLog(session_id=save.id, client_msg_id="m-1", seq=1)
    member_session.add(turn)
    member_session.commit()
    assert len(script.public_id) == 36 and len(version.public_id) == 36 and len(save.public_id) == 36
    assert len({script.public_id, version.public_id, save.public_id}) == 3
    assert (version.status, version.spec_version, version.ending_mode, version.characters_json) == (VERSION_DRAFT, 1, ENDING_NONE, [])
    assert (save.status, save.version, save.notebook_text, save.notebook_version, save.notebook_upto_turn, save.elapsed_months) == (SESSION_ACTIVE, 1, "", 1, 0, 0)
    assert (save.content_tier, save.is_archived, save.entities_json, save.goals_json) == ("safe", False, {}, {})
    assert (turn.turn_status, turn.turn_number, turn.llm_raw_response) == (TURN_STREAMING, None, "")
    assert make_user(member_session, 2).content_tier_preference == "safe"          # 伺服器端預設是最保守的


def test_version_numbers_are_unique_per_script_but_can_repeat_across_scripts(member_session):
    first, second = make_script(member_session), make_script(member_session)
    make_version(member_session, first, 1)
    make_version(member_session, second, 1)
    member_session.commit()
    expect_integrity_error(member_session, lambda: make_version(member_session, first, 1, status="rejected"))


def test_at_most_one_published_version_per_script(member_session):
    script, other = make_script(member_session), make_script(member_session)
    make_version(member_session, script, 1, status=VERSION_PUBLISHED, live_script_id=script.id)
    make_version(member_session, other, 1, status=VERSION_PUBLISHED, live_script_id=other.id)
    make_version(member_session, script, 2, status="superseded")          # live_script_id 為空的版本可以有很多個
    make_version(member_session, script, 3, status="rejected")
    member_session.commit()
    expect_integrity_error(member_session, lambda: make_version(member_session, script, 4, status=VERSION_PUBLISHED, live_script_id=script.id))


def test_at_most_one_open_version_per_script(member_session):
    script = make_script(member_session)
    make_version(member_session, script, 1, status="published", live_script_id=script.id)
    make_version(member_session, script, 2, status=VERSION_DRAFT, open_script_id=script.id)
    member_session.commit()
    expect_integrity_error(member_session, lambda: make_version(member_session, script, 3, status="pending_review", open_script_id=script.id))


def test_seed_keys_are_unique_only_when_present(member_session):
    make_script(member_session, seed_key="builtin-a")
    make_script(member_session, seed_key=None)
    make_script(member_session, seed_key=None)            # 多個沒有種子鍵的劇本可以並存
    make_script(member_session, seed_key="builtin-b")
    member_session.commit()
    expect_integrity_error(member_session, lambda: make_script(member_session, seed_key="builtin-a"))


def test_a_version_cannot_repeat_a_tag(member_session):
    version = make_version(member_session, make_script(member_session))
    member_session.add_all([ScriptVersionTag(version_id=version.id, tag="仙俠"), ScriptVersionTag(version_id=version.id, tag="科幻")])
    member_session.commit()
    expect_integrity_error(member_session, lambda: member_session.add(ScriptVersionTag(version_id=version.id, tag="仙俠")))


def test_turn_message_ids_and_sequence_numbers_are_unique_per_session(member_session):
    user = make_user(member_session, 1)
    first, second = make_rpg_session(member_session, user), make_rpg_session(member_session, user)
    member_session.add_all([MemoryLog(session_id=first.id, client_msg_id="m-1", seq=1), MemoryLog(session_id=second.id, client_msg_id="m-1", seq=1)])
    member_session.commit()                                # 不同存檔可以重複
    expect_integrity_error(member_session, lambda: member_session.add(MemoryLog(session_id=first.id, client_msg_id="m-1", seq=2)))
    expect_integrity_error(member_session, lambda: member_session.add(MemoryLog(session_id=first.id, client_msg_id="m-2", seq=1)))


def count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def test_deleting_a_user_cascades_to_scripts_versions_tags_sessions_and_turns(member_session):
    author, bystander = make_user(member_session, 1), make_user(member_session, 2)
    script = make_script(member_session, author)
    version = make_version(member_session, script, status=VERSION_PUBLISHED, live_script_id=script.id)
    member_session.add(ScriptVersionTag(version_id=version.id, tag="仙俠"))
    own_save = make_rpg_session(member_session, author, version)          # 作者自己的存檔：參照自己的版本，一起被刪
    member_session.add(MemoryLog(session_id=own_save.id, client_msg_id="m-1", seq=1))
    other_script = make_script(member_session, bystander)
    make_version(member_session, other_script)
    member_session.commit()

    member_session.execute(delete(User).where(User.id == author.id))        # 資料庫層的串聯，不經 ORM
    member_session.commit()
    assert (count(member_session, Script), count(member_session, ScriptVersion)) == (1, 1)
    assert (count(member_session, ScriptVersionTag), count(member_session, RpgSession), count(member_session, MemoryLog)) == (0, 0, 0)


def test_another_players_save_must_have_its_source_reference_cleared_before_the_version_goes(member_session):
    """SQL Server 不允許 SET NULL 與串聯並存（多重串聯路徑），所以參照是無動作的外鍵：沒先清空就刪除會被擋下，清空後存檔仍在。"""
    author, player = make_user(member_session, 1), make_user(member_session, 2)
    script = make_script(member_session, author)
    version = make_version(member_session, script, status=VERSION_PUBLISHED, live_script_id=script.id)
    save = make_rpg_session(member_session, player, version)
    member_session.commit()
    save_id, author_id = save.id, author.id

    expect_integrity_error(member_session, lambda: member_session.execute(delete(User).where(User.id == author_id)))
    assert count(member_session, ScriptVersion) == 1 and count(member_session, User) == 2         # 被擋下，沒有留下半套

    member_session.execute(update(RpgSession).where(RpgSession.source_version_id == version.id).values(source_version_id=None))
    member_session.execute(delete(User).where(User.id == author_id))
    member_session.commit()
    kept = member_session.get(RpgSession, save_id)
    assert kept is not None and kept.source_version_id is None and kept.script_snapshot_json == {"title": "劇本"}
    assert count(member_session, Script) == 0 and count(member_session, ScriptVersion) == 0
