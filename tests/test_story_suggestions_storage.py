"""rpg-story-view 的資料面（批 2）：存檔的 `suggestions` 在快照、個人資料匯出、帳號抹除與期限清除裡的行為。

- 快照（`GET /api/game/sessions/<id>`）：新存檔為空；有效時帶出字串陣列；完成新回合或修訂成功後過期為空；不含錨點細節與其他存檔的建議。
- 匯出：JSON 為字串陣列、`sessions.csv` 單一欄位各項換行連接、沒有建議為空、公式字元被中和、不含錨點；只匯出自己的。
- 抹除與期限清除：建議在存檔的同一個資料列，不需要新的刪除路徑；這裡證明建議的文字隨存檔消失，而不是假設。

快照走 HTTP（SQLite）；匯出、抹除與期限清除以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行。
"""
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.llm.types import LLMTransientError
from app.models import Base, RpgSession, User
from app.repositories.deletion_tombstone_repository import DeletionTombstoneRepository
from app.repositories.retention_repository import RetentionRepository
from app.repositories.script_repository import ScriptRepository
from app.repositories.session_repository import SessionRepository
from app.repositories.user_repository import UserRepository
from app.services.account_erasure_service import AccountErasureService
from app.services.data_retention_service import SESSION_RETENTION, DataRetentionService
from app.services.personal_export_service import SESSION_COLUMNS
from tests.support.auth import START, FakeClock, add_user
from tests.support.llm import Chunk, Fail, FakeLLMProvider
from tests.support.protagonist import protagonist
from tests.support.script_data import validated
from tests.test_game_api import db, make_save as make_api_save, parse, player, script_turn, turn
from tests.test_game_revision_endpoints import NEW_ACTION, edit, script_revision
from tests.test_llm_providers_by_purpose import BOTH, make_app
from tests.test_personal_export_rpg_data import export_csv, export_json, rows
from tests.test_personal_export_rpg_data import make_save as make_export_save

DAY = timedelta(days=1)
ITEMS = ("走向山門", "向守衛打聽消息 😀", "回頭看看來時的路")
SECRET = "SUGGESTION-SECRET-文字"


@pytest.fixture()
def provider():
    return FakeLLMProvider()


@pytest.fixture()
def app(provider):
    return make_app({**BOTH}, rpg_llm_provider=provider)


def store(app, public_id, items=ITEMS, *, seq=0, revision=0):
    """直接以 Repository 保存一組建議（不經端點：這一批還沒有產生建議的服務）。"""
    with db(app) as s:
        user_id = s.scalar(select(RpgSession.user_id).where(RpgSession.public_id == public_id))
        result = SessionRepository(s).save_suggestions(user_id, public_id, seq=seq, revision=revision, items=items)
        s.commit()
        return result


def snapshot(client, save_id):
    response = client.get(f"/api/game/sessions/{save_id}")
    assert response.status_code == 200
    return response.get_json()["session"]


# ---- 快照 ------------------------------------------------------------------------------------------


def test_a_new_saves_snapshot_has_an_empty_suggestions_array(app):
    client, uid = player(app)
    save_id = make_api_save(app, uid)
    assert snapshot(client, save_id)["suggestions"] == []


def test_valid_suggestions_appear_in_the_snapshot_as_plain_strings_without_the_anchor(app):
    client, uid = player(app)
    save_id = make_api_save(app, uid)
    assert store(app, save_id).outcome == "saved"
    body = snapshot(client, save_id)
    assert body["suggestions"] == list(ITEMS)
    assert isinstance(body["suggestions"], list) and all(type(item) is str for item in body["suggestions"])    # 只有字串，沒有錨點的結構
    assert "suggestions_json" not in body and "suggestions_anchor" not in body and "suggestions_seq" not in body
    assert set(body["last_turn"] or {}) <= {"client_msg_id", "seq", "revision", "revisable", "reason", "free_left", "regenerate_cost", "edit_cost"}


def test_a_completed_new_turn_expires_the_suggestions_in_the_snapshot(app, provider):
    client, uid = player(app)
    save_id = make_api_save(app, uid)
    store(app, save_id)
    assert snapshot(client, save_id)["suggestions"] == list(ITEMS)
    script_turn(provider)
    parse(turn(client, save_id))
    assert snapshot(client, save_id)["suggestions"] == []


def test_suggestions_for_the_current_screen_survive_an_interrupted_attempt(app, provider):
    """中斷的嘗試沒有完成，錨點不動：同一個畫面的建議仍然有效（被擋的回合同理，repository 測試涵蓋）。"""
    client, uid = player(app)
    save_id = make_api_save(app, uid)
    store(app, save_id)
    provider.script_stream(Chunk("清晨的海風吹過山門。"), Fail(LLMTransientError("network")))
    parse(turn(client, save_id, msg="gone"))
    assert snapshot(client, save_id)["suggestions"] == list(ITEMS)


