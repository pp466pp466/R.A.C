"""data-retention 規格（rpg-script-reports 新增）：劇本檢舉在**結案**後 12 個月清除；未結案的不清。

- 365 天為期限（從結案時間起算；364 天保留、過 365 天刪除）；預覽只計數；分批；輸出與日誌只有類別與筆數，沒有理由與自寫文字。
- 未結案的檢舉不論建立多久都不清（與所屬版本同壽命）；結案的三種結果（下架、不成立、被取代）一視同仁。
- 只刪檢舉本身：被檢舉的版本、檢舉人與作者的帳號都不受影響；一類失敗不阻止其餘類別。

以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行。
"""
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import ScriptReport, ScriptVersion, User
from app.models.script_report import CLOSED_DISMISSED, CLOSED_REMOVED, CLOSED_RESULTS, CLOSED_SUPERSEDED
from app.repositories.retention_repository import RetentionRepository
from app.repositories.script_repository import ScriptRepository
from app.services.data_retention_service import SCRIPT_REPORT_RETENTION, DataRetentionService
from tests.support.auth import START, FakeClock, add_user
from tests.support.script_data import validated

SECOND = timedelta(seconds=1)
DAY = timedelta(days=1)
SECRET_NOTE = "檢舉的自寫文字SCAN-RETENTION-NOTE"


def service(session, **kwargs) -> DataRetentionService:
    return DataRetentionService(
        repository=kwargs.pop("repository", RetentionRepository(session)), clock=FakeClock(), commit=session.commit,
        rollback=session.rollback, **kwargs,
    )


def counts(report) -> dict[str, int]:
    return {r.category: r.matched for r in report.results}


def make_versions(session, count: int) -> list[ScriptVersion]:
    """作者的 `count` 個劇本各一個版本（一位檢舉人對每個版本至多一筆，所以多筆檢舉要多個版本）。"""
    author = add_user(session, 90)
    repo = ScriptRepository(session)
    versions = []
    for n in range(count):
        result = validated(title=f"劇本{n}")
        versions.append(repo.create_script(
            author.id, author_name="筆名", author_code="a1b2c3", content=result.content, content_hash=result.content_hash, total_chars=result.total_chars,
        ))
    return versions


def report(session, reporter_id, version, *, closed_at=None, result=CLOSED_REMOVED, created_at=None, note=SECRET_NOTE) -> ScriptReport:
    row = ScriptReport(
        reporter_id=reporter_id, version_id=version.id, reasons_json=["other"], red_line=False, note=note,
        closed_at=closed_at, closed_result=None if closed_at is None else result,
    )
    if created_at is not None:
        row.created_at = created_at
        row.updated_at = created_at
    session.add(row)
    session.flush()
    return row


def remaining(session) -> set[int]:
    session.expire_all()
    return set(session.scalars(select(ScriptReport.id)))


def test_the_period_is_365_days_counted_from_the_closing():
    assert SCRIPT_REPORT_RETENTION == timedelta(days=365)


def test_a_report_closed_more_than_365_days_ago_is_deleted(member_session):
    s = member_session
    reporter = add_user(s, 1)
    (version,) = make_versions(s, 1)
    report(s, reporter.id, version, closed_at=START - SCRIPT_REPORT_RETENTION - SECOND)
    s.commit()
    assert counts(service(s).run())["script_reports"] == 1 and remaining(s) == set()


def test_the_boundary_a_report_closed_exactly_365_days_ago_or_364_days_ago_is_kept(member_session):
    s = member_session
    reporter = add_user(s, 1)
    first, second = make_versions(s, 2)
    on_the_line = report(s, reporter.id, first, closed_at=START - SCRIPT_REPORT_RETENTION)
    inside = report(s, reporter.id, second, closed_at=START - SCRIPT_REPORT_RETENTION + DAY)
    s.commit()
    assert counts(service(s).run())["script_reports"] == 0
    assert remaining(s) == {on_the_line.id, inside.id}


def test_an_open_report_is_never_purged_however_old_it_is(member_session):
    """未結案的檢舉與所屬版本同壽命：建立再久都不清（只有結案才開始計算 12 個月）。"""
    s = member_session
    reporter = add_user(s, 1)
    (version,) = make_versions(s, 1)
    ancient = report(s, reporter.id, version, closed_at=None, created_at=START - 5 * SCRIPT_REPORT_RETENTION)
    s.commit()
    result = service(s).run()
    assert counts(result)["script_reports"] == 0 and remaining(s) == {ancient.id}
    assert counts(service(s).run(dry_run=True))["script_reports"] == 0              # 預覽也不把它算進去


@pytest.mark.parametrize("closed_result", CLOSED_RESULTS)
def test_every_closing_result_is_treated_alike(member_session, closed_result):
    s = member_session
    reporter = add_user(s, 1)
    (version,) = make_versions(s, 1)
    report(s, reporter.id, version, closed_at=START - SCRIPT_REPORT_RETENTION - DAY, result=closed_result)
    s.commit()
    assert counts(service(s).run())["script_reports"] == 1


