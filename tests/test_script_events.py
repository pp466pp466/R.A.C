"""service-observability 規格「劇本、暱稱與審查事件只含白名單欄位」：事件函式只收得下固定列舉、整數與內部編號。

不是「記得不要放」，而是「放不進來」：函式簽名沒有任何文字參數可以接內容，類別與原因傳入列舉以外的值會立刻出錯。
"""
import inspect
import logging

import pytest

from app.models.script import REVIEW_CATEGORIES, REVIEW_VERDICTS
from app.observability import script_events
from app.observability.json_logging import ALLOWED_FIELDS

LOG_FUNCTIONS = {
    name: function for name, function in inspect.getmembers(script_events, inspect.isfunction)
    if name.startswith("log_") and function.__module__ == script_events.__name__
}
# 允許出現在簽名裡的參數：內部編號、整數、固定列舉（以名稱區分），以及鍵名（只接固定列舉的營運限制鍵）
ALLOWED_PARAMETERS = {"user_id", "total_chars", "flagged", "stage", "layer", "categories", "model", "verdict", "attempt", "reason", "key"}


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
