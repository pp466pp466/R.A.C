"""service-observability 規格：用量事件只含白名單欄位（分類與整數），不含任何文字內容。"""
import inspect

import pytest

from app.observability.json_logging import ALLOWED_FIELDS
from app.observability.usage_events import (
    BUDGET_CALLS, BUDGET_TOKENS, DENIED_REASONS, EVENT_BUDGET_WARNING, EVENT_DENIED, EVENT_SUSPENDED, log_budget_warning,
    log_denied, log_suspended,
)

BASE_FIELDS = {"time", "level", "logger", "message"}


def fields(line: dict) -> dict:
    return {key: value for key, value in line.items() if key not in BASE_FIELDS}


@pytest.mark.parametrize("reason", DENIED_REASONS)
def test_a_denial_logs_the_user_and_the_fixed_reason_only(log_lines, reason):
    log_denied(user_id=7, reason=reason)
    [line] = log_lines()
    assert line["level"] == "INFO"
    assert fields(line) == {"event": EVENT_DENIED, "user_id": 7, "reason": reason}


def test_the_denial_reasons_are_the_five_documented_ones():
    assert set(DENIED_REASONS) == {"rate_limited", "daily_quota", "conversation_limit", "suspended", "global_budget"}


def test_a_free_text_denial_reason_is_refused_so_no_text_can_reach_the_log(log_lines):
    with pytest.raises(ValueError):
        log_denied(user_id=1, reason="使用者說了一些話")
    assert log_lines() == []


@pytest.mark.parametrize("resource", [BUDGET_TOKENS, BUDGET_CALLS])
def test_the_budget_warning_carries_the_resource_and_two_integers(log_lines, resource):
    log_budget_warning(resource=resource, used=850_000, limit=1_000_000)
    [line] = log_lines()
    assert line["level"] == "WARNING"
    assert fields(line) == {"event": EVENT_BUDGET_WARNING, "reason": resource, "used": 850_000, "limit": 1_000_000}
    assert isinstance(line["used"], int) and isinstance(line["limit"], int)


def test_an_unknown_budget_resource_is_refused():
    with pytest.raises(ValueError):
        log_budget_warning(resource="money", used=1, limit=2)


def test_a_suspension_logs_the_user_and_the_reason_code(log_lines):
    log_suspended(user_id=42)
    [line] = log_lines()
    assert line["level"] == "WARNING"
    assert fields(line) == {"event": EVENT_SUSPENDED, "user_id": 42, "reason": "safety_blocks"}


def test_every_key_the_usage_events_write_is_in_the_whitelist(log_lines):
    log_denied(user_id=1, reason="rate_limited")
    log_budget_warning(resource="tokens", used=1, limit=2)
    log_suspended(user_id=1)
    for line in log_lines():
        assert set(line) <= BASE_FIELDS | set(ALLOWED_FIELDS), set(line) - BASE_FIELDS - set(ALLOWED_FIELDS)


def test_the_new_whitelist_fields_are_only_used_by_the_usage_events():
    assert {"used", "limit"} <= set(ALLOWED_FIELDS)


def test_no_event_function_can_receive_text():
    """放不進來比記得不要放更可靠：沒有任何參數是文字內容。"""
    allowed = {"user_id", "reason", "resource", "used", "limit"}
    for function in (log_denied, log_budget_warning, log_suspended):
        assert set(inspect.signature(function).parameters) <= allowed, function.__name__
