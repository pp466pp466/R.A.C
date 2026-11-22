"""admin-script-review 規格的回應形狀（`admin_script_views`，純函式）：鍵集合固定、沒有內部編號與 email、清單沒有內文與備註全文、
詳情才有完整內容與備註、模型層結果只在該帶的時候帶、時間格式一致。
"""
from datetime import datetime, timezone

import pytest

from app.models.script import VERSION_PENDING, VERSION_PUBLISHED, VERSION_REMOVED
from app.repositories.admin_script_repository import ScriptVersionDetail, ScriptVersionRow
from app.services import admin_script_views as views


def make_row(**changes) -> ScriptVersionRow:
    values = dict(
        version_public_id="11111111-1111-1111-1111-111111111111", script_public_id="22222222-2222-2222-2222-222222222222",
        version_no=2, status=VERSION_PENDING, title="青雲問道", summary="一句簡介", author_name="筆名", author_code="a1b2c3",
        author_public_id="33333333-3333-3333-3333-333333333333", audience="female", content_tier="safe",
        submitted_at=datetime(2026, 11, 20, 8, 30, 15, 123456, tzinfo=timezone.utc), published_at=None, decided_at=None,
        review_verdict="flag", review_categories=("minor_suspected", "other_concern"), removed_by=None, decision_category=None,
        has_note=False, is_builtin=False,
    )
    values.update(changes)
    return ScriptVersionRow(**values)


def make_detail(row=None, **changes) -> ScriptVersionDetail:
    values = dict(row=row or make_row(), content={"title": "青雲問道", "background": "靈氣復甦"}, decision_note="只有管理員看得到")
    values.update(changes)
    return ScriptVersionDetail(**values)


def test_the_list_item_has_exactly_the_documented_keys():
    assert set(views.list_item(make_row())) == set(views.LIST_KEYS)
    assert len(views.LIST_KEYS) == len(set(views.LIST_KEYS))


def test_the_detail_has_exactly_the_list_keys_plus_the_note_and_the_content():
    assert set(views.detail(make_detail())) == set(views.DETAIL_KEYS)
    assert set(views.DETAIL_KEYS) - set(views.LIST_KEYS) == {"decision_note", "content"}


def test_no_internal_integer_ids_email_or_password_fields_can_appear():
    forbidden = {"user_id", "creator_id", "email", "password_hash", "token_version", "birth_date", "age_range", "review_hash", "content_hash"}
    assert not forbidden & set(views.DETAIL_KEYS)
    item = views.list_item(make_row())
    assert all(not isinstance(value, int) or key in {"version_no"} or isinstance(value, bool) for key, value in item.items())


def test_is_builtin_comes_from_the_row_and_has_nothing_to_do_with_having_an_author():
    """內建劇本與「沒有作者」是兩回事（匿名保留的劇本沒有作者但不是內建）：兩個鍵各自忠實反映資料列。"""
    builtin = views.list_item(make_row(author_public_id=None, is_builtin=True))
    assert builtin["author_id"] is None and builtin["is_builtin"] is True
    retained = views.list_item(make_row(author_public_id=None, is_builtin=False))
    assert retained["author_id"] is None and retained["is_builtin"] is False
    assert views.list_item(make_row())["is_builtin"] is False
    assert views.detail(make_detail(make_row(is_builtin=True)))["is_builtin"] is True


def test_the_list_item_values_come_from_the_row():
    item = views.list_item(make_row())
    assert item["id"] == "11111111-1111-1111-1111-111111111111" and item["script_id"] == "22222222-2222-2222-2222-222222222222"
    assert item["author_id"] == "33333333-3333-3333-3333-333333333333"
    assert (item["version_no"], item["status"], item["title"], item["summary"]) == (2, VERSION_PENDING, "青雲問道", "一句簡介")
    assert (item["author_name"], item["author_code"], item["audience"], item["content_tier"]) == ("筆名", "a1b2c3", "female", "safe")


def test_times_are_iso_in_utc_to_the_second_and_missing_ones_are_null():
    item = views.list_item(make_row())
    assert item["submitted_at"] == "2026-11-20T08:30:15+00:00"
    assert item["published_at"] is None and item["decided_at"] is None


def test_the_review_result_is_only_given_in_the_list_for_pending_versions():
    pending = views.list_item(make_row(status=VERSION_PENDING))
    assert pending["review_verdict"] == "flag" and pending["review_categories"] == ["minor_suspected", "other_concern"]
    for status in (VERSION_PUBLISHED, VERSION_REMOVED):
        other = views.list_item(make_row(status=status))
        assert other["review_verdict"] is None and other["review_categories"] == []


def test_the_detail_gives_the_review_result_for_any_status_as_long_as_the_row_carries_one():
    published = views.detail(make_detail(make_row(status=VERSION_PUBLISHED)))
    assert published["review_verdict"] == "flag" and published["review_categories"] == ["minor_suspected", "other_concern"]
    stale = views.detail(make_detail(make_row(review_verdict=None, review_categories=())))
    assert stale["review_verdict"] is None and stale["review_categories"] == []


def test_a_builtin_script_has_no_author_id_and_an_unset_audience_reads_as_everyone():
    item = views.list_item(make_row(author_public_id=None, audience=None))
    assert item["author_id"] is None and item["audience"] == "all"


def test_the_list_item_never_contains_the_body_or_the_note_but_the_detail_does():
    row = make_row(status=VERSION_REMOVED, has_note=True, removed_by="admin", decision_category="other_policy")
    text = repr(views.list_item(row))
    assert "靈氣復甦" not in text and "只有管理員看得到" not in text
    item = views.list_item(row)
    assert item["has_note"] is True and item["removed_by"] == "admin" and item["decision_category"] == "other_policy"
    detail = views.detail(make_detail(row))
    assert detail["decision_note"] == "只有管理員看得到" and detail["content"]["background"] == "靈氣復甦"


@pytest.mark.parametrize("has_note", [True, False, 1, 0])
def test_has_note_is_always_a_boolean(has_note):
    assert views.list_item(make_row(has_note=has_note))["has_note"] is bool(has_note)


def test_the_category_list_is_a_fresh_list_not_the_rows_tuple():
    row = make_row()
    item = views.list_item(row)
    assert isinstance(item["review_categories"], list)
    item["review_categories"].append("changed")
    assert row.review_categories == ("minor_suspected", "other_concern")
