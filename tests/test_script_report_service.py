"""script-reports 規格（rpg-script-reports 的 2.2）：`ScriptReportService.submit` 的檢查順序、驗證、重複、每日上限與寫入的內容。

一般測試以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行；並行測試用 `concurrent_engine`，
以真實的執行緒與獨立連線證明「兩個相同的請求只有一個成功」。HTTP 層（狀態碼、CSRF、回應形狀）在 `test_session_api_report.py`。
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import PROJECT_ROOT
from app.models import RuntimeLimit, ScriptReport
from app.models.script import (
    VERSION_DRAFT, VERSION_PENDING, VERSION_PUBLISHED, VERSION_REJECTED, VERSION_REMOVED, VERSION_SUPERSEDED,
)
from app.models.script_report import CLOSED_DISMISSED, REPORT_NOTE_MAX_CHARS, REPORT_REASONS
from app.models.user import ROLE_ADMIN, STATUS_DELETION_PENDING, STATUS_SUSPENDED
from app.repositories.runtime_limit_repository import RuntimeLimitRepository
from app.repositories.script_report_repository import ScriptReportRepository
from app.repositories.script_repository import ScriptRepository
from app.services.access_policy import AccessPolicy
from app.services.current_user import CurrentUser
from app.services.errors import (
    AgeConfirmationRequired, AlreadyReported, Forbidden, NotFound, ReportLimit, ReportOwnScript, ValidationError,
)
from app.services.runtime_limits import RuntimeLimits
from app.services.script_report_service import (
    REASONS_ERROR, TEXT_TOO_LONG_ERROR, TEXT_TYPE_ERROR, ScriptReportService, check_report_body,
)
from tests.support.auth import START, FakeClock, add_user
from tests.support.script_data import validated
from tests.support.script_review import make_author, new_draft, put_in_status, script_public_id

GHOST = "00000000-0000-4000-8000-000000000000"
SPECIAL_TEXT = "檢舉文字：<script>alert(1)</script> 'x'; DROP TABLE users -- 😀"


def service_for(session: Session, clock: FakeClock | None = None) -> ScriptReportService:
    clock = clock or FakeClock()
    return ScriptReportService(
        scripts=ScriptRepository(session), reports=ScriptReportRepository(session), policy=AccessPolicy(clock=clock),
        limits=RuntimeLimits(limits=RuntimeLimitRepository(session), clock=clock), clock=clock,
    )


def player(session: Session, n: int = 1, **overrides) -> CurrentUser:
    return CurrentUser.from_model(add_user(session, n, **overrides))


def published(session: Session, author=None, **changes):
    """作者的一個新劇本（第 1 版）上架；回傳版本。"""
    author = author or make_author(session, 9)
    return put_in_status(ScriptRepository(session), author, VERSION_PUBLISHED, **changes)


def reports(session: Session) -> list[ScriptReport]:
    session.expire_all()
    return list(session.scalars(select(ScriptReport).order_by(ScriptReport.id)))


def set_limit(session: Session, value: int) -> None:
    session.add(RuntimeLimit(key="script_report_daily", int_value=value, updated_at=START))
    session.flush()


# ---- 成功 ---------------------------------------------------------------------------------------


def test_a_report_is_stored_unclosed_and_the_version_stays_published(member_session):
    version, reporter = published(member_session), player(member_session)
    assert service_for(member_session).submit(reporter, version.public_id, ["plagiarism"], "和某作品很像") is None
    [report] = reports(member_session)
    assert (report.reporter_id, report.version_id) == (reporter.id, version.id)
    assert report.reasons_json == ["plagiarism"] and report.note == "和某作品很像" and report.red_line is False
    assert report.closed_at is None and report.closed_result is None
    assert report.created_at == START
    member_session.refresh(version)
    assert version.status == VERSION_PUBLISHED                                         # 檢舉不下架、不改狀態


def test_many_reports_never_unpublish_the_version(member_session):
    version = published(member_session)
    set_limit(member_session, 100)
    for n in range(1, 9):
        service_for(member_session).submit(player(member_session, n), version.public_id, ["other"], None)
    assert len(reports(member_session)) == 8
    member_session.refresh(version)
    assert version.status == VERSION_PUBLISHED


def test_several_reasons_are_stored_in_the_canonical_order_regardless_of_the_order_given(member_session):
    version, reporter = published(member_session), player(member_session)
    service_for(member_session).submit(reporter, version.public_id, ["other", "plagiarism", "minor_sexual"], None)
    [report] = reports(member_session)
    assert report.reasons_json == ["minor_sexual", "plagiarism", "other"]


@pytest.mark.parametrize(
    ("reasons", "red_line"),
    [
        (["minor_sexual"], True), (["sexual_violence"], True), (["real_person_sexual"], True),
        (["tier_mismatch"], False), (["plagiarism"], False), (["privacy_harassment"], False), (["other"], False),
        (["plagiarism", "real_person_sexual"], True), (["other", "tier_mismatch", "privacy_harassment"], False),
        (list(REPORT_REASONS), True),
    ],
)
def test_the_red_line_flag_is_true_exactly_when_one_of_the_three_red_line_reasons_is_chosen(member_session, reasons, red_line):
    version, reporter = published(member_session), player(member_session)
    service_for(member_session).submit(reporter, version.public_id, reasons, None)
    assert reports(member_session)[0].red_line is red_line


def test_an_admin_can_report_like_any_other_confirmed_member(member_session):
    """管理員也可檢舉（檢舉只升高佇列的優先度，不是管理動作）。"""
    version = published(member_session)
    admin = player(member_session, 2, role=ROLE_ADMIN)
    service_for(member_session).submit(admin, version.public_id, ["other"], None)
    assert len(reports(member_session)) == 1


def test_a_member_whose_publishing_is_suspended_can_still_report(member_session):
    version = published(member_session)
    suspended = player(member_session, 2, publish_suspended_at=START)
    service_for(member_session).submit(suspended, version.public_id, ["other"], None)
    assert len(reports(member_session)) == 1


@pytest.mark.parametrize("tier", ["free", "plus", "vip"])
def test_the_member_tier_changes_nothing(member_session, tier):
    version = published(member_session)
    service_for(member_session).submit(player(member_session, 2, member_tier=tier), version.public_id, ["other"], None)
    assert len(reports(member_session)) == 1


def test_a_retained_anonymous_script_can_be_reported(member_session):
    """匿名保留的玩家劇本沒有作者關聯，但它不是內建劇本（沒有種子鍵）、也不是檢舉人自己的：檢舉成功。"""
    author = make_author(member_session, 9)
    version = published(member_session, author)
    result = ScriptRepository(member_session).retain_published_anonymously(author.id)
    assert (result.kept, result.deleted) == (1, 0)
    member_session.expire_all()
    script = ScriptRepository(member_session).get_script_of(version)
    assert script.creator_id is None and script.seed_key is None
    service_for(member_session).submit(player(member_session, 2), version.public_id, ["other"], None)
    assert len(reports(member_session)) == 1


# ---- 目標版本：只有上架中的非內建版本；404 與 403 早於本文驗證 -----------------------------------------


@pytest.mark.parametrize(
    "status", [VERSION_DRAFT, VERSION_PENDING, VERSION_REJECTED, VERSION_SUPERSEDED, VERSION_REMOVED],
)
def test_a_version_that_is_not_published_is_not_found_and_nothing_is_written(member_session, status):
    version = put_in_status(ScriptRepository(member_session), make_author(member_session, 9), status)
    with pytest.raises(NotFound):
        service_for(member_session).submit(player(member_session), version.public_id, ["other"], None)
    assert reports(member_session) == []


@pytest.mark.parametrize("version_id", [GHOST, "not-a-uuid", "", " ", "'; DROP TABLE script_versions; --", "x" * 500, None, 123, ["a"]])
def test_a_missing_malformed_or_non_string_id_is_not_found(member_session, version_id):
    with pytest.raises(NotFound):
        service_for(member_session).submit(player(member_session), version_id, ["other"], None)
    assert reports(member_session) == []


def test_a_builtin_script_is_not_found_whatever_the_author_field_says(member_session):
    """判斷內建劇本依種子鍵：內建劇本與不存在的版本得到逐字相同的 404（不洩漏它存在）。"""
    result = validated()
    version, _ = ScriptRepository(member_session).publish_builtin(
        "seed-report", content=result.content, content_hash=result.content_hash, total_chars=result.total_chars, now=START,
    )
    reporter = player(member_session)
    with pytest.raises(NotFound) as builtin:
        service_for(member_session).submit(reporter, version.public_id, ["other"], None)
    with pytest.raises(NotFound) as ghost:
        service_for(member_session).submit(reporter, GHOST, ["other"], None)
    assert (builtin.value.code, builtin.value.message) == (ghost.value.code, ghost.value.message)
    assert reports(member_session) == []


def test_the_authors_own_published_version_is_forbidden_with_a_dedicated_code(member_session):
    author_row = add_user(member_session, 1)
    version = published(member_session, author_row)
    with pytest.raises(ReportOwnScript) as denied:
        service_for(member_session).submit(CurrentUser.from_model(author_row), version.public_id, ["other"], None)
    assert isinstance(denied.value, Forbidden) and denied.value.code == "report_own_script"
    assert reports(member_session) == []


def test_not_found_and_own_script_come_before_the_body_is_validated(member_session):
    """無效的識別碼不會洩漏欄位規則：404 與 403 早於 `reasons`、`text` 的驗證。"""
    author_row = add_user(member_session, 1)
    mine = published(member_session, author_row)
    reporter = player(member_session, 2)
    with pytest.raises(NotFound):
        service_for(member_session).submit(reporter, GHOST, [], "x" * 999)
    with pytest.raises(ReportOwnScript):
        service_for(member_session).submit(CurrentUser.from_model(author_row), mine.public_id, [], "x" * 999)
    assert reports(member_session) == []


# ---- 帳號與年齡閘門 -----------------------------------------------------------------------------


def test_a_member_without_the_age_check_is_refused_before_anything_else(member_session):
    """年齡確認的閘門是第一關：連目標不存在也先回 `age_confirmation_required`（不洩漏任何資訊給還不能用的帳號）。"""
    version = published(member_session)
    unconfirmed = player(member_session, 2, age=None)
    for target in (version.public_id, GHOST):
        with pytest.raises(AgeConfirmationRequired) as denied:
            service_for(member_session).submit(unconfirmed, target, [], None)
        assert denied.value.code == "age_confirmation_required"
    assert reports(member_session) == []


def test_an_admin_without_the_age_check_is_refused_too(member_session):
    version = published(member_session)
    with pytest.raises(AgeConfirmationRequired):
        service_for(member_session).submit(player(member_session, 2, age=None, role=ROLE_ADMIN), version.public_id, ["other"], None)
    assert reports(member_session) == []


@pytest.mark.parametrize("status", [STATUS_SUSPENDED, STATUS_DELETION_PENDING])
def test_an_account_that_is_not_active_cannot_report(member_session, status):
    version = published(member_session)
    with pytest.raises(Forbidden):
        service_for(member_session).submit(player(member_session, 2, status=status), version.public_id, ["other"], None)
    assert reports(member_session) == []


# ---- 本文驗證 -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reasons",
    [
        [], None, "other", {"other": True}, 5, ["spam"], ["OTHER"], ["other", "spam"], ["other", "other"], [None], [1], [["other"]],
        [{"a": 1}], list(REPORT_REASONS) + ["other"], ("other",),
    ],
)
def test_invalid_reasons_are_rejected_with_the_reasons_field_and_nothing_is_written(member_session, reasons):
    version, reporter = published(member_session), player(member_session)
    with pytest.raises(ValidationError) as caught:
        service_for(member_session).submit(reporter, version.public_id, reasons, None)
    assert set(caught.value.fields) == {"reasons"} and caught.value.fields["reasons"] == REASONS_ERROR
    assert reports(member_session) == []


def test_a_duplicate_reason_is_rejected_not_silently_deduplicated():
    with pytest.raises(ValidationError):
        check_report_body(["other", "other"], None)
    assert check_report_body(["other"], None) == (["other"], None)


@pytest.mark.parametrize("count", range(1, len(REPORT_REASONS) + 1))
def test_one_to_seven_distinct_reasons_are_accepted(count):
    reasons, _ = check_report_body(list(REPORT_REASONS[:count]), None)
    assert reasons == list(REPORT_REASONS[:count])


def test_exactly_200_characters_are_accepted_and_201_are_rejected(member_session):
    version, reporter = published(member_session), player(member_session)
    service_for(member_session).submit(reporter, version.public_id, ["other"], "字" * REPORT_NOTE_MAX_CHARS)
    assert reports(member_session)[0].note == "字" * REPORT_NOTE_MAX_CHARS
    other = player(member_session, 2)
    with pytest.raises(ValidationError) as caught:
        service_for(member_session).submit(other, version.public_id, ["other"], "字" * (REPORT_NOTE_MAX_CHARS + 1))
    assert set(caught.value.fields) == {"text"} and caught.value.fields["text"] == TEXT_TOO_LONG_ERROR
    assert len(reports(member_session)) == 1


def test_the_length_counts_unicode_characters_not_bytes_or_utf16_units():
    assert check_report_body(["other"], "😀" * REPORT_NOTE_MAX_CHARS)[1] == "😀" * REPORT_NOTE_MAX_CHARS      # 200 個字元（UTF-16 是 400 個單位、UTF-8 是 800 位元組）
    with pytest.raises(ValidationError):
        check_report_body(["other"], "😀" * (REPORT_NOTE_MAX_CHARS + 1))


def test_the_text_is_normalised_to_nfc_before_counting():
    decomposed = "é" * 150                      # 每個 e 加組合重音符號：NFC 之後是 150 個字元（未正規化是 300 個）
    cleaned = check_report_body(["other"], decomposed)[1]
    assert cleaned == "é" * 150 and len(cleaned) == 150


def test_control_characters_and_surrounding_whitespace_are_removed():
    assert check_report_body(["other"], "  \x00你\x07好\x1b\n\t世界  ")[1] == "你好世界"


@pytest.mark.parametrize("text", [None, "", "   ", "\x00\x01\n\t", "　"])
def test_a_missing_empty_or_blank_text_means_no_text(text):
    assert check_report_body(["other"], text)[1] is None


@pytest.mark.parametrize("text", [5, True, ["a"], {"a": 1}, b"abc"])
def test_a_text_that_is_not_a_string_is_rejected(text):
    with pytest.raises(ValidationError) as caught:
        check_report_body(["other"], text)
    assert caught.value.fields == {"text": TEXT_TYPE_ERROR}


def test_a_lone_surrogate_cannot_be_stored_and_is_rejected():
    with pytest.raises(ValidationError) as caught:
        check_report_body(["other"], "壞\ud800字")
    assert set(caught.value.fields) == {"text"}


def test_every_invalid_field_is_reported_at_once_without_echoing_what_was_typed():
    with pytest.raises(ValidationError) as caught:
        check_report_body(["spam-secret"], "字" * 999)
    assert set(caught.value.fields) == {"reasons", "text"}
    assert "spam-secret" not in "".join(caught.value.fields.values()) and "字字字" not in "".join(caught.value.fields.values())


def test_the_text_is_stored_as_plain_text_without_interpreting_anything(member_session):
    version, reporter = published(member_session), player(member_session)
    service_for(member_session).submit(reporter, version.public_id, ["other"], SPECIAL_TEXT)
    assert reports(member_session)[0].note == SPECIAL_TEXT                             # 原樣保存（參數化寫入；標記與 SQL 片段都只是文字）


# ---- 重複 ---------------------------------------------------------------------------------------


def test_the_same_member_cannot_report_the_same_version_twice(member_session):
    version, reporter = published(member_session), player(member_session)
    service_for(member_session).submit(reporter, version.public_id, ["other"], "第一次")
    with pytest.raises(AlreadyReported) as caught:
        service_for(member_session).submit(reporter, version.public_id, ["plagiarism"], "第二次")
    assert isinstance(caught.value, AlreadyReported) and caught.value.code == "already_reported"
    [report] = reports(member_session)
    assert report.note == "第一次" and report.reasons_json == ["other"]               # 仍是第一筆，沒有被改寫


def test_a_closed_report_still_blocks_a_second_report_on_the_same_version(member_session):
    version, reporter = published(member_session), player(member_session)
    service_for(member_session).submit(reporter, version.public_id, ["other"], None)
    ScriptReportRepository(member_session).close_open(version.id, CLOSED_DISMISSED, START)
    with pytest.raises(AlreadyReported):
        service_for(member_session).submit(reporter, version.public_id, ["other"], None)
    assert len(reports(member_session)) == 1


def test_a_superseded_old_version_is_no_longer_reportable(member_session):
    """新版本核可上架之後，被取代的舊版本不再上架：檢舉它得到 404（要檢舉的是現在上架的新版本）。"""
    first = put_in_status(ScriptRepository(member_session), make_author(member_session, 9), VERSION_SUPERSEDED)
    with pytest.raises(NotFound):
        service_for(member_session).submit(player(member_session, 2), first.public_id, ["other"], None)
    assert reports(member_session) == []


def test_a_member_who_reported_the_old_version_can_report_the_new_one(member_session):
    """唯一性按版本：檢舉過第 1 版的玩家，第 2 版核可上架後可以再檢舉第 2 版（檢舉不跟著劇本走）。"""
    author = make_author(member_session, 9)
    repo = ScriptRepository(member_session)
    first = put_in_status(repo, author, VERSION_PUBLISHED)
    reporter = player(member_session, 2)
    service_for(member_session).submit(reporter, first.public_id, ["other"], "舊版")
    second = new_draft(repo, author, script_public_id(repo, first))
    repo.submit_for_review(author.id, second.public_id, now=START)
    repo.approve_version(second.public_id, now=START + timedelta(minutes=1))
    member_session.expire_all()
    service_for(member_session).submit(reporter, second.public_id, ["other"], "新版")
    assert sorted(report.note for report in reports(member_session)) == ["新版", "舊版"]


# ---- 每日上限 -----------------------------------------------------------------------------------


def make_versions(session: Session, count: int):
    author = make_author(session, 9)
    return [published(session, author, title=f"劇本{index}") for index in range(count)]


def test_the_default_limit_is_five_per_taipei_day_and_the_sixth_is_a_429_with_retry_after(member_session):
    versions, reporter = make_versions(member_session, 6), player(member_session)
    service = service_for(member_session)
    for version in versions[:5]:
        service.submit(reporter, version.public_id, ["other"], None)
    with pytest.raises(ReportLimit) as caught:
        service.submit(reporter, versions[5].public_id, ["other"], None)
    assert caught.value.code == "report_limit"
    assert caught.value.retry_after == 4 * 3600                                         # START 是台灣時間 20:00，距明日 00:00 四小時
    assert len(reports(member_session)) == 5


def test_a_duplicate_does_not_consume_the_quota_and_is_answered_before_the_limit(member_session):
    versions, reporter = make_versions(member_session, 6), player(member_session)
    service = service_for(member_session)
    for version in versions[:5]:
        service.submit(reporter, version.public_id, ["other"], None)
    with pytest.raises(AlreadyReported):                                               # 已達上限，但這是重複：回 409，不是 429
        service.submit(reporter, versions[0].public_id, ["other"], None)
    assert len(reports(member_session)) == 5


def test_a_rejected_request_does_not_consume_the_quota(member_session):
    versions, reporter = make_versions(member_session, 6), player(member_session)
    service = service_for(member_session)
    for _ in range(10):
        with pytest.raises(ValidationError):
            service.submit(reporter, versions[0].public_id, [], None)
        with pytest.raises(NotFound):
            service.submit(reporter, GHOST, ["other"], None)
    for version in versions[:5]:
        service.submit(reporter, version.public_id, ["other"], None)
    assert len(reports(member_session)) == 5


def test_the_limit_is_per_member(member_session):
    versions = make_versions(member_session, 6)
    first, second = player(member_session, 1), player(member_session, 2)
    service = service_for(member_session)
    for version in versions[:5]:
        service.submit(first, version.public_id, ["other"], None)
    service.submit(second, versions[5].public_id, ["other"], None)                    # 另一位玩家不受影響
    assert len(reports(member_session)) == 6


def test_the_day_resets_at_midnight_taipei_time(member_session):
    """台灣 00:00 是前一天的 16:00 UTC：差一秒還是同一天，到點就重置。"""
    clock = FakeClock(datetime(2026, 10, 27, 15, 59, 58, tzinfo=timezone.utc))
    versions, reporter = make_versions(member_session, 7), player(member_session)
    service = service_for(member_session, clock)
    for version in versions[:5]:
        service.submit(reporter, version.public_id, ["other"], None)
    with pytest.raises(ReportLimit) as caught:
        service.submit(reporter, versions[5].public_id, ["other"], None)
    assert caught.value.retry_after == 2
    clock.advance(seconds=1)
    with pytest.raises(ReportLimit) as caught:
        service.submit(reporter, versions[5].public_id, ["other"], None)
    assert caught.value.retry_after == 1                                              # 最少 1 秒
    clock.advance(seconds=1)                                                          # 台灣 00:00:00
    service.submit(reporter, versions[5].public_id, ["other"], None)
    assert len(reports(member_session)) == 6


def test_yesterdays_reports_do_not_count_today(member_session):
    clock = FakeClock(datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc))              # 台灣 23:00
    versions, reporter = make_versions(member_session, 10), player(member_session)
    service = service_for(member_session, clock)
    for version in versions[:5]:
        service.submit(reporter, version.public_id, ["other"], None)
    clock.advance(hours=2)                                                            # 台灣隔天 01:00
    for version in versions[5:10]:
        service.submit(reporter, version.public_id, ["other"], None)
    assert len(reports(member_session)) == 10


def test_closed_reports_still_count_toward_todays_limit(member_session):
    versions, reporter = make_versions(member_session, 6), player(member_session)
    service = service_for(member_session)
    for version in versions[:5]:
        service.submit(reporter, version.public_id, ["other"], None)
    for version in versions[:5]:
        ScriptReportRepository(member_session).close_open(version.id, CLOSED_DISMISSED, START)
    with pytest.raises(ReportLimit):
        service.submit(reporter, versions[5].public_id, ["other"], None)


def test_a_limit_of_zero_pauses_reporting_for_everyone(member_session):
    version, reporter = published(member_session), player(member_session)
    set_limit(member_session, 0)
    with pytest.raises(ReportLimit):
        service_for(member_session).submit(reporter, version.public_id, ["other"], None)
    assert reports(member_session) == []


def test_the_limit_is_read_from_the_database_every_time(member_session):
    versions, reporter = make_versions(member_session, 4), player(member_session)
    service = service_for(member_session)
    set_limit(member_session, 1)
    service.submit(reporter, versions[0].public_id, ["other"], None)
    with pytest.raises(ReportLimit):
        service.submit(reporter, versions[1].public_id, ["other"], None)             # 上限改為 1：下一個請求就是 429（同一個 Service 實例，沒有快取）
    RuntimeLimitRepository(member_session).set_value("script_report_daily", 3, START)
    service.submit(reporter, versions[1].public_id, ["other"], None)
    service.submit(reporter, versions[2].public_id, ["other"], None)
    with pytest.raises(ReportLimit):
        service.submit(reporter, versions[3].public_id, ["other"], None)
    assert len(reports(member_session)) == 3


def test_a_corrupt_limit_falls_back_to_five_and_never_to_unlimited(member_session):
    versions, reporter = make_versions(member_session, 6), player(member_session)
    set_limit(member_session, 10**6)                                                  # 範圍外（最大 100）
    service = service_for(member_session)
    for version in versions[:5]:
        service.submit(reporter, version.public_id, ["other"], None)
    with pytest.raises(ReportLimit):
        service.submit(reporter, versions[5].public_id, ["other"], None)


# ---- 並行 ---------------------------------------------------------------------------------------


def test_two_identical_reports_at_the_same_time_succeed_exactly_once(concurrent_engine):
    with Session(concurrent_engine) as s:
        version = published(s)
        reporter = player(s, 2)
        version_public_id = version.public_id
        s.commit()
    barrier = Barrier(2)

    def worker(_index: int) -> str:
        with Session(concurrent_engine) as s:
            service = service_for(s, FakeClock())
            barrier.wait(timeout=30)
            try:
                service.submit(reporter, version_public_id, ["other"], "同時送出")
                s.commit()
                return "ok"
            except AlreadyReported:
                s.rollback()
                return "duplicate"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [future.result(timeout=60) for future in [pool.submit(worker, i) for i in range(2)]]
    assert sorted(outcomes) == ["duplicate", "ok"]
    with Session(concurrent_engine) as s:
        assert s.scalar(select(func.count(ScriptReport.id))) == 1


# ---- 不呼叫模型、日誌只有整數 ---------------------------------------------------------------------


def test_the_service_has_no_model_dependency():
    """檢舉的文字不送入任何模型：Service 沒有接收模型的參數，也不匯入任何模型呼叫或審查（模型層）相關的模組。"""
    import ast
    import inspect

    tree = ast.parse((PROJECT_ROOT / "app" / "services" / "script_report_service.py").read_text(encoding="utf-8-sig"))
    imported = [
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    ] + [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
    for module in imported:
        assert not any(word in module for word in ("llm", "gemini", "openai", "review", "provider")), module
    assert set(inspect.signature(ScriptReportService.__init__).parameters) == {"self", "scripts", "reports", "policy", "limits", "clock"}


def test_a_submission_logs_only_the_user_id_and_two_integers(member_session, log_lines):
    version, reporter = published(member_session), player(member_session)
    service_for(member_session).submit(reporter, version.public_id, ["plagiarism", "other"], SPECIAL_TEXT)
    [event] = [line for line in log_lines() if line.get("event") == "script.report_submitted"]
    assert event["user_id"] == reporter.id and event["count"] == 2 and event["chars"] == len(SPECIAL_TEXT)
    dumped = str(log_lines())
    for secret in (SPECIAL_TEXT, "DROP TABLE", version.public_id, version.title, "plagiarism"):
        assert secret not in dumped, secret


def test_a_limited_report_logs_one_event_with_only_the_user_id(member_session, log_lines):
    version, reporter = published(member_session), player(member_session)
    set_limit(member_session, 0)
    with pytest.raises(ReportLimit):
        service_for(member_session).submit(reporter, version.public_id, ["other"], SPECIAL_TEXT)
    [event] = [line for line in log_lines() if line.get("event") == "script.report_limited"]
    assert event["user_id"] == reporter.id
    assert SPECIAL_TEXT not in str(log_lines()) and version.public_id not in str(log_lines())
