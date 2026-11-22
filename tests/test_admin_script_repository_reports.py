"""rpg-script-reports 的 admin-script-reports 規格（`AdminScriptRepository` 的檢舉部分，唯讀）：

- 「被檢舉」清單（`reported`）只列上架中且至少有一筆**未結案**檢舉的版本，結案之後消失、之後再被檢舉會回來；
- 優先度四層：紅線 → 未結案人數（多者在前）→ 最早的未結案檢舉（舊者在前）→ 版本編號；紅線與最早時間都只看**未結案**的檢舉，
  紅線只讀建立時另存的 `red_line` 布林（不是從 JSON 理由推出來）；
- 所有清單與詳情都有 `report_count` 與 `red_line_reported`（沒有檢舉為 0 與假）；
- `list_reports` 回傳該版本的全部檢舉（含已結案、由舊到新），資料列**沒有檢舉人**。

用 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行：外連接的子查詢、`COALESCE`、`CASE` 與對子查詢欄位排序加
`OFFSET` 是兩種資料庫最容易分歧的地方。
"""
import dataclasses
from types import SimpleNamespace

import pytest

from app.models.script import VERSION_DRAFT, VERSION_PENDING, VERSION_PUBLISHED, VERSION_REMOVED
from app.models.script_report import (
    CLOSED_DISMISSED, CLOSED_REMOVED, REASON_MINOR_SEXUAL, REASON_OTHER, REASON_PLAGIARISM, REASON_REAL_PERSON_SEXUAL,
    REASON_SEXUAL_VIOLENCE, REASON_TIER_MISMATCH, RED_LINE_REASONS,
)
from app.repositories.admin_script_repository import LIST_REPORTED, LISTABLE_STATUSES, AdminScriptRepository, ScriptReportRow
from app.repositories.script_report_repository import ScriptReportRepository
from app.repositories.script_repository import ScriptRepository
from tests.support.script_data import validated
from tests.support.script_review import at, make_author, new_draft, put_in_status

TEXT = "自寫的檢舉文字：<b>不是標記</b> 'x'; DROP TABLE users -- 😀"


@pytest.fixture()
def kit(member_session):
    session = member_session
    return SimpleNamespace(
        session=session, scripts=ScriptRepository(session), reports=ScriptReportRepository(session),
        admin=AdminScriptRepository(session), author=make_author(session, 1),
        reporters=[make_author(session, n) for n in range(2, 9)],
    )


def published(kit, title: str, **changes):
    return put_in_status(kit.scripts, kit.author, VERSION_PUBLISHED, title=title, now=at(1), **changes)


def report(kit, version, who: int = 0, *, reasons=(REASON_OTHER,), red_line=None, note=None, now=None):
    """第 `who` 位檢舉人對版本送出一筆檢舉；`red_line` 預設由理由算出（與 Service 相同），可明確指定以測試「只讀另存的旗標」。"""
    if red_line is None:
        red_line = any(reason in RED_LINE_REASONS for reason in reasons)
    return kit.reports.add(kit.reporters[who].id, version.id, list(reasons), red_line, note, now=now or at(2))


def listed(kit, **kwargs) -> list[str]:
    return [row.title for row in kit.admin.list_by_status(LIST_REPORTED, limit=kwargs.get("limit", 50), offset=kwargs.get("offset", 0))]


def row_of(kit, version, status):
    return next(r for r in kit.admin.list_by_status(status, limit=50, offset=0) if r.version_public_id == version.public_id)


# ---- 只列上架中且有未結案檢舉的版本 ------------------------------------------------------------------


def test_the_reported_view_is_a_listable_status_and_not_a_version_status():
    assert LIST_REPORTED == "reported" and LIST_REPORTED in LISTABLE_STATUSES
    assert LIST_REPORTED not in (VERSION_PENDING, VERSION_PUBLISHED, VERSION_REMOVED)


def test_only_a_published_version_with_an_open_report_is_listed(kit):
    open_one = published(kit, "甲有未結案")
    closed_only = published(kit, "乙都已結案")
    published(kit, "丙沒有檢舉")
    report(kit, open_one), report(kit, closed_only)
    assert kit.reports.close_open(closed_only.id, CLOSED_DISMISSED, at(5)) == 1
    assert listed(kit) == ["甲有未結案"]


def test_a_version_that_is_not_published_is_never_listed_even_if_a_report_row_exists(kit):
    """版本離開上架時檢舉會被系統結案；這裡直接寫入不合常理的資料，證明清單本身也只認上架中。"""
    pending = put_in_status(kit.scripts, kit.author, VERSION_PENDING, title="待審的")
    draft = put_in_status(kit.scripts, kit.author, VERSION_DRAFT, title="草稿的")
    removed = put_in_status(kit.scripts, kit.author, VERSION_REMOVED, title="下架的")
    for version in (pending, draft, removed):
        report(kit, version)
    assert listed(kit) == []