def test_a_successful_revision_expires_the_suggestions_and_the_next_screen_may_have_new_ones(app, provider):
    client, uid = player(app)
    save_id = make_api_save(app, uid)
    script_turn(provider)
    parse(turn(client, save_id))
    last = snapshot(client, save_id)["last_turn"]
    assert store(app, save_id, ("一", "二"), seq=last["seq"], revision=last["revision"]).outcome == "saved"
    assert snapshot(client, save_id)["suggestions"] == ["一", "二"]
    script_revision(provider)
    parse(edit(client, save_id, action=NEW_ACTION))
    assert snapshot(client, save_id)["suggestions"] == []
    after = snapshot(client, save_id)["last_turn"]
    assert (after["seq"], after["revision"]) == (last["seq"], last["revision"] + 1)
    assert store(app, save_id, ("新一", "新二"), seq=after["seq"], revision=after["revision"]).outcome == "saved"
    assert snapshot(client, save_id)["suggestions"] == ["新一", "新二"]


def test_a_players_suggestions_are_not_in_another_players_snapshot_and_that_save_is_a_404(app):
    client, uid = player(app, 1)
    other, other_uid = player(app, 2)
    mine, theirs = make_api_save(app, uid), make_api_save(app, other_uid)
    store(app, mine)
    assert snapshot(other, theirs)["suggestions"] == []
    foreign = other.get(f"/api/game/sessions/{mine}")
    assert foreign.status_code == 404 and "走向山門" not in foreign.get_data(as_text=True)


def test_an_ended_session_snapshot_still_shows_its_valid_suggestions_as_part_of_the_readable_history(app):
    client, uid = player(app)
    save_id = make_api_save(app, uid)
    store(app, save_id)
    with db(app) as s:
        assert SessionRepository(s).end_session(uid, save_id, now=START) is True
        s.commit()
    body = snapshot(client, save_id)
    assert body["ended"] is True and body["suggestions"] == list(ITEMS)       # 已完結仍可讀；是否能再產生是端點的事（批 3）


# ---- 匯出 ------------------------------------------------------------------------------------------


def saved_for_export(session, me, *, items=ITEMS, seq=0, revision=0, **overrides):
    save = make_export_save(session, me, validated().content, **overrides)
    SessionRepository(session).save_suggestions(me.id, save.public_id, seq=seq, revision=revision, items=items)
    session.commit()
    return save


def test_the_json_export_has_the_suggestions_as_a_string_array_and_no_anchor(member_session):
    s = member_session
    me = add_user(s, 1)
    saved_for_export(s, me)
    (entry,) = export_json(s, me)["sessions"]
    assert entry["suggestions"] == list(ITEMS)
    assert "seq" not in entry and "revision" not in entry and "anchor" not in entry and "suggestions_json" not in entry


def test_the_json_export_has_an_empty_array_when_there_are_no_suggestions(member_session):
    s = member_session
    me = add_user(s, 1)
    make_export_save(s, me, validated().content)
    (entry,) = export_json(s, me)["sessions"]
    assert entry["suggestions"] == []


def test_the_csv_export_joins_the_items_with_newlines_in_one_column(member_session):
    s = member_session
    me = add_user(s, 1)
    saved_for_export(s, me)
    (row,) = rows(export_csv(s, me)["sessions.csv"])
    assert row["suggestions"] == "\n".join(ITEMS) and "suggestions" in SESSION_COLUMNS
    assert "seq" not in row and "revision" not in row                                   # 不含錨點與內部編號


def test_the_csv_export_is_empty_for_a_save_without_suggestions_and_other_columns_are_unchanged(member_session):
    s = member_session
    me = add_user(s, 1)
    make_export_save(s, me, validated().content, notebook="記事簿")
    (row,) = rows(export_csv(s, me)["sessions.csv"])
    assert row["suggestions"] == "" and row["notebook_text"] == "記事簿" and row["script_title"] == "青雲問道"


def test_a_formula_character_at_the_start_of_the_csv_cell_is_neutralised_like_other_player_text(member_session):
    s = member_session
    me = add_user(s, 1)
    saved_for_export(s, me, items=("=HYPERLINK(\"http://evil\")", "第二項"))
    (row,) = rows(export_csv(s, me)["sessions.csv"])
    assert row["suggestions"].startswith("'=") and row["suggestions"].endswith("\n第二項")


def test_expired_suggestions_still_in_the_database_are_exported_as_personal_data(member_session):
    s = member_session
    me = add_user(s, 1)
    save = saved_for_export(s, me)                                                      # 保存時錨點是 (0, 0)
    saves = SessionRepository(s)
    saves.start_turn(me.id, save.public_id, client_msg_id="t-1", user_action="行動1", now=START + timedelta(minutes=1))
    saves.complete_turn(me.id, save.public_id, "t-1", llm_raw_response="劇情1", structured_result=None, now=START + timedelta(minutes=1))
    s.commit()                                                                          # 之後完成了一回合 → 錨點改變，建議已過期
    assert saves.suggestion_state(me.id, save.public_id).items == ()
    (entry,) = export_json(s, me)["sessions"]
    assert entry["suggestions"] == list(ITEMS)


