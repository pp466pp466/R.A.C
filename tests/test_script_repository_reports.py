"""rpg-script-reports 的 script-reports 規格「版本離開上架時未結案的檢舉由系統自動結案」與 script-takedown 規格「內建劇本以種子鍵辨識」：

- 下架（作者與管理員共用 `_remove`）時，這個版本未結案的檢舉在同一個交易結為 `removed`；
- 核可新版本、內建劇本重新上架時，被取代的版本未結案的檢舉結為 `superseded`；
- 已結案的檢舉不被改寫、別的版本的檢舉不受影響、轉移失敗時不結案、恢復上架不重開；
- 管理員下架的內建劇本守門看**種子鍵**，不是「沒有作者」（匿名保留的劇本沒有作者，但可以被下架）。

以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行。
"""
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from app.models import Script, ScriptReport, ScriptVersion
from app.models.script import RETIRED_AUTHOR_NAME, VERSION_PUBLISHED, VERSION_REMOVED, VERSION_SUPERSEDED
from app.models.script_report import CLOSED_DISMISSED, CLOSED_REMOVED, CLOSED_SUPERSEDED, REASON_OTHER
from app.repositories.errors import BuiltinNotRemovableError, InvalidVersionTransitionError
from app.repositories.script_report_repository import ScriptReportRepository
from app.repositories.script_repository import ScriptRepository
from tests.support.script_data import validated
from tests.support.script_review import at, make_author, new_draft, new_script, put_in_status, script_public_id


@pytest.fixture()
def kit(member_session):
    session = member_session
    author = make_author(session, 1)
    reporters = [make_author(session, n) for n in (2, 3, 4)]
    return SimpleNamespace(
        session=session, scripts=ScriptRepository(session), reports=ScriptReportRepository(session), author=author, reporters=reporters,
    )


def report(kit, version, reporter_index: int = 0, *, now=None) -> ScriptReport:
    return kit.reports.add(kit.reporters[reporter_index].id, version.id, [REASON_OTHER], False, None, now=now or at(1))


def outcome(kit, version) -> list[tuple]:
    """這個版本全部檢舉的 (檢舉人編號, 結案時間, 結案結果)，依檢舉人排序。"""
    kit.session.expire_all()
    rows = kit.session.execute(
        select(ScriptReport.reporter_id, ScriptReport.closed_at, ScriptReport.closed_result)
        .where(ScriptReport.version_id == version.id).order_by(ScriptReport.reporter_id)
    ).all()
    return [tuple(row) for row in rows]


def results(kit, version) -> list:
    return [result for _, _, result in outcome(kit, version)]


def published(kit, **changes) -> ScriptVersion:
    return put_in_status(kit.scripts, kit.author, VERSION_PUBLISHED, **changes)


def status_of(kit, version) -> str:
    kit.session.expire_all()
    return kit.session.get(ScriptVersion, version.id).status


# ---- 下架 ------------------------------------------------------------------------------------


def test_the_author_removing_a_version_closes_its_open_reports_as_removed(kit):
    version = published(kit)
    report(kit, version, 0), report(kit, version, 1)

    kit.scripts.remove_own(kit.author.id, version.public_id, at(10))

    assert status_of(kit, version) == VERSION_REMOVED
    assert [(closed_at, result) for _, closed_at, result in outcome(kit, version)] == [(at(10), CLOSED_REMOVED)] * 2


def test_an_admin_removing_a_version_closes_its_open_reports_as_removed(kit):
    version = published(kit)
    report(kit, version, 0), report(kit, version, 1), report(kit, version, 2)

    kit.scripts.remove_by_admin(version.public_id, "other_policy", "備註", at(10))

    assert status_of(kit, version) == VERSION_REMOVED
    assert results(kit, version) == [CLOSED_REMOVED] * 3


def test_a_version_without_reports_is_removed_without_trouble(kit):
    version = published(kit)
    assert kit.scripts.remove_own(kit.author.id, version.public_id, at(10)) is not None
    assert status_of(kit, version) == VERSION_REMOVED


