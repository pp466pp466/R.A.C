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
    log_event(security_events.CSRF_REJECTED)
    payload = log_lines()[-1]
    assert "user_id" not in payload


def test_failures_are_warnings_and_successes_are_info(log_lines):
    log_event(security_events.LOGIN_SUCCESS, user_id=1)
    log_event(security_events.ACCOUNT_LOCKED, user_id=1)
    log_event(security_events.CSRF_REJECTED)
    levels = [line["level"] for line in log_lines()[-3:]]
    assert levels == ["INFO", "WARNING", "WARNING"]


AGE_EVENTS = {
    security_events.AGE_CONFIRMED_LATER: ("auth.age_confirmed.later", "INFO"),
    security_events.AGE_GATE_DENIED: ("auth.age_gate_denied", "INFO"),
    security_events.AGE_REJECTED_UNDERAGE: ("auth.age_rejected.underage", "WARNING"),
    security_events.AGE_REJECTED_MISMATCH: ("auth.age_rejected.mismatch", "WARNING"),
    security_events.ACCOUNT_INACTIVE_PURGED: ("account.inactive_purged", "INFO"),
}


def test_the_age_gate_events_are_registered_with_the_expected_levels(log_lines):
    """被年齡閘門擋下是正常的拒絕（資訊）；Google 回呼因未確認年齡被拒絕是異常的流程（警告）。"""
    for constant, (name, level) in AGE_EVENTS.items():
        assert constant == name and name in EVENT_NAMES
        log_event(constant, user_id=5)
        payload = log_lines()[-1]
        assert (payload["event"], payload["level"], payload["user_id"]) == (name, level, 5)


def test_the_missing_adult_declaration_event_is_a_warning_with_only_the_name(log_lines):
    """adult-only-signup：新使用者沒勾「我確認已滿 18 歲」而被拒絕建立帳號時只記事件名稱（此時還沒有帳號，也就沒有內部編號）。"""
    assert security_events.GOOGLE_REJECTED_ADULT == "auth.google_rejected.adult" and "auth.google_rejected.adult" in EVENT_NAMES
    log_event(security_events.GOOGLE_REJECTED_ADULT)
    payload = log_lines()[-1]
    assert (payload["event"], payload["level"]) == ("auth.google_rejected.adult", "WARNING")
    assert "user_id" not in payload
    assert set(payload) <= {"time", "level", "logger", "message", *ALLOWED_FIELDS}


def test_the_underage_erasure_event_carries_only_the_event_name_and_the_internal_id(log_lines):
    """未滿 18 歲被抹除：事件名稱加內部整數編號，沒有 email、生日、年齡區間或暱稱的欄位（日誌欄位白名單也不允許）。"""
    assert security_events.ACCOUNT_UNDERAGE_ERASED == "account.underage_erased" and "account.underage_erased" in EVENT_NAMES
    log_event(security_events.ACCOUNT_UNDERAGE_ERASED, user_id=77)
    payload = log_lines()[-1]
    assert (payload["event"], payload["user_id"]) == ("account.underage_erased", 77)
    assert set(payload) <= {"time", "level", "logger", "message", *ALLOWED_FIELDS}
    assert not {"email", "birth_date", "age_range", "nickname", "display_name"} & set(ALLOWED_FIELDS)


@pytest.mark.parametrize(
    ("code", "event"),
    [("age_not_eligible", "auth.age_rejected.underage"), ("age_mismatch", "auth.age_rejected.mismatch")],
)
def test_a_refused_age_step_logs_one_warning_without_any_input(log_lines, code, event):
    security_events.log_age_rejection(code)
    (payload,) = [line for line in log_lines() if line.get("event")]
    assert (payload["event"], payload["level"]) == (event, "WARNING")
    assert "user_id" not in payload and set(payload) <= {"time", "level", "logger", "message", *ALLOWED_FIELDS}


@pytest.mark.parametrize("code", ["birth_date_required", "age_range_required", "invalid_birth_date", "something_else"])
def test_fixable_field_errors_in_the_age_step_are_not_logged_as_events(log_lines, code):
    security_events.log_age_rejection(code)
    assert [line for line in log_lines() if line.get("event")] == []


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


def test_the_story_preference_event_is_registered_as_an_info_event_with_only_the_name_and_the_id(log_lines):
    """story-preference：事件名稱本身不含值；偏好改變是正常的操作，不是警告。"""
    assert security_events.STORY_PREFERENCE_CHANGED == "account.story_preference_changed" and "account.story_preference_changed" in EVENT_NAMES
    log_event(security_events.STORY_PREFERENCE_CHANGED, user_id=9)
    payload = log_lines()[-1]
    assert (payload["event"], payload["level"], payload["user_id"]) == ("account.story_preference_changed", "INFO", 9)
    assert set(payload) <= {"time", "level", "logger", "message", *ALLOWED_FIELDS}
