"""service-observability 規格：後台事件只含固定列舉與內部編號，不含任何文字內容。"""
import inspect

import pytest

from app.models.admin_audit_log import AUDIT_ACTIONS
from app.observability.admin_events import (
    DANGEROUS_ACTIONS, EVENT_ACCESS_DENIED, EVENT_ACTION, EVENT_REAUTH_FAILED, log_access_denied, log_action,
    log_reauth_failed,
)
from app.observability.json_logging import ALLOWED_FIELDS

BASE_FIELDS = {"time", "level", "logger", "message"}


def fields(line: dict) -> dict:
    return {key: value for key, value in line.items() if key not in BASE_FIELDS}


def test_a_denied_access_logs_the_event_the_internal_id_and_the_fixed_reason(log_lines):
    log_access_denied(user_id=7)
    [line] = log_lines()
    assert line["level"] == "INFO"
    assert fields(line) == {"event": EVENT_ACCESS_DENIED, "user_id": 7, "reason": "not_admin"}


def test_a_denied_access_may_have_no_user(log_lines):
    log_access_denied(user_id=None)
    assert "user_id" not in fields(log_lines()[0])                # 沒有使用者時日誌格式器省略該欄位


@pytest.mark.parametrize("action", DANGEROUS_ACTIONS)
def test_a_failed_reauthentication_logs_the_attempted_action_as_a_warning(log_lines, action):
    log_reauth_failed(user_id=3, action=action)
    [line] = log_lines()
    assert line["level"] == "WARNING"
    assert fields(line) == {"event": EVENT_REAUTH_FAILED, "user_id": 3, "reason": action}


@pytest.mark.parametrize("action", ["user.lookup", "conversation.read", "failures.read", "reauth.failed", "x", ""])
def test_only_the_dangerous_actions_can_fail_reauthentication(log_lines, action):
    with pytest.raises(ValueError):
        log_reauth_failed(user_id=3, action=action)
    assert log_lines() == []


def test_the_dangerous_actions_are_exactly_the_five_that_need_a_password():
    assert set(DANGEROUS_ACTIONS) == {
        "user.suspend", "user.unsuspend", "user.change_tier", "conversation.delete", "account.unlock",
    }


@pytest.mark.parametrize("action", AUDIT_ACTIONS)
def test_every_audit_action_can_be_logged_as_an_action(log_lines, action):
    log_action(user_id=1, action=action)
    [line] = log_lines()
    assert fields(line) == {"event": EVENT_ACTION, "user_id": 1, "reason": action}


def test_a_free_text_action_is_refused_so_no_text_can_reach_the_log(log_lines):
    with pytest.raises(ValueError):
        log_action(user_id=1, action="使用者說了一些話")
    assert log_lines() == []


def test_every_key_the_admin_events_write_is_in_the_whitelist(log_lines):
    log_access_denied(user_id=1)
    log_reauth_failed(user_id=1, action="user.suspend")
    log_action(user_id=1, action="user.suspend")
    for line in log_lines():
        assert set(line) <= BASE_FIELDS | set(ALLOWED_FIELDS), set(line) - BASE_FIELDS - set(ALLOWED_FIELDS)


def test_no_event_function_can_receive_text_or_personal_data():
    allowed = {"user_id", "action"}
    for function in (log_access_denied, log_reauth_failed, log_action):
        assert set(inspect.signature(function).parameters) <= allowed, function.__name__
