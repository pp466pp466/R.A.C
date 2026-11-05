"""personal-data-export 規格（rpg-data-and-script 的修改）：匯出自己的劇本（全部版本）、存檔、記事簿與回合。

- JSON 的 `scripts` 與 `sessions`（含每個存檔的 `turns` 與 `world_state`）；CSV 的 `scripts.csv`、`sessions.csv`、`turns.csv`，沒有世界狀態。
- 不含他人劇本的複本、種子鍵、內容雜湊、內部整數編號與模型的結構化輸出；公式字元被中和；大量回合仍以分頁逐段產生。

以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行。
"""
import csv
import io
import json
import zipfile
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import RpgSession, ScriptVersion, User
from app.repositories.chat_repository import ChatRepository
from app.repositories.lock_appeal_repository import LockAppealRepository
from app.repositories.script_repository import ScriptRepository
from app.repositories.script_submission_repository import ScriptSubmissionRepository
from app.repositories.session_repository import SessionRepository
from app.repositories.user_identity_repository import UserIdentityRepository
from app.repositories.user_repository import UserRepository
from app.services.current_user import CurrentUser
from app.services.personal_export_service import PersonalExportService, SCRIPT_COLUMNS, SESSION_COLUMNS, TURN_COLUMNS
from tests.support.auth import START, FakeClock, add_user
from tests.support.script_data import validated
from tests.support.protagonist import protagonist

OTHER_BACKGROUND = "別人的劇本才有的背景設定OTHER-BACKGROUND"
OTHER_CHARACTER = "別人的角色OTHER-CHARACTER"
SECRET_STRUCTURED = "STRUCTURED-RESULT-SECRET"


def service_for(session) -> PersonalExportService:
    return PersonalExportService(
        users=UserRepository(session), identities=UserIdentityRepository(session), chats=ChatRepository(session),
        appeals=LockAppealRepository(session), scripts=ScriptRepository(session), sessions=SessionRepository(session),
        submissions=ScriptSubmissionRepository(session), clock=FakeClock(),
    )


def export_json(session, user: User) -> dict:
    session.expire_all()
    result = service_for(session).build(CurrentUser.from_model(session.get(User, user.id)), "json")
    raw = result.handle.read()
    result.handle.close()
    assert not raw.startswith(b"\xef\xbb\xbf")
    return json.loads(raw.decode("utf-8"))


def export_csv(session, user: User) -> dict[str, bytes]:
    session.expire_all()
    result = service_for(session).build(CurrentUser.from_model(session.get(User, user.id)), "csv")
    with zipfile.ZipFile(result.handle) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    result.handle.close()
    return files


def rows(raw: bytes) -> list[dict[str, str]]:
    assert raw.startswith(b"\xef\xbb\xbf")
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline="")))


def make_script(session, owner: User, *, publish_first: bool = True, **changes):
    """作者的劇本：版本 1 已發佈，版本 2 是草稿（`publish_first=False` 時只有一個草稿）。"""
    scripts = ScriptRepository(session)
    first = validated(**changes)
    version = scripts.create_script(owner.id, author_name="筆名甲", author_code="abc123", content=first.content, content_hash=first.content_hash, total_chars=first.total_chars)
    if publish_first:
        scripts.submit_for_review(owner.id, version.public_id)
        scripts.approve_version(version.public_id, now=START)
        second = validated(**{**changes, "title": "第二版草稿標題"})
        draft = scripts.create_draft_version(owner.id, scripts.get_script_of(version).public_id, content=second.content, content_hash=second.content_hash, total_chars=second.total_chars)
        return version, draft
    return version, None


def make_save(session, owner: User, snapshot: dict, *, source=None, turns: int = 0, notebook: str = "", **overrides):
    saves = SessionRepository(session)
    values = dict(
        source_version_id=source.id if source else None, snapshot=snapshot, snapshot_spec_version=1, protagonist=protagonist(),
        content_tier="safe", now=START,
    )
    values.update(overrides)
    save = saves.create(owner.id, **values)
    save.notebook_text = notebook
    save.current_location = "古劍山莊"
    save.status_summary = "剛入山門"
    for index in range(1, turns + 1):
        saves.start_turn(owner.id, save.public_id, client_msg_id=f"t-{index}", user_action=f"行動{index}", now=START + timedelta(minutes=index))
        saves.complete_turn(owner.id, save.public_id, f"t-{index}", llm_raw_response=f"劇情{index}", structured_result={"secret": SECRET_STRUCTURED}, now=START + timedelta(minutes=index))
    session.commit()
    return save


