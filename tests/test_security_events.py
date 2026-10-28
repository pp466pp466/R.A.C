"""service-observability 規格：日誌允許欄位新增安全事件名稱；事件只用固定列舉。"""
import json
import logging
import re

import pytest

from app.config import PROJECT_ROOT
from app.observability import security_events
from app.observability.json_logging import ALLOWED_FIELDS, JsonFormatter
from app.observability.security_events import EVENT_NAMES, log_event


def format_record(**extra) -> dict:
    record = logging.LogRecord("app.test", logging.INFO, __file__, 1, "訊息", None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return json.loads(JsonFormatter().format(record))


def test_event_is_an_allowed_log_field():
    assert "event" in ALLOWED_FIELDS
    assert format_record(event="auth.login_success")["event"] == "auth.login_success"


def test_fields_outside_the_allowlist_are_still_dropped():
    payload = format_record(event="auth.login_success", email="a@example.com", password="x", token="t")
    assert set(payload) <= {"time", "level", "logger", "message", *ALLOWED_FIELDS}
    assert "email" not in payload and "password" not in payload and "token" not in payload


def test_log_event_writes_one_json_line_with_the_event_and_user_id(log_lines):
    log_event(security_events.LOGIN_FAILED_BAD_PASSWORD, user_id=42)
    payload = log_lines()[-1]
    assert payload["event"] == "auth.login_failed.bad_password"
    assert payload["user_id"] == 42
    assert payload["level"] == "WARNING"
    assert set(payload) <= {"time", "level", "logger", "message", *ALLOWED_FIELDS}


def test_user_id_is_omitted_when_unknown(log_lines):
    log_event(security_events.LOGIN_FAILED_UNKNOWN_USER)
    payload = log_lines()[-1]
    assert "user_id" not in payload


def test_failures_are_warnings_and_successes_are_info(log_lines):
    log_event(security_events.LOGIN_SUCCESS, user_id=1)
    log_event(security_events.ACCOUNT_LOCKED, user_id=1)
    log_event(security_events.CSRF_REJECTED)
    levels = [line["level"] for line in log_lines()[-3:]]
    assert levels == ["INFO", "WARNING", "WARNING"]


def test_an_unregistered_event_name_is_refused():
    with pytest.raises(ValueError):
        log_event("auth.something_made_up")
    with pytest.raises(ValueError):
        log_event("user alice@example.com failed to log in")


def test_event_names_are_a_fixed_ascii_enumeration_without_free_text():
    assert len(EVENT_NAMES) >= 30
    for name in EVENT_NAMES:
        assert re.fullmatch(r"[a-z_]+(\.[a-z_]+)+", name), f"事件名稱格式不符：{name}"


def test_a_request_id_can_be_given_explicitly_for_events_outside_a_request(log_lines):
    log_event(security_events.MAIL_SEND_FAILED, request_id="req-abcdef123456")
    payload = log_lines()[-1]
    assert payload["request_id"] == "req-abcdef123456"


def test_every_event_constant_used_in_the_code_exists():
    """程式裡以 `security_events.XXX` 引用的常數都必須存在，避免拼錯名稱到執行時才爆。"""
    referenced: set[str] = set()
    for path in (PROJECT_ROOT / "app").rglob("*.py"):
        referenced |= set(re.findall(r"security_events\.([A-Z][A-Z_]+)", path.read_text(encoding="utf-8-sig")))
    missing = {name for name in referenced if not hasattr(security_events, name)}
    assert not missing, f"未定義的事件常數：{sorted(missing)}"
