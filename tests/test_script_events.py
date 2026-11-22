"""service-observability 規格「劇本、暱稱與審查事件只含白名單欄位」：事件函式只收得下固定列舉、整數與內部編號。

不是「記得不要放」，而是「放不進來」：函式簽名沒有任何文字參數可以接內容，類別與原因傳入列舉以外的值會立刻出錯。
"""
import inspect
import logging

import pytest

from app.models.script import DECISION_CATEGORIES, REMOVERS, REVIEW_CATEGORIES, REVIEW_VERDICTS
from app.observability import script_events
from app.observability.json_logging import ALLOWED_FIELDS

LOG_FUNCTIONS = {
    name: function for name, function in inspect.getmembers(script_events, inspect.isfunction)
    if name.startswith("log_") and function.__module__ == script_events.__name__
}
# 允許出現在簽名裡的參數：內部編號、整數、固定列舉（以名稱區分），以及鍵名（只接固定列舉的營運限制鍵）
ALLOWED_PARAMETERS = {
    "user_id", "total_chars", "flagged", "stage", "layer", "categories", "model", "verdict", "attempt", "reason", "key",
    "category", "remover",             # 審核與下架（rpg-script-publishing）：固定類別與下架者的列舉，傳入列舉以外的值會立刻出錯
    "reason_count", "text_length", "count", "result",     # 檢舉（rpg-script-reports）：整數與固定列舉的結案結果
}


@pytest.fixture(autouse=True)
def _info_level(caplog):
    """INFO 層級的事件也要被收集（先設定層級，再呼叫事件函式）。"""
    caplog.set_level(logging.INFO, logger="app.script")


def capture(caplog):
    return [record for record in caplog.records if record.name == "app.script"]


def test_every_logging_function_takes_only_ids_integers_or_enumerations():
    assert LOG_FUNCTIONS, "找不到事件函式"
    for name, function in LOG_FUNCTIONS.items():
        parameters = set(inspect.signature(function).parameters)
        assert parameters <= ALLOWED_PARAMETERS, (name, parameters - ALLOWED_PARAMETERS)
        for forbidden in ("nickname", "code", "title", "text", "content", "tag", "tags", "author_name", "message", "raw", "body"):
            assert forbidden not in parameters, (name, forbidden)


def test_every_event_name_is_unique_and_namespaced():
    names = [value for key, value in vars(script_events).items() if key.startswith("EVENT_") and isinstance(value, str)]
    assert len(names) == len(set(names)) == len(script_events.EVENT_NAMES)
    assert all(name.split(".")[0] in {"account", "script", "limits"} for name in names)


def test_the_extra_fields_are_all_in_the_log_whitelist(caplog):
    script_events.log_nickname_set(user_id=1)
    script_events.log_created(user_id=1, total_chars=10)
    script_events.log_updated(user_id=1, total_chars=11)
    script_events.log_version_created(user_id=1, total_chars=12)
    script_events.log_deleted(user_id=1)
    script_events.log_withdrawn(user_id=1)
    script_events.log_author_name_refreshed(user_id=1)
    script_events.log_submitted(user_id=1, flagged=True)
    script_events.log_auto_rejected(user_id=1, stage="submit", layer="rules", categories=["minor_sexual"])
    script_events.log_review(user_id=1, model="some-model", verdict="pass", attempt=1)
    script_events.log_review_unavailable(user_id=1, reason="budget")
    script_events.log_publish_suspended(user_id=1)
    script_events.log_publish_lifted(user_id=1)
    script_events.log_approved(user_id=1)
    script_events.log_rejected(user_id=1, category="other_policy")
    script_events.log_removed(user_id=1, remover="admin", category="minor_sexual")
    script_events.log_restored(user_id=1, remover="author")
    script_events.log_report_submitted(user_id=1, reason_count=2, text_length=30)
    script_events.log_report_limited(user_id=1)
    script_events.log_reports_dismissed(user_id=1, count=3)
    script_events.log_reports_auto_closed(count=2, result="removed")
    script_events.log_limit_invalid(key="script_limit.free")
    script_events.log_limit_changed(key="script_limit.free")
    records = capture(caplog)
    assert {record.event for record in records} == set(script_events.EVENT_NAMES)
    standard = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
    for record in records:
        extra = set(record.__dict__) - standard
        assert extra <= set(ALLOWED_FIELDS) | {"event"}, (record.event, extra - set(ALLOWED_FIELDS))