# ---- 劇本 -----------------------------------------------------------------------------------

def test_the_json_has_every_version_of_my_scripts_with_all_the_spec_fields(member_session):
    s = member_session
    me = add_user(s, 1)
    published, draft = make_script(s, me, content_tier="standard", tags=["仙俠", "Fantasy"])
    s.commit()
    data = export_json(s, me)
    (script,) = data["scripts"]
    assert script["author_name"] == "筆名甲" and script["author_code"] == "abc123" and script["author_locked_at"] is not None
    first, second = script["versions"]
    assert (first["version_no"], first["status"], second["version_no"], second["status"]) == (1, "published", 2, "draft")
    for key in ("title", "summary", "world_name", "content_tier", "background", "opening", "rules", "tags", "calendar", "ending_mode",
                "ending_text", "ending_age", "characters", "locations", "milestones", "spec_version", "created_at", "published_at"):
        assert key in first, key
    assert first["title"] == "青雲問道" and first["content_tier"] == "standard" and first["tags"] == ["仙俠", "Fantasy"]
    assert first["calendar"] == {"name": "乾歷朝", "start_year": 1200} and first["characters"][0]["name"] == "林曉"
    assert first["published_at"] is not None and second["published_at"] is None and second["title"] == "第二版草稿標題"


@pytest.mark.parametrize("status", ["pending", "rejected", "superseded"])
def test_versions_in_every_state_are_exported(member_session, status):
    s = member_session
    me = add_user(s, 1)
    scripts = ScriptRepository(s)
    version, _ = make_script(s, me, publish_first=False)
    scripts.submit_for_review(me.id, version.public_id)
    if status == "rejected":
        scripts.reject_version(version.public_id)
    elif status == "superseded":
        scripts.approve_version(version.public_id, now=START)
        newer = validated(title="取代它的新版本")
        successor = scripts.create_draft_version(me.id, scripts.get_script_of(version).public_id, content=newer.content, content_hash=newer.content_hash, total_chars=newer.total_chars)
        scripts.submit_for_review(me.id, successor.public_id)
        scripts.approve_version(successor.public_id, now=START)
    s.commit()
    statuses = [v["status"] for v in export_json(s, me)["scripts"][0]["versions"]]
    assert {"pending": ["pending_review"], "rejected": ["rejected"], "superseded": ["superseded", "published"]}[status] == statuses


def test_another_players_scripts_are_never_in_my_export(member_session):
    s = member_session
    me, other = add_user(s, 1), add_user(s, 2)
    make_script(s, me, title="我的劇本")
    make_script(s, other, title="別人的劇本", background=OTHER_BACKGROUND)
    s.commit()
    raw = json.dumps(export_json(s, me), ensure_ascii=False)
    assert "我的劇本" in raw and "別人的劇本" not in raw and OTHER_BACKGROUND not in raw
    csv_raw = export_csv(s, me)["scripts.csv"].decode("utf-8-sig")
    assert "別人的劇本" not in csv_raw and OTHER_BACKGROUND not in csv_raw


def test_built_in_scripts_are_not_part_of_anyones_export(member_session):
    s = member_session
    me = add_user(s, 1)
    result = validated(title="內建劇本")
    ScriptRepository(s).publish_builtin("builtin-x", content=result.content, content_hash=result.content_hash, total_chars=result.total_chars, now=START)
    s.commit()
    data = export_json(s, me)
    assert data["scripts"] == [] and data["sessions"] == []


