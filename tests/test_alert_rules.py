"""operational-alerts 規格：五項告警的門檻、邊界與狀態轉換（純函式，不碰資料庫與時鐘）。"""
from datetime import datetime, timedelta, timezone

import pytest

from app.models.alert_state import SEVERITY_CRITICAL, SEVERITY_WARNING, STATE_FIRING, STATE_OK
from app.services import alert_rules as rules
from app.services.alert_rules import (
    ACTION_ESCALATE, ACTION_FIRING, ACTION_LOWER, ACTION_NONE, ACTION_REMIND, ACTION_RESOLVE, Evaluation, PreviousState,
)

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


# ---- 全站用量 -----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("tokens", "calls", "severity"),
    [
        (0, 0, None),
        (799_999, 359, None),                    # 都剛好差一點
        (800_000, 0, SEVERITY_WARNING),          # token 剛好 80%
        (0, 360, SEVERITY_WARNING),              # 呼叫次數剛好 80%
        (999_999, 449, SEVERITY_WARNING),
        (1_000_000, 0, SEVERITY_CRITICAL),       # token 剛好 100%
        (0, 450, SEVERITY_CRITICAL),
        (2_000_000, 10, SEVERITY_CRITICAL),      # 超過
    ],
)
def test_usage_budget_thresholds_at_the_boundaries(tokens, calls, severity):
    result = rules.evaluate_usage_budget(tokens_used=tokens, token_limit=1_000_000, calls_used=calls, call_limit=450)
    assert result.severity == severity and not result.hold and result.key == "usage_budget"
    assert result.threshold == pytest.approx(0.8)


def test_usage_value_is_the_higher_of_the_two_ratios():
    result = rules.evaluate_usage_budget(tokens_used=500_000, token_limit=1_000_000, calls_used=405, call_limit=450)
    assert result.value == pytest.approx(0.9) and result.severity == SEVERITY_WARNING


# ---- 模型錯誤率 ---------------------------------------------------------------------

def counts(**by_status: int) -> dict[str, int]:
    return by_status


@pytest.mark.parametrize(
    ("status_counts", "severity", "hold"),
    [
        (counts(ok=17, rate_limited=3), SEVERITY_WARNING, False),                # 3/20 = 15%
        (counts(ok=18, server_error=2), None, False),                            # 2/20 = 10% 剛好：必須超過
        (counts(ok=90, timeout=10), None, False),                                # 10/100 = 10%
        (counts(ok=89, timeout=11), SEVERITY_WARNING, False),                    # 11/100
        (counts(ok=9, network=1), None, False),                                  # 1/10 = 10%（樣本剛好 10）
        (counts(ok=8, network=2), SEVERITY_WARNING, False),                      # 2/10
        (counts(rate_limited=5), None, True),                                    # 樣本 5：維持原狀，即使全部失敗
        (counts(ok=9), None, True),                                              # 樣本 9
        (counts(), None, True),                                                  # 完全沒有資料
        (counts(ok=10, blocked=10), None, False),                                # 被安全過濾阻擋不算失敗
        (counts(ok=10, config_error=10), None, False),                           # 設定錯誤另有專屬告警
        (counts(ok=5, blocked=5, rate_limited=1), None, False),                  # 1/11
        (counts(ok=5, blocked=5, rate_limited=2), SEVERITY_WARNING, False),      # 2/12
        (counts(ok=10, validation_failed=10), None, False),                      # 輸出驗證失敗不是供應商的錯
    ],
)
def test_error_rate_thresholds_and_sample_floor(status_counts, severity, hold):
    result = rules.evaluate_error_rate(status_counts)
    assert (result.severity, result.hold) == (severity, hold)
    assert result.threshold == pytest.approx(0.1) and result.key == "llm_error_rate"


def test_error_rate_value_is_the_failure_share_of_all_chat_calls():
    assert rules.evaluate_error_rate(counts(ok=17, rate_limited=3)).value == pytest.approx(0.15)


# ---- 設定錯誤 -----------------------------------------------------------------------

@pytest.mark.parametrize(("count", "severity"), [(0, None), (1, SEVERITY_CRITICAL), (7, SEVERITY_CRITICAL)])
def test_any_config_error_is_critical(count, severity):
    result = rules.evaluate_config_errors(count)
    assert result.severity == severity and not result.hold and result.value == count


# ---- 延遲 p95 ----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("samples", "p95", "severity", "hold"),
    [
        (100, 12_000, SEVERITY_WARNING, False),
        (100, 10_001, SEVERITY_WARNING, False),
        (100, 10_000, None, False),              # 剛好 10 秒：必須超過
        (100, 2_000, None, False),
        (20, 30_000, SEVERITY_WARNING, False),   # 樣本剛好 20
        (19, 30_000, None, True),                # 樣本 19：維持原狀
        (0, None, None, True),
        (50, None, None, True),                  # 沒有取到值也不下結論
    ],
)
def test_latency_thresholds_and_sample_floor(samples, p95, severity, hold):
    result = rules.evaluate_latency(sample_count=samples, p95_ms=p95)
    assert (result.severity, result.hold) == (severity, hold)
    assert result.threshold == 10_000 and result.key == "llm_latency_p95"


@pytest.mark.parametrize(
    ("n", "offset"), [(1, 0), (2, 1), (19, 18), (20, 18), (21, 19), (100, 94), (101, 95), (200, 189)],
)
def test_percentile_offset_is_the_nearest_rank_with_integer_arithmetic(n, offset):
    assert rules.percentile_offset(n) == offset
    assert 0 <= rules.percentile_offset(n) < n


