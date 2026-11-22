"""admin-script-review 規格「審核清單依狀態列出並排序」與「送審過的版本可讀全文」的資料行為（`AdminScriptRepository`，唯讀）。

用 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行：排序（含 `CASE` 與 `COALESCE`）與 JSON 欄位經由 `CASE`
取回，是兩種資料庫最容易分歧的地方。
"""
import dataclasses
import inspect

import pytest
from sqlalchemy import update

from app.models import ScriptVersion
from app.models.script import (
    VERSION_DRAFT, VERSION_PENDING, VERSION_PUBLISHED, VERSION_REJECTED, VERSION_REMOVED, VERSION_SUPERSEDED,
)
from app.repositories.admin_script_repository import LISTABLE_STATUSES, AdminScriptRepository, ScriptVersionRow
from app.repositories.script_repository import ScriptRepository
from tests.support.script_review import at, make_author, new_script, put_in_status


@pytest.fixture()
def repo(member_session):
    return ScriptRepository(member_session)


@pytest.fixture()
def admin_repo(member_session):
    return AdminScriptRepository(member_session)


@pytest.fixture()
def author(member_session):
    return make_author(member_session)


def flag(repo, version, categories=("minor_suspected",), verdict="flag"):
    assert repo.record_review(version.id, version.content_hash, verdict, categories, at())


def titles(rows):
    return [row.title for row in rows]


# ---- 待審：有疑慮的排最前，其餘由舊到新 ----------------------------------------------------------------------


def test_pending_lists_the_flagged_first_then_oldest_first(repo, admin_repo, author):
    older = put_in_status(repo, author, VERSION_PENDING, title="甲較舊通過", now=at(1))
    newer = put_in_status(repo, author, VERSION_PENDING, title="乙較新有疑慮", now=at(2))
    newest = put_in_status(repo, author, VERSION_PENDING, title="丙最新通過", now=at(3))
    flag(repo, newer)
    flag(repo, older, categories=(), verdict="pass")
    flag(repo, newest, categories=(), verdict="pass")
    assert titles(admin_repo.list_by_status(VERSION_PENDING, limit=50, offset=0)) == ["乙較新有疑慮", "甲較舊通過", "丙最新通過"]


def test_pending_without_any_concern_is_oldest_first(repo, admin_repo, author):
    put_in_status(repo, author, VERSION_PENDING, title="後送審", now=at(9))
    put_in_status(repo, author, VERSION_PENDING, title="先送審", now=at(1))
    assert titles(admin_repo.list_by_status(VERSION_PENDING, limit=50, offset=0)) == ["先送審", "後送審"]


def test_the_same_submission_time_falls_back_to_the_id(repo, admin_repo, author):
    put_in_status(repo, author, VERSION_PENDING, title="同時間先建立", now=at(5))
    put_in_status(repo, author, VERSION_PENDING, title="同時間後建立", now=at(5))
    assert titles(admin_repo.list_by_status(VERSION_PENDING, limit=50, offset=0)) == ["同時間先建立", "同時間後建立"]


def test_a_review_result_that_no_longer_matches_the_content_does_not_count_as_a_concern(repo, admin_repo, author, member_session):
    """結果與內容雜湊對應才算數：結果對應到別的內容（雜湊不符）時不排最前，回傳的結果與類別也是空的。"""
    stale = put_in_status(repo, author, VERSION_PENDING, title="結果已失效", now=at(1))
    put_in_status(repo, author, VERSION_PENDING, title="較晚但有效", now=at(2))
    flag(repo, stale)
    member_session.execute(update(ScriptVersion).where(ScriptVersion.id == stale.id).values(review_hash="0" * 64))
    member_session.expire_all()
    rows = admin_repo.list_by_status(VERSION_PENDING, limit=50, offset=0)
    assert titles(rows) == ["結果已失效", "較晚但有效"]                           # 沒有疑慮：照送審時間，最舊的在前
    assert rows[0].review_verdict is None and rows[0].review_categories == ()


def test_a_missing_submission_time_uses_the_creation_time(repo, admin_repo, author, member_session):
    """遷移之前就在待審的版本沒有送審時間，以建立時間代替（不會排到最前或最後）。"""
    old = put_in_status(repo, author, VERSION_PENDING, title="舊資料沒有送審時間", now=at(50))
    put_in_status(repo, author, VERSION_PENDING, title="新資料", now=at(10))
    member_session.execute(update(ScriptVersion).where(ScriptVersion.id == old.id).values(submitted_at=None, created_at=at(-30)))
    member_session.expire_all()
    assert titles(admin_repo.list_by_status(VERSION_PENDING, limit=50, offset=0)) == ["舊資料沒有送審時間", "新資料"]


