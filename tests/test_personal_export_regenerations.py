"""personal-data-export 規格（rpg-turn-revision 的修改）：匯出重新生成與編輯的回饋。

- JSON：每個存檔區塊的 `regenerations`（回合序號、種類、分類、理由、是否免費、結果、時間；含失敗與被擋的嘗試）；
- CSV：zip 內的 `turn_regenerations.csv`（UTF-8-sig，公式字元被中和）；
- 不含內部編號、預扣的冪等鍵與回合前快照，也沒有被取代的行動與劇情；沒有回饋的使用者得到空清單；他人的回饋永遠不在我的匯出裡。

以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行。
"""
import csv
import io
import json
import zipfile

from app.models import User
from app.models.rpg_session import REGEN_APPLIED, REGEN_BLOCKED, REGEN_FAILED, REGEN_KIND_EDIT, REGEN_KIND_REGENERATE
from app.repositories.chat_repository import ChatRepository
from app.repositories.lock_appeal_repository import LockAppealRepository
from app.repositories.script_repository import ScriptRepository
from app.repositories.script_submission_repository import ScriptSubmissionRepository
from app.repositories.session_repository import SessionRepository
from app.repositories.turn_regeneration_repository import TurnRegenerationRepository
from app.repositories.user_identity_repository import UserIdentityRepository
from app.repositories.user_repository import UserRepository
from app.services import personal_export_service
from app.services.current_user import CurrentUser
from app.services.personal_export_service import PersonalExportService, REGENERATION_COLUMNS
from tests.support.auth import FakeClock, add_user
from tests.support.script_data import validated
from tests.test_personal_export_rpg_data import make_save

REASON_A = "劇情和設定對不上，想換個寫法"
REASON_B = "=HYPERLINK(\"http://example.com\")的公式字元"
WORLD = {
    "entities_json": None, "goals_json": None, "milestone_progress_json": None, "elapsed_months": 0, "current_location": "", "status_summary": "",
}
SNAPSHOT_SECRET = "回合前快照的獨特地點SNAPSHOT-SECRET"


def service_for(session) -> PersonalExportService:
    return PersonalExportService(
        users=UserRepository(session), identities=UserIdentityRepository(session), chats=ChatRepository(session),
        appeals=LockAppealRepository(session), scripts=ScriptRepository(session), sessions=SessionRepository(session),
        submissions=ScriptSubmissionRepository(session), clock=FakeClock(), regenerations=TurnRegenerationRepository(session),
    )


def export_json(session, user: User) -> dict:
    session.expire_all()
    result = service_for(session).build(CurrentUser.from_model(session.get(User, user.id)), "json")
    raw = result.handle.read()
    result.handle.close()
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


def save_with_feedback(session, owner: User):
    """一個存檔、一個有回合前快照的回合，以及四筆回饋：成功的免費重新生成、被擋的免費重新生成、失敗的付費重新生成，以及成功的編輯。"""
    save = make_save(session, owner, validated().content)
    saves = SessionRepository(session)
    saves.start_turn(
        owner.id, save.public_id, client_msg_id="r-1", user_action="被修訂的行動", now=FakeClock()(),
        state_before={**WORLD, "current_location": SNAPSHOT_SECRET},
    )
    saves.complete_turn(owner.id, save.public_id, "r-1", llm_raw_response="劇情", structured_result=None, now=FakeClock()())
    feedback = TurnRegenerationRepository(session)
    for attempt, kind, category, reason, free, result in (
        (2, REGEN_KIND_REGENERATE, "setting_mismatch", REASON_A, True, REGEN_APPLIED),
        (3, REGEN_KIND_REGENERATE, "other", REASON_B, True, REGEN_BLOCKED),
        (4, REGEN_KIND_REGENERATE, "typo_or_quality", "錯字太多，看不下去", False, REGEN_FAILED),
        (5, REGEN_KIND_EDIT, None, None, False, REGEN_APPLIED),
    ):
        assert feedback.add_pending(owner.id, save.public_id, turn_seq=1, attempt=attempt, kind=kind, category=category, reason=reason, is_free=free)
        assert feedback.mark_result(owner.id, save.public_id, turn_seq=1, attempt=attempt, result=result)
    session.commit()
    return save


# ---- JSON -------------------------------------------------------------------------------------

def test_the_json_has_every_feedback_row_of_the_save_including_failed_and_blocked_attempts(member_session):
    s = member_session
    me = add_user(s, 1)
    save_with_feedback(s, me)
    data = export_json(s, me)
    (session_block,) = data["sessions"]
    items = session_block["regenerations"]
    assert [(item["kind"], item["category"], item["reason"], item["is_free"], item["result"]) for item in items] == [
        (REGEN_KIND_REGENERATE, "setting_mismatch", REASON_A, True, REGEN_APPLIED),
        (REGEN_KIND_REGENERATE, "other", REASON_B, True, REGEN_BLOCKED),
        (REGEN_KIND_REGENERATE, "typo_or_quality", "錯字太多，看不下去", False, REGEN_FAILED),
        (REGEN_KIND_EDIT, None, None, False, REGEN_APPLIED),
    ]
    assert all(item["turn_seq"] == 1 for item in items)
    assert all(set(item) == {"turn_seq", "kind", "category", "reason", "is_free", "result", "created_at", "updated_at"} for item in items)
    assert all(item["created_at"] and item["updated_at"] for item in items)                              # ISO 8601 字串


