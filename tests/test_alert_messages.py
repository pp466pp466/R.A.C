"""operational-alerts 規格「電子郵件的內容」：繁體中文、數值與門檻與台灣時間、沒有任何使用者資料。"""
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from app.models.alert_state import ALERT_KEYS, SEVERITY_CRITICAL, SEVERITY_WARNING
from app.services import alert_messages
from app.services.alert_messages import ALERT_HINTS, ALERT_NAMES, alert_mail, event_value, format_threshold, format_value
from app.services.alert_rules import ACTION_ESCALATE, ACTION_FIRING, ACTION_REMIND, ACTION_RESOLVE

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
SINCE = NOW - timedelta(hours=2)                      # 台灣時間 2026 年 10 月 30 日 18:00


def mail(alert="usage_budget", kind=ACTION_FIRING, severity=SEVERITY_WARNING, value=0.853, threshold=0.8, since=SINCE):
    return alert_mail(alert=alert, kind=kind, severity=severity, value=value, threshold=threshold, since=since, now=NOW)


def test_every_alert_has_a_name_and_a_hint():
    assert set(ALERT_NAMES) == set(ALERT_HINTS) == set(ALERT_KEYS)
    assert all(hint.strip() for hint in ALERT_HINTS.values())


def test_a_firing_mail_has_the_name_severity_value_threshold_start_and_a_hint():
    content = mail()
    assert content.subject == "【智能客服】告警：全站用量（進入告警）"
    for needle in ("全站用量：進入告警", "嚴重度：警告", "目前數值：85.3%（門檻：80%）", "開始時間：2026 年 10 月 30 日 18:00（台灣時間）", ALERT_HINTS["usage_budget"]):
        assert needle in content.body, needle
    assert content.body.endswith("\n") and "\r" not in content.body


@pytest.mark.parametrize(
    ("kind", "label"), [(ACTION_FIRING, "進入告警"), (ACTION_ESCALATE, "升級"), (ACTION_REMIND, "提醒"), (ACTION_RESOLVE, "已恢復")],
)
def test_each_kind_has_its_own_subject_label(kind, label):
    content = mail(kind=kind, severity=None if kind == ACTION_RESOLVE else SEVERITY_CRITICAL)
    assert content.subject == f"【智能客服】告警：全站用量（{label}）"


def test_an_escalation_says_critical():
    assert "嚴重度：嚴重" in mail(kind=ACTION_ESCALATE, severity=SEVERITY_CRITICAL, value=1.02).body


def test_a_resolved_mail_has_no_severity_line_and_says_when_it_recovered():
    body = mail(kind=ACTION_RESOLVE, severity=None, value=0.42).body
    assert "嚴重度" not in body and "目前數值：42.0%" in body
    assert "恢復時間：2026 年 10 月 30 日 20:00（台灣時間）" in body and "不需要處理" in body
    assert ALERT_HINTS["usage_budget"] not in body


@pytest.mark.parametrize(
    ("alert", "value", "threshold", "shown_value", "shown_threshold"),
    [
        ("usage_budget", 0.8, 0.8, "80.0%", "80%"),
        ("llm_error_rate", 0.154, 0.1, "15.4%", "10%"),
        ("llm_config_error", 3.0, 1.0, "3 次", "1 次"),
        ("llm_latency_p95", 12_345.0, 10_000.0, "12.3 秒", "10 秒"),
        ("login_lockouts", 7.0, 5.0, "7 個帳號", "5 個帳號"),
    ],
)
def test_values_and_thresholds_are_written_in_human_units(alert, value, threshold, shown_value, shown_threshold):
    assert format_value(alert, value) == shown_value and format_threshold(alert, threshold) == shown_threshold
    body = mail(alert=alert, value=value, threshold=threshold).body
    assert f"目前數值：{shown_value}（門檻：{shown_threshold}）" in body


@pytest.mark.parametrize(
    ("alert", "value", "expected"),
    [("usage_budget", 0.853, 85), ("llm_error_rate", 0.154, 15), ("llm_config_error", 3.0, 3), ("llm_latency_p95", 12_345.4, 12_345), ("login_lockouts", 7.0, 7)],
)
def test_the_event_value_is_an_integer_in_the_natural_unit(alert, value, expected):
    assert event_value(alert, value) == expected and isinstance(event_value(alert, value), int)


def test_the_mail_is_traditional_chinese_with_no_user_data_urls_or_recipient():
    for alert in ALERT_KEYS:
        content = mail(alert=alert)
        assert "http" not in content.body and "@" not in content.body and "@" not in content.subject
        assert "\n" not in content.subject and "\r" not in content.subject
        assert any("一" <= ch <= "鿿" for ch in content.body)


def test_the_message_function_cannot_even_receive_user_data():
    """函式的參數只有告警代碼、種類、嚴重度、數字與時間：放不進使用者資料、收件人或對話內容。"""
    assert set(inspect.signature(alert_messages.alert_mail).parameters) == {"alert", "kind", "severity", "value", "threshold", "since", "now"}


def test_unknown_alerts_and_kinds_are_rejected():
    with pytest.raises(ValueError):
        mail(alert="made_up")
    with pytest.raises(ValueError):
        mail(kind="shout")