def test_closing_removes_a_version_from_the_view_and_a_new_report_brings_it_back(kit):
    version = published(kit, "反覆被檢舉")
    report(kit, version, 0), report(kit, version, 1)
    assert listed(kit) == ["反覆被檢舉"]
    assert kit.reports.close_open_by_public_id(version.public_id, CLOSED_DISMISSED, at(5)) == 2
    assert listed(kit) == []
    report(kit, version, 2, now=at(6))                                      # 結案之後新的玩家仍可檢舉
    assert listed(kit) == ["反覆被檢舉"]
    assert row_of(kit, version, LIST_REPORTED).report_count == 1            # 已結案的兩筆不計


def test_a_builtin_script_never_appears_because_it_cannot_be_reported(kit):
    content = validated(title="官方劇本")
    kit.scripts.publish_builtin(
        "official-seed", content=content.content, content_hash=content.content_hash, total_chars=content.total_chars, now=at(1),
    )
    assert listed(kit) == []


# ---- 優先度排序 -------------------------------------------------------------------------------------


def test_a_red_line_report_outranks_a_larger_number_of_other_reports(kit):
    many = published(kit, "抄襲五筆")
    one_red = published(kit, "紅線一筆")
    for who in range(5):
        report(kit, many, who, reasons=(REASON_PLAGIARISM,))
    report(kit, one_red, 0, reasons=(REASON_MINOR_SEXUAL,))
    assert listed(kit) == ["紅線一筆", "抄襲五筆"]


@pytest.mark.parametrize("reason", RED_LINE_REASONS)
def test_each_of_the_three_red_line_reasons_counts(kit, reason):
    plain = published(kit, "普通兩筆")
    red = published(kit, "紅線一筆")
    report(kit, plain, 0), report(kit, plain, 1)
    report(kit, red, 0, reasons=(REASON_TIER_MISMATCH, reason))              # 紅線理由夾在其他理由之中也算
    assert listed(kit) == ["紅線一筆", "普通兩筆"]
    assert row_of(kit, red, LIST_REPORTED).red_line_reported is True
    assert row_of(kit, plain, LIST_REPORTED).red_line_reported is False


def test_one_red_line_report_among_many_makes_the_version_red(kit):
    mixed = published(kit, "四筆含一筆紅線")
    plain = published(kit, "四筆都不是紅線")
    for who in range(3):
        report(kit, mixed, who, reasons=(REASON_PLAGIARISM,))
        report(kit, plain, who, reasons=(REASON_PLAGIARISM,))
    report(kit, mixed, 3, reasons=(REASON_REAL_PERSON_SEXUAL,))
    report(kit, plain, 3, reasons=(REASON_OTHER,))
    assert listed(kit) == ["四筆含一筆紅線", "四筆都不是紅線"]


def test_among_red_line_versions_more_open_reports_come_first(kit):
    two = published(kit, "紅線兩筆")
    three = published(kit, "紅線三筆")
    for who in range(2):
        report(kit, two, who, reasons=(REASON_SEXUAL_VIOLENCE,))
    for who in range(3):
        report(kit, three, who, reasons=(REASON_SEXUAL_VIOLENCE,))
    assert listed(kit) == ["紅線三筆", "紅線兩筆"]


def test_with_the_same_count_the_earlier_open_report_comes_first_not_the_lower_id(kit):
    later = published(kit, "編號小但檢舉晚")                                  # 先建立（編號較小）
    earlier = published(kit, "編號大但檢舉早")
    report(kit, later, 0, now=at(9))
    report(kit, earlier, 0, now=at(3))
    assert listed(kit) == ["編號大但檢舉早", "編號小但檢舉晚"]


def test_with_everything_equal_the_lower_version_id_comes_first(kit):
    first = published(kit, "先建立")
    second = published(kit, "後建立")
    report(kit, second, 0, now=at(4)), report(kit, first, 0, now=at(4))
    assert listed(kit) == ["先建立", "後建立"]


