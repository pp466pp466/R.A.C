"""rpg-script-reports 的 admin-script-reports 與 admin-audit-log 規格：`AdminService` 的檢舉部分——

- 「被檢舉」清單（`status=reported`）與狀態驗證；
- 讀取檢舉內容：每次**先寫** `report.read` 稽核再回傳（稽核失敗就不回傳）、草稿與不存在同一個 404 且沒有任何寫入、回應沒有檢舉人；
- 結案為「不成立」：不需要密碼、與稽核同一個交易、沒有未結案回 409 `no_open_reports`、之後新的玩家仍可檢舉、並行只有一個成功；
- 結案為「下架」沿用管理員下架（須密碼）：密碼錯誤檢舉仍未結案；
- 稽核、日誌與事件找不到自寫文字、理由、檢舉人與標題。

用 `AdminKit`（真的 Repository 與真的密碼確認，只有時鐘是假的），一般測試在 SQLite 與真實 SQL Server 各跑一次；並行測試用 `concurrent_engine`。
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import Script, ScriptReport, User
from app.models.script import RETIRED_AUTHOR_NAME, VERSION_DRAFT, VERSION_PENDING, VERSION_PUBLISHED
from app.models.script_report import (
    CLOSED_DISMISSED, CLOSED_REMOVED, REASON_MINOR_SEXUAL, REASON_OTHER, REASON_PLAGIARISM, RED_LINE_REASONS,
)
from app.repositories.admin_script_repository import LIST_REPORTED, LISTABLE_STATUSES
from app.repositories.script_report_repository import ScriptReportRepository
from app.services import admin_script_views
from app.services.current_user import CurrentUser
from app.services.errors import Conflict, NotFound, ValidationError
from tests.support.admin import GOOD_PASSWORD, WRONG_PASSWORD, AdminKit
from tests.support.script_review import at, put_in_status

TEXT = "只有管理員讀得到的檢舉文字 <script>alert(1)</script> 😀"
TITLE = "不該出現在稽核的標題"
NOTE = "只有管理員看得到的審核備註"
MISSING = "00000000-0000-4000-8000-000000000000"


@pytest.fixture()
def kit(member_session):
    return AdminKit(member_session)


@pytest.fixture()
def admin(kit):
    return kit.admin()


@pytest.fixture()
def author(kit):
    return kit.member(2)


@pytest.fixture()
def reporters(kit):
    return [kit.member(n) for n in (11, 12, 13, 14)]


def reports_of(kit):
    return ScriptReportRepository(kit.session)


def live(kit, author, **kwargs):
    version = put_in_status(kit.scripts, author, VERSION_PUBLISHED, now=at(1), **kwargs)
    kit.session.commit()
    return version


def report(kit, version, reporter, *, reasons=(REASON_OTHER,), note=None, now=None):
    red_line = any(reason in RED_LINE_REASONS for reason in reasons)
    reports_of(kit).add(reporter.id, version.id, list(reasons), red_line, note, now=now or at(2))
    kit.session.commit()


def states(kit, version) -> list[tuple]:
    kit.session.expire_all()
    rows = kit.session.execute(
        select(ScriptReport.closed_at, ScriptReport.closed_result).where(ScriptReport.version_id == version.id).order_by(ScriptReport.id)
    ).all()
    return [tuple(row) for row in rows]


def actions(kit):
    return [row.action for row in kit.audit_rows()]


# ---- 只有管理員 ----------------------------------------------------------------------------------------


CALLS = {
    "read_reports": lambda service, who, pid: service.script_reports(who, pid),
    "dismiss": lambda service, who, pid: service.dismiss_script_reports(who, pid),
    "queue": lambda service, who, pid: service.script_queue(who, status="reported"),
}


@pytest.mark.parametrize("name", sorted(CALLS))
def test_a_player_gets_a_404_from_every_report_method_and_nothing_changes(kit, author, reporters, name):
    version = live(kit, author)
    report(kit, version, reporters[0])
    player = kit.current(kit.member(3))
    with pytest.raises(NotFound):
        CALLS[name](kit.service, player, version.public_id)
    assert kit.audit_rows() == [] and states(kit, version) == [(None, None)]


# ---- 被檢舉清單 ----------------------------------------------------------------------------------------


def test_reported_is_an_accepted_status_and_lists_the_reported_versions_with_the_two_counters(kit, admin, author, reporters):
    hot = live(kit, author, title="被檢舉的")
    live(kit, author, title="沒被檢舉")
    report(kit, hot, reporters[0], reasons=(REASON_PLAGIARISM,)), report(kit, hot, reporters[1], reasons=(REASON_MINOR_SEXUAL,))
    items = kit.service.script_queue(admin, status="reported")
    assert [item["title"] for item in items] == ["被檢舉的"]
    assert set(items[0]) == set(admin_script_views.LIST_KEYS)
    assert (items[0]["report_count"], items[0]["red_line_reported"], items[0]["status"]) == (2, True, VERSION_PUBLISHED)
    assert kit.audit_rows() == []                                              # 清單沒有任何檢舉的文字，列出不寫稽核


def test_every_other_list_carries_the_counters_too_and_the_default_list_is_still_pending(kit, admin, author, reporters):
    put_in_status(kit.scripts, author, VERSION_PENDING, title="待審的", now=at(1))
    hot = live(kit, author, title="上架被檢舉")
    kit.session.commit()
    report(kit, hot, reporters[0])
    pending = kit.service.script_queue(admin)
    assert [i["title"] for i in pending] == ["待審的"] and (pending[0]["report_count"], pending[0]["red_line_reported"]) == (0, False)
    published = kit.service.script_queue(admin, status="published")
    assert [(i["title"], i["report_count"]) for i in published] == [("上架被檢舉", 1)]


@pytest.mark.parametrize("status", ["draft", "nope", "", None, 5, "Reported", "reported "])
def test_a_status_outside_the_four_lists_is_a_400_naming_the_field(kit, admin, status):
    with pytest.raises(ValidationError) as exc:
        kit.service.script_queue(admin, status=status)
    assert "status" in exc.value.fields and "reported" in exc.value.fields["status"]


def test_the_listable_statuses_are_the_three_states_and_the_reported_view():
    assert LISTABLE_STATUSES == ("pending_review", "published", "removed", LIST_REPORTED)


# ---- 讀取檢舉內容 --------------------------------------------------------------------------------------


def test_reading_returns_every_report_and_writes_one_read_audit_with_only_identifiers_and_the_count(kit, admin, author, reporters):
    version = live(kit, author, title=TITLE)
    report(kit, version, reporters[0], reasons=(REASON_PLAGIARISM, REASON_OTHER), note=TEXT, now=at(2))
    assert reports_of(kit).close_open(version.id, CLOSED_DISMISSED, at(3)) == 1
    report(kit, version, reporters[1], reasons=(REASON_MINOR_SEXUAL,), now=at(4))
    kit.session.commit()
    result = kit.service.script_reports(admin, version.public_id)
    assert set(result) == {"reports"} and len(result["reports"]) == 2
    first, second = result["reports"]
    assert all(set(item) == set(admin_script_views.REPORT_KEYS) for item in result["reports"])
    assert (first["reasons"], first["text"], first["closed_result"]) == ([REASON_PLAGIARISM, REASON_OTHER], TEXT, CLOSED_DISMISSED)
    assert (second["reasons"], second["text"], second["closed_at"], second["closed_result"]) == ([REASON_MINOR_SEXUAL], None, None, None)
    [audit] = kit.audit_rows()
    assert audit.action == "report.read" and audit.admin_display_name is not None
    assert audit.target_user_public_id == author.public_id
    script_id = kit.scripts.get_script_of(version).public_id
    assert audit.detail == {"version_id": version.public_id, "script_id": script_id, "count": 2}


def test_a_read_of_a_version_without_reports_is_still_audited_with_a_zero_count(kit, admin, author):
    version = live(kit, author)
    assert kit.service.script_reports(admin, version.public_id) == {"reports": []}
    [audit] = kit.audit_rows()
    assert (audit.action, audit.detail["count"]) == ("report.read", 0)


def test_the_response_has_no_reporter_identity_of_any_kind(kit, admin, author, reporters):
    version = live(kit, author)
    for reporter in reporters:
        report(kit, version, reporter, note=TEXT)
    text = repr(kit.service.script_reports(admin, version.public_id))
    for reporter in reporters:
        for secret in (reporter.email, reporter.public_id, reporter.display_name):
            assert secret not in text, secret
    assert text.count(TEXT) == len(reporters)                                  # 文字本身是有的（那是管理員該讀的）


def test_the_audit_is_written_before_the_reports_are_returned_and_a_failed_audit_returns_nothing(kit, admin, author, reporters, monkeypatch):
    version = live(kit, author)
    report(kit, version, reporters[0], note=TEXT)
    order = []
    real_add = kit.audit.add
    monkeypatch.setattr(kit.audit, "add", lambda **kw: (order.append("audit"), real_add(**kw))[1])
    result = kit.service.script_reports(admin, version.public_id)
    order.append("returned")
    assert order == ["audit", "returned"] and result["reports"][0]["text"] == TEXT

    def failing(**kwargs):
        raise RuntimeError("稽核寫入失敗")

    monkeypatch.setattr(kit.audit, "add", failing)
    with pytest.raises(RuntimeError):                                          # 例外往外拋，呼叫端拿不到任何檢舉內容
        kit.service.script_reports(admin, version.public_id)


@pytest.mark.parametrize("target", ["draft", "missing", "not-a-uuid", "", None, 5])
def test_a_draft_and_an_unknown_id_are_the_same_404_with_nothing_written(kit, admin, author, target):
    draft = put_in_status(kit.scripts, author, VERSION_DRAFT)
    kit.session.commit()
    pid = draft.public_id if target == "draft" else MISSING if target == "missing" else target
    with pytest.raises(NotFound) as read_error:
        kit.service.script_reports(admin, pid)
    with pytest.raises(NotFound) as dismiss_error:
        kit.service.dismiss_script_reports(admin, pid)
    assert read_error.value.code == dismiss_error.value.code == "not_found" and read_error.value.message == dismiss_error.value.message
    assert kit.audit_rows() == []


def test_a_script_whose_author_is_gone_has_a_null_audit_target_but_keeps_the_identifiers(kit, admin, author, reporters):
    version = live(kit, author)
    report(kit, version, reporters[0])
    kit.session.execute(
        update(Script).where(Script.id == kit.scripts.get_script_of(version).id)
        .values(creator_id=None, author_name=RETIRED_AUTHOR_NAME, author_code=None).execution_options(synchronize_session=False)
    )
    kit.session.commit()
    kit.service.script_reports(admin, version.public_id)
    kit.service.dismiss_script_reports(admin, version.public_id)
    read, dismiss = reversed(kit.audit_rows())
    assert (read.action, read.target_user_public_id) == ("report.read", None) and read.detail["version_id"] == version.public_id
    assert (dismiss.action, dismiss.target_user_public_id) == ("report.dismiss", None)


# ---- 結案為不成立 --------------------------------------------------------------------------------------


def test_dismissing_closes_every_open_report_keeps_the_version_live_and_needs_no_password(kit, admin, author, reporters):
    version = live(kit, author, title=TITLE)
    for reporter in reporters[:3]:
        report(kit, version, reporter, note=TEXT)
    result = kit.service.dismiss_script_reports(admin, version.public_id)
    assert result["version"]["status"] == VERSION_PUBLISHED and result["version"]["report_count"] == 0
    assert result["version"]["red_line_reported"] is False and set(result["version"]) == set(admin_script_views.LIST_KEYS)
    assert [closed for _, closed in states(kit, version)] == [CLOSED_DISMISSED] * 3
    assert all(closed_at == kit.clock() for closed_at, _ in states(kit, version))
    assert kit.scripts.get_published(version.public_id) is not None            # 公開目錄不變
    [audit] = kit.audit_rows()
    assert audit.action == "report.dismiss" and audit.target_user_public_id == author.public_id
    assert audit.detail == {"version_id": version.public_id, "script_id": kit.scripts.get_script_of(version).public_id, "count": 3}
    assert kit.persisted.calls == 0                                            # 沒有碰密碼確認（沒有 reauth.failed 也沒有保存失敗計數）


def test_dismissing_only_closes_the_open_ones_and_never_rewrites_a_closed_report(kit, admin, author, reporters):
    version = live(kit, author)
    report(kit, version, reporters[0], now=at(2))
    assert reports_of(kit).close_open(version.id, CLOSED_REMOVED, at(3)) == 1          # 一筆早已結案
    kit.session.commit()
    report(kit, version, reporters[1], now=at(4))
    kit.service.dismiss_script_reports(admin, version.public_id)
    assert states(kit, version) == [(at(3), CLOSED_REMOVED), (kit.clock(), CLOSED_DISMISSED)]
    assert kit.audit_rows()[0].detail["count"] == 1


def test_dismissing_with_no_open_report_is_a_409_with_nothing_written(kit, admin, author, reporters):
    quiet = live(kit, author, title="沒有檢舉")
    with pytest.raises(Conflict) as exc:
        kit.service.dismiss_script_reports(admin, quiet.public_id)
    assert exc.value.code == "no_open_reports" and kit.audit_rows() == []
    closed = live(kit, author, title="都已結案")
    report(kit, closed, reporters[0])
    kit.service.dismiss_script_reports(admin, closed.public_id)
    with pytest.raises(Conflict) as again:                                      # 第二位管理員搶在後面按「不成立」
        kit.service.dismiss_script_reports(admin, closed.public_id)
    assert again.value.code == "no_open_reports" and actions(kit) == ["report.dismiss"]


def test_a_new_report_after_dismissal_is_open_and_the_version_is_listed_again(kit, admin, author, reporters):
    version = live(kit, author, title="反覆被檢舉")
    report(kit, version, reporters[0])
    kit.service.dismiss_script_reports(admin, version.public_id)
    assert kit.service.script_queue(admin, status="reported") == []
    report(kit, version, reporters[1], now=at(9))
    assert [i["title"] for i in kit.service.script_queue(admin, status="reported")] == ["反覆被檢舉"]
    assert states(kit, version)[1] == (None, None)


def test_dismissal_logs_one_event_with_the_admin_and_the_count_only(kit, admin, author, reporters, caplog):
    caplog.set_level(logging.INFO, logger="app")
    version = live(kit, author, title=TITLE)
    report(kit, version, reporters[0], note=TEXT), report(kit, version, reporters[1], note=TEXT)
    kit.service.dismiss_script_reports(admin, version.public_id)
    [event] = [r for r in caplog.records if getattr(r, "event", None) == "script.reports_dismissed"]
    assert (event.user_id, event.count) == (admin.id, 2)
    assert not [r for r in caplog.records if getattr(r, "event", None) == "script.reports_auto_closed"]   # 不是系統自動結案


def test_a_failed_audit_rolls_the_dismissal_back_with_it(kit, admin, author, reporters, monkeypatch):
    version = live(kit, author)
    report(kit, version, reporters[0])

    def failing(**kwargs):
        raise RuntimeError("稽核寫入失敗")

    monkeypatch.setattr(kit.audit, "add", failing)
    with pytest.raises(RuntimeError):
        kit.service.dismiss_script_reports(admin, version.public_id)
    kit.session.rollback()                                                      # 請求失敗時，請求交易會整個回復
    assert states(kit, version) == [(None, None)]


# ---- 結案為下架（沿用管理員下架）--------------------------------------------------------------------------


def test_removing_with_the_password_closes_the_reports_as_removed_and_a_later_dismissal_is_a_409(kit, admin, author, reporters):
    version = live(kit, author)
    report(kit, version, reporters[0]), report(kit, version, reporters[1])
    kit.service.remove_script(admin, version.public_id, category="other_policy", note=NOTE, password=GOOD_PASSWORD)
    assert [closed for _, closed in states(kit, version)] == [CLOSED_REMOVED] * 2
    assert kit.service.script_queue(admin, status="reported") == []
    with pytest.raises(Conflict) as exc:
        kit.service.dismiss_script_reports(admin, version.public_id)
    assert exc.value.code == "no_open_reports"
    assert actions(kit) == ["script.remove"]                                    # 沒有 report.dismiss：不成立的結案沒有發生


def test_a_wrong_password_leaves_the_reports_open_and_there_is_no_password_free_takedown(kit, admin, author, reporters):
    version = live(kit, author)
    report(kit, version, reporters[0])
    with pytest.raises(ValidationError):
        kit.service.remove_script(admin, version.public_id, category="other_policy", password=WRONG_PASSWORD)
    assert states(kit, version) == [(None, None)] and kit.scripts.get_published(version.public_id) is not None
    public = {name for name in dir(kit.service) if not name.startswith("_") and "report" in name}
    assert public == {"script_reports", "dismiss_script_reports"}               # 沒有「一鍵下架並結案」之類的捷徑


def test_restoring_a_removed_version_does_not_reopen_its_reports(kit, admin, author, reporters):
    version = live(kit, author)
    report(kit, version, reporters[0])
    kit.service.remove_script(admin, version.public_id, category="other_policy", password=GOOD_PASSWORD)
    kit.service.restore_script(admin, version.public_id, password=GOOD_PASSWORD)
    assert [closed for _, closed in states(kit, version)] == [CLOSED_REMOVED]
    assert kit.service.script_queue(admin, status="reported") == []


# ---- 稽核、日誌與事件不含文字 -------------------------------------------------------------------------------


def test_the_audit_the_log_and_the_events_never_contain_the_note_the_reasons_the_reporter_or_the_title(kit, admin, author, reporters, caplog):
    caplog.set_level(logging.INFO, logger="app")
    version = live(kit, author, title=TITLE, summary="簡介文字不該出現")
    report(kit, version, reporters[0], reasons=(REASON_MINOR_SEXUAL, REASON_OTHER), note=TEXT)
    report(kit, version, reporters[1], reasons=(REASON_PLAGIARISM,), note=TEXT)
    kit.service.script_queue(admin, status="reported")
    kit.service.script_reports(admin, version.public_id)
    kit.service.dismiss_script_reports(admin, version.public_id)
    report(kit, version, reporters[2], note=TEXT, now=at(8))
    kit.service.remove_script(admin, version.public_id, category="other_policy", note=NOTE, password=GOOD_PASSWORD)
    forbidden = (TEXT, "alert(1)", TITLE, "簡介文字不該出現", NOTE, "靈氣復甦", "林曉", author.email, "筆名", REASON_MINOR_SEXUAL, REASON_PLAGIARISM)
    stored = repr(kit.audit_rows())
    logged = " ".join(f"{record.getMessage()} {sorted(record.__dict__.items(), key=str)}" for record in caplog.records)
    for reporter in reporters:
        forbidden += (reporter.email, reporter.public_id, reporter.display_name)
    for text in forbidden:
        assert text not in stored, text
        assert text not in logged, text
    assert actions(kit) == ["script.remove", "report.dismiss", "report.read"]


# ---- 並行 ----------------------------------------------------------------------------------------------


def test_two_dismissals_at_the_same_time_succeed_exactly_once(concurrent_engine):
    with Session(concurrent_engine) as s:
        kit = AdminKit(s)
        kit.admin()
        owner = kit.member(2)
        version = put_in_status(kit.scripts, owner, VERSION_PUBLISHED, now=at(1))
        for n in (11, 12):
            ScriptReportRepository(s).add(kit.member(n).id, version.id, [REASON_OTHER], False, None, now=at(2))
        s.commit()
        pid, version_id = version.public_id, version.id
        admin_id = s.scalar(select(User.id).where(User.role == "admin"))

    barrier = Barrier(2)

    def worker(_: int) -> str:
        with Session(concurrent_engine) as session:
            kit = AdminKit(session)
            admin = CurrentUser.from_model(session.get(User, admin_id))
            barrier.wait(timeout=30)
            try:
                kit.service.dismiss_script_reports(admin, pid)
            except Conflict as error:
                session.rollback()
                assert error.code == "no_open_reports"
                return "conflict"
            session.commit()
            return "closed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(worker, range(2)))
    assert outcomes == ["closed", "conflict"]
    with Session(concurrent_engine) as s:
        rows = s.execute(select(ScriptReport.closed_result).where(ScriptReport.version_id == version_id)).scalars().all()
        assert rows == [CLOSED_DISMISSED] * 2                                   # 每一筆只被結案一次、沒有被改寫
        kit = AdminKit(s)
        assert [row.action for row in kit.audit_rows()] == ["report.dismiss"]  # 輸的那一方沒有留下稽核