def test_the_script_export_has_no_seed_key_content_hash_or_internal_numbers(member_session):
    s = member_session
    me = add_user(s, 1)
    published, _ = make_script(s, me)
    s.commit()
    version_hashes = [v.content_hash for v in s.scalars(select(ScriptVersion))]
    raw = json.dumps(export_json(s, me), ensure_ascii=False)
    for forbidden in (*version_hashes, "seed_key", "content_hash", "total_chars", "live_script_id", "open_script_id", "creator_id", "script_id\":", "version_id"):
        assert forbidden not in raw, forbidden
    parsed = json.loads(raw)["scripts"][0]
    assert isinstance(parsed["id"], str) and len(parsed["id"]) == 36 and "id" not in parsed["versions"][0]


# ---- 存檔、記事簿與回合 -----------------------------------------------------------------------------

def test_the_json_has_my_save_with_the_notebook_in_full_and_its_turns_in_sequence_order(member_session):
    s = member_session
    me = add_user(s, 1)
    published, _ = make_script(s, me)
    snapshot = ScriptRepository(s).content_of(published)
    save = make_save(s, me, snapshot, source=published, turns=3, notebook="記事簿的全文\n含換行")
    SessionRepository(s).end_session(me.id, save.public_id, now=START + timedelta(days=1))
    s.commit()
    (entry,) = export_json(s, me)["sessions"]
    assert entry["script_title"] == "青雲問道" and entry["status"] == "ended" and entry["content_tier"] == "safe"
    assert (entry["player_name"], entry["character_age"], entry["current_location"], entry["status_summary"]) == ("旅人", 18, "古劍山莊", "剛入山門")
    assert entry["notebook_text"] == "記事簿的全文\n含換行"
    assert entry["created_at"] and entry["last_active_at"] and entry["ended_at"]
    assert [(t["seq"], t["turn_number"], t["status"], t["user_action"], t["story"]) for t in entry["turns"]] == [
        (1, 1, "complete", "行動1", "劇情1"), (2, 2, "complete", "行動2", "劇情2"), (3, 3, "complete", "行動3", "劇情3"),
    ]
    assert set(entry["world_state"]) == {"entities", "goals", "milestone_progress"}


def test_interrupted_and_blocked_turns_are_exported_with_their_status_and_no_turn_number(member_session):
    s = member_session
    me = add_user(s, 1)
    save = make_save(s, me, validated().content, turns=1)
    saves = SessionRepository(s)
    saves.start_turn(me.id, save.public_id, client_msg_id="x-1", user_action="被中斷的行動", now=START)
    saves.interrupt_turn(me.id, save.public_id, "x-1", partial_response="半截")
    saves.start_turn(me.id, save.public_id, client_msg_id="x-2", user_action="被擋的行動", now=START)
    saves.block_turn(me.id, save.public_id, "x-2")
    s.commit()
    turns = export_json(s, me)["sessions"][0]["turns"]
    assert [(t["status"], t["turn_number"]) for t in turns] == [("complete", 1), ("interrupted", None), ("blocked", None)]
    assert turns[1]["story"] == "半截"


def test_a_save_made_from_someone_elses_script_exports_only_its_title_not_the_copy(member_session):
    s = member_session
    author, reader = add_user(s, 1), add_user(s, 2)
    published, _ = make_script(s, author, title="別人寫的劇本", background=OTHER_BACKGROUND, characters=[
        {"name": OTHER_CHARACTER[:20], "personality": "冷靜", "age": 30, "appearance": "高瘦"},
    ])
    make_save(s, reader, ScriptRepository(s).content_of(published), source=published, turns=1)
    s.commit()
    data = export_json(s, reader)
    raw = json.dumps(data, ensure_ascii=False)
    assert data["sessions"][0]["script_title"] == "別人寫的劇本" and data["scripts"] == []
    assert OTHER_BACKGROUND not in raw and OTHER_CHARACTER[:20] not in raw and "script_snapshot" not in raw and "milestones" not in raw
    files = export_csv(s, reader)
    for name, content in files.items():
        assert OTHER_BACKGROUND.encode() not in content and OTHER_CHARACTER[:20].encode() not in content, name