def test_removal_only_touches_the_removed_versions_reports(kit):
    """同一位作者的另一個劇本、同劇本被取代的舊版本，它們的檢舉都不受影響。"""
    mine = published(kit)
    other_script = published(kit, title="另一個劇本")
    old = put_in_status(kit.scripts, kit.author, VERSION_SUPERSEDED, title="有舊版本的劇本")
    report(kit, mine, 0), report(kit, other_script, 0), report(kit, old, 0)
    kit.reports.close_open(old.id, CLOSED_SUPERSEDED, at(3))               # 舊版本的檢舉早已因被取代而結案

    kit.scripts.remove_own(kit.author.id, mine.public_id, at(10))

    assert results(kit, mine) == [CLOSED_REMOVED]
    assert results(kit, other_script) == [None]
    assert outcome(kit, old) == [(kit.reporters[0].id, at(3), CLOSED_SUPERSEDED)]


def test_reports_closed_earlier_keep_their_result_and_time_when_the_version_is_removed(kit):
    version = published(kit)
    report(kit, version, 0)
    kit.reports.close_open(version.id, CLOSED_DISMISSED, at(5))              # 管理員先判定不成立
    report(kit, version, 1, now=at(6))                                       # 之後又有人檢舉

    kit.scripts.remove_own(kit.author.id, version.public_id, at(10))

    assert outcome(kit, version) == [
        (kit.reporters[0].id, at(5), CLOSED_DISMISSED),                      # 已結案的不被改寫
        (kit.reporters[1].id, at(10), CLOSED_REMOVED),
    ]


def test_a_removal_that_cannot_happen_closes_nothing(kit):
    """版本不是上架中（這裡是已經下架）時轉移拋出例外，之後才加進來的檢舉保持未結案。"""
    version = published(kit)
    kit.scripts.remove_own(kit.author.id, version.public_id, at(10))
    late = report(kit, version, 0, now=at(11))

    with pytest.raises(InvalidVersionTransitionError):
        kit.scripts.remove_own(kit.author.id, version.public_id, at(12))
    with pytest.raises(InvalidVersionTransitionError):
        kit.scripts.remove_by_admin(version.public_id, "other_policy", None, at(12))

    kit.session.expire_all()
    assert kit.session.get(ScriptReport, late.id).closed_at is None


def test_restoring_a_removed_version_does_not_reopen_its_reports(kit):
    version = published(kit)
    report(kit, version, 0)
    kit.scripts.remove_own(kit.author.id, version.public_id, at(10))

    kit.scripts.restore_own(kit.author.id, version.public_id)

    assert status_of(kit, version) == VERSION_PUBLISHED
    assert outcome(kit, version) == [(kit.reporters[0].id, at(10), CLOSED_REMOVED)]        # 結案是歷史事實
    report(kit, version, 1, now=at(20))                                                    # 其他玩家可以再檢舉，它是未結案的
    kit.scripts.remove_own(kit.author.id, version.public_id, at(30))
    assert outcome(kit, version) == [
        (kit.reporters[0].id, at(10), CLOSED_REMOVED), (kit.reporters[1].id, at(30), CLOSED_REMOVED),
    ]


# ---- 被新版本取代 -------------------------------------------------------------------------------


def next_version(kit, old: ScriptVersion, *, now, **changes) -> ScriptVersion:
    draft = new_draft(kit.scripts, kit.author, script_public_id(kit.scripts, old), **changes)
    kit.scripts.submit_for_review(kit.author.id, draft.public_id, now=now)
    return draft


def test_approving_a_new_version_closes_the_old_versions_reports_as_superseded(kit):
    old = published(kit)
    report(kit, old, 0), report(kit, old, 1)
    new = next_version(kit, old, now=at(9), title="第二版")

    kit.scripts.approve_version(new.public_id, now=at(10))

    assert status_of(kit, old) == VERSION_SUPERSEDED and status_of(kit, new) == VERSION_PUBLISHED
    assert [(closed_at, result) for _, closed_at, result in outcome(kit, old)] == [(at(10), CLOSED_SUPERSEDED)] * 2
    assert outcome(kit, new) == []


def test_a_removed_old_version_keeps_the_removed_result_when_it_is_superseded(kit):
    """下架的舊版本在下架時就已結案；之後被新版本取代（不可再恢復）不改寫結果。"""
    old = published(kit)
    report(kit, old, 0)
    kit.scripts.remove_own(kit.author.id, old.public_id, at(5))
    new = next_version(kit, old, now=at(9), title="第二版")

    kit.scripts.approve_version(new.public_id, now=at(10))

    assert status_of(kit, old) == VERSION_SUPERSEDED
    assert outcome(kit, old) == [(kit.reporters[0].id, at(5), CLOSED_REMOVED)]