def test_the_earliest_time_and_the_red_line_only_look_at_open_reports(kit):
    """版本甲有一筆很早、已結案的紅線檢舉，與較晚的未結案普通檢舉：既不是紅線，最早時間也是未結案那一筆（不是已結案的）。"""
    old_closed = published(kit, "甲舊的已結案")
    steady = published(kit, "乙較早未結案")
    report(kit, old_closed, 0, reasons=(REASON_MINOR_SEXUAL,), now=at(1))
    assert kit.reports.close_open(old_closed.id, CLOSED_DISMISSED, at(2)) == 1
    report(kit, old_closed, 1, now=at(8))
    report(kit, steady, 0, now=at(5))
    assert listed(kit) == ["乙較早未結案", "甲舊的已結案"]
    assert row_of(kit, old_closed, LIST_REPORTED).red_line_reported is False
    assert row_of(kit, old_closed, LIST_REPORTED).report_count == 1


def test_the_red_line_flag_is_read_from_the_stored_boolean_not_derived_from_the_reasons_json(kit):
    """排序與聚合不得依 JSON 欄位內容（AGENTS.md 第 4 節）：理由寫著紅線但旗標為假就不是紅線，反之亦然。"""
    json_only = published(kit, "理由是紅線旗標為假")
    flag_only = published(kit, "旗標為真理由普通")
    report(kit, json_only, 0, reasons=(REASON_MINOR_SEXUAL,), red_line=False)
    report(kit, flag_only, 0, reasons=(REASON_OTHER,), red_line=True)
    assert row_of(kit, json_only, LIST_REPORTED).red_line_reported is False
    assert row_of(kit, flag_only, LIST_REPORTED).red_line_reported is True
    assert listed(kit) == ["旗標為真理由普通", "理由是紅線旗標為假"]


def test_the_four_layers_together(kit):
    versions = {name: published(kit, name) for name in ("丁", "丙", "乙", "甲", "戊")}
    report(kit, versions["戊"], 0, now=at(7))                                            # 一筆、最晚
    report(kit, versions["甲"], 0, now=at(6))                                            # 一筆、較早
    for who in range(2):
        report(kit, versions["乙"], who, now=at(8))                                      # 兩筆
    report(kit, versions["丙"], 0, reasons=(REASON_MINOR_SEXUAL,), now=at(9))            # 一筆紅線
    for who in range(3):
        report(kit, versions["丁"], who, reasons=(REASON_SEXUAL_VIOLENCE,), now=at(10))  # 三筆紅線
    assert listed(kit) == ["丁", "丙", "乙", "甲", "戊"]


def test_limit_and_offset_page_through_the_priority_order(kit):
    versions = [published(kit, f"第{n}份") for n in (1, 2, 3)]
    for index, version in enumerate(versions):
        for who in range(3 - index):                                                      # 第1份三筆、第2份兩筆、第3份一筆
            report(kit, version, who)
    assert listed(kit, limit=1, offset=1) == ["第2份"]
    assert listed(kit, limit=2, offset=0) == ["第1份", "第2份"]
    assert listed(kit, limit=5, offset=3) == []


# ---- 所有清單與詳情都帶計數 -----------------------------------------------------------------------


def test_every_list_has_the_two_report_keys_and_a_version_without_reports_has_zero_and_false(kit):
    published(kit, "上架")
    put_in_status(kit.scripts, kit.author, VERSION_PENDING, title="待審")
    put_in_status(kit.scripts, kit.author, VERSION_REMOVED, title="下架")
    for status in (VERSION_PENDING, VERSION_PUBLISHED, VERSION_REMOVED):
        rows = kit.admin.list_by_status(status, limit=50, offset=0)
        assert rows and all((row.report_count, row.red_line_reported) == (0, False) for row in rows)
        assert all(type(row.report_count) is int and type(row.red_line_reported) is bool for row in rows)


def test_the_published_list_and_the_detail_show_the_open_report_count_and_the_red_line(kit):
    hot = published(kit, "被檢舉的")
    quiet = published(kit, "沒被檢舉")
    report(kit, hot, 0, reasons=(REASON_PLAGIARISM,)), report(kit, hot, 1, reasons=(REASON_MINOR_SEXUAL,))
    assert (row_of(kit, hot, VERSION_PUBLISHED).report_count, row_of(kit, hot, VERSION_PUBLISHED).red_line_reported) == (2, True)
    assert (row_of(kit, quiet, VERSION_PUBLISHED).report_count, row_of(kit, quiet, VERSION_PUBLISHED).red_line_reported) == (0, False)
    detail = kit.admin.get_reviewable(hot.public_id).row
    assert (detail.report_count, detail.red_line_reported) == (2, True)
    assert kit.admin.get_reviewable(quiet.public_id).row.report_count == 0


