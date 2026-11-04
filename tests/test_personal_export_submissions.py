"""personal-data-export 規格（rpg-script-authoring 的修改）：匯出自己的送審紀錄，且劇本區塊不含審查欄位。

- JSON 的 `script_submissions`（結果、類別、時間，由舊到新）與 CSV 的 `script_submissions.csv`（UTF-8-sig、公式字元中和）。
- 紀錄本來就沒有劇本文字；他人的紀錄不會出現；劇本區塊沒有審查結果的欄位（判定、類別、審查時間、內容雜湊）。

以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行。
"""
import csv
import io
import json
import zipfile
from datetime import timedelta

from app.models import User
from app.models.script import REVIEW_FLAG
from app.models.script_submission import SUBMISSION_AUTO_REJECTED, SUBMISSION_LIFTED, SUBMISSION_SENT
from app.repositories.chat_repository import ChatRepository
from app.repositories.lock_appeal_repository import LockAppealRepository
from app.repositories.script_repository import ScriptRepository
from app.repositories.script_submission_repository import ScriptSubmissionRepository
from app.repositories.session_repository import SessionRepository
from app.repositories.user_identity_repository import UserIdentityRepository
from app.repositories.user_repository import UserRepository
from app.services.current_user import CurrentUser
from app.services.personal_export_service import SUBMISSION_COLUMNS, PersonalExportService
from tests.support.auth import START, FakeClock, add_user
from tests.support.script_data import validated

TITLE = "只該出現在劇本區塊的標題SCRIPT-TITLE-ONLY"


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
    return json.loads(raw.decode("utf-8"))


def export_csv(session, user: User) -> dict[str, bytes]:
    session.expire_all()
    result = service_for(session).build(CurrentUser.from_model(session.get(User, user.id)), "csv")
    with zipfile.ZipFile(result.handle) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    result.handle.close()
    return files


def rows(raw: bytes) -> list[list[str]]:
    assert raw.startswith(b"\xef\xbb\xbf")
    return list(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline="")))


def seed_records(session, user: User, other: User) -> None:
    repo = ScriptSubmissionRepository(session)
    repo.record(user.id, SUBMISSION_AUTO_REJECTED, ("minor_sexual", "non_consensual"), START)
    repo.record(user.id, SUBMISSION_SENT, (), START + timedelta(hours=1))
    repo.record(user.id, SUBMISSION_LIFTED, (), START + timedelta(hours=2))
    repo.record(other.id, SUBMISSION_AUTO_REJECTED, ("sexual_violence",), START)
    session.commit()


def test_the_json_has_my_submission_records_oldest_first_with_only_outcome_categories_and_time(member_session):
    user, other = add_user(member_session, 1), add_user(member_session, 2)
    seed_records(member_session, user, other)
    data = export_json(member_session, user)
    assert [(r["outcome"], r["categories"]) for r in data["script_submissions"]] == [
        ("auto_rejected", ["minor_sexual", "non_consensual"]), ("sent", []), ("lifted", []),
    ]
    assert all(set(r) == {"outcome", "categories", "created_at"} for r in data["script_submissions"])
    assert data["script_submissions"][0]["created_at"] == "2026-10-27T12:00:00Z"
    assert "sexual_violence" not in json.dumps(data, ensure_ascii=False)                  # 他人的紀錄


def test_the_json_has_an_empty_list_when_there_are_no_records(member_session):
    user = add_user(member_session, 1)
    assert export_json(member_session, user)["script_submissions"] == []


def test_the_csv_has_a_submissions_file_with_a_bom_the_documented_columns_and_nobody_elses(member_session):
    user, other = add_user(member_session, 1), add_user(member_session, 2)
    seed_records(member_session, user, other)
    files = export_csv(member_session, user)
    table = rows(files["script_submissions.csv"])
    assert table[0] == list(SUBMISSION_COLUMNS) and len(table) == 4
    assert table[1][:2] == ["auto_rejected", "minor_sexual、non_consensual"]
    assert [row[0] for row in table[1:]] == ["auto_rejected", "sent", "lifted"]
    assert b"sexual_violence" not in files["script_submissions.csv"]


def test_a_cell_that_starts_like_a_formula_is_neutralised(member_session):
    user = add_user(member_session, 1)
    ScriptSubmissionRepository(member_session).record(user.id, SUBMISSION_AUTO_REJECTED, ("=cmd|' /C calc'!A0",), START)
    member_session.commit()
    table = rows(export_csv(member_session, user)["script_submissions.csv"])
    assert table[1][1] == "'=cmd|' /C calc'!A0"


def test_a_record_carries_no_script_text_and_no_internal_ids(member_session):
    user = add_user(member_session, 1)
    ScriptSubmissionRepository(member_session).record(user.id, SUBMISSION_AUTO_REJECTED, ("minor_sexual",), START)
    scripts = ScriptRepository(member_session)
    first = validated(title=TITLE)
    scripts.create_script(user.id, author_name="筆名", author_code="abc123", content=first.content, content_hash=first.content_hash, total_chars=first.total_chars)
    member_session.commit()
    data = export_json(member_session, user)
    block = json.dumps(data["script_submissions"], ensure_ascii=False)
    assert TITLE not in block and '"id"' not in block and '"user_id"' not in block
    assert TITLE in json.dumps(data["scripts"], ensure_ascii=False)                       # 劇本文字只在劇本區塊
    csv_text = export_csv(member_session, user)["script_submissions.csv"].decode("utf-8-sig")
    assert TITLE not in csv_text


def test_the_script_block_has_none_of_the_review_fields(member_session):
    user = add_user(member_session, 1)
    scripts = ScriptRepository(member_session)
    built = validated()
    version = scripts.create_script(user.id, author_name="筆名", author_code="abc123", content=built.content, content_hash=built.content_hash, total_chars=built.total_chars)
    assert scripts.record_review(version.id, built.content_hash, REVIEW_FLAG, ("tier_mismatch",), START) is True
    member_session.commit()
    data = export_json(member_session, user)
    text = json.dumps(data["scripts"], ensure_ascii=False)
    for hidden in ("review", "reviewed_at", "tier_mismatch", "content_hash", built.content_hash):
        assert hidden not in text, hidden
    files = export_csv(member_session, user)
    header = rows(files["scripts.csv"])[0]
    assert not [name for name in header if "review" in name or "hash" in name]
    assert b"tier_mismatch" not in files["scripts.csv"]


def test_the_export_does_not_log_the_categories_or_any_record(member_session, log_lines):
    user = add_user(member_session, 1)
    ScriptSubmissionRepository(member_session).record(user.id, SUBMISSION_AUTO_REJECTED, ("minor_sexual",), START)
    member_session.commit()
    export_json(member_session, user)
    export_csv(member_session, user)
    assert "minor_sexual" not in log_lines.raw()