def test_a_failed_approval_leaves_the_old_version_and_its_reports_untouched(kit):
    """新版本不是待審（還是草稿）：核可拋出例外，同一個 SAVEPOINT 內對舊版本做的取代與結案一併回復。"""
    old = published(kit)
    report(kit, old, 0)
    draft = new_draft(kit.scripts, kit.author, script_public_id(kit.scripts, old), title="還沒送審")

    with pytest.raises(InvalidVersionTransitionError):
        kit.scripts.approve_version(draft.public_id, now=at(10))

    assert status_of(kit, old) == VERSION_PUBLISHED
    assert results(kit, old) == [None]


def test_republishing_a_builtin_script_closes_reports_on_the_replaced_version_as_superseded(kit):
    """內建劇本不可被檢舉，Service 不會讓它發生；但 Repository 層的不變量與其他入口一致：被取代的版本沒有未結案的檢舉。"""
    result = validated()
    first, created = kit.scripts.publish_builtin(
        "builtin-a", content=result.content, content_hash=result.content_hash, total_chars=result.total_chars, now=at(0)
    )
    assert created is True
    report(kit, first, 0)

    second, created = kit.scripts.publish_builtin(
        "builtin-a", content=result.content, content_hash=result.content_hash, total_chars=result.total_chars, now=at(10)
    )

    assert created is False and second.id != first.id and status_of(kit, first) == VERSION_SUPERSEDED
    assert outcome(kit, first) == [(kit.reporters[0].id, at(10), CLOSED_SUPERSEDED)]


# ---- 內建劇本看種子鍵，不是「沒有作者」 -----------------------------------------------------------------


def test_an_admin_cannot_remove_a_builtin_script(kit):
    result = validated()
    version, _ = kit.scripts.publish_builtin(
        "builtin-b", content=result.content, content_hash=result.content_hash, total_chars=result.total_chars, now=at(0)
    )
    with pytest.raises(BuiltinNotRemovableError):
        kit.scripts.remove_by_admin(version.public_id, "other_policy", None, at(10))
    assert status_of(kit, version) == VERSION_PUBLISHED


def test_an_admin_can_remove_a_script_that_has_no_author_but_is_not_builtin(kit):
    """匿名保留的劇本沒有作者（`creator_id` 為空、筆名是「已註銷的作者」）但沒有種子鍵：它是玩家創作的，管理員可以下架。"""
    version = published(kit)
    report(kit, version, 0)
    script = kit.scripts.get_script_of(version)
    kit.session.execute(
        update(Script).where(Script.id == script.id).values(creator_id=None, author_name=RETIRED_AUTHOR_NAME, author_code=None)
        .execution_options(synchronize_session=False)
    )
    kit.session.expire_all()
    assert kit.session.get(Script, script.id).creator_id is None and kit.session.get(Script, script.id).seed_key is None

    kit.scripts.remove_by_admin(version.public_id, "other_policy", "備註", at(10))

    assert status_of(kit, version) == VERSION_REMOVED
    assert results(kit, version) == [CLOSED_REMOVED]


def test_the_author_scope_never_matches_a_script_without_an_author(kit):
    """沒有作者的劇本不屬於任何人：任何人的「自己的」查詢都找不到它（所以不能被誰自行下架或修改）。"""
    version = published(kit)
    script = kit.scripts.get_script_of(version)
    kit.session.execute(
        update(Script).where(Script.id == script.id).values(creator_id=None).execution_options(synchronize_session=False)
    )
    kit.session.expire_all()
    assert kit.scripts.get_owned_version(kit.author.id, version.public_id) is None
    assert kit.scripts.remove_own(kit.author.id, version.public_id, at(10)) is None
    assert kit.scripts.count_live_owned(kit.author.id) == 0


def test_count_live_owned_counts_only_the_authors_published_versions(kit):
    published(kit), published(kit, title="第二個劇本")
    put_in_status(kit.scripts, kit.author, VERSION_REMOVED, title="下架的")
    new_script(kit.scripts, kit.author, title="只有草稿")
    put_in_status(kit.scripts, kit.reporters[0], VERSION_PUBLISHED, title="別人的")
    old = put_in_status(kit.scripts, kit.author, VERSION_SUPERSEDED, title="有舊版本")        # 舊版本被取代，新版本上架中：只算新的那一個
    assert old is not None

    assert kit.scripts.count_live_owned(kit.author.id) == 3
    assert kit.scripts.count_live_owned(kit.reporters[0].id) == 1
    assert kit.scripts.count_live_owned(kit.reporters[1].id) == 0