def test_the_count_drops_when_reports_are_closed_and_a_version_that_went_through_a_takedown_shows_zero(kit):
    version = published(kit, "會被下架")
    report(kit, version, 0), report(kit, version, 1)
    kit.scripts.remove_by_admin(version.public_id, "other_policy", "備註", at(10))           # 下架時系統自動結案
    removed = row_of(kit, version, VERSION_REMOVED)
    assert (removed.report_count, removed.red_line_reported) == (0, False)
    assert listed(kit) == []


def test_the_count_is_per_version_not_per_script(kit):
    first = published(kit, "第一版")
    report(kit, first, 0)
    script_public_id = kit.scripts.get_script_of(first).public_id
    second = new_draft(kit.scripts, kit.author, script_public_id, title="第二版")
    kit.scripts.submit_for_review(kit.author.id, second.public_id, now=at(3))
    kit.scripts.approve_version(second.public_id, now=at(4))                                  # 核可時舊版被取代、其檢舉自動結案
    assert row_of(kit, second, VERSION_PUBLISHED).report_count == 0
    report(kit, second, 1, now=at(5))
    assert row_of(kit, second, VERSION_PUBLISHED).report_count == 1


# ---- 讀取檢舉內容 ---------------------------------------------------------------------------------


def test_list_reports_returns_every_report_of_the_version_oldest_first_including_closed_ones(kit):
    version = published(kit, "被檢舉")
    other = published(kit, "別的版本")
    report(kit, version, 0, reasons=(REASON_PLAGIARISM, REASON_OTHER), note=TEXT, now=at(2))
    assert kit.reports.close_open(version.id, CLOSED_DISMISSED, at(3)) == 1
    report(kit, version, 1, reasons=(REASON_MINOR_SEXUAL,), now=at(4))
    report(kit, other, 0, note="別的版本的檢舉不該出現")
    rows = kit.admin.list_reports(version.public_id)
    assert [(row.reasons, row.text, row.created_at, row.closed_at, row.closed_result) for row in rows] == [
        ((REASON_PLAGIARISM, REASON_OTHER), TEXT, at(2), at(3), CLOSED_DISMISSED),
        ((REASON_MINOR_SEXUAL,), None, at(4), None, None),
    ]
    assert all(isinstance(row, ScriptReportRow) for row in rows)


def test_a_report_row_has_no_reporter_and_the_reporters_identity_is_nowhere_in_it(kit):
    version = published(kit, "被檢舉")
    report(kit, version, 0, note=TEXT)
    (row,) = kit.admin.list_reports(version.public_id)
    names = {field.name for field in dataclasses.fields(row)}
    assert names == {"reasons", "text", "created_at", "closed_at", "closed_result"}
    flat = repr(row)
    reporter = kit.reporters[0]
    for secret in (reporter.email, reporter.public_id, reporter.display_name):
        assert secret not in flat


def test_reports_are_ordered_by_creation_time_then_id(kit):
    version = published(kit, "被檢舉")
    for who, minute in ((0, 5), (1, 5), (2, 1)):
        report(kit, version, who, note=f"第{who}位", now=at(minute))
    assert [row.text for row in kit.admin.list_reports(version.public_id)] == ["第2位", "第0位", "第1位"]


@pytest.mark.parametrize("identifier", ["00000000-0000-4000-8000-000000000000", "not-a-uuid", ""])
def test_an_unknown_identifier_has_no_reports(kit, identifier):
    report(kit, published(kit, "被檢舉"), 0)
    assert kit.admin.list_reports(identifier) == []


def test_a_version_without_reports_has_an_empty_list(kit):
    assert kit.admin.list_reports(published(kit, "沒有檢舉").public_id) == []


def test_the_note_is_returned_as_plain_data_without_any_change(kit):
    version = published(kit, "被檢舉")
    report(kit, version, 0, note=TEXT)
    assert kit.admin.list_reports(version.public_id)[0].text == TEXT


# ---- 唯讀 ------------------------------------------------------------------------------------------


def test_reading_reports_and_the_reported_view_changes_nothing(kit):
    version = published(kit, "被檢舉")
    report(kit, version, 0)
    kit.session.flush()
    before = (set(kit.session.new), set(kit.session.dirty), set(kit.session.deleted))
    kit.admin.list_by_status(LIST_REPORTED, limit=10, offset=0)
    kit.admin.list_reports(version.public_id)
    kit.admin.get_reviewable(version.public_id)
    assert (set(kit.session.new), set(kit.session.dirty), set(kit.session.deleted)) == before


def test_closed_result_values_pass_through_unchanged(kit):
    version = published(kit, "被檢舉")
    report(kit, version, 0)
    kit.scripts.remove_own(kit.author.id, version.public_id, at(9))
    assert [row.closed_result for row in kit.admin.list_reports(version.public_id)] == [CLOSED_REMOVED]