def test_only_my_suggestions_are_exported(member_session):
    s = member_session
    me, other = add_user(s, 1), add_user(s, 2)
    saved_for_export(s, me, items=("我的建議",))
    saved_for_export(s, other, items=(SECRET,))
    exported = json.dumps(export_json(s, me), ensure_ascii=False)
    assert "我的建議" in exported and SECRET not in exported
    assert SECRET not in export_csv(s, me)["sessions.csv"].decode("utf-8-sig")


def test_a_player_without_a_save_exports_no_suggestions(member_session):
    s = member_session
    me = add_user(s, 1)
    assert export_json(s, me)["sessions"] == []
    assert rows(export_csv(s, me)["sessions.csv"]) == []


# ---- 抹除與期限清除：建議隨存檔消失 ------------------------------------------------------------------------


def everything_in_the_database(session) -> str:
    session.expire_all()
    parts = []
    for table in Base.metadata.tables.values():
        parts += [str(row) for row in session.execute(select(table)).all()]
    return "\n".join(parts)


def make_save_with_secret(session, user, *, now=START):
    save = SessionRepository(session).create(
        user.id, source_version_id=None, snapshot={"title": "劇本"}, snapshot_spec_version=1,
        protagonist=protagonist("旅人", 19), content_tier="safe", now=now,
    )
    SessionRepository(session).save_suggestions(user.id, save.public_id, seq=0, revision=0, items=(SECRET, "另一項"))
    return save


def erasure(session) -> AccountErasureService:
    return AccountErasureService(
        users=UserRepository(session), scripts=ScriptRepository(session), tombstones=DeletionTombstoneRepository(session),
        commit=session.commit, rollback=session.rollback,
    )


def test_erasing_a_player_removes_their_suggestions_with_the_save(member_session):
    s = member_session
    user = add_user(s, 1, last_login_at=START - 400 * DAY, created_at=START - 500 * DAY)
    make_save_with_secret(s, user)
    s.commit()
    assert "SUGGESTION-SECRET" in everything_in_the_database(s)
    assert erasure(s).erase_inactive(user.id, cutoff=START - 365 * DAY, now=START) is True
    assert "SUGGESTION-SECRET" not in everything_in_the_database(s)
    assert s.scalar(select(func.count()).select_from(RpgSession)) == 0


def test_erasing_one_player_leaves_another_players_suggestions_alone(member_session):
    s = member_session
    gone = add_user(s, 1, last_login_at=START - 400 * DAY, created_at=START - 500 * DAY)
    kept = add_user(s, 2)
    make_save_with_secret(s, gone)
    kept_save = SessionRepository(s).create(
        kept.id, source_version_id=None, snapshot={"title": "劇本"}, snapshot_spec_version=1, protagonist=protagonist("留下的主角", 19), content_tier="safe", now=START,
    )
    SessionRepository(s).save_suggestions(kept.id, kept_save.public_id, seq=0, revision=0, items=("留下的建議",))
    s.commit()
    assert erasure(s).erase_inactive(gone.id, cutoff=START - 365 * DAY, now=START) is True
    text = everything_in_the_database(s)
    assert "SUGGESTION-SECRET" not in text and "留下的建議" in text and s.get(User, kept.id) is not None


def test_a_save_past_its_retention_period_is_purged_with_its_suggestions_and_a_recent_one_keeps_them(member_session):
    s = member_session
    old, recent = add_user(s, 1), add_user(s, 2)
    make_save_with_secret(s, old, now=START - SESSION_RETENTION - DAY)
    kept = SessionRepository(s).create(
        recent.id, source_version_id=None, snapshot={"title": "劇本"}, snapshot_spec_version=1, protagonist=protagonist("新主角", 19), content_tier="safe", now=START - DAY,
    )
    SessionRepository(s).save_suggestions(recent.id, kept.public_id, seq=0, revision=0, items=("近期的建議",))
    s.commit()
    report = DataRetentionService(repository=RetentionRepository(s), clock=FakeClock(), commit=s.commit, rollback=s.rollback).run()
    assert {r.category: r.matched for r in report.results}["sessions"] == 1
    text = everything_in_the_database(s)
    assert "SUGGESTION-SECRET" not in text and "近期的建議" in text


def test_an_admin_has_no_path_to_a_players_suggestions(member_session):
    s = member_session
    admin, player_user = add_user(s, 1, role="admin"), add_user(s, 2)
    save = make_save_with_secret(s, player_user)
    s.commit()
    repo = SessionRepository(s)
    assert repo.suggestion_state(admin.id, save.public_id) is None
    assert repo.save_suggestions(admin.id, save.public_id, seq=0, revision=0, items=("管理員的",)) is None
    assert list(repo.export_sessions(admin.id)) == []