def test_the_pending_list_carries_the_flagged_categories(repo, admin_repo, author):
    version = put_in_status(repo, author, VERSION_PENDING, title="有疑慮")
    flag(repo, version, categories=("minor_suspected", "other_concern"))
    row = admin_repo.list_by_status(VERSION_PENDING, limit=50, offset=0)[0]
    assert row.review_verdict == "flag" and set(row.review_categories) == {"minor_suspected", "other_concern"}


# ---- 上架中與已下架 ------------------------------------------------------------------------------------


def test_published_is_newest_publication_first(repo, admin_repo, author):
    put_in_status(repo, author, VERSION_PUBLISHED, title="較早上架", now=at(1))
    put_in_status(repo, author, VERSION_PUBLISHED, title="較晚上架", now=at(7))
    assert titles(admin_repo.list_by_status(VERSION_PUBLISHED, limit=50, offset=0)) == ["較晚上架", "較早上架"]


def test_removed_is_newest_decision_first_and_carries_who_and_why(repo, admin_repo, author):
    put_in_status(repo, author, VERSION_REMOVED, title="作者自己下架", removed_by="author", now=at(2))
    put_in_status(repo, author, VERSION_REMOVED, title="管理員下架", removed_by="admin", now=at(8))
    rows = admin_repo.list_by_status(VERSION_REMOVED, limit=50, offset=0)
    assert titles(rows) == ["管理員下架", "作者自己下架"]
    assert (rows[0].removed_by, rows[0].decision_category, rows[0].has_note) == ("admin", "other_policy", True)
    assert (rows[1].removed_by, rows[1].decision_category, rows[1].has_note) == ("author", None, False)


def test_each_list_only_has_its_own_status_and_never_drafts_rejected_or_superseded(repo, admin_repo, author):
    for status in (VERSION_DRAFT, VERSION_PENDING, VERSION_PUBLISHED, VERSION_REJECTED, VERSION_REMOVED, VERSION_SUPERSEDED):
        put_in_status(repo, author, status, title=f"狀態{status}"[:30])
    for status in LISTABLE_STATUSES:
        rows = admin_repo.list_by_status(status, limit=50, offset=0)
        assert {row.status for row in rows} == {status}
        assert f"狀態{VERSION_DRAFT}" not in titles(rows) and f"狀態{VERSION_REJECTED}" not in titles(rows)
    # 被取代的版本（第 1 版）不在任何清單；取代它的第 2 版（沿用預設標題）在上架清單
    assert not any(f"狀態{VERSION_SUPERSEDED}" in titles(admin_repo.list_by_status(s, limit=50, offset=0)) for s in LISTABLE_STATUSES)
    assert "青雲問道" in titles(admin_repo.list_by_status(VERSION_PUBLISHED, limit=50, offset=0))


@pytest.mark.parametrize("status", [VERSION_DRAFT, VERSION_REJECTED, VERSION_SUPERSEDED, "unknown", "", None])
def test_a_status_outside_the_three_lists_is_a_programming_error(admin_repo, status):
    with pytest.raises(ValueError):
        admin_repo.list_by_status(status, limit=10, offset=0)


def test_limit_and_offset_page_through_the_sorted_list(repo, admin_repo, author):
    for minute in (1, 2, 3):
        put_in_status(repo, author, VERSION_PENDING, title=f"第{minute}份", now=at(minute))
    assert titles(admin_repo.list_by_status(VERSION_PENDING, limit=1, offset=1)) == ["第2份"]
    assert titles(admin_repo.list_by_status(VERSION_PENDING, limit=2, offset=0)) == ["第1份", "第2份"]
    assert admin_repo.list_by_status(VERSION_PENDING, limit=5, offset=3) == []


# ---- 資料列的形狀 --------------------------------------------------------------------------------------


