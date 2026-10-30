"""service-observability 規格「告警事件只含固定列舉與數字」：欄位白名單、列舉外的值被拒絕。"""
import pytest

from app.observability import alert_events
from app.observability.json_logging import ALLOWED_FIELDS


def events(log_lines, name):
    return [line for line in log_lines() if line.get("event") == name]


def test_firing_carries_the_code_the_severity_and_an_integer_only(log_lines):
    alert_events.log_firing(alert="usage_budget", severity="critical", value=102)
    (line,) = events(log_lines, "alert.firing")
    assert (line["reason"], line["severity"], line["count"], line["level"]) == ("usage_budget", "critical", 102, "WARNING")
    assert set(line) <= {"time", "level", "logger", "message", "request_id", "event", "reason", "severity", "count"}


def test_resolved_has_no_severity(log_lines):
    alert_events.log_resolved(alert="llm_error_rate", value=4)
    (line,) = events(log_lines, "alert.resolved")
    assert (line["reason"], line["count"], line["level"]) == ("llm_error_rate", 4, "INFO") and "severity" not in line


def test_delivery_and_check_failures_carry_only_the_code(log_lines):
    alert_events.log_delivery_failed(alert="llm_config_error")
    alert_events.log_check_failed(alert="login_lockouts")
    (failed,) = events(log_lines, "alert.delivery_failed")
    (check,) = events(log_lines, "alert.check_failed")
    assert (failed["reason"], failed["level"]) == ("llm_config_error", "WARNING")
    assert (check["reason"], check["level"]) == ("login_lockouts", "ERROR")
    for line in (failed, check):
        assert "count" not in line and "severity" not in line


@pytest.mark.parametrize(
    "call",
    [
        lambda: alert_events.log_firing(alert="made_up", severity="warning", value=1),
        lambda: alert_events.log_firing(alert="usage_budget", severity="loud", value=1),
        lambda: alert_events.log_resolved(alert="ops@example.com", value=1),
        lambda: alert_events.log_delivery_failed(alert="free text with spaces"),
        lambda: alert_events.log_check_failed(alert=""),
    ],
)
def test_a_value_outside_the_enumeration_fails_immediately(call):
    with pytest.raises(ValueError):
        call()


def test_the_severity_field_is_in_the_log_whitelist():
    assert "severity" in ALLOWED_FIELDS and "count" in ALLOWED_FIELDS


def test_the_heartbeat_has_no_field_other_than_the_event_name(log_lines):
    alert_events.log_check_completed()
    (line,) = events(log_lines, "alert.check_completed")
    assert line["level"] == "INFO"
    assert set(line) <= {"time", "level", "logger", "message", "request_id", "event"}
    assert not {"reason", "severity", "count", "user_id"} & set(line)
