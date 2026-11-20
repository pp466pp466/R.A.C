"""admin-script-review 與 script-takedown（管理員部分）規格：`AdminService` 的劇本審核——清單、讀全文（先寫稽核）、核可、人工退回、
管理員下架與恢復；密碼的界線（核可與退回不要、下架與恢復要）、備註與類別驗證、前置狀態與競態的 409、稽核與動作同交易、
稽核與日誌與事件找不到備註與劇本文字、並行的核可與退回只有一個成功。

用 `AdminKit`（真的 Repository 與真的密碼確認，只有時鐘是假的），一般測試在 SQLite 與真實 SQL Server 各跑一次；並行測試用 `concurrent_engine`。
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ScriptSubmission, ScriptVersion, User
from app.models.admin_audit_log import AUDIT_ACTIONS
from app.models.script import (
    DECISION_CATEGORIES, MAX_DECISION_NOTE_CHARS, VERSION_DRAFT, VERSION_PENDING, VERSION_PUBLISHED, VERSION_REJECTED,
    VERSION_REMOVED, VERSION_SUPERSEDED,
)
from app.repositories.errors import (
    BuiltinNotRemovableError, InvalidVersionTransitionError, LiveVersionExistsError, RemovedByAuthorError,
)
from app.services import admin_script_views
from app.services.current_user import CurrentUser
from app.services.errors import (
    BuiltinNotRemovable, LiveExists, NotFound, NotPending, NotPublished, NotRemoved, RemovedByAuthor, ValidationError,
)
from tests.support.admin import GOOD_PASSWORD, WRONG_PASSWORD, AdminKit
from tests.support.script_data import validated
from tests.support.script_review import at, new_draft, put_in_status, script_public_id

NOTE = "只有管理員看得到的審核備註"
TITLE = "不該出現在稽核的標題"


@pytest.fixture()
def kit(member_session):
    return AdminKit(member_session)


@pytest.fixture()
def admin(kit):
    return kit.admin()


@pytest.fixture()
def author(kit):
    return kit.member(2)


def actions(kit):
    return [row.action for row in kit.audit_rows()]


def status_of(kit, version) -> str:
    kit.session.expire_all()
    return kit.session.get(ScriptVersion, version.id).status


def version_in(kit, author, status, **kwargs):
    version = put_in_status(kit.scripts, author, status, **kwargs)
    kit.session.commit()
    return version


# ---- 只有管理員 ----------------------------------------------------------------------------------------


CALLS = {
    "queue": lambda service, who, pid: service.script_queue(who),
    "read": lambda service, who, pid: service.read_script(who, pid),
    "approve": lambda service, who, pid: service.approve_script(who, pid),
    "reject": lambda service, who, pid: service.reject_script(who, pid, category="other_policy", note=NOTE),
    "remove": lambda service, who, pid: service.remove_script(who, pid, category="other_policy", password=GOOD_PASSWORD),
    "restore": lambda service, who, pid: service.restore_script(who, pid, password=GOOD_PASSWORD),
}


@pytest.mark.parametrize("name", sorted(CALLS))
def test_a_player_gets_a_404_from_every_script_review_method_and_nothing_changes(kit, author, name):
    version = version_in(kit, author, VERSION_PENDING)
    player = kit.current(kit.member(3))
    with pytest.raises(NotFound):
        CALLS[name](kit.service, player, version.public_id)
    assert status_of(kit, version) == VERSION_PENDING and kit.audit_rows() == []


# ---- 清單 ----------------------------------------------------------------------------------------------


def test_the_queue_defaults_to_pending_and_puts_the_flagged_first(kit, admin, author):
    older = version_in(kit, author, VERSION_PENDING, title="甲較舊通過", now=at(1))
    newer = version_in(kit, author, VERSION_PENDING, title="乙較新有疑慮", now=at(2))
    version_in(kit, author, VERSION_PENDING, title="丙最新通過", now=at(3))
    assert kit.scripts.record_review(newer.id, newer.content_hash, "flag", ("minor_suspected",), at())
    assert kit.scripts.record_review(older.id, older.content_hash, "pass", (), at())
    kit.session.commit()
    items = kit.service.script_queue(admin)
    assert [item["title"] for item in items] == ["乙較新有疑慮", "甲較舊通過", "丙最新通過"]
    assert items[0]["review_verdict"] == "flag" and items[0]["review_categories"] == ["minor_suspected"]


@pytest.mark.parametrize("status, expected", [(VERSION_PENDING, "待審"), (VERSION_PUBLISHED, "上架"), (VERSION_REMOVED, "下架")])
def test_each_list_has_its_own_versions_in_the_documented_shape(kit, admin, author, status, expected):
    for s, title in ((VERSION_PENDING, "待審"), (VERSION_PUBLISHED, "上架"), (VERSION_REMOVED, "下架"), (VERSION_REJECTED, "退回")):
        version_in(kit, author, s, title=title)
    items = kit.service.script_queue(admin, status=status)
    assert [item["title"] for item in items] == [expected]
    assert set(items[0]) == set(admin_script_views.LIST_KEYS)
    assert items[0]["author_id"] == author.public_id


def test_the_queue_has_no_script_text_and_no_audit(kit, admin, author):
    version_in(kit, author, VERSION_REMOVED, removed_by="admin")
    text = repr(kit.service.script_queue(admin, status=VERSION_REMOVED))
    assert "靈氣復甦" not in text and "林曉" not in text and "下架的備註" not in text and author.email not in text
    assert kit.audit_rows() == []


@pytest.mark.parametrize(
    "arguments, field",
    [({"status": VERSION_DRAFT}, "status"), ({"status": "nope"}, "status"), ({"status": None}, "status"), ({"status": 3}, "status"),
     ({"limit": 101}, "limit"), ({"limit": 0}, "limit"), ({"offset": -1}, "offset")],
)
def test_an_invalid_status_or_page_is_a_400_naming_the_field(kit, admin, arguments, field):
    with pytest.raises(ValidationError) as exc:
        kit.service.script_queue(admin, **arguments)
    assert field in exc.value.fields


def test_the_queue_reports_a_bad_status_and_a_bad_page_together(kit, admin):
    with pytest.raises(ValidationError) as exc:
        kit.service.script_queue(admin, status="nope", limit=500)
    assert set(exc.value.fields) == {"status", "limit"}


def test_the_queue_pages(kit, admin, author):
    for minute in (1, 2, 3):
        version_in(kit, author, VERSION_PENDING, title=f"第{minute}份", now=at(minute))
    assert [i["title"] for i in kit.service.script_queue(admin, limit=1, offset=1)] == ["第2份"]


# ---- 讀全文（先寫稽核）-----------------------------------------------------------------------------------


def test_reading_a_pending_version_returns_the_full_content_and_writes_a_read_audit(kit, admin, author):
    version = version_in(kit, author, VERSION_PENDING)
    result = kit.service.read_script(admin, version.public_id)["version"]
    assert set(result) == set(admin_script_views.DETAIL_KEYS)
    assert result["content"]["background"].startswith("靈氣復甦") and result["content"]["characters"][0]["name"] == "林曉"
    [row] = kit.audit_rows()
    assert row.action == "script.read" and row.target_user_public_id == author.public_id
    assert row.detail == {"version_id": version.public_id, "script_id": script_public_id(kit.scripts, version), "status": VERSION_PENDING}
    assert row.admin_display_name == "使用者1" and row.reason is None


def test_every_read_is_audited_even_a_repeat(kit, admin, author):
    version = version_in(kit, author, VERSION_PUBLISHED)
    kit.service.read_script(admin, version.public_id)
    kit.service.read_script(admin, version.public_id)
    assert actions(kit) == ["script.read", "script.read"]


def test_the_detail_of_a_rejected_version_has_the_category_and_the_note_but_the_audit_does_not(kit, admin, author):
    version = version_in(kit, author, VERSION_PENDING, title=TITLE)
    kit.service.reject_script(admin, version.public_id, category="other_policy", note=NOTE)
    result = kit.service.read_script(admin, version.public_id)["version"]
    assert (result["decision_category"], result["decision_note"], result["has_note"]) == ("other_policy", NOTE, True)
    assert NOTE not in repr(kit.audit_rows()) and TITLE not in repr(kit.audit_rows())


def test_a_draft_a_withdrawn_version_and_a_missing_id_are_the_same_404_and_leave_no_audit(kit, admin, author):
    draft = version_in(kit, author, VERSION_DRAFT)
    withdrawn = version_in(kit, author, VERSION_PENDING)
    kit.scripts.withdraw(author.id, withdrawn.public_id)
    kit.session.commit()
    messages = set()
    for public_id in (draft.public_id, withdrawn.public_id, "00000000-0000-0000-0000-000000000000", "nope", ""):
        with pytest.raises(NotFound) as exc:
            kit.service.read_script(admin, public_id)
        messages.add((exc.value.code, exc.value.message))
    assert len(messages) == 1 and kit.audit_rows() == []


def test_a_builtin_script_is_read_with_no_target_user(kit, admin):
    result = validated(title="官方劇本")
    version, _ = kit.scripts.publish_builtin(
        "official", content=result.content, content_hash=result.content_hash, total_chars=result.total_chars, now=at(1),
    )
    kit.session.commit()
    kit.service.read_script(admin, version.public_id)
    [row] = kit.audit_rows()
    assert row.target_user_public_id is None and row.action == "script.read"


def test_when_the_read_audit_cannot_be_written_nothing_is_returned(kit, admin, author, monkeypatch):
    version = version_in(kit, author, VERSION_PENDING)

    def failing(**kwargs):
        raise RuntimeError("稽核寫入失敗")

    monkeypatch.setattr(kit.audit, "add", failing)
    with pytest.raises(RuntimeError):
        kit.service.read_script(admin, version.public_id)


# ---- 核可 ----------------------------------------------------------------------------------------------


def test_approving_publishes_the_version_and_writes_one_audit_with_only_identifiers(kit, admin, author):
    version = version_in(kit, author, VERSION_PENDING, title=TITLE)
    result = kit.service.approve_script(admin, version.public_id)["version"]
    assert (result["status"], result["id"]) == (VERSION_PUBLISHED, version.public_id) and set(result) == set(admin_script_views.LIST_KEYS)
    assert result["published_at"] is not None
    assert status_of(kit, version) == VERSION_PUBLISHED
    [row] = kit.audit_rows()
    assert row.action == "script.approve" and row.target_user_public_id == author.public_id
    assert row.detail == {"version_id": version.public_id, "script_id": script_public_id(kit.scripts, version), "status": VERSION_PUBLISHED}
    assert TITLE not in repr(row) and author.email not in repr(row)


def test_approving_the_second_version_supersedes_the_first(kit, admin, author):
    first = version_in(kit, author, VERSION_PUBLISHED)
    second = new_draft(kit.scripts, author, script_public_id(kit.scripts, first))
    kit.scripts.submit_for_review(author.id, second.public_id, now=at(5))
    kit.session.commit()
    kit.service.approve_script(admin, second.public_id)
    assert (status_of(kit, first), status_of(kit, second)) == (VERSION_SUPERSEDED, VERSION_PUBLISHED)


def test_the_first_approval_locks_the_pen_name(kit, admin, author):
    version = version_in(kit, author, VERSION_PENDING)
    assert kit.scripts.get_script_of(version).author_locked_at is None
    kit.service.approve_script(admin, version.public_id)
    kit.session.expire_all()
    assert kit.scripts.get_script_of(version).author_locked_at is not None


def test_approving_a_removed_version_s_successor_makes_the_removed_one_unrestorable(kit, admin, author):
    removed = version_in(kit, author, VERSION_REMOVED, removed_by="admin")
    successor = new_draft(kit.scripts, author, script_public_id(kit.scripts, removed))
    kit.scripts.submit_for_review(author.id, successor.public_id, now=at(5))
    kit.session.commit()
    kit.service.approve_script(admin, successor.public_id)
    assert status_of(kit, removed) == VERSION_SUPERSEDED
    with pytest.raises(NotRemoved):
        kit.service.restore_script(admin, removed.public_id, password=GOOD_PASSWORD)


@pytest.mark.parametrize("status", [VERSION_DRAFT, VERSION_PUBLISHED, VERSION_REJECTED, VERSION_REMOVED, VERSION_SUPERSEDED])
def test_approving_a_version_that_is_not_pending_is_a_409_without_audit_or_change(kit, admin, author, status):
    version = version_in(kit, author, status)
    before = status_of(kit, version)
    with pytest.raises(NotPending) as exc:
        kit.service.approve_script(admin, version.public_id)
    assert exc.value.code == "not_pending" and "待審" in exc.value.message and "撤回" not in exc.value.message
    assert status_of(kit, version) == before and kit.audit_rows() == []


def test_approving_after_the_author_withdrew_is_a_409(kit, admin, author):
    version = version_in(kit, author, VERSION_PENDING)
    kit.scripts.withdraw(author.id, version.public_id)
    kit.session.commit()
    with pytest.raises(NotPending):
        kit.service.approve_script(admin, version.public_id)
    assert kit.audit_rows() == []


def test_approving_or_rejecting_an_unknown_id_is_a_404(kit, admin):
    for call in (
        lambda: kit.service.approve_script(admin, "00000000-0000-0000-0000-000000000000"),
        lambda: kit.service.reject_script(admin, "nope", category="other_policy"),
        lambda: kit.service.approve_script(admin, None),
    ):
        with pytest.raises(NotFound):
            call()


def test_approving_and_rejecting_never_ask_for_the_password(kit, admin, author, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("核可與退回不應要求重新輸入密碼")

    monkeypatch.setattr(kit.service, "_confirm_password", forbidden)
    first = version_in(kit, author, VERSION_PENDING, title="核可的")
    second = version_in(kit, author, VERSION_PENDING, title="退回的")
    kit.service.approve_script(admin, first.public_id)
    kit.service.reject_script(admin, second.public_id, category="other_policy")
    assert actions(kit) == ["script.reject", "script.approve"]


# ---- 退回 ----------------------------------------------------------------------------------------------


def test_rejecting_records_the_category_the_note_and_a_category_only_audit(kit, admin, author):
    version = version_in(kit, author, VERSION_PENDING, title=TITLE)
    result = kit.service.reject_script(admin, version.public_id, category="other_policy", note=NOTE)["version"]
    assert (result["status"], result["decision_category"], result["has_note"]) == (VERSION_REJECTED, "other_policy", True)
    assert "decision_note" not in result and NOTE not in repr(result)
    kit.session.expire_all()
    row = kit.session.get(ScriptVersion, version.id)
    assert (row.status, row.decision_category, row.decision_note, row.decided_at) == (VERSION_REJECTED, "other_policy", NOTE, kit.clock())
    assert row.open_script_id is None
    [audit] = kit.audit_rows()
    assert audit.action == "script.reject" and audit.reason is None
    assert audit.detail == {"version_id": version.public_id, "script_id": script_public_id(kit.scripts, version), "status": VERSION_REJECTED, "category": "other_policy"}
    assert NOTE not in repr(audit) and TITLE not in repr(audit)


@pytest.mark.parametrize("category", DECISION_CATEGORIES)
def test_every_decision_category_is_accepted(kit, admin, author, category):
    version = version_in(kit, author, VERSION_PENDING)
    assert kit.service.reject_script(admin, version.public_id, category=category)["version"]["decision_category"] == category


@pytest.mark.parametrize("category", [None, "", "free text", "OTHER_POLICY", 3, ["other_policy"], "tier_mismatch "])
def test_a_missing_or_unknown_category_is_a_400_on_category_and_the_version_stays_pending(kit, admin, author, category):
    version = version_in(kit, author, VERSION_PENDING)
    with pytest.raises(ValidationError) as exc:
        kit.service.reject_script(admin, version.public_id, category=category, note=NOTE)
    assert set(exc.value.fields) == {"category"}
    assert status_of(kit, version) == VERSION_PENDING and kit.audit_rows() == []


def test_a_note_of_exactly_the_limit_is_accepted_and_one_more_is_a_400_on_note(kit, admin, author):
    version = version_in(kit, author, VERSION_PENDING)
    with pytest.raises(ValidationError) as exc:
        kit.service.reject_script(admin, version.public_id, category="other_policy", note="字" * (MAX_DECISION_NOTE_CHARS + 1))
    assert set(exc.value.fields) == {"note"} and status_of(kit, version) == VERSION_PENDING
    kit.service.reject_script(admin, version.public_id, category="other_policy", note="字" * MAX_DECISION_NOTE_CHARS)
    kit.session.expire_all()
    assert len(kit.session.get(ScriptVersion, version.id).decision_note) == MAX_DECISION_NOTE_CHARS


def test_a_bad_category_and_a_bad_note_are_reported_together(kit, admin, author):
    version = version_in(kit, author, VERSION_PENDING)
    with pytest.raises(ValidationError) as exc:
        kit.service.reject_script(admin, version.public_id, category="nope", note=123)
    assert set(exc.value.fields) == {"category", "note"}


def test_the_note_is_nfc_normalized_stripped_of_control_characters_and_keeps_line_breaks(kit, admin, author):
    version = version_in(kit, author, VERSION_PENDING)
    kit.service.reject_script(admin, version.public_id, category="other_policy", note="  café\n第二行\x00\x07結尾  ")
    assert kit.service.read_script(admin, version.public_id)["version"]["decision_note"] == "café\n第二行結尾"


@pytest.mark.parametrize("note", [None, "", "   ", "\n\t \x00"])
def test_an_empty_note_means_no_note(kit, admin, author, note):
    version = version_in(kit, author, VERSION_PENDING)
    result = kit.service.reject_script(admin, version.public_id, category="other_policy", note=note)["version"]
    assert result["has_note"] is False
    assert kit.service.read_script(admin, version.public_id)["version"]["decision_note"] is None


def test_a_note_that_cannot_be_stored_is_a_400(kit, admin, author):
    version = version_in(kit, author, VERSION_PENDING)
    with pytest.raises(ValidationError) as exc:
        kit.service.reject_script(admin, version.public_id, category="other_policy", note="孤立的代理字元\ud800")
    assert set(exc.value.fields) == {"note"}


@pytest.mark.parametrize("status", [VERSION_DRAFT, VERSION_PUBLISHED, VERSION_REJECTED, VERSION_REMOVED, VERSION_SUPERSEDED])
def test_rejecting_a_version_that_is_not_pending_is_a_409_and_changes_nothing(kit, admin, author, status):
    version = version_in(kit, author, status)
    with pytest.raises(NotPending):
        kit.service.reject_script(admin, version.public_id, category="other_policy", note=NOTE)
    assert status_of(kit, version) == status and kit.audit_rows() == []


def test_a_manual_rejection_is_not_an_automatic_one(kit, admin, author):
    """人工退回不計入送審次數或連續被自動拒絕：沒有新增任何送審紀錄，作者也沒有被暫停發佈。"""
    version = version_in(kit, author, VERSION_PENDING)
    before = kit.session.scalar(select(func.count()).select_from(ScriptSubmission))
    kit.service.reject_script(admin, version.public_id, category="minor_sexual", note=NOTE)
    assert kit.session.scalar(select(func.count()).select_from(ScriptSubmission)) == before
    kit.session.expire_all()
    assert kit.session.get(User, author.id).publish_suspended_at is None


# ---- 下架 ----------------------------------------------------------------------------------------------


def test_removing_needs_the_password_and_records_the_remover_category_and_note(kit, admin, author):
    version = version_in(kit, author, VERSION_PUBLISHED, title=TITLE)
    result = kit.service.remove_script(admin, version.public_id, category="other_policy", note=NOTE, password=GOOD_PASSWORD)["version"]
    assert (result["status"], result["removed_by"], result["decision_category"], result["has_note"]) == (VERSION_REMOVED, "admin", "other_policy", True)
    kit.session.expire_all()
    row = kit.session.get(ScriptVersion, version.id)
    assert (row.status, row.removed_by, row.decision_note, row.live_script_id, row.open_script_id) == (VERSION_REMOVED, "admin", NOTE, None, None)
    [audit] = kit.audit_rows()
    assert audit.action == "script.remove" and audit.target_user_public_id == author.public_id
    assert audit.detail == {"version_id": version.public_id, "script_id": script_public_id(kit.scripts, version), "status": VERSION_REMOVED, "category": "other_policy"}
    assert NOTE not in repr(audit) and TITLE not in repr(audit)


@pytest.mark.parametrize("password", [None, "", WRONG_PASSWORD, 123])
def test_a_wrong_or_missing_password_is_refused_with_a_reauth_audit_and_the_version_stays_live(kit, admin, author, password):
    version = version_in(kit, author, VERSION_PUBLISHED)
    with pytest.raises(ValidationError) as exc:
        kit.service.remove_script(admin, version.public_id, category="other_policy", note=NOTE, password=password)
    assert set(exc.value.fields) == {"password"}
    assert status_of(kit, version) == VERSION_PUBLISHED
    [audit] = kit.audit_rows()
    assert (audit.action, audit.detail, audit.target_user_public_id) == ("reauth.failed", {"attempted": "script.remove"}, author.public_id)
    assert NOTE not in repr(audit)
    assert kit.persisted.calls >= 1                                    # 4xx 的請求也保留 `reauth.failed`（錯誤密碼另有失敗計數的保存）


def test_the_category_is_checked_before_the_password(kit, admin, author):
    version = version_in(kit, author, VERSION_PUBLISHED)
    with pytest.raises(ValidationError) as exc:
        kit.service.remove_script(admin, version.public_id, category=None, password=WRONG_PASSWORD)
    assert set(exc.value.fields) == {"category"} and kit.audit_rows() == [] and kit.persisted.calls == 0


@pytest.mark.parametrize("status", [VERSION_DRAFT, VERSION_PENDING, VERSION_REJECTED, VERSION_REMOVED, VERSION_SUPERSEDED])
def test_only_a_live_version_can_be_removed_and_the_password_is_not_asked_first(kit, admin, author, status):
    version = version_in(kit, author, status)
    with pytest.raises(NotPublished) as exc:
        kit.service.remove_script(admin, version.public_id, category="other_policy", password=WRONG_PASSWORD)
    assert exc.value.code == "not_published" and status_of(kit, version) == status and kit.audit_rows() == []


def test_a_builtin_script_cannot_be_removed(kit, admin):
    result = validated(title="官方劇本")
    version, _ = kit.scripts.publish_builtin(
        "official", content=result.content, content_hash=result.content_hash, total_chars=result.total_chars, now=at(1),
    )
    kit.session.commit()
    with pytest.raises(BuiltinNotRemovable) as exc:
        kit.service.remove_script(admin, version.public_id, category="other_policy", password=GOOD_PASSWORD)
    assert exc.value.code == "builtin_not_removable" and status_of(kit, version) == VERSION_PUBLISHED and kit.audit_rows() == []


def test_removing_an_unknown_id_is_a_404(kit, admin):
    with pytest.raises(NotFound):
        kit.service.remove_script(admin, "00000000-0000-0000-0000-000000000000", category="other_policy", password=GOOD_PASSWORD)


# ---- 恢復 ----------------------------------------------------------------------------------------------


def test_restoring_a_version_the_admin_removed_needs_the_password_and_clears_the_decision(kit, admin, author):
    version = version_in(kit, author, VERSION_REMOVED, removed_by="admin")
    kit.session.expire_all()
    published_at = kit.session.get(ScriptVersion, version.id).published_at
    result = kit.service.restore_script(admin, version.public_id, password=GOOD_PASSWORD)["version"]
    assert (result["status"], result["removed_by"], result["decision_category"], result["has_note"]) == (VERSION_PUBLISHED, None, None, False)
    kit.session.expire_all()
    row = kit.session.get(ScriptVersion, version.id)
    assert (row.published_at, row.decision_note, row.decided_at, row.live_script_id) == (published_at, None, None, row.script_id)
    [audit] = kit.audit_rows()
    assert audit.action == "script.restore" and audit.detail == {
        "version_id": version.public_id, "script_id": script_public_id(kit.scripts, version), "status": VERSION_PUBLISHED,
    }


@pytest.mark.parametrize("password", [None, "", WRONG_PASSWORD])
def test_restoring_with_a_wrong_password_is_refused_with_a_reauth_audit(kit, admin, author, password):
    version = version_in(kit, author, VERSION_REMOVED, removed_by="admin")
    with pytest.raises(ValidationError) as exc:
        kit.service.restore_script(admin, version.public_id, password=password)
    assert set(exc.value.fields) == {"password"} and status_of(kit, version) == VERSION_REMOVED
    [audit] = kit.audit_rows()
    assert (audit.action, audit.detail) == ("reauth.failed", {"attempted": "script.restore"})


def test_the_admin_does_not_restore_what_the_author_removed(kit, admin, author):
    version = version_in(kit, author, VERSION_REMOVED, removed_by="author")
    with pytest.raises(RemovedByAuthor) as exc:
        kit.service.restore_script(admin, version.public_id, password=WRONG_PASSWORD)
    assert exc.value.code == "removed_by_author" and status_of(kit, version) == VERSION_REMOVED and kit.audit_rows() == []


@pytest.mark.parametrize("status", [VERSION_DRAFT, VERSION_PENDING, VERSION_PUBLISHED, VERSION_REJECTED, VERSION_SUPERSEDED])
def test_only_a_removed_version_can_be_restored(kit, admin, author, status):
    version = version_in(kit, author, status)
    with pytest.raises(NotRemoved) as exc:
        kit.service.restore_script(admin, version.public_id, password=GOOD_PASSWORD)
    assert exc.value.code == "not_removed" and status_of(kit, version) == status and kit.audit_rows() == []


def test_a_remove_then_restore_round_trip_leaves_the_version_live_again(kit, admin, author):
    version = version_in(kit, author, VERSION_PUBLISHED)
    kit.service.remove_script(admin, version.public_id, category="other_policy", note=NOTE, password=GOOD_PASSWORD)
    assert kit.scripts.get_published(version.public_id) is None
    kit.service.restore_script(admin, version.public_id, password=GOOD_PASSWORD)
    assert kit.scripts.get_published(version.public_id) is not None
    assert actions(kit) == ["script.restore", "script.remove"]


# ---- 競態：前置檢查通過之後，Repository 的條件式 UPDATE 仍是最後一道 -------------------------------------------


def lose_the_race(exception):
    def raiser(*args, **kwargs):
        raise exception

    return raiser


@pytest.mark.parametrize("repository_error, service_error", [
    (InvalidVersionTransitionError(), NotPending),
])
def test_losing_the_race_on_an_approval_or_rejection_is_a_409(kit, admin, author, monkeypatch, repository_error, service_error):
    version = version_in(kit, author, VERSION_PENDING)
    monkeypatch.setattr(kit.scripts, "approve_version", lose_the_race(repository_error))
    monkeypatch.setattr(kit.scripts, "reject_version", lose_the_race(repository_error))
    with pytest.raises(service_error):
        kit.service.approve_script(admin, version.public_id)
    with pytest.raises(service_error):
        kit.service.reject_script(admin, version.public_id, category="other_policy")
    assert kit.audit_rows() == []


def test_losing_the_race_on_a_removal_is_a_409_and_leaves_no_audit(kit, admin, author, monkeypatch):
    version = version_in(kit, author, VERSION_PUBLISHED)
    monkeypatch.setattr(kit.scripts, "remove_by_admin", lose_the_race(InvalidVersionTransitionError()))
    with pytest.raises(NotPublished):
        kit.service.remove_script(admin, version.public_id, category="other_policy", password=GOOD_PASSWORD)
    monkeypatch.setattr(kit.scripts, "remove_by_admin", lose_the_race(BuiltinNotRemovableError()))
    with pytest.raises(BuiltinNotRemovable):
        kit.service.remove_script(admin, version.public_id, category="other_policy", password=GOOD_PASSWORD)
    assert kit.audit_rows() == []


@pytest.mark.parametrize("repository_error, service_error", [
    (InvalidVersionTransitionError(), NotRemoved), (RemovedByAuthorError(), RemovedByAuthor), (LiveVersionExistsError(), LiveExists),
])
def test_losing_the_race_on_a_restore_maps_to_the_documented_409(kit, admin, author, monkeypatch, repository_error, service_error):
    version = version_in(kit, author, VERSION_REMOVED, removed_by="admin")
    monkeypatch.setattr(kit.scripts, "restore_by_admin", lose_the_race(repository_error))
    with pytest.raises(service_error) as exc:
        kit.service.restore_script(admin, version.public_id, password=GOOD_PASSWORD)
    assert exc.value.code == service_error.code and kit.audit_rows() == []


def test_the_repository_returning_nothing_after_the_pre_check_is_a_404(kit, admin, author, monkeypatch):
    version = version_in(kit, author, VERSION_PENDING)
    monkeypatch.setattr(kit.scripts, "approve_version", lambda *a, **k: None)
    monkeypatch.setattr(kit.scripts, "reject_version", lambda *a, **k: None)
    with pytest.raises(NotFound):
        kit.service.approve_script(admin, version.public_id)
    with pytest.raises(NotFound):
        kit.service.reject_script(admin, version.public_id, category="other_policy")


# ---- 稽核與動作同一個交易 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("name, status", [("approve", VERSION_PENDING), ("reject", VERSION_PENDING), ("remove", VERSION_PUBLISHED)])
def test_when_the_audit_fails_the_change_is_rolled_back_with_it(kit, admin, author, monkeypatch, name, status):
    if name == "approve" and kit.session.get_bind().dialect.name == "sqlite":
        pytest.skip(
            "核可用 SAVEPOINT：pysqlite 在沒有進行中的交易時，RELEASE 會直接提交（驅動的已知行為，只影響本機 SQLite；"
            "SQL Server 的同一個測試通過）"
        )
    version = version_in(kit, author, status)

    def failing(**kwargs):
        raise RuntimeError("稽核寫入失敗")

    monkeypatch.setattr(kit.audit, "add", failing)
    with pytest.raises(RuntimeError):
        CALLS[name](kit.service, admin, version.public_id)
    kit.session.rollback()                                             # 請求失敗時，請求交易會整個回復
    assert status_of(kit, version) == status


# ---- 稽核、日誌與事件不含備註與劇本文字 ---------------------------------------------------------------------


def test_the_audit_the_log_and_the_events_never_contain_the_note_the_title_or_the_script_text(kit, admin, author, caplog):
    caplog.set_level(logging.INFO, logger="app")
    rejected = version_in(kit, author, VERSION_PENDING, title=TITLE, summary="簡介文字不該出現")
    kit.service.read_script(admin, rejected.public_id)
    kit.service.reject_script(admin, rejected.public_id, category="other_policy", note=NOTE)
    live = version_in(kit, author, VERSION_PENDING, title=TITLE + "二", summary="簡介文字不該出現")
    kit.service.approve_script(admin, live.public_id)
    kit.service.remove_script(admin, live.public_id, category="tier_mismatch", note=NOTE, password=GOOD_PASSWORD)
    kit.service.restore_script(admin, live.public_id, password=GOOD_PASSWORD)
    with pytest.raises(ValidationError):
        kit.service.remove_script(admin, live.public_id, category="other_policy", note=NOTE, password=WRONG_PASSWORD)
    forbidden = (NOTE, TITLE, "簡介文字不該出現", "靈氣復甦", "林曉", author.email, "筆名")
    stored = repr(kit.audit_rows())
    logged = " ".join(f"{record.getMessage()} {sorted(record.__dict__.items(), key=str)}" for record in caplog.records)
    for text in forbidden:
        assert text not in stored, text
        assert text not in logged, text
    events = {getattr(record, "event", None) for record in caplog.records}
    assert {"script.approved", "script.rejected", "script.removed", "script.restored"} <= events
    assert all(row.action in AUDIT_ACTIONS for row in kit.audit_rows())


def test_the_script_events_record_the_admin_as_the_actor_with_fixed_values(kit, admin, author, caplog):
    caplog.set_level(logging.INFO, logger="app.script")
    version = version_in(kit, author, VERSION_PENDING)
    kit.service.approve_script(admin, version.public_id)
    kit.service.remove_script(admin, version.public_id, category="non_consensual", password=GOOD_PASSWORD)
    kit.service.restore_script(admin, version.public_id, password=GOOD_PASSWORD)
    records = [r for r in caplog.records if r.name == "app.script"]
    assert [(r.event, r.user_id, getattr(r, "kind", None), getattr(r, "reason", None)) for r in records] == [
        ("script.approved", admin.id, None, None), ("script.removed", admin.id, "admin", "non_consensual"),
        ("script.restored", admin.id, "admin", None),
    ]


# ---- 並行 ----------------------------------------------------------------------------------------------


def test_an_approval_and_a_rejection_at_the_same_time_succeed_at_most_once(concurrent_engine):
    with Session(concurrent_engine) as s:
        kit = AdminKit(s)
        kit.admin()
        owner = kit.member(2)
        version = put_in_status(kit.scripts, owner, VERSION_PENDING)
        s.commit()
        pid = version.public_id
        admin_id = s.scalar(select(User.id).where(User.role == "admin"))

    barrier = Barrier(2)

    def worker(action: str) -> str:
        with Session(concurrent_engine) as session:
            kit = AdminKit(session)
            admin = CurrentUser.from_model(session.get(User, admin_id))
            barrier.wait(timeout=30)
            try:
                if action == "approve":
                    kit.service.approve_script(admin, pid)
                else:
                    kit.service.reject_script(admin, pid, category="other_policy", note=NOTE)
            except NotPending:
                session.rollback()
                return "conflict"
            session.commit()
            return action

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [f.result(timeout=60) for f in [pool.submit(worker, "approve"), pool.submit(worker, "reject")]]
    assert sorted(outcomes).count("conflict") == 1 and len(set(outcomes)) == 2, outcomes
    winner = next(o for o in outcomes if o != "conflict")
    with Session(concurrent_engine) as s:
        row = s.scalar(select(ScriptVersion).where(ScriptVersion.public_id == pid))
        assert row.status == (VERSION_PUBLISHED if winner == "approve" else VERSION_REJECTED)
        audits = [a.action for a in AdminKit(s).audit.list(limit=10, offset=0)]
        assert audits == [f"script.{winner}"]                              # 輸的一方沒有留下稽核