def test_a_row_has_the_catalog_fields_and_the_author_public_id_but_no_internal_ids_or_texts(repo, admin_repo, author):
    version = put_in_status(repo, author, VERSION_PENDING, title="目錄資訊", summary="一句簡介", world_name="某世界", now=at(3))
    row = admin_repo.list_by_status(VERSION_PENDING, limit=1, offset=0)[0]
    assert isinstance(row, ScriptVersionRow)
    assert row.version_public_id == version.public_id and row.script_public_id == repo.get_script_of(version).public_id
    assert (row.title, row.summary, row.version_no, row.status, row.content_tier) == ("目錄資訊", "一句簡介", 1, VERSION_PENDING, "safe")
    assert row.author_public_id == author.public_id and row.author_name == f"筆名{author.id}" and row.author_code == "a1b2c3"
    assert row.submitted_at == at(3) and row.published_at is None and row.decided_at is None
    names = {field.name for field in dataclasses.fields(ScriptVersionRow)}
    assert not {name for name in names if name in {"id", "user_id", "script_id", "creator_id", "email", "background", "characters", "decision_note"}}
    values = [getattr(row, name) for name in names]
    assert author.email not in " ".join(str(value) for value in values)


def test_the_list_has_no_script_body_and_only_says_whether_a_note_exists(repo, admin_repo, author):
    put_in_status(repo, author, VERSION_REJECTED, title="被退回")           # put_in_status 以備註「管理員的備註」退回
    rows = [row for status in LISTABLE_STATUSES for row in admin_repo.list_by_status(status, limit=50, offset=0)]
    for row in rows:
        text = repr(row)
        assert "靈氣復甦" not in text and "林曉" not in text and "管理員的備註" not in text and "下架的備註" not in text
    removed = put_in_status(repo, author, VERSION_REMOVED, title="含備註", removed_by="admin")
    row = next(r for r in admin_repo.list_by_status(VERSION_REMOVED, limit=50, offset=0) if r.version_public_id == removed.public_id)
    assert row.has_note is True and "下架的備註" not in repr(row)


def test_a_builtin_script_has_no_author(member_session, repo, admin_repo):
    from tests.support.script_data import validated

    result = validated(title="官方劇本")
    version, _ = repo.publish_builtin(
        "official-seed", content=result.content, content_hash=result.content_hash, total_chars=result.total_chars, now=at(1),
    )
    row = next(r for r in admin_repo.list_by_status(VERSION_PUBLISHED, limit=50, offset=0) if r.version_public_id == version.public_id)
    assert row.author_public_id is None and row.author_name == "官方"
    assert admin_repo.find_ref(version.public_id).is_builtin is True and row.is_builtin is True


def test_a_script_without_an_author_is_builtin_only_when_it_has_a_seed_key(member_session, repo, admin_repo, author):
    """匿名保留的劇本沒有作者（`creator_id` 為空、筆名「已註銷的作者」）但沒有種子鍵：`is_builtin` 為假；有作者的劇本也為假。"""
    from app.models import Script
    from app.models.script import RETIRED_AUTHOR_NAME

    mine = put_in_status(repo, author, VERSION_PUBLISHED, title="有作者")
    retained = put_in_status(repo, author, VERSION_PUBLISHED, title="匿名保留")
    member_session.execute(
        update(Script).where(Script.id == repo.get_script_of(retained).id)
        .values(creator_id=None, author_name=RETIRED_AUTHOR_NAME, author_code=None).execution_options(synchronize_session=False)
    )
    member_session.expire_all()
    rows = {r.version_public_id: r for r in admin_repo.list_by_status(VERSION_PUBLISHED, limit=50, offset=0)}
    assert (rows[mine.public_id].is_builtin, rows[mine.public_id].author_public_id is None) == (False, False)
    assert (rows[retained.public_id].is_builtin, rows[retained.public_id].author_public_id is None) == (False, True)
    assert admin_repo.find_ref(retained.public_id).is_builtin is False and admin_repo.find_ref(retained.public_id).author_public_id is None
    assert admin_repo.get_reviewable(retained.public_id).row.is_builtin is False


def test_an_old_version_without_an_audience_reads_as_unset(repo, admin_repo, author, member_session):
    version = put_in_status(repo, author, VERSION_PENDING, title="沒有頻道")
    member_session.execute(update(ScriptVersion).where(ScriptVersion.id == version.id).values(audience=None))
    member_session.expire_all()
    assert admin_repo.list_by_status(VERSION_PENDING, limit=1, offset=0)[0].audience is None