@pytest.mark.parametrize("stage, layer", [("elsewhere", "rules"), ("save", "somewhere"), ("", ""), (None, None)])
def test_an_unknown_stage_or_layer_is_a_programming_error(stage, layer):
    with pytest.raises(ValueError):
        script_events.log_auto_rejected(user_id=1, stage=stage, layer=layer, categories=["minor_sexual"])


@pytest.mark.parametrize("categories", [[], ["not_a_category"], ["minor_sexual", "free text about the story"], [""]])
def test_categories_outside_the_enumeration_are_refused_so_free_text_cannot_reach_the_log(categories):
    with pytest.raises(ValueError):
        script_events.log_auto_rejected(user_id=1, stage="save", layer="rules", categories=categories)


def test_every_review_category_and_verdict_is_accepted(caplog):
    for category in REVIEW_CATEGORIES:
        script_events.log_auto_rejected(user_id=1, stage="open", layer="model", categories=[category])
    for verdict in REVIEW_VERDICTS:
        script_events.log_review(user_id=1, model=None, verdict=verdict, attempt=1)
    assert len(capture(caplog)) == len(REVIEW_CATEGORIES) + len(REVIEW_VERDICTS)


def test_an_unknown_verdict_or_unavailable_reason_is_refused():
    with pytest.raises(ValueError):
        script_events.log_review(user_id=1, model="m", verdict="maybe", attempt=1)
    with pytest.raises(ValueError):
        script_events.log_review_unavailable(user_id=1, reason="the model said something")


def test_the_rejection_log_keeps_the_categories_sorted_and_joined_without_content(caplog):
    script_events.log_auto_rejected(user_id=7, stage="submit", layer="model", categories=["non_consensual", "minor_sexual", "minor_sexual"])
    (record,) = capture(caplog)
    assert (record.kind, record.outcome, record.reason, record.user_id) == ("submit", "model", "minor_sexual,non_consensual", 7)


# ---- 審核與下架（rpg-script-publishing）：只有內部編號與固定列舉，備註放不進來 ---------------------------------------


def test_the_review_and_takedown_events_have_no_parameter_that_could_carry_a_note():
    for name in ("log_approved", "log_rejected", "log_removed", "log_restored"):
        parameters = set(inspect.signature(LOG_FUNCTIONS[name]).parameters)
        assert not parameters & {"note", "reason", "text", "title", "content", "message", "category_text"}, (name, parameters)


def test_the_review_events_carry_only_the_actor_the_remover_and_the_fixed_category(caplog):
    script_events.log_approved(user_id=3)
    script_events.log_rejected(user_id=3, category="other_policy")
    script_events.log_removed(user_id=3, remover="admin", category="real_person_sexual")
    script_events.log_removed(user_id=4, remover="author")
    script_events.log_restored(user_id=3, remover="admin")
    approved, rejected, removed_by_admin, removed_by_author, restored = capture(caplog)
    assert (approved.event, approved.user_id) == (script_events.EVENT_APPROVED, 3)
    assert (rejected.event, rejected.reason) == (script_events.EVENT_REJECTED, "other_policy")
    assert (removed_by_admin.event, removed_by_admin.kind, removed_by_admin.reason) == (script_events.EVENT_REMOVED, "admin", "real_person_sexual")
    assert (removed_by_author.kind, removed_by_author.reason, removed_by_author.user_id) == ("author", None, 4)
    assert (restored.event, restored.kind) == (script_events.EVENT_RESTORED, "admin")


def test_every_decision_category_and_both_removers_are_accepted(caplog):
    for category in DECISION_CATEGORIES:
        script_events.log_rejected(user_id=1, category=category)
        script_events.log_removed(user_id=1, remover="admin", category=category)
    for remover in REMOVERS:
        script_events.log_restored(user_id=1, remover=remover)
    assert len(capture(caplog)) == 2 * len(DECISION_CATEGORIES) + len(REMOVERS)


@pytest.mark.parametrize("category", ["", "free text with a reviewer note", "OTHER_POLICY", None])
def test_a_category_outside_the_enumeration_is_refused_so_a_note_cannot_reach_the_log(category):
    with pytest.raises(ValueError):
        script_events.log_rejected(user_id=1, category=category)


