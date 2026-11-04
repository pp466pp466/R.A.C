"""data-retention 規格（rpg-script-authoring 新增）：劇本送審紀錄在建立後 12 個月清除。

- 365 天為期限（364 天保留、過 365 天刪除）；預覽只計數；分批；輸出與日誌只有類別與筆數，沒有任何紀錄內容。
- 只刪紀錄本身：帳號的暫停發佈旗標不受影響，其他類別的資料不受影響。

以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行。
"""
from datetime import timedelta

from sqlalchemy import func, select

from app.models import ScriptSubmission, User
from app.repositories.retention_repository import RetentionRepository
from app.services.data_retention_service import SCRIPT_SUBMISSION_RETENTION, DataRetentionService
from tests.support.auth import START, FakeClock, add_user

SECOND = timedelta(seconds=1)
DAY = timedelta(days=1)


def service(session, **kwargs) -> DataRetentionService:
    return DataRetentionService(
        repository=RetentionRepository(session), clock=FakeClock(), commit=session.commit, rollback=session.rollback, **kwargs,
    )


def counts(report) -> dict[str, int]:
    return {r.category: r.matched for r in report.results}


def record(session, user_id, created_at, *, outcome="auto_rejected", categories=("minor_sexual",)) -> ScriptSubmission:
    row = ScriptSubmission(user_id=user_id, outcome=outcome, categories_json=list(categories) or None, created_at=created_at)
    session.add(row)
    session.flush()
    return row


def remaining(session) -> set[int]:
    session.expire_all()
    return set(session.scalars(select(ScriptSubmission.id)))


def test_the_period_is_365_days():
    assert SCRIPT_SUBMISSION_RETENTION == timedelta(days=365)


def test_a_record_older_than_365_days_is_deleted(member_session):
    s = member_session
    user = add_user(s, 1)
    record(s, user.id, START - SCRIPT_SUBMISSION_RETENTION - SECOND)
    s.commit()
    assert counts(service(s).run())["script_submissions"] == 1 and remaining(s) == set()


def test_a_record_exactly_365_days_old_or_364_days_old_is_kept(member_session):
    s = member_session
    user = add_user(s, 1)
    on_the_line = record(s, user.id, START - SCRIPT_SUBMISSION_RETENTION)
    inside = record(s, user.id, START - SCRIPT_SUBMISSION_RETENTION + DAY)
    s.commit()
    assert counts(service(s).run())["script_submissions"] == 0
    assert remaining(s) == {on_the_line.id, inside.id}


def test_the_preview_counts_without_deleting(member_session):
    s = member_session
    user = add_user(s, 1)
    record(s, user.id, START - SCRIPT_SUBMISSION_RETENTION - DAY)
    record(s, user.id, START - SCRIPT_SUBMISSION_RETENTION - 2 * DAY, outcome="sent", categories=())
    s.commit()
    report = service(s).run(dry_run=True)
    assert report.dry_run and counts(report)["script_submissions"] == 2
    assert len(remaining(s)) == 2


def test_many_records_are_purged_in_batches_and_a_second_run_finds_nothing(member_session):
    s = member_session
    user = add_user(s, 1)
    for n in range(7):
        record(s, user.id, START - SCRIPT_SUBMISSION_RETENTION - (n + 1) * DAY)
    keep = record(s, user.id, START - DAY)
    s.commit()
    assert counts(service(s, batch_size=3).run())["script_submissions"] == 7
    assert remaining(s) == {keep.id}
    assert counts(service(s).run())["script_submissions"] == 0


def test_only_the_records_of_the_expired_age_are_deleted_across_accounts(member_session):
    s = member_session
    first, second = add_user(s, 1), add_user(s, 2)
    gone = record(s, first.id, START - SCRIPT_SUBMISSION_RETENTION - DAY)
    kept_a = record(s, first.id, START - DAY)
    kept_b = record(s, second.id, START - 10 * DAY)
    s.commit()
    gone_id, kept_ids = gone.id, {kept_a.id, kept_b.id}
    service(s).run()
    assert remaining(s) == kept_ids and gone_id not in remaining(s)


def test_the_suspension_flag_and_the_account_are_untouched(member_session):
    s = member_session
    user = add_user(s, 1, publish_suspended_at=START - 400 * DAY)
    for _ in range(3):
        record(s, user.id, START - SCRIPT_SUBMISSION_RETENTION - DAY)
    s.commit()
    service(s).run()
    s.expire_all()
    row = s.get(User, user.id)
    assert row is not None and row.publish_suspended_at is not None
    assert s.scalar(select(func.count(ScriptSubmission.id))) == 0


def test_the_event_and_the_output_carry_only_the_category_and_the_count(member_session, log_lines):
    s = member_session
    user = add_user(s, 1)
    record(s, user.id, START - SCRIPT_SUBMISSION_RETENTION - DAY, categories=("non_consensual",))
    s.commit()
    service(s).run()
    purged = [(line["reason"], line["count"]) for line in log_lines() if line.get("event") == "retention.purged" and line["reason"] == "script_submissions"]
    assert purged == [("script_submissions", 1)]
    assert "non_consensual" not in log_lines.raw() and "auto_rejected" not in log_lines.raw()
