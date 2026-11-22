"""personal-data-export 規格（rpg-script-reports 的修改）：匯出自己送出的劇本檢舉與匿名保留偏好。

- JSON 的 `script_reports`（被檢舉版本的對外識別碼、理由、自寫文字、送出時間，由舊到新）與 CSV 的 `script_reports.csv`（UTF-8-sig、公式字元中和）。
- **不含**結案欄位與結果（檢舉人不會被通知結果）、版本標題與作者（不洩漏被檢舉作者的任何資訊）、內部編號，也沒有別人的檢舉；
  被檢舉的作者匯出時沒有任何「別人對我的檢舉」。
- 帳號資料多一個欄位 `keep_published_scripts`（匿名保留偏好，預設關）。

以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行。
"""
import csv
import io
import json
import zipfile
from datetime import timedelta

from app.models import ScriptReport, User
from app.models.script import VERSION_PUBLISHED
from app.models.script_report import CLOSED_DISMISSED, CLOSED_REMOVED, CLOSED_SUPERSEDED
from app.repositories.chat_repository import ChatRepository
from app.repositories.lock_appeal_repository import LockAppealRepository
from app.repositories.script_report_repository import ScriptReportRepository
from app.repositories.script_repository import ScriptRepository
from app.repositories.script_submission_repository import ScriptSubmissionRepository
from app.repositories.session_repository import SessionRepository
from app.repositories.user_identity_repository import UserIdentityRepository
from app.repositories.user_repository import UserRepository
from app.services.current_user import CurrentUser
from app.services.personal_export_service import ACCOUNT_COLUMNS, REPORT_COLUMNS, PersonalExportService
from tests.support.auth import START, FakeClock, add_user
from tests.support.script_review import make_author, put_in_status

TITLE = "被檢舉劇本的標題REPORTED-TITLE"
NICKNAME = "被檢舉作者的筆名REPORTED-AUTHOR"
NOTE = "我的檢舉文字MY-REPORT-NOTE"
OTHER_NOTE = "別人的檢舉文字OTHER-REPORT-NOTE"