def test_a_few_extreme_values_do_not_move_the_p95_but_six_percent_do():
    """規格的兩個情境：100 次呼叫中 6 次 12 秒（p95 = 12 秒），3 次 30 秒（p95 = 2 秒）。"""
    six = sorted([2_000] * 94 + [12_000] * 6)
    three = sorted([2_000] * 97 + [30_000] * 3)
    assert six[rules.percentile_offset(100)] == 12_000 and three[rules.percentile_offset(100)] == 2_000


def test_percentile_offset_rejects_an_empty_sample():
    with pytest.raises(ValueError):
        rules.percentile_offset(0)


# ---- 登入失敗激增 -------------------------------------------------------------------

@pytest.mark.parametrize(("locked", "severity"), [(0, None), (1, None), (4, None), (5, SEVERITY_WARNING), (40, SEVERITY_WARNING)])
def test_lockout_threshold_counts_accounts(locked, severity):
    result = rules.evaluate_lockouts(locked)
    assert result.severity == severity and result.threshold == 5 and result.key == "login_lockouts"


# ---- 狀態轉換 -----------------------------------------------------------------------

def firing(severity=SEVERITY_WARNING, value=0.9) -> Evaluation:
    return Evaluation("usage_budget", severity, value, 0.8)


def calm() -> Evaluation:
    return Evaluation("usage_budget", None, 0.1, 0.8)


def previous(state=STATE_FIRING, severity=SEVERITY_WARNING, notified_ago: timedelta | None = timedelta(minutes=5)) -> PreviousState:
    return PreviousState(state, severity if state == STATE_FIRING else None, None if notified_ago is None else NOW - notified_ago, NOW - timedelta(hours=1))


@pytest.mark.parametrize(
    ("prev", "evaluation", "action"),
    [
        (None, calm(), ACTION_NONE),
        (None, firing(), ACTION_FIRING),                                         # 第一次檢查就成立
        (previous(STATE_OK), calm(), ACTION_NONE),
        (previous(STATE_OK), firing(), ACTION_FIRING),
        (previous(), firing(), ACTION_NONE),                                     # 持續告警：不重複寄
        (previous(), firing(SEVERITY_CRITICAL), ACTION_ESCALATE),
        (previous(severity=SEVERITY_CRITICAL), firing(SEVERITY_WARNING), ACTION_LOWER),   # 降級只更新、不寄信
        (previous(severity=SEVERITY_CRITICAL), firing(SEVERITY_CRITICAL), ACTION_NONE),
        (previous(), calm(), ACTION_RESOLVE),
        (previous(severity=SEVERITY_CRITICAL), calm(), ACTION_RESOLVE),
        (previous(notified_ago=timedelta(hours=6)), firing(), ACTION_REMIND),    # 剛好滿 6 小時
        (previous(notified_ago=timedelta(hours=6) - timedelta(seconds=1)), firing(), ACTION_NONE),
        (previous(notified_ago=timedelta(days=3)), firing(), ACTION_REMIND),
        (previous(notified_ago=None), firing(), ACTION_REMIND),                  # 從未成功通知過（寄信曾經失敗）
        (previous(notified_ago=timedelta(hours=7), severity=SEVERITY_CRITICAL), firing(SEVERITY_WARNING), ACTION_REMIND),
    ],
)
def test_decide_covers_every_transition(prev, evaluation, action):
    assert rules.decide(prev, evaluation, NOW) == action


@pytest.mark.parametrize("prev", [None, previous(STATE_OK), previous(), previous(severity=SEVERITY_CRITICAL, notified_ago=timedelta(days=1))])
def test_an_insufficient_sample_never_changes_anything(prev):
    held = Evaluation("llm_error_rate", None, 0.0, 0.1, hold=True)
    assert rules.decide(prev, held, NOW) == ACTION_NONE


def test_next_state_values_for_each_action():
    ev = firing(SEVERITY_CRITICAL)
    assert rules.next_state(ACTION_FIRING, ev, NOW) == rules.StateValues(STATE_FIRING, SEVERITY_CRITICAL, NOW, NOW)
    assert rules.next_state(ACTION_ESCALATE, ev, NOW) == rules.StateValues(STATE_FIRING, SEVERITY_CRITICAL, None, NOW)
    assert rules.next_state(ACTION_REMIND, ev, NOW) == rules.StateValues(STATE_FIRING, SEVERITY_CRITICAL, None, NOW)
    assert rules.next_state(ACTION_RESOLVE, calm(), NOW) == rules.StateValues(STATE_OK, None, NOW, NOW)
    assert rules.next_state(ACTION_LOWER, firing(SEVERITY_WARNING), NOW) == rules.StateValues(STATE_FIRING, SEVERITY_WARNING, None, None)
    assert rules.next_state(ACTION_NONE, ev, NOW) is None


def test_only_four_actions_send_mail():
    assert set(rules.NOTIFYING_ACTIONS) == {ACTION_FIRING, ACTION_ESCALATE, ACTION_REMIND, ACTION_RESOLVE}
    assert ACTION_LOWER not in rules.NOTIFYING_ACTIONS and ACTION_NONE not in rules.NOTIFYING_ACTIONS


def test_the_documented_constants_are_the_ones_in_the_spec():
    assert (rules.ERROR_WINDOW, rules.CONFIG_ERROR_WINDOW, rules.LATENCY_WINDOW) == (timedelta(minutes=5), timedelta(minutes=5), timedelta(minutes=15))
    assert (rules.ERROR_MIN_SAMPLE, rules.LATENCY_MIN_SAMPLE, rules.LOCKOUT_THRESHOLD) == (10, 20, 5)
    assert rules.REMINDER_INTERVAL == timedelta(hours=6) and rules.LATENCY_P95_THRESHOLD_MS == 10_000
    assert set(rules.ERROR_RATE_FAILURE_STATUSES) == {"rate_limited", "timeout", "server_error", "network"}
