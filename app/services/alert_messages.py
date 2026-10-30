"""營運告警信的文案（繁體中文純文字，見 operational-alerts 規格與 ops-alerting 的 design D7）。

與 `mail_messages.py` 相同的規則：文案是常數加 `str.format`，**不使用 Jinja**。這裡顯示的全是系統自己的文字與數字，
**沒有任何使用者資料、對話內容、模型原文或機密**——函式的參數裡根本沒有這些，所以放不進去。
每位收件人各寄一封（`MailSender.send` 一次一個收件人）。
"""
from __future__ import annotations

from datetime import datetime

from app.models.alert_state import (
    ALERT_KEYS, ALERT_LLM_CONFIG_ERROR, ALERT_LLM_ERROR_RATE, ALERT_LLM_LATENCY_P95, ALERT_LOGIN_LOCKOUTS, ALERT_USAGE_BUDGET,
    SEVERITY_CRITICAL, SEVERITY_WARNING,
)
from app.services.alert_rules import ACTION_ESCALATE, ACTION_FIRING, ACTION_REMIND, ACTION_RESOLVE
from app.services.mail_messages import PRODUCT_NAME, MailContent, taiwan_time_text

ALERT_NAMES = {
    ALERT_USAGE_BUDGET: "全站用量",
    ALERT_LLM_ERROR_RATE: "模型錯誤率",
    ALERT_LLM_CONFIG_ERROR: "模型設定錯誤",
    ALERT_LLM_LATENCY_P95: "對話延遲（p95）",
    ALERT_LOGIN_LOCKOUTS: "登入失敗激增",
}

# 處理建議：一句話，告訴值班的人第一步該看哪裡
ALERT_HINTS = {
    ALERT_USAGE_BUDGET: "全站今日的 token 或呼叫次數已接近或達到上限；達到上限時客服會回覆「系統忙碌」。請確認供應商的配額，必要時調整 USAGE_DAILY_TOKEN_LIMIT 與 USAGE_DAILY_CALL_LIMIT。",
    ALERT_LLM_ERROR_RATE: "近 5 分鐘有超過一成的對話呼叫因速率限制、逾時或伺服器錯誤失敗。請查看供應商的服務狀態與配額，備援模型是否已接手。",
    ALERT_LLM_CONFIG_ERROR: "模型供應商回應了設定錯誤（請求不合法或無權限），多半是金鑰被撤銷、過期或模型名稱失效。請立即檢查金鑰與模型設定。",
    ALERT_LLM_LATENCY_P95: "近 15 分鐘完成的對話呼叫，有 5% 以上超過 10 秒。請查看供應商的延遲與是否已觸發逾時備援。",
    ALERT_LOGIN_LOCKOUTS: "目前有多個帳號因連續登入失敗而被鎖定，可能有人在大量試探密碼。請查看入口層與日誌中的登入失敗事件，必要時啟用更嚴格的限流。",
}

SEVERITY_LABELS = {SEVERITY_WARNING: "警告", SEVERITY_CRITICAL: "嚴重"}
KIND_LABELS = {ACTION_FIRING: "進入告警", ACTION_ESCALATE: "升級", ACTION_REMIND: "提醒", ACTION_RESOLVE: "已恢復"}

_KINDS_WITH_SEVERITY = (ACTION_FIRING, ACTION_ESCALATE, ACTION_REMIND)

assert set(ALERT_NAMES) == set(ALERT_HINTS) == set(ALERT_KEYS), "每項告警都要有名稱與處理建議"


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def format_value(alert: str, value: float) -> str:
    """把數值寫成人看得懂的樣子（比例、秒、次數）。"""
    if alert in (ALERT_USAGE_BUDGET, ALERT_LLM_ERROR_RATE):
        return _percent(value)
    if alert == ALERT_LLM_LATENCY_P95:
        return f"{value / 1000:.1f} 秒"
    if alert == ALERT_LLM_CONFIG_ERROR:
        return f"{int(value)} 次"
    return f"{int(value)} 個帳號"


def format_threshold(alert: str, threshold: float) -> str:
    if alert in (ALERT_USAGE_BUDGET, ALERT_LLM_ERROR_RATE):
        return f"{threshold * 100:g}%"
    if alert == ALERT_LLM_LATENCY_P95:
        return f"{threshold / 1000:g} 秒"
    if alert == ALERT_LLM_CONFIG_ERROR:
        return f"{int(threshold)} 次"
    return f"{int(threshold)} 個帳號"


def event_value(alert: str, value: float) -> int:
    """日誌事件的整數數值：比例以百分比（四捨五入）、延遲為毫秒、其餘為筆數。"""
    if alert in (ALERT_USAGE_BUDGET, ALERT_LLM_ERROR_RATE):
        return round(value * 100)
    return round(value)


def alert_mail(
    *,
    alert: str,
    kind: str,
    severity: str | None,
    value: float,
    threshold: float,
    since: datetime | None,
    now: datetime,
) -> MailContent:
    """告警信。`kind` 是 `ACTION_FIRING`、`ACTION_ESCALATE`、`ACTION_REMIND` 或 `ACTION_RESOLVE`；`since` 是這一輪告警的開始時間。"""
    if alert not in ALERT_NAMES:
        raise ValueError(f"未知的告警代碼：{alert!r}")
    if kind not in KIND_LABELS:
        raise ValueError(f"未知的通知種類：{kind!r}")
    name, label = ALERT_NAMES[alert], KIND_LABELS[kind]
    lines = [f"{name}：{label}", ""]
    if kind in _KINDS_WITH_SEVERITY:
        assert severity in SEVERITY_LABELS
        lines.append(f"嚴重度：{SEVERITY_LABELS[severity]}")
    lines.append(f"目前數值：{format_value(alert, value)}（門檻：{format_threshold(alert, threshold)}）")
    if since is not None:
        lines.append(f"開始時間：{taiwan_time_text(since)}")
    if kind == ACTION_RESOLVE:
        lines += [f"恢復時間：{taiwan_time_text(now)}", "", "這項指標已回到正常範圍，不需要處理。"]
    else:
        lines += ["", ALERT_HINTS[alert]]
    lines += ["", f"{PRODUCT_NAME}（營運告警，由系統自動寄出；同一項告警每 6 小時至多提醒一次）"]
    return MailContent(subject=f"【{PRODUCT_NAME}】告警：{name}（{label}）", body="\n".join(lines) + "\n")
