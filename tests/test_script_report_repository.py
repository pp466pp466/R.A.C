"""rpg-script-reports 的資料行為（script-reports 與 script-store 規格）：`ScriptReportRepository` 的建立、唯一約束、每日計數的日界、
條件式結案（只結未結案的、已結案不被改寫）、依版本刪除、個人資料匯出的欄位白名單。

一般測試以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行；並行測試用 `concurrent_engine`，
以真實的執行緒與獨立連線證明「最多一個成功」。
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ScriptReport, ScriptVersion, User
from app.models.base import utcnow
from app.models.script_report import (
    CLOSED_DISMISSED, CLOSED_REMOVED, CLOSED_RESULTS, CLOSED_SUPERSEDED, REASON_MINOR_SEXUAL, REASON_OTHER, REASON_PLAGIARISM,
)
from app.repositories.errors import AlreadyReportedError
from app.repositories.script_report_repository import ScriptReportRepository
from app.repositories.script_repository import ScriptRepository
from app.services.quota_policy import taipei_day_bounds
from tests.support.script_data import validated

NOW = utcnow()
SPECIAL_NOTE = "自寫文字：<b>不是標記</b> 'x'; DROP TABLE users -- 😀"


def make_user(session, n: int = 1) -> User:
    user = User(email=f"u{n}@example.com", email_canonical=f"u{n}@example.com", display_name=f"使用者{n}", status="active")
    session.add(user)
    session.flush()
    return user


def make_version(session, author: User, **changes) -> ScriptVersion:
    """作者的一個新劇本的第 1 版（草稿即可：Repository 層不要求版本已上架，上架的限制在 Service）。"""
    result = validated(**changes)
    return ScriptRepository(session).create_script(
        author.id, author_name=f"筆名{author.id}", author_code="a1b2c3", content=result.content, content_hash=result.content_hash,
        total_chars=result.total_chars,
    )


@pytest.fixture()
def repo(member_session):
    return ScriptReportRepository(member_session)


@pytest.fixture()
def world(member_session):
    """作者一位、檢舉人三位、作者的一個版本。"""
    author = make_user(member_session, 1)
    reporters = [make_user(member_session, n) for n in (2, 3, 4)]
    version = make_version(member_session, author)
    return author, reporters, version


def add_report(repo, reporter, version, *, reasons=(REASON_OTHER,), red_line=False, note=None, now=None) -> ScriptReport:
    return repo.add(reporter.id, version.id, reasons, red_line, note, now=now)


# ---- 建立與唯一約束 --------------------------------------------------------------

def test_add_stores_the_fields_and_starts_unclosed(repo, world, member_session):
    _, (alice, *_), version = world
    report = repo.add(alice.id, version.id, [REASON_MINOR_SEXUAL, REASON_OTHER], True, SPECIAL_NOTE, now=NOW)
    member_session.expire_all()
    stored = member_session.get(ScriptReport, report.id)
    assert (stored.reporter_id, stored.version_id) == (alice.id, version.id)
    assert stored.reasons_json == [REASON_MINOR_SEXUAL, REASON_OTHER] and stored.red_line is True
    assert stored.note == SPECIAL_NOTE                                       # 特殊字元與 emoji 原樣保存（以參數化寫入，不是拼字串）
    assert stored.closed_at is None and stored.closed_result is None
    assert stored.created_at == NOW and stored.updated_at == NOW


def test_add_without_a_note_stores_null_and_defaults_the_time(repo, world, member_session):
    _, (alice, *_), version = world
    report = add_report(repo, alice, version)
    assert report.note is None and report.red_line is False
    assert abs((report.created_at - utcnow()).total_seconds()) < 60


def test_the_same_reporter_cannot_report_the_same_version_twice(repo, world, member_session):
    _, (alice, bob, _), version = world
    add_report(repo, alice, version)
    with pytest.raises(AlreadyReportedError):
        add_report(repo, alice, version, reasons=(REASON_PLAGIARISM,), note="第二次")
    assert member_session.scalar(select(func.count(ScriptReport.id))) == 1
    assert member_session.scalar(select(ScriptReport.note)) is None           # 第一筆沒有被第二次覆寫
    add_report(repo, bob, version)                                            # 別人檢舉同一版本不受影響
    assert member_session.scalar(select(func.count(ScriptReport.id))) == 2


def test_a_duplicate_only_rolls_back_its_own_savepoint(repo, world, member_session):
    """重複的檢舉只回復這一筆：同一個交易中先前尚未提交的變更仍在，Session 可繼續使用。"""
    _, (alice, bob, _), version = world
    add_report(repo, alice, version)
    pending = make_user(member_session, 9)                                    # 同一交易內、尚未提交的其他變更
    with pytest.raises(AlreadyReportedError):
        add_report(repo, alice, version)
    assert member_session.get(User, pending.id) is not None
    add_report(repo, bob, version)                                            # 失敗之後 Session 仍可用
    assert member_session.scalar(select(func.count(ScriptReport.id))) == 2


def test_a_new_version_of_the_same_script_can_be_reported_again_by_the_same_person(repo, world, member_session):
    author, (alice, *_), first = world
    second = ScriptRepository(member_session).create_script(
        author.id, author_name="筆名", author_code="a1b2c3", content=validated(title="另一個").content,
        content_hash=validated(title="另一個").content_hash, total_chars=validated(title="另一個").total_chars,
    )
    add_report(repo, alice, first)
    add_report(repo, alice, second)                                           # 唯一性按版本，不按人或劇本
    assert member_session.scalar(select(func.count(ScriptReport.id))) == 2


def test_an_integrity_error_that_is_not_a_duplicate_is_not_reported_as_one(repo, world, member_session):
    """版本不存在（外鍵錯誤）不是「已檢舉過」：原樣拋出，不被誤判為重複。"""
    _, (alice, *_), version = world
    with pytest.raises(IntegrityError):
        repo.add(alice.id, version.id + 9999, [REASON_OTHER], False, None)
    assert member_session.scalar(select(func.count(ScriptReport.id))) == 0


def test_exists_ignores_closing(repo, world, member_session):
    _, (alice, bob, _), version = world
    assert not repo.exists(alice.id, version.id)
    add_report(repo, alice, version)
    assert repo.exists(alice.id, version.id) and not repo.exists(bob.id, version.id)
    repo.close_open(version.id, CLOSED_DISMISSED, NOW)
    assert repo.exists(alice.id, version.id)                                  # 已結案仍算「已檢舉過」（每個版本只能一次）


# ---- 每日計數 ---------------------------------------------------------------------

def test_count_between_uses_an_inclusive_start_and_an_exclusive_end_and_only_counts_the_reporter(repo, world, member_session):
    author, (alice, bob, _), first = world
    versions = [first] + [make_version(member_session, author, title=f"第{n}個") for n in range(1, 4)]
    start, end = taipei_day_bounds(NOW)
    add_report(repo, alice, versions[0], now=start - timedelta(microseconds=1))        # 前一天的最後一刻：不算
    add_report(repo, alice, versions[1], now=start)                                    # 起點：算
    add_report(repo, alice, versions[2], now=end - timedelta(microseconds=1))          # 當天最後一刻：算
    add_report(repo, alice, versions[3], now=end)                                      # 終點：不算（下一天）
    add_report(repo, bob, versions[1], now=start + timedelta(hours=1))                 # 別人的：不算
    assert repo.count_between(alice.id, start, end) == 2
    assert repo.count_between(bob.id, start, end) == 1
    assert repo.count_between(alice.id, end, end + timedelta(days=1)) == 1


def test_count_between_counts_closed_reports_too(repo, world, member_session):
    _, (alice, *_), version = world
    start, end = taipei_day_bounds(NOW)
    add_report(repo, alice, version, now=start + timedelta(hours=1))
    repo.close_open(version.id, CLOSED_REMOVED, NOW)
    assert repo.count_between(alice.id, start, end) == 1                      # 結案不歸還額度


# ---- 結案 -------------------------------------------------------------------------

def states(session, version):
    session.expire_all()
    rows = session.execute(
        select(ScriptReport.reporter_id, ScriptReport.closed_at, ScriptReport.closed_result)
        .where(ScriptReport.version_id == version.id).order_by(ScriptReport.id)
    ).all()
    return [(r.closed_at, r.closed_result) for r in rows]


def test_close_open_closes_every_open_report_of_that_version_only(repo, world, member_session):
    author, (alice, bob, carol), version = world
    other = make_version(member_session, author, title="另一個版本")
    for reporter in (alice, bob, carol):
        add_report(repo, reporter, version)
    add_report(repo, alice, other)
    assert repo.close_open(version.id, CLOSED_REMOVED, NOW) == 3
    assert states(member_session, version) == [(NOW, CLOSED_REMOVED)] * 3
    assert states(member_session, other) == [(None, None)]                    # 別的版本不受影響
    assert repo.close_open(version.id, CLOSED_REMOVED, NOW) == 0              # 沒有未結案的：0 筆


def test_close_open_never_rewrites_a_report_that_is_already_closed(repo, world, member_session):
    _, (alice, bob, _), version = world
    add_report(repo, alice, version)
    assert repo.close_open(version.id, CLOSED_DISMISSED, NOW) == 1
    add_report(repo, bob, version)                                            # 之後才進來的檢舉
    later = NOW + timedelta(days=2)
    assert repo.close_open(version.id, CLOSED_REMOVED, later) == 1            # 只結新的那一筆
    assert states(member_session, version) == [(NOW, CLOSED_DISMISSED), (later, CLOSED_REMOVED)]
    assert repo.close_open(version.id, CLOSED_SUPERSEDED, later + timedelta(days=1)) == 0
    assert states(member_session, version) == [(NOW, CLOSED_DISMISSED), (later, CLOSED_REMOVED)]       # 結案是歷史事實，不被覆蓋


@pytest.mark.parametrize("result", CLOSED_RESULTS)
def test_close_open_accepts_each_documented_result(repo, world, member_session, result):
    _, (alice, *_), version = world
    add_report(repo, alice, version)
    assert repo.close_open(version.id, result, NOW) == 1
    assert states(member_session, version) == [(NOW, result)]


@pytest.mark.parametrize("bad", ["", "closed", "REMOVED", "removed ", None, 1])
def test_close_open_rejects_a_result_outside_the_fixed_list_without_touching_anything(repo, world, member_session, bad):
    _, (alice, *_), version = world
    add_report(repo, alice, version)
    with pytest.raises(ValueError):
        repo.close_open(version.id, bad, NOW)
    assert states(member_session, version) == [(None, None)]


# ---- 刪除 -------------------------------------------------------------------------

def test_delete_for_versions_removes_open_and_closed_reports_of_those_versions_only(repo, world, member_session):
    author, (alice, bob, _), first = world
    second = make_version(member_session, author, title="第二個")
    third = make_version(member_session, author, title="第三個")
    add_report(repo, alice, first)
    add_report(repo, bob, first)
    repo.close_open(first.id, CLOSED_REMOVED, NOW)                            # 已結案的也一併刪除
    add_report(repo, alice, second)
    add_report(repo, alice, third)
    assert repo.delete_for_versions([first.id, second.id]) == 3
    remaining = member_session.scalars(select(ScriptReport.version_id)).all()
    assert remaining == [third.id]


def test_delete_for_versions_with_nothing_to_delete_is_a_no_op(repo, world, member_session):
    _, (alice, *_), version = world
    add_report(repo, alice, version)
    assert repo.delete_for_versions([]) == 0
    assert repo.delete_for_versions([version.id + 5000]) == 0
    assert member_session.scalar(select(func.count(ScriptReport.id))) == 1


def test_delete_for_versions_handles_more_ids_than_a_single_statement_can_carry(repo, world, member_session):
    """版本很多時分批刪除（SQL Server 單一語句的參數上限約 2100）；重複的編號只算一次。"""
    _, (alice, *_), version = world
    add_report(repo, alice, version)
    ids = list(range(100_000, 102_500)) + [version.id, version.id]
    assert repo.delete_for_versions(ids) == 1
    assert member_session.scalar(select(func.count(ScriptReport.id))) == 0


# ---- 匯出 -------------------------------------------------------------------------

def test_export_has_exactly_the_four_allowed_keys_and_no_closing_information(repo, world, member_session):
    _, (alice, bob, _), version = world
    add_report(repo, alice, version, reasons=(REASON_MINOR_SEXUAL, REASON_OTHER), red_line=True, note=SPECIAL_NOTE, now=NOW)
    add_report(repo, bob, version)
    repo.close_open(version.id, CLOSED_REMOVED, NOW)
    exported = list(repo.export_for_reporter(alice.id))
    assert len(exported) == 1
    assert set(exported[0]) == {"version_public_id", "reasons", "text", "created_at"}          # 沒有 closed_*、版本標題或作者資訊
    assert exported[0] == {
        "version_public_id": version.public_id, "reasons": [REASON_MINOR_SEXUAL, REASON_OTHER], "text": SPECIAL_NOTE, "created_at": NOW,
    }


def test_export_contains_only_the_reporters_own_reports_oldest_first_across_pages(repo, world, member_session):
    author, (alice, bob, _), first = world
    versions = [first] + [make_version(member_session, author, title=f"第{n}個") for n in range(1, 5)]
    for index, version in enumerate(versions):
        add_report(repo, alice, version, note=f"第 {index} 筆", now=NOW + timedelta(minutes=index))
    add_report(repo, bob, versions[0], note="別人的")
    texts = [row["text"] for row in repo.export_for_reporter(alice.id, page_size=2)]            # 分三頁讀取
    assert texts == [f"第 {n} 筆" for n in range(5)]
    assert [row["text"] for row in repo.export_for_reporter(bob.id)] == ["別人的"]


def test_export_of_a_person_without_reports_is_empty(repo, world, member_session):
    _, (alice, *_), _ = world
    assert list(repo.export_for_reporter(alice.id)) == []


# ---- 並行 -------------------------------------------------------------------------

def run_in_threads(engine, workers):
    barrier = Barrier(len(workers))

    def task(index: int):
        with Session(engine) as s:
            barrier.wait(timeout=30)
            outcome = workers[index](s)
            s.commit()
            return outcome

    with ThreadPoolExecutor(max_workers=len(workers)) as pool:
        return [f.result(timeout=60) for f in [pool.submit(task, i) for i in range(len(workers))]]


def test_two_identical_reports_at_the_same_time_succeed_exactly_once(concurrent_engine):
    with Session(concurrent_engine) as s:
        author, reporter = make_user(s, 1), make_user(s, 2)
        version = make_version(s, author)
        reporter_id, version_id = reporter.id, version.id
        s.commit()

    def worker(s: Session) -> str:
        try:
            ScriptReportRepository(s).add(reporter_id, version_id, [REASON_OTHER], False, "同時送出")
            return "added"
        except AlreadyReportedError:
            return "duplicate"

    assert sorted(run_in_threads(concurrent_engine, [worker, worker])) == ["added", "duplicate"]
    with Session(concurrent_engine) as s:
        assert s.scalar(select(func.count(ScriptReport.id))) == 1


def test_two_closings_at_the_same_time_close_each_report_exactly_once(concurrent_engine):
    with Session(concurrent_engine) as s:
        author = make_user(s, 1)
        version = make_version(s, author)
        reporters = [make_user(s, n) for n in (2, 3, 4)]
        for reporter in reporters:
            ScriptReportRepository(s).add(reporter.id, version.id, [REASON_OTHER], False, None)
        version_id = version.id
        s.commit()

    def dismissing(s: Session) -> tuple[str, int]:
        return CLOSED_DISMISSED, ScriptReportRepository(s).close_open(version_id, CLOSED_DISMISSED, NOW)

    def removing(s: Session) -> tuple[str, int]:
        return CLOSED_REMOVED, ScriptReportRepository(s).close_open(version_id, CLOSED_REMOVED, NOW + timedelta(seconds=1))

    outcomes = run_in_threads(concurrent_engine, [dismissing, removing])
    assert sum(count for _, count in outcomes) == 3                           # 每一筆只被結案一次，不重複計算
    with Session(concurrent_engine) as s:
        rows = s.execute(select(ScriptReport.closed_at, ScriptReport.closed_result)).all()
        assert len(rows) == 3 and all(closed_at is not None for closed_at, _ in rows)
        winners = {result for _, result in rows}
        # 兩個結案各自只結自己搶到的列；每一列的結果必為兩者之一，且與贏得該列的那一方一致
        assert winners <= {CLOSED_DISMISSED, CLOSED_REMOVED}
        counts = dict(outcomes)
        assert sum(1 for _, result in rows if result == CLOSED_DISMISSED) == counts[CLOSED_DISMISSED]
        assert sum(1 for _, result in rows if result == CLOSED_REMOVED) == counts[CLOSED_REMOVED]