def test_the_export_never_contains_internal_ids_or_the_models_structured_output(member_session):
    s = member_session
    me = add_user(s, 1)
    save = make_save(s, me, validated().content, turns=2)
    s.commit()
    s.expire_all()
    stored = s.get(RpgSession, save.id)
    raw = json.dumps(export_json(s, me), ensure_ascii=False)
    for forbidden in (SECRET_STRUCTURED, "structured_result", "client_msg_id", "t-1", "source_version_id", "notebook_version", "notebook_upto_turn", '"version":', "user_id", "session_id"):
        assert forbidden not in raw, forbidden
    for text in (b"t-1", SECRET_STRUCTURED.encode()):
        assert not any(text in content for content in export_csv(s, me).values())
    assert stored.id and isinstance(json.loads(raw)["sessions"][0]["id"], str)


def test_another_players_saves_and_turns_are_never_included(member_session):
    s = member_session
    me, other = add_user(s, 1), add_user(s, 2)
    make_save(s, me, validated(title="我的存檔劇本").content, turns=1)
    make_save(s, other, validated(title="別人的存檔劇本").content, turns=2, notebook="別人的記事簿OTHER-NOTEBOOK", protagonist=protagonist("別人的主角"))
    s.commit()
    raw = json.dumps(export_json(s, me), ensure_ascii=False)
    assert "我的存檔劇本" in raw and "別人的存檔劇本" not in raw and "OTHER-NOTEBOOK" not in raw and "別人的主角" not in raw
    assert not any(b"OTHER-NOTEBOOK" in c for c in export_csv(s, me).values())


def test_a_user_with_nothing_still_gets_empty_script_and_session_lists(member_session):
    s = member_session
    me = add_user(s, 1)
    data = export_json(s, me)
    assert data["scripts"] == [] and data["sessions"] == []
    files = export_csv(s, me)
    assert rows(files["scripts.csv"]) == [] and rows(files["sessions.csv"]) == [] and rows(files["turns.csv"]) == []
    assert files["scripts.csv"].decode("utf-8-sig").strip() == ",".join(SCRIPT_COLUMNS)


# ---- CSV ------------------------------------------------------------------------------------

def test_every_csv_file_starts_with_a_bom_and_has_the_documented_columns_and_no_world_state(member_session):
    s = member_session
    me = add_user(s, 1)
    published, _ = make_script(s, me)
    make_save(s, me, ScriptRepository(s).content_of(published), source=published, turns=2, notebook="記事簿")
    s.commit()
    files = export_csv(s, me)
    assert all(content.startswith(b"\xef\xbb\xbf") for content in files.values())
    scripts, sessions, turns = rows(files["scripts.csv"]), rows(files["sessions.csv"]), rows(files["turns.csv"])
    assert list(scripts[0]) == list(SCRIPT_COLUMNS) and len(scripts) == 2
    assert list(sessions[0]) == list(SESSION_COLUMNS) and list(turns[0]) == list(TURN_COLUMNS)
    assert (scripts[0]["version_no"], scripts[0]["status"], scripts[1]["status"]) == ("1", "published", "draft")
    assert scripts[0]["tags"] == "仙俠、成長" and json.loads(scripts[0]["characters"])[0]["name"] == "林曉"
    assert sessions[0]["notebook_text"] == "記事簿" and sessions[0]["script_title"] == "青雲問道"
    assert [(t["seq"], t["turn_number"], t["user_action"], t["story"], t["script_title"]) for t in turns] == [
        ("1", "1", "行動1", "劇情1", "青雲問道"), ("2", "2", "行動2", "劇情2", "青雲問道"),
    ]
    joined = b"".join(files.values()).decode("utf-8-sig")
    for forbidden in ("entities", "goals", "milestone_progress", "world_state"):
        assert forbidden not in joined


@pytest.mark.parametrize("dangerous", ["=HYPERLINK(\"http://evil\")", "+1+1", "-2+3", "@SUM(A1)"])
def test_formula_characters_in_titles_actions_and_stories_are_neutralised(member_session, dangerous):
    s = member_session
    me = add_user(s, 1)
    make_script(s, me, publish_first=False, title=dangerous[:30])
    save = make_save(s, me, validated(title=dangerous[:30]).content)
    saves = SessionRepository(s)
    saves.start_turn(me.id, save.public_id, client_msg_id="f-1", user_action=dangerous, now=START)
    saves.complete_turn(me.id, save.public_id, "f-1", llm_raw_response=dangerous, structured_result=None, now=START)
    s.commit()
    files = export_csv(s, me)
    assert rows(files["scripts.csv"])[0]["title"] == "'" + dangerous[:30]
    assert rows(files["sessions.csv"])[0]["script_title"] == "'" + dangerous[:30]
    turn = rows(files["turns.csv"])[0]
    assert turn["user_action"] == "'" + dangerous and turn["story"] == "'" + dangerous


