"""service-observability 規格「劇本、暱稱與審查事件只含白名單欄位」的主角部分：事件函式只收得下固定列舉、整數與內部編號。

不是「記得不要放」，而是「放不進來」：函式簽名沒有任何文字參數可以接內容，類別與原因傳入列舉以外的值會立刻出錯。
"""
import inspect
import logging

import pytest

from app.observability import protagonist_events
from app.observability.json_logging import ALLOWED_FIELDS

LOG_FUNCTIONS = {
    name: function for name, function in inspect.getmembers(protagonist_events, inspect.isfunction)
    if name.startswith("log_") and function.__module__ == protagonist_events.__name__
}
ALLOWED_PARAMETERS = {"user_id", "model", "attempt", "remaining", "reason", "used", "limit", "stage", "layer", "categories", "verdict", "tier", "anchor"}
FORBIDDEN_PARAMETERS = (
    "name", "personality", "appearance", "occupation", "speech_style", "hidden_trait", "character", "protagonist", "anchor_text", "text",
    "intro", "content", "message", "raw", "body", "ref", "title",
)


@pytest.fixture(autouse=True)
def _info_level(caplog):
    caplog.set_level(logging.INFO, logger="app.protagonist")


def capture(caplog):
    return [record for record in caplog.records if record.name == "app.protagonist"]


def test_every_logging_function_takes_only_ids_integers_or_enumerations():
    assert LOG_FUNCTIONS, "找不到事件函式"
    for name, function in LOG_FUNCTIONS.items():
        parameters = set(inspect.signature(function).parameters)
        assert parameters <= ALLOWED_PARAMETERS, (name, parameters - ALLOWED_PARAMETERS)
        for forbidden in FORBIDDEN_PARAMETERS:
            assert forbidden not in parameters, (name, forbidden)


def test_every_event_name_is_unique_and_namespaced():
    names = [value for key, value in vars(protagonist_events).items() if key.startswith("EVENT_") and isinstance(value, str)]
    assert len(names) == len(set(names)) == len(protagonist_events.EVENT_NAMES) == 7
    assert all(name.split(".")[0] in {"protagonist", "session"} for name in names)


def test_every_event_is_emitted_and_its_extra_fields_are_all_in_the_log_whitelist(caplog):
    protagonist_events.log_suggested(user_id=1, model="m", attempt=1, remaining=3)
    protagonist_events.log_suggest_unavailable(user_id=1, reason="budget")
    protagonist_events.log_suggest_limited(user_id=1, used=5, limit=5)
    protagonist_events.log_rejected(user_id=1, stage="start", layer="model", categories=["minor_sexual", "other_concern"])
    protagonist_events.log_review(user_id=1, model="m", verdict="flag", attempt=2)
    protagonist_events.log_review_unavailable(user_id=1, reason="degraded")
    protagonist_events.log_session_started(user_id=1, tier="standard", anchor="anchored")
    records = capture(caplog)
    assert {record.event for record in records} == set(protagonist_events.EVENT_NAMES)
    standard = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
    for record in records:
        extra = set(record.__dict__) - standard
        assert extra <= set(ALLOWED_FIELDS) | {"event"}, (record.event, extra - set(ALLOWED_FIELDS))


def test_the_values_are_numbers_or_enumerations(caplog):
    protagonist_events.log_rejected(user_id=7, stage="suggest", layer="rules", categories=["minor_sexual"])
    protagonist_events.log_session_started(user_id=7, tier="safe", anchor="skipped")
    rejected, started = capture(caplog)
    assert (rejected.user_id, rejected.kind, rejected.outcome, rejected.reason) == (7, "suggest", "rules", "minor_sexual")
    assert (started.user_id, started.kind, started.outcome) == (7, "safe", "skipped")


@pytest.mark.parametrize("stage, layer", [("elsewhere", "rules"), ("start", "somewhere"), ("", ""), (None, None)])
def test_an_unknown_stage_or_layer_is_a_programming_error(stage, layer):
    with pytest.raises(ValueError):
        protagonist_events.log_rejected(user_id=1, stage=stage, layer=layer, categories=["minor_sexual"])


@pytest.mark.parametrize("categories", [[], ["not_a_category"], ["minor_sexual", "free text about the protagonist"], [""]])
def test_categories_outside_the_enumeration_are_refused_so_free_text_cannot_reach_the_log(categories):
    with pytest.raises(ValueError):
        protagonist_events.log_rejected(user_id=1, stage="start", layer="rules", categories=categories)


@pytest.mark.parametrize("call", [
    lambda: protagonist_events.log_suggest_unavailable(user_id=1, reason="because the story is sad"),
    lambda: protagonist_events.log_review_unavailable(user_id=1, reason="nope"),
    lambda: protagonist_events.log_review(user_id=1, model="m", verdict="approved", attempt=1),
    lambda: protagonist_events.log_session_started(user_id=1, tier="godmode", anchor="anchored"),
    lambda: protagonist_events.log_session_started(user_id=1, tier="safe", anchor="the player wrote something"),
])
def test_other_enumerations_are_enforced_too(call):
    with pytest.raises(ValueError):
        call()
