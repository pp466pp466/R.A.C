"""personal-data-export 規格（rpg-script-publishing 的新增需求）：匯出納入劇本版本的審核與下架結果——狀態（含 `removed`）、
決定的類別、決定的時間與下架者；**絕不含管理員的審核備註**（JSON 與 CSV 兩種格式都一樣）；沒有決定的版本三個欄位為空；
恢復上架之後三個欄位清空。

以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行；匯出的組裝沿用 `test_personal_export_rpg_data` 的輔助。
"""
import json

import pytest

from app.models.script import (
    REMOVER_ADMIN, REMOVER_AUTHOR, VERSION_PENDING, VERSION_PUBLISHED, VERSION_REJECTED, VERSION_REMOVED,
)
from app.repositories.script_repository import ScriptRepository
from app.services.personal_export_service import SCRIPT_COLUMNS
from tests.support.auth import add_user
from tests.support.script_review import at, put_in_status
from tests.test_personal_export_rpg_data import export_csv, export_json, rows

REJECT_NOTE = "只有管理員看得到的退回備註"
REMOVE_NOTE = "只有管理員看得到的下架備註"
NEW_KEYS = ("decision_category", "decided_at", "removed_by")


def one_version(session, author, status, **kwargs):
    """作者只有一個劇本、一個版本，放進指定的狀態後匯出（JSON）那個版本；回傳 (版本的匯出內容, 原始文字)。"""
    put_in_status(ScriptRepository(session), author, status, **kwargs)
    session.commit()
    data = export_json(session, author)
    (script,) = data["scripts"]
    (version,) = script["versions"]
    return version, json.dumps(data, ensure_ascii=False)


def test_a_rejected_version_exports_its_status_category_and_time_but_not_the_note(member_session):
    me = add_user(member_session, 1)
    version, raw = one_version(member_session, me, VERSION_REJECTED)
    assert version["status"] == "rejected"
    assert (version["decision_category"], version["removed_by"]) == ("other_policy", None)
    assert version["decided_at"] is not None and version["decided_at"].endswith("Z")
    assert "管理員的備註" not in raw


def test_an_admin_takedown_exports_removed_with_the_remover_category_and_time_but_not_the_note(member_session):
    me = add_user(member_session, 1)
    version, raw = one_version(member_session, me, VERSION_REMOVED, removed_by=REMOVER_ADMIN)
    assert version["status"] == "removed" and version["removed_by"] == "admin" and version["decision_category"] == "other_policy"
    assert version["decided_at"] is not None
    assert "下架的備註" not in raw


def test_an_author_takedown_exports_removed_by_author_without_a_category(member_session):
    me = add_user(member_session, 1)
    version, _ = one_version(member_session, me, VERSION_REMOVED, removed_by=REMOVER_AUTHOR)
    assert version["status"] == "removed" and version["removed_by"] == "author"
    assert version["decision_category"] is None and version["decided_at"] is not None


@pytest.mark.parametrize("status", [VERSION_PENDING, VERSION_PUBLISHED])
def test_a_version_nobody_decided_on_has_empty_decision_fields(member_session, status):
    me = add_user(member_session, 1)
    version, _ = one_version(member_session, me, status)
    assert (version["decision_category"], version["decided_at"], version["removed_by"]) == (None, None, None)


def test_a_restored_version_exports_with_the_decision_fields_cleared(member_session):
    me = add_user(member_session, 1)
    repo = ScriptRepository(member_session)
    removed = put_in_status(repo, me, VERSION_REMOVED, removed_by=REMOVER_AUTHOR)
    repo.restore_own(me.id, removed.public_id)
    member_session.commit()
    (script,) = export_json(member_session, me)["scripts"]
    (version,) = script["versions"]
    assert version["status"] == "published" and (version["decision_category"], version["decided_at"], version["removed_by"]) == (None, None, None)


def test_the_json_keys_of_a_version_include_the_new_fields_and_no_note(member_session):
    me = add_user(member_session, 1)
    version, _ = one_version(member_session, me, VERSION_REMOVED, removed_by=REMOVER_ADMIN)
    assert set(NEW_KEYS) <= set(version) and not any("note" in key for key in version)


def test_the_csv_has_the_new_columns_with_the_values_and_no_note_anywhere(member_session):
    me = add_user(member_session, 1)
    repo = ScriptRepository(member_session)
    put_in_status(repo, me, VERSION_REJECTED)
    put_in_status(repo, me, VERSION_REMOVED, removed_by=REMOVER_ADMIN)
    put_in_status(repo, me, VERSION_REMOVED, removed_by=REMOVER_AUTHOR)
    put_in_status(repo, me, VERSION_PUBLISHED)
    member_session.commit()
    files = export_csv(member_session, me)
    table = rows(files["scripts.csv"])
    assert list(table[0]) == list(SCRIPT_COLUMNS) and set(NEW_KEYS) <= set(SCRIPT_COLUMNS)
    assert not any("note" in column for column in SCRIPT_COLUMNS)
    by_status = {(row["status"], row["removed_by"]): row for row in table}
    assert by_status[("rejected", "")]["decision_category"] == "other_policy" and by_status[("rejected", "")]["decided_at"].endswith("Z")
    assert by_status[("removed", "admin")]["decision_category"] == "other_policy" and by_status[("removed", "admin")]["decided_at"]
    assert by_status[("removed", "author")]["decision_category"] == "" and by_status[("removed", "author")]["decided_at"]
    assert (by_status[("published", "")]["decision_category"], by_status[("published", "")]["decided_at"]) == ("", "")
    for name, raw in files.items():
        text = raw.decode("utf-8-sig")
        assert "管理員的備註" not in text and "下架的備註" not in text, name


def test_no_file_of_the_json_export_has_any_note_even_when_both_kinds_of_notes_exist(member_session):
    me = add_user(member_session, 1)
    repo = ScriptRepository(member_session)
    put_in_status(repo, me, VERSION_REJECTED)
    put_in_status(repo, me, VERSION_REMOVED, removed_by=REMOVER_ADMIN)
    member_session.commit()
    raw = json.dumps(export_json(member_session, me), ensure_ascii=False)
    assert "管理員的備註" not in raw and "下架的備註" not in raw and "decision_note" not in raw