@pytest.mark.parametrize("category", ["", "free text with a reviewer note", "OTHER_POLICY"])
def test_a_takedown_category_outside_the_enumeration_is_refused_but_none_means_no_category(category):
    with pytest.raises(ValueError):
        script_events.log_removed(user_id=1, remover="admin", category=category)
    script_events.log_removed(user_id=1, remover="author", category=None)          # 作者自行下架沒有類別


# ---- 檢舉（rpg-script-reports）：只有內部編號、整數與固定列舉，文字與識別碼放不進來 ---------------------------------


def test_the_report_events_have_no_parameter_that_could_carry_text_or_an_identifier():
    for name in ("log_report_submitted", "log_report_limited", "log_reports_dismissed", "log_reports_auto_closed"):
        parameters = set(inspect.signature(LOG_FUNCTIONS[name]).parameters)
        assert not parameters & {
            "text", "note", "reasons", "reason", "title", "content", "message", "version_id", "version", "script", "public_id", "author", "reporter",
        }, (name, parameters)


def test_a_submitted_report_logs_the_reporter_and_two_integers_only(caplog):
    script_events.log_report_submitted(user_id=7, reason_count=3, text_length=120)
    (record,) = capture(caplog)
    assert (record.event, record.user_id, record.count, record.chars) == (script_events.EVENT_REPORT_SUBMITTED, 7, 3, 120)
    standard = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
    assert set(record.__dict__) - standard == {"event", "user_id", "count", "chars"}


def test_a_limited_report_logs_only_the_user_id_at_warning_level(caplog):
    script_events.log_report_limited(user_id=7)
    (record,) = capture(caplog)
    assert (record.event, record.user_id, record.levelno) == (script_events.EVENT_REPORT_LIMITED, 7, logging.WARNING)
    assert set(record.__dict__) - set(logging.makeLogRecord({}).__dict__) - {"message", "asctime"} == {"event", "user_id"}


def test_a_dismissal_logs_the_admin_and_the_count_only(caplog):
    script_events.log_reports_dismissed(user_id=9, count=4)
    (record,) = capture(caplog)
    assert (record.event, record.user_id, record.count) == (script_events.EVENT_REPORTS_DISMISSED, 9, 4)


@pytest.mark.parametrize("result", ["removed", "superseded"])
def test_an_auto_close_logs_the_count_and_the_fixed_result(caplog, result):
    script_events.log_reports_auto_closed(count=3, result=result)
    (record,) = capture(caplog)
    assert (record.event, record.count, record.outcome) == (script_events.EVENT_REPORTS_AUTO_CLOSED, 3, result)
    assert not hasattr(record, "user_id") or record.user_id is None                # 系統自動結案，事件本身不帶任何人（請求內由日誌過濾器補上目前的使用者）


def test_an_auto_close_of_zero_reports_logs_nothing(caplog):
    """只在 `count > 0` 時記錄：這個條件在函式內，呼叫端不可能漏掉。"""
    script_events.log_reports_auto_closed(count=0, result="removed")
    script_events.log_reports_auto_closed(count=0, result="superseded")
    assert capture(caplog) == []


@pytest.mark.parametrize("result", ["dismissed", "REMOVED", "", "some free text", None])
def test_an_auto_close_result_outside_the_enumeration_is_refused(result):
    """「不成立」是管理員的決定（另有事件），不屬於系統自動結案；列舉以外的值立即出錯，自由文字進不了日誌。"""
    with pytest.raises(ValueError):
        script_events.log_reports_auto_closed(count=1, result=result)


@pytest.mark.parametrize("value", [-1, True, 1.5, "3", None])
def test_a_count_that_is_not_a_non_negative_integer_is_refused(value):
    with pytest.raises(ValueError):
        script_events.log_report_submitted(user_id=1, reason_count=value, text_length=0)
    with pytest.raises(ValueError):
        script_events.log_report_submitted(user_id=1, reason_count=1, text_length=value)
    with pytest.raises(ValueError):
        script_events.log_reports_dismissed(user_id=1, count=value)
    with pytest.raises(ValueError):
        script_events.log_reports_auto_closed(count=value, result="removed")


@pytest.mark.parametrize("remover", ["", "root", "ADMIN", None])
def test_a_remover_outside_the_enumeration_is_refused(remover):
    with pytest.raises(ValueError):
        script_events.log_removed(user_id=1, remover=remover)
    with pytest.raises(ValueError):
        script_events.log_restored(user_id=1, remover=remover)