def test_commas_quotes_and_newlines_in_a_notebook_and_a_story_round_trip(member_session):
    s = member_session
    me = add_user(s, 1)
    text = "含,逗號、\"引號\"與\n換行"
    save = make_save(s, me, validated().content, notebook=text)
    saves = SessionRepository(s)
    saves.start_turn(me.id, save.public_id, client_msg_id="c-1", user_action=text, now=START)
    saves.complete_turn(me.id, save.public_id, "c-1", llm_raw_response=text, structured_result=None, now=START)
    s.commit()
    files = export_csv(s, me)
    assert rows(files["sessions.csv"])[0]["notebook_text"] == text and rows(files["turns.csv"])[0]["story"] == text
    assert export_json(s, me)["sessions"][0]["turns"][0]["story"] == text


# ---- 大量資料以分頁逐段產生 ---------------------------------------------------------------------------

def test_many_turns_scripts_and_saves_come_out_complete_in_order_across_page_boundaries(member_session, monkeypatch):
    s = member_session
    me = add_user(s, 1)
    for index in range(1, 6):
        make_script(s, me, publish_first=False, title=f"劇本{index}")
    saves = [make_save(s, me, validated(title=f"存檔劇本{index}").content, turns=0) for index in range(1, 4)]
    big = SessionRepository(s)
    for number in range(1, 8):
        big.start_turn(me.id, saves[0].public_id, client_msg_id=f"p-{number}", user_action=f"行動{number}", now=START)
        big.complete_turn(me.id, saves[0].public_id, f"p-{number}", llm_raw_response=f"劇情{number}", structured_result=None, now=START)
    s.commit()
    originals = (ScriptRepository.export_scripts, SessionRepository.export_sessions, SessionRepository.export_turns)
    monkeypatch.setattr(ScriptRepository, "export_scripts", lambda self, user_id, page_size=2: originals[0](self, user_id, page_size=2))
    monkeypatch.setattr(SessionRepository, "export_sessions", lambda self, user_id, page_size=2: originals[1](self, user_id, page_size=2))
    monkeypatch.setattr(SessionRepository, "export_turns", lambda self, user_id, public_id, page_size=3: originals[2](self, user_id, public_id, page_size=3))
    data = export_json(s, me)
    assert [script["versions"][0]["title"] for script in data["scripts"]] == [f"劇本{i}" for i in range(1, 6)]
    assert [save["script_title"] for save in data["sessions"]] == ["存檔劇本1", "存檔劇本2", "存檔劇本3"]
    assert [turn["seq"] for turn in data["sessions"][0]["turns"]] == list(range(1, 8))
    files = export_csv(s, me)
    assert len(rows(files["scripts.csv"])) == 5 and len(rows(files["sessions.csv"])) == 3 and len(rows(files["turns.csv"])) == 7


def test_the_export_does_not_log_any_script_or_story_text(member_session, log_lines):
    s = member_session
    me = add_user(s, 1)
    published, _ = make_script(s, me, title="日誌不應出現的劇本標題")
    make_save(s, me, ScriptRepository(s).content_of(published), source=published, turns=1, notebook="日誌不應出現的記事簿")
    s.commit()
    export_json(s, me)
    export_csv(s, me)
    raw = log_lines.raw()
    assert "日誌不應出現" not in raw and "劇情1" not in raw and "行動1" not in raw


# ---- 玩家主角（rpg-character-creation）：角色卡與錨定事實隨存檔匯出，自我介紹本來就不存在 ------------------------------

PROTAGONIST_ANCHOR = {"kind": "location", "ref": "古劍山莊", "text": "出生的地方PROTAGONIST-ANCHOR"}