def test_the_export_never_carries_internal_ids_idempotency_keys_snapshots_or_replaced_content(member_session):
    s = member_session
    me = add_user(s, 1)
    save = save_with_feedback(s, me)
    encoded = json.dumps(export_json(s, me), ensure_ascii=False)
    assert SNAPSHOT_SECRET not in encoded and "state_before" not in encoded                                # 回合前快照不匯出
    assert "attempt" not in encoded and "revising" not in encoded and "regen_count" not in encoded
    assert f"turn:{save.public_id}" not in encoded                                                         # 沒有預扣的冪等鍵
    for item in json.loads(encoded)["sessions"][0]["regenerations"]:
        assert not {"id", "session_id", "attempt"} & set(item)
    files = export_csv(s, me)
    assert SNAPSHOT_SECRET.encode() not in b"".join(files.values())
    assert b"state_before" not in b"".join(files.values())


def test_a_save_without_feedback_and_a_player_without_saves_get_empty_lists(member_session):
    s = member_session
    me, nobody = add_user(s, 1), add_user(s, 2)
    make_save(s, me, validated().content, turns=2)
    data = export_json(s, me)
    assert data["sessions"][0]["regenerations"] == []
    assert export_json(s, nobody)["sessions"] == []
    files = export_csv(s, me)
    assert "turn_regenerations.csv" in files and rows(files["turn_regenerations.csv"]) == []
    assert rows(export_csv(s, nobody)["turn_regenerations.csv"]) == []


def test_another_players_feedback_is_never_in_my_export(member_session):
    s = member_session
    me, other = add_user(s, 1), add_user(s, 2)
    save_with_feedback(s, other)
    make_save(s, me, validated().content, turns=1)
    assert export_json(s, me)["sessions"][0]["regenerations"] == []
    assert rows(export_csv(s, me)["turn_regenerations.csv"]) == []
    theirs = export_json(s, other)["sessions"][0]["regenerations"]
    assert len(theirs) == 4


# ---- CSV --------------------------------------------------------------------------------------

def test_the_csv_zip_has_a_turn_regenerations_file_with_one_row_per_feedback_in_utf8_sig(member_session):
    s = member_session
    me = add_user(s, 1)
    save = save_with_feedback(s, me)
    files = export_csv(s, me)
    assert "turn_regenerations.csv" in files
    raw = files["turn_regenerations.csv"]
    assert raw.startswith(b"\xef\xbb\xbf")
    parsed = rows(raw)
    assert list(parsed[0]) == list(REGENERATION_COLUMNS)
    assert [(row["kind"], row["category"], row["result"], row["is_free"]) for row in parsed] == [
        (REGEN_KIND_REGENERATE, "setting_mismatch", REGEN_APPLIED, "True"), (REGEN_KIND_REGENERATE, "other", REGEN_BLOCKED, "True"),
        (REGEN_KIND_REGENERATE, "typo_or_quality", REGEN_FAILED, "False"), (REGEN_KIND_EDIT, "", REGEN_APPLIED, "False"),
    ]
    assert all(row["session_id"] == save.public_id and row["script_title"] == "青雲問道" and row["turn_seq"] == "1" for row in parsed)
    assert parsed[0]["reason"] == REASON_A and parsed[3]["reason"] == ""                                   # 繁體中文不亂碼；編輯沒有理由


def test_a_reason_that_starts_with_a_formula_character_is_neutralised_in_the_csv_only(member_session):
    s = member_session
    me = add_user(s, 1)
    save_with_feedback(s, me)
    parsed = rows(export_csv(s, me)["turn_regenerations.csv"])
    assert parsed[1]["reason"] == "'" + REASON_B                                                           # 試算表不會把它當成公式執行
    assert export_json(s, me)["sessions"][0]["regenerations"][1]["reason"] == REASON_B                     # JSON 原樣


def test_a_large_number_of_feedback_rows_are_read_page_by_page(member_session, monkeypatch):
    s = member_session
    me = add_user(s, 1)
    save = make_save(s, me, validated().content, turns=1)
    feedback = TurnRegenerationRepository(s)
    for attempt in range(2, 9):
        feedback.add_pending(me.id, save.public_id, turn_seq=1, attempt=attempt, kind=REGEN_KIND_EDIT, category=None, reason=None, is_free=False)
    s.commit()
    monkeypatch.setattr(personal_export_service, "PAGE_SIZE", 3)                                          # 7 筆分 3 頁
    assert len(export_json(s, me)["sessions"][0]["regenerations"]) == 7
    assert len(rows(export_csv(s, me)["turn_regenerations.csv"])) == 7