# ---- 單一版本 ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [VERSION_PENDING, VERSION_PUBLISHED, VERSION_REJECTED, VERSION_REMOVED, VERSION_SUPERSEDED])
def test_every_version_that_was_submitted_can_be_read_in_full(repo, admin_repo, author, status):
    version = put_in_status(repo, author, status, title="可讀的版本")
    found = admin_repo.get_reviewable(version.public_id)
    assert found is not None and found.row.status == status and found.row.title == "可讀的版本"
    assert found.content == repo.content_of(version)
    assert "靈氣復甦" in found.content["background"] and found.content["characters"][0]["name"] == "林曉"


def test_a_draft_cannot_be_read_and_looks_exactly_like_a_missing_id(repo, admin_repo, author):
    draft = put_in_status(repo, author, VERSION_DRAFT)
    assert admin_repo.get_reviewable(draft.public_id) is None
    assert admin_repo.get_reviewable("00000000-0000-0000-0000-000000000000") is None
    assert admin_repo.get_reviewable("not-a-uuid") is None


def test_a_withdrawn_version_is_a_draft_again_and_cannot_be_read(repo, admin_repo, author):
    version = put_in_status(repo, author, VERSION_PENDING)
    assert admin_repo.get_reviewable(version.public_id) is not None
    repo.withdraw(author.id, version.public_id)
    assert admin_repo.get_reviewable(version.public_id) is None


def test_the_detail_carries_the_admin_note_and_the_decision(repo, admin_repo, author):
    version = put_in_status(repo, author, VERSION_REJECTED)
    found = admin_repo.get_reviewable(version.public_id)
    assert found.decision_note == "管理員的備註"
    assert (found.row.decision_category, found.row.has_note, found.row.decided_at) == ("other_policy", True, at())


def test_the_detail_review_result_is_only_given_while_it_matches_the_content(repo, admin_repo, author, member_session):
    version = put_in_status(repo, author, VERSION_PENDING)
    flag(repo, version, categories=("tier_mismatch",))
    assert admin_repo.get_reviewable(version.public_id).row.review_categories == ("tier_mismatch",)
    member_session.execute(update(ScriptVersion).where(ScriptVersion.id == version.id).values(review_hash="f" * 64))
    member_session.expire_all()
    found = admin_repo.get_reviewable(version.public_id)
    assert found.row.review_verdict is None and found.row.review_categories == ()


def test_find_ref_finds_any_status_including_drafts_without_any_content(repo, admin_repo, author):
    draft = put_in_status(repo, author, VERSION_DRAFT)
    removed = put_in_status(repo, author, VERSION_REMOVED, removed_by="admin")
    ref = admin_repo.find_ref(draft.public_id)
    assert (ref.status, ref.removed_by, ref.is_builtin, ref.author_public_id) == (VERSION_DRAFT, None, False, author.public_id)
    ref = admin_repo.find_ref(removed.public_id)
    assert (ref.status, ref.removed_by) == (VERSION_REMOVED, "admin")
    assert ref.script_public_id == repo.get_script_of(removed).public_id
    assert admin_repo.find_ref("00000000-0000-0000-0000-000000000000") is None
    assert not {field.name for field in dataclasses.fields(type(ref))} & {"id", "content", "decision_note", "title"}


def test_find_ref_follows_the_state_after_a_transition(repo, admin_repo, author, member_session):
    version = put_in_status(repo, author, VERSION_PENDING)
    assert admin_repo.find_ref(version.public_id).status == VERSION_PENDING
    repo.approve_version(version.public_id, now=at(1))
    assert admin_repo.find_ref(version.public_id).status == VERSION_PUBLISHED
    repo.remove_own(author.id, version.public_id, at(2))
    ref = admin_repo.find_ref(version.public_id)
    assert (ref.status, ref.removed_by) == (VERSION_REMOVED, "author")


# ---- 唯讀 ----------------------------------------------------------------------------------------------


def test_the_admin_script_repository_is_read_only():
    public = {name for name, member in inspect.getmembers(AdminScriptRepository, inspect.isfunction) if not name.startswith("_")}
    assert public == {"list_by_status", "get_reviewable", "find_ref"}


def test_reading_changes_nothing(repo, admin_repo, author, member_session):
    version = put_in_status(repo, author, VERSION_PENDING)
    member_session.flush()
    before = (member_session.new.copy(), member_session.dirty.copy(), member_session.deleted.copy())
    admin_repo.list_by_status(VERSION_PENDING, limit=10, offset=0)
    admin_repo.get_reviewable(version.public_id)
    admin_repo.find_ref(version.public_id)
    assert (set(member_session.new), set(member_session.dirty), set(member_session.deleted)) == tuple(set(part) for part in before)