def make_protagonist_save(session, owner, **extra):
    return make_save(
        session, owner, validated().content,
        protagonist=protagonist("林舟PROTAGONIST-NAME", 19, anchor=PROTAGONIST_ANCHOR, occupation="遊方郎中", speech_style="說話慢", hidden_trait="怕黑", **extra),
    )


def test_the_json_has_the_protagonist_card_and_anchor_in_each_save(member_session):
    s = member_session
    me = add_user(s, 1)
    make_protagonist_save(s, me)
    s.commit()
    (entry,) = export_json(s, me)["sessions"]
    assert (entry["player_name"], entry["character_age"]) == ("林舟PROTAGONIST-NAME", 19)
    card = entry["protagonist"]["character"]
    assert (card["name"], card["age"], card["personality"], card["appearance"]) == ("林舟PROTAGONIST-NAME", 19, "沉默愛思考", "高瘦，常穿灰衣")
    assert (card["occupation"], card["speech_style"], card["hidden_trait"]) == ("遊方郎中", "說話慢", "怕黑") and "image" not in card
    assert entry["protagonist"]["anchor"] == PROTAGONIST_ANCHOR


def test_a_save_without_an_anchor_exports_a_null_anchor(member_session):
    s = member_session
    me = add_user(s, 1)
    make_save(s, me, validated().content, protagonist=protagonist("無聯繫", 20))
    s.commit()
    (entry,) = export_json(s, me)["sessions"]
    assert entry["protagonist"]["anchor"] is None and entry["protagonist"]["character"]["name"] == "無聯繫"


def test_a_save_from_before_the_protagonist_column_exports_name_and_age_without_error(member_session):
    s = member_session
    me = add_user(s, 1)
    legacy = RpgSession(
        user_id=me.id, script_snapshot_json={"title": "舊"}, player_name="舊存檔主角", character_age=30,
        entities_json={}, goals_json={"active": [], "archived": []}, milestone_progress_json={},
    )
    s.add(legacy)
    s.commit()
    data = export_json(s, me)
    (entry,) = data["sessions"]
    assert (entry["player_name"], entry["character_age"], entry["protagonist"]) == ("舊存檔主角", 30, None)
    (row,) = rows(export_csv(s, me)["sessions.csv"])
    assert (row["player_name"], row["character_age"]) == ("舊存檔主角", "30")
    assert all(row[name] == "" for name in ("personality", "appearance", "occupation", "speech_style", "hidden_trait", "anchor_kind", "anchor_ref", "anchor_text"))


def test_the_csv_flattens_the_card_and_anchor_into_columns_and_neutralises_formulas(member_session):
    s = member_session
    me = add_user(s, 1)
    make_protagonist_save(s, me, personality='=HYPERLINK("http://evil")')
    s.commit()
    (row,) = rows(export_csv(s, me)["sessions.csv"])
    assert row["personality"].startswith("'=") and row["appearance"] == "高瘦，常穿灰衣" and row["occupation"] == "遊方郎中"
    assert (row["anchor_kind"], row["anchor_ref"], row["anchor_text"]) == ("location", "古劍山莊", "出生的地方PROTAGONIST-ANCHOR")
    assert {"personality", "appearance", "occupation", "speech_style", "hidden_trait", "anchor_kind", "anchor_ref", "anchor_text"} <= set(SESSION_COLUMNS)


def test_another_users_protagonist_never_appears_in_my_export(member_session):
    s = member_session
    me, other = add_user(s, 1), add_user(s, 2)
    make_save(s, me, validated().content, protagonist=protagonist("我的主角", 20))
    make_protagonist_save(s, other)
    s.commit()
    assert "PROTAGONIST-NAME" not in json.dumps(export_json(s, me), ensure_ascii=False)
    assert b"PROTAGONIST" not in export_csv(s, me)["sessions.csv"]


def test_the_export_has_no_intro_and_no_internal_fields_of_the_protagonist(member_session):
    s = member_session
    me = add_user(s, 1)
    make_protagonist_save(s, me)
    s.commit()
    raw = json.dumps(export_json(s, me), ensure_ascii=False)
    for forbidden in ("intro", "自我介紹", "protagonist_json", '"image"'):
        assert forbidden not in raw