# ---- 自動結案的日誌事件（script.reports_auto_closed）：轉移確定成功之後才記、只在筆數大於 0 時記 --------------------


def auto_closed(log_lines) -> list[tuple[int, str]]:
    return [(line["count"], line["outcome"]) for line in log_lines() if line.get("event") == "script.reports_auto_closed"]


def test_an_authors_removal_logs_one_event_with_the_number_of_closed_reports(kit, log_lines):
    version = published(kit)
    report(kit, version, 0), report(kit, version, 1)
    kit.scripts.remove_own(kit.author.id, version.public_id, at(10))
    assert auto_closed(log_lines) == [(2, "removed")]


def test_an_admins_removal_logs_the_same_event(kit, log_lines):
    version = published(kit)
    report(kit, version, 0), report(kit, version, 1), report(kit, version, 2)
    kit.scripts.remove_by_admin(version.public_id, "other_policy", "備註", at(10))
    assert auto_closed(log_lines) == [(3, "removed")]


def test_a_removal_without_open_reports_logs_no_close_event(kit, log_lines):
    version = published(kit)
    kit.scripts.remove_own(kit.author.id, version.public_id, at(10))
    assert auto_closed(log_lines) == []


def test_a_removal_that_cannot_happen_logs_no_close_event(kit, log_lines):
    version = published(kit)
    kit.scripts.remove_own(kit.author.id, version.public_id, at(10))
    report(kit, version, 0, now=at(11))
    with pytest.raises(InvalidVersionTransitionError):
        kit.scripts.remove_own(kit.author.id, version.public_id, at(12))
    assert auto_closed(log_lines) == []


def test_approving_a_new_version_logs_the_number_of_reports_closed_as_superseded(kit, log_lines):
    old = published(kit)
    report(kit, old, 0), report(kit, old, 1)
    new = new_draft(kit.scripts, kit.author, script_public_id(kit.scripts, old), title="新版本")
    kit.scripts.submit_for_review(kit.author.id, new.public_id, now=at(5))
    kit.scripts.approve_version(new.public_id, now=at(10))
    assert auto_closed(log_lines) == [(2, "superseded")]


def test_approving_when_the_old_version_has_no_open_reports_logs_no_close_event(kit, log_lines):
    old = published(kit)
    new = new_draft(kit.scripts, kit.author, script_public_id(kit.scripts, old), title="新版本")
    kit.scripts.submit_for_review(kit.author.id, new.public_id, now=at(5))
    kit.scripts.approve_version(new.public_id, now=at(10))
    assert auto_closed(log_lines) == []


def test_a_failed_approval_logs_no_close_event(kit, log_lines):
    """核可失敗、SAVEPOINT 回復時，不留下「已結案」的紀錄（事件在 SAVEPOINT 成功之後才記）。"""
    old = published(kit)
    report(kit, old, 0)
    draft = new_draft(kit.scripts, kit.author, script_public_id(kit.scripts, old), title="還沒送審")
    with pytest.raises(InvalidVersionTransitionError):
        kit.scripts.approve_version(draft.public_id, now=at(10))
    assert auto_closed(log_lines) == []


def test_restoring_a_version_logs_no_close_event(kit, log_lines):
    version = published(kit)
    report(kit, version, 0)
    kit.scripts.remove_own(kit.author.id, version.public_id, at(10))
    kit.scripts.restore_own(kit.author.id, version.public_id)
    assert auto_closed(log_lines) == [(1, "removed")]                    # 只有下架那一次；恢復不重開、不再記錄


def test_the_close_event_carries_no_identifier_or_text(kit, log_lines):
    version = published(kit, title="只該出現在資料庫的標題SCAN-CLOSE-TITLE")
    kit.reports.add(kit.reporters[0].id, version.id, [REASON_OTHER], False, "檢舉文字SCAN-CLOSE-NOTE", now=at(1))
    kit.scripts.remove_own(kit.author.id, version.public_id, at(10))
    raw = log_lines.raw()
    for secret in (version.public_id, "SCAN-CLOSE-TITLE", "SCAN-CLOSE-NOTE"):
        assert secret not in raw, secret
    assert auto_closed(log_lines) == [(1, "removed")]