def test_the_age_of_the_closing_not_the_creation_decides(member_session):
    """很久以前建立、但最近才結案的檢舉保留；最近建立、卻（不可能但資料上存在地）早早結案的依結案時間算。"""
    s = member_session
    reporter = add_user(s, 1)
    old_recent_close, new_old_close = make_versions(s, 2)
    kept = report(s, reporter.id, old_recent_close, created_at=START - 3 * SCRIPT_REPORT_RETENTION, closed_at=START - DAY)
    gone = report(s, reporter.id, new_old_close, created_at=START - DAY, closed_at=START - SCRIPT_REPORT_RETENTION - DAY)
    s.commit()
    gone_id = gone.id
    assert counts(service(s).run())["script_reports"] == 1
    assert remaining(s) == {kept.id} and gone_id not in remaining(s)


def test_the_preview_counts_without_deleting(member_session):
    s = member_session
    reporter = add_user(s, 1)
    first, second, third = make_versions(s, 3)
    report(s, reporter.id, first, closed_at=START - SCRIPT_REPORT_RETENTION - DAY)
    report(s, reporter.id, second, closed_at=START - SCRIPT_REPORT_RETENTION - 2 * DAY, result=CLOSED_DISMISSED)
    report(s, reporter.id, third, closed_at=START - DAY, result=CLOSED_SUPERSEDED)
    s.commit()
    result = service(s).run(dry_run=True)
    assert result.dry_run and counts(result)["script_reports"] == 2
    assert len(remaining(s)) == 3


def test_many_reports_are_purged_in_batches_and_a_second_run_finds_nothing(member_session):
    s = member_session
    reporter = add_user(s, 1)
    versions = make_versions(s, 8)
    for n, version in enumerate(versions[:7]):
        report(s, reporter.id, version, closed_at=START - SCRIPT_REPORT_RETENTION - (n + 1) * DAY)
    keep = report(s, reporter.id, versions[7], closed_at=START - DAY)
    s.commit()
    assert counts(service(s, batch_size=3).run())["script_reports"] == 7
    assert remaining(s) == {keep.id}
    assert counts(service(s).run())["script_reports"] == 0


def test_only_the_reports_that_are_past_their_period_go_across_reporters(member_session):
    s = member_session
    first, second = add_user(s, 1), add_user(s, 2)
    version_a, version_b = make_versions(s, 2)
    gone = report(s, first.id, version_a, closed_at=START - SCRIPT_REPORT_RETENTION - DAY)
    kept_a = report(s, second.id, version_a, closed_at=START - DAY)
    kept_b = report(s, first.id, version_b)
    s.commit()
    gone_id, kept_ids = gone.id, {kept_a.id, kept_b.id}
    service(s).run()
    assert remaining(s) == kept_ids and gone_id not in remaining(s)


def test_the_versions_and_the_accounts_are_untouched(member_session):
    s = member_session
    reporter = add_user(s, 1)
    (version,) = make_versions(s, 1)
    version_id = version.id
    report(s, reporter.id, version, closed_at=START - SCRIPT_REPORT_RETENTION - DAY)
    s.commit()
    service(s).run()
    s.expire_all()
    assert s.get(ScriptVersion, version_id) is not None
    assert s.scalar(select(func.count(User.id))) == 2                              # 檢舉人與作者
    assert s.scalar(select(func.count(ScriptReport.id))) == 0


def test_the_purge_itself_refuses_to_delete_an_open_report_even_if_given_its_id(member_session):
    """防禦：就算傳入未結案檢舉的編號，刪除語句本身的條件也不會刪它。"""
    s = member_session
    reporter = add_user(s, 1)
    (version,) = make_versions(s, 1)
    open_report = report(s, reporter.id, version)
    s.commit()
    assert RetentionRepository(s).purge_script_reports([open_report.id]) == 0
    assert remaining(s) == {open_report.id}


def test_a_failing_report_purge_does_not_stop_the_other_categories(member_session, log_lines):
    s = member_session
    reporter = add_user(s, 1)
    (version,) = make_versions(s, 1)
    report(s, reporter.id, version, closed_at=START - SCRIPT_REPORT_RETENTION - DAY)
    s.commit()

    class ReportsBreak(RetentionRepository):
        def purge_script_reports(self, ids):
            raise RuntimeError("資料庫暫時失敗：含機密文字 sekret-detail")

    result = service(s, repository=ReportsBreak(s)).run()
    assert result.failed == ["script_reports"] and counts(result)["script_reports"] == 0
    assert [line["reason"] for line in log_lines() if line.get("event") == "retention.purge_failed"] == ["script_reports"]
    assert "sekret-detail" not in log_lines.raw()
    assert len(remaining(s)) == 1                                                 # 失敗的那一批回復，檢舉還在


def test_the_event_carries_only_the_category_and_the_count(member_session, log_lines):
    s = member_session
    reporter = add_user(s, 1)
    (version,) = make_versions(s, 1)
    report(s, reporter.id, version, closed_at=START - SCRIPT_REPORT_RETENTION - DAY)
    s.commit()
    service(s).run()
    purged = [(line["reason"], line["count"]) for line in log_lines() if line.get("event") == "retention.purged" and line["reason"] == "script_reports"]
    assert purged == [("script_reports", 1)]
    assert SECRET_NOTE not in log_lines.raw()
