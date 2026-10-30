"""營運告警的門檻與判斷（見 operational-alerts 規格與 ops-alerting 的 design D1、D3、D4、D5）。

全部是**純函式**：輸入是已經查好的數字與時間，輸出是判斷結果。不碰資料庫、不讀時鐘、不寄信，所以每個邊界
（剛好 10%、第 95 百分位、剛好 6 小時）都能用表格逐一測試（`tests/test_alert_rules.py`）。

門檻是這裡的常數，不是環境變數（design D5）：它們是規範的「建議初值」，要依實際流量調整，調整就是一次有測試的程式變更。
比例一律用 `Fraction` 比較，避免浮點數讓「剛好 10%」被誤判。

每項規則回傳 `Evaluation`：
- `severity` 為 `None` 代表正常；`warning`、`critical` 代表告警；
- `hold` 為真代表樣本不足、**維持原狀**（不因為沒有資料就宣告恢復，也不憑少數樣本就宣告告警）。
`decide` 再依「上一次的狀態」決定這次要做什麼：進入、升級、提醒、恢復、靜默降級，或什麼都不做。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from fractions import Fraction
from typing import Mapping

from app.models.alert_state import (
    ALERT_LLM_CONFIG_ERROR, ALERT_LLM_ERROR_RATE, ALERT_LLM_LATENCY_P95, ALERT_LOGIN_LOCKOUTS, ALERT_USAGE_BUDGET,
    SEVERITY_CRITICAL, SEVERITY_WARNING, STATE_FIRING, STATE_OK,
)
from app.models.llm_call_log import (
    STATUS_CONFIG_ERROR, STATUS_NETWORK, STATUS_RATE_LIMITED, STATUS_SERVER_ERROR, STATUS_TIMEOUT,
)

# ---- 門檻 ------------------------------------------------------------------------------

USAGE_WARNING_RATIO = Fraction(4, 5)             # 全站用量達上限的 80%（AGENTS.md 第 10 節）
USAGE_CRITICAL_RATIO = Fraction(1)               # 達 100%：客服已回覆「系統忙碌」

ERROR_WINDOW = timedelta(minutes=5)
ERROR_RATE_THRESHOLD = Fraction(1, 10)           # 必須「超過」10%
ERROR_MIN_SAMPLE = 10
# 429、逾時、伺服器錯誤與網路錯誤算失敗；被內容安全過濾阻擋是正常行為、設定錯誤另有專屬告警，都不計入分子（仍在分母）
ERROR_RATE_FAILURE_STATUSES = (STATUS_RATE_LIMITED, STATUS_TIMEOUT, STATUS_SERVER_ERROR, STATUS_NETWORK)

CONFIG_ERROR_WINDOW = timedelta(minutes=5)

LATENCY_WINDOW = timedelta(minutes=15)
LATENCY_PERCENTILE = 95
LATENCY_P95_THRESHOLD_MS = 10_000                # 必須「超過」10 秒
LATENCY_MIN_SAMPLE = 20

LOCKOUT_THRESHOLD = 5                            # 目前被鎖定的帳號數；鎖定 15 分鐘，所以等同「近 15 分鐘新增的鎖定」

REMINDER_INTERVAL = timedelta(hours=6)

_SEVERITY_RANK = {None: 0, SEVERITY_WARNING: 1, SEVERITY_CRITICAL: 2}

ACTION_NONE = "none"
ACTION_FIRING = "firing"          # 正常 → 告警：寄「進入」
ACTION_ESCALATE = "escalate"      # 嚴重度升高：寄「升級」
ACTION_REMIND = "remind"          # 持續告警且距上次通知滿 6 小時：寄「提醒」
ACTION_RESOLVE = "resolve"        # 告警 → 正常：寄「已恢復」
ACTION_LOWER = "lower"            # 嚴重度降低但仍在告警：只更新狀態，不寄信（之後再升高才會再通知）
NOTIFYING_ACTIONS = (ACTION_FIRING, ACTION_ESCALATE, ACTION_REMIND, ACTION_RESOLVE)


@dataclass(frozen=True)
class Evaluation:
    key: str
    severity: str | None            # None = 正常
    value: float                    # 比例（0 到 1 以上）、毫秒或筆數；告警信與狀態表的「目前數值」
    threshold: float                # 同一個單位的門檻
    hold: bool = False              # 樣本不足：維持原狀

    @property
    def firing(self) -> bool:
        return self.severity is not None


@dataclass(frozen=True)
class PreviousState:
    state: str                      # STATE_OK 或 STATE_FIRING
    severity: str | None
    last_notified_at: datetime | None
    changed_at: datetime | None = None


@dataclass(frozen=True)
class StateValues:
    """搶佔成功後要寫入的新狀態（`AlertStateRepository.claim`）。`last_notified_at` 為 None 代表不改動（靜默降級）。"""

    state: str
    severity: str | None
    changed_at: datetime | None     # None = 不改動（維持「這個狀態是何時開始的」）
    last_notified_at: datetime | None


# ---- 五項規則 --------------------------------------------------------------------------------

def evaluate_usage_budget(*, tokens_used: int, token_limit: int, calls_used: int, call_limit: int) -> Evaluation:
    """全站今日用量：token 與呼叫次數任一項達 80% 為警告、達 100% 為嚴重；數值取兩者中較高的比例。"""
    ratio = max(Fraction(tokens_used, token_limit), Fraction(calls_used, call_limit))
    if ratio >= USAGE_CRITICAL_RATIO:
        severity = SEVERITY_CRITICAL
    elif ratio >= USAGE_WARNING_RATIO:
        severity = SEVERITY_WARNING
    else:
        severity = None
    return Evaluation(ALERT_USAGE_BUDGET, severity, float(ratio), float(USAGE_WARNING_RATIO))


def evaluate_error_rate(status_counts: Mapping[str, int]) -> Evaluation:
    """近 5 分鐘對話呼叫的失敗比率：（429＋逾時＋伺服器錯誤＋網路錯誤）／全部，超過 10% 為警告；樣本少於 10 次維持原狀。"""
    total = sum(status_counts.values())
    failures = sum(status_counts.get(status, 0) for status in ERROR_RATE_FAILURE_STATUSES)
    if total < ERROR_MIN_SAMPLE:
        return Evaluation(ALERT_LLM_ERROR_RATE, None, float(Fraction(failures, total)) if total else 0.0, float(ERROR_RATE_THRESHOLD), hold=True)
    rate = Fraction(failures, total)
    return Evaluation(
        ALERT_LLM_ERROR_RATE, SEVERITY_WARNING if rate > ERROR_RATE_THRESHOLD else None, float(rate), float(ERROR_RATE_THRESHOLD)
    )


def evaluate_config_errors(config_error_count: int) -> Evaluation:
    """近 5 分鐘出現任何一次設定錯誤（多半是金鑰被撤銷）就是嚴重。"""
    return Evaluation(ALERT_LLM_CONFIG_ERROR, SEVERITY_CRITICAL if config_error_count >= 1 else None, float(config_error_count), 1.0)


def evaluate_latency(*, sample_count: int, p95_ms: int | None) -> Evaluation:
    """近 15 分鐘完成的對話呼叫的 p95 延遲：超過 10 秒為警告；樣本少於 20 次維持原狀。"""
    if sample_count < LATENCY_MIN_SAMPLE or p95_ms is None:
        return Evaluation(ALERT_LLM_LATENCY_P95, None, float(p95_ms or 0), float(LATENCY_P95_THRESHOLD_MS), hold=True)
    return Evaluation(
        ALERT_LLM_LATENCY_P95, SEVERITY_WARNING if p95_ms > LATENCY_P95_THRESHOLD_MS else None, float(p95_ms), float(LATENCY_P95_THRESHOLD_MS)
    )


def evaluate_lockouts(locked_accounts: int) -> Evaluation:
    """目前被鎖定的帳號數達 5 個為警告（同一個帳號被反覆嘗試只算一個）。"""
    return Evaluation(ALERT_LOGIN_LOCKOUTS, SEVERITY_WARNING if locked_accounts >= LOCKOUT_THRESHOLD else None, float(locked_accounts), float(LOCKOUT_THRESHOLD))


def percentile_offset(sample_count: int, percentile: int = LATENCY_PERCENTILE) -> int:
    """最近排名法（nearest rank）：第 `ceil(p·n/100)` 小的值，回傳它由 0 起算的位置（供 `ORDER BY … OFFSET` 使用）。

    全部用整數運算，沒有浮點誤差：n=100、p=95 → 位置 94（第 95 小）；n=20 → 位置 18（第 19 小）。
    """
    if sample_count < 1:
        raise ValueError("樣本數必須至少為 1。")
    return (percentile * sample_count + 99) // 100 - 1


# ---- 狀態轉換 --------------------------------------------------------------------------------

def decide(previous: PreviousState | None, evaluation: Evaluation, now: datetime) -> str:
    """依上一次的狀態與這次的評估，決定要做什麼。樣本不足（hold）一律什麼都不做。"""
    if evaluation.hold:
        return ACTION_NONE
    was_firing = previous is not None and previous.state == STATE_FIRING
    if not was_firing:
        return ACTION_FIRING if evaluation.firing else ACTION_NONE
    if not evaluation.firing:
        return ACTION_RESOLVE
    assert previous is not None
    before, after = _SEVERITY_RANK[previous.severity], _SEVERITY_RANK[evaluation.severity]
    if after > before:
        return ACTION_ESCALATE
    if previous.last_notified_at is None or now - previous.last_notified_at >= REMINDER_INTERVAL:
        return ACTION_REMIND
    return ACTION_LOWER if after < before else ACTION_NONE


def next_state(action: str, evaluation: Evaluation, now: datetime) -> StateValues | None:
    """搶佔成功後要寫入的新狀態；沒有狀態變化的動作（`none`）回傳 None。"""
    if action == ACTION_FIRING:
        return StateValues(STATE_FIRING, evaluation.severity, now, now)
    if action in (ACTION_ESCALATE, ACTION_REMIND):
        return StateValues(STATE_FIRING, evaluation.severity, None, now)
    if action == ACTION_RESOLVE:
        return StateValues(STATE_OK, None, now, now)
    if action == ACTION_LOWER:
        return StateValues(STATE_FIRING, evaluation.severity, None, None)
    return None