def service_for(session) -> PersonalExportService:
    return PersonalExportService(
        users=UserRepository(session), identities=UserIdentityRepository(session), chats=ChatRepository(session),
        appeals=LockAppealRepository(session), scripts=ScriptRepository(session), sessions=SessionRepository(session),
        submissions=ScriptSubmissionRepository(session), reports=ScriptReportRepository(session), clock=FakeClock(),
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


def world(session):
    """作者一位、檢舉人兩位、作者的兩份上架劇本（標題含可辨識的字串）。"""
    author = make_author(session, 9)
    first = put_in_status(ScriptRepository(session), author, VERSION_PUBLISHED, title=TITLE)
    second = put_in_status(ScriptRepository(session), author, VERSION_PUBLISHED, title=TITLE + "二")
    me, other = add_user(session, 1), add_user(session, 2)
    return author, first, second, me, other


def test_the_json_has_my_reports_oldest_first_with_exactly_four_keys(member_session):
    s = member_session
    _, first, second, me, other = world(s)
    repo = ScriptReportRepository(s)
    repo.add(me.id, first.id, ["plagiarism", "other"], False, NOTE, now=START)
    repo.add(me.id, second.id, ["minor_sexual"], True, None, now=START + timedelta(hours=1))
    repo.add(other.id, first.id, ["other"], False, OTHER_NOTE, now=START)
    s.commit()
    data = export_json(s, me)
    assert data["script_reports"] == [
        {"version_id": first.public_id, "reasons": ["plagiarism", "other"], "text": NOTE, "created_at": "2026-10-27T12:00:00Z"},
        {"version_id": second.public_id, "reasons": ["minor_sexual"], "text": None, "created_at": "2026-10-27T13:00:00Z"},
    ]
    assert all(set(item) == {"version_id", "reasons", "text", "created_at"} for item in data["script_reports"])


def test_the_json_never_carries_other_peoples_reports_the_title_the_author_or_closing_information(member_session):
    s = member_session
    author, first, _, me, other = world(s)
    repo = ScriptReportRepository(s)
    repo.add(me.id, first.id, ["other"], False, NOTE, now=START)
    repo.add(other.id, first.id, ["other"], False, OTHER_NOTE, now=START)
    repo.close_open(first.id, CLOSED_REMOVED, START + timedelta(days=1))
    s.commit()
    text = json.dumps(export_json(s, me)["script_reports"], ensure_ascii=False)
    assert OTHER_NOTE not in text and TITLE not in text and author.public_id not in text
    for forbidden in ("closed", "removed", "dismissed", "superseded", "red_line", "reporter", "author", "\"id\""):
        assert forbidden not in text, forbidden
    assert OTHER_NOTE not in json.dumps(export_json(s, me), ensure_ascii=False)


def test_every_closing_result_exports_the_same_shape_and_hides_the_result(member_session):
    s = member_session
    author, first, second, me, _ = world(s)
    third = put_in_status(ScriptRepository(s), author, VERSION_PUBLISHED, title="第三份")
    repo = ScriptReportRepository(s)
    for version, result in ((first, CLOSED_REMOVED), (second, CLOSED_DISMISSED), (third, CLOSED_SUPERSEDED)):
        repo.add(me.id, version.id, ["other"], False, None, now=START)
        repo.close_open(version.id, result, START + timedelta(days=1))
    s.commit()
    data = export_json(s, me)
    assert len(data["script_reports"]) == 3 and all(set(item) == {"version_id", "reasons", "text", "created_at"} for item in data["script_reports"])
    assert not any(word in json.dumps(data["script_reports"]) for word in (CLOSED_REMOVED, CLOSED_DISMISSED, CLOSED_SUPERSEDED))


def test_the_json_has_an_empty_list_when_i_never_reported(member_session):
    s = member_session
    _, first, _, me, other = world(s)
    ScriptReportRepository(s).add(other.id, first.id, ["other"], False, OTHER_NOTE, now=START)
    s.commit()
    assert export_json(s, me)["script_reports"] == []


def test_the_author_of_a_reported_script_gets_no_reports_in_their_export(member_session):
    """作者看不到檢舉人與檢舉內容：被檢舉的作者匯出時沒有任何「別人對我的檢舉」。"""
    s = member_session
    author_row, other = add_user(s, 1), add_user(s, 2)
    version = put_in_status(ScriptRepository(s), author_row, VERSION_PUBLISHED, title=TITLE)
    ScriptReportRepository(s).add(other.id, version.id, ["other"], False, OTHER_NOTE, now=START)
    s.commit()
    data = export_json(s, author_row)
    assert data["script_reports"] == [] and OTHER_NOTE not in json.dumps(data, ensure_ascii=False)
    assert OTHER_NOTE.encode() not in b"".join(export_csv(s, author_row).values())


def test_the_csv_has_a_reports_file_with_a_bom_the_documented_columns_and_only_my_rows(member_session):
    s = member_session
    _, first, second, me, other = world(s)
    repo = ScriptReportRepository(s)
    repo.add(me.id, first.id, ["plagiarism", "other"], False, NOTE, now=START)
    repo.add(me.id, second.id, ["minor_sexual"], True, None, now=START + timedelta(hours=1))
    repo.add(other.id, first.id, ["other"], False, OTHER_NOTE, now=START)
    s.commit()
    files = export_csv(s, me)
    table = rows(files["script_reports.csv"])
    assert table[0] == list(REPORT_COLUMNS) == ["version_id", "reasons", "text", "created_at"] and len(table) == 3
    assert table[1] == [first.public_id, "plagiarism、other", NOTE, "2026-10-27T12:00:00Z"]
    assert table[2][:2] == [second.public_id, "minor_sexual"] and table[2][2] == ""
    assert OTHER_NOTE.encode() not in files["script_reports.csv"] and TITLE.encode() not in files["script_reports.csv"]


def test_the_csv_has_a_header_only_file_when_i_never_reported(member_session):
    s = member_session
    _, _, _, me, _ = world(s)
    table = rows(export_csv(s, me)["script_reports.csv"])
    assert table == [list(REPORT_COLUMNS)]


def test_a_text_that_starts_like_a_formula_is_neutralised_in_the_csv(member_session):
    s = member_session
    _, first, _, me, _ = world(s)
    ScriptReportRepository(s).add(me.id, first.id, ["other"], False, "=cmd|' /C calc'!A0", now=START)
    s.commit()
    assert rows(export_csv(s, me)["script_reports.csv"])[1][2] == "'=cmd|' /C calc'!A0"


def test_exporting_changes_nothing(member_session):
    s = member_session
    _, first, _, me, _ = world(s)
    ScriptReportRepository(s).add(me.id, first.id, ["other"], False, NOTE, now=START)
    s.commit()
    export_json(s, me)
    export_csv(s, me)
    s.expire_all()
    [stored] = s.query(ScriptReport).all()
    assert stored.note == NOTE and stored.closed_at is None


def test_the_account_block_has_the_retention_preference_off_by_default_and_on_when_chosen(member_session):
    s = member_session
    me = add_user(s, 1)
    assert "keep_published_scripts" in ACCOUNT_COLUMNS
    assert export_json(s, me)["account"]["keep_published_scripts"] is False
    me.keep_published_scripts = True
    s.commit()
    assert export_json(s, me)["account"]["keep_published_scripts"] is True
    table = rows(export_csv(s, me)["account.csv"])
    assert table[1][table[0].index("keep_published_scripts")] == "True"


def test_the_report_export_is_not_empty_because_of_a_missing_registry_wiring(member_session):
    """組裝處（registry）必須接上檢舉的 Repository：漏接會讓匯出默默少一類個資。"""
    import re

    from app.config import PROJECT_ROOT

    source = (PROJECT_ROOT / "app" / "services" / "registry.py").read_text(encoding="utf-8-sig")
    block = re.search(r"def get_personal_export_service\(\).*?(?=\n\ndef )", source, re.S).group(0)
    assert "reports=ScriptReportRepository(session)" in block
    assert len(list(member_session.query(ScriptReport))) == 0                         # 只是借用 fixture 確認兩種資料庫都能跑這個測試
