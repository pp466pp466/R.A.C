"""營運告警的結構化日誌事件（見 service-observability 規格「告警事件只含固定列舉與數字」與 ops-alerting 的 design D10）。

五種事件，欄位限於白名單內的「事件名稱、告警代碼、嚴重度、整數數值」：
- `alert.firing`：告警進入、升級或提醒（嚴重度寫在 `severity`；警告層級）；
- `alert.resolved`：告警恢復（資訊層級）；
- `alert.delivery_failed`：這一次通知沒有寄出（只有告警代碼；警告層級）；
- `alert.check_failed`：這一項告警的評估失敗，例如資料庫查詢出錯（只有告警代碼；錯誤層級）；
- `alert.check_completed`：`check-alerts` 完整跑完一次的心跳（**只有事件名稱**；資訊層級），平台用它偵測「告警檢查停擺」
  （見 platform-alerts 規格）。只在非預覽、非唯讀且沒有任何一項評估失敗時記錄。
**絕不含**收件人地址、信件內容、使用者資料、模型原文或例外訊息——函式根本沒有接收這些的參數。告警代碼與嚴重度是固定列舉，
傳入列舉以外的值立刻出錯，所以日誌裡不會出現自由文字。預覽模式不呼叫這裡的任何函式。
"""
from __future__ import annotations

import logging

from app.models.alert_state import ALERT_KEYS, ALERT_SEVERITIES

logger = logging.getLogger("app.alerts")

EVENT_FIRING = "alert.firing"
EVENT_RESOLVED = "alert.resolved"
EVENT_DELIVERY_FAILED = "alert.delivery_failed"
EVENT_CHECK_FAILED = "alert.check_failed"
EVENT_CHECK_COMPLETED = "alert.check_completed"


def _check_alert(alert: str) -> None:
    if alert not in ALERT_KEYS:
        raise ValueError(f"未知的告警代碼：{alert!r}")


def log_firing(*, alert: str, severity: str, value: int) -> None:
    _check_alert(alert)
    if severity not in ALERT_SEVERITIES:
        raise ValueError(f"未知的嚴重度：{severity!r}")
    logger.warning("營運告警成立", extra={"event": EVENT_FIRING, "reason": alert, "severity": severity, "count": int(value)})


def log_resolved(*, alert: str, value: int) -> None:
    _check_alert(alert)
    logger.info("營運告警已恢復", extra={"event": EVENT_RESOLVED, "reason": alert, "count": int(value)})


def log_delivery_failed(*, alert: str) -> None:
    _check_alert(alert)
    logger.warning("營運告警的通知沒有寄出", extra={"event": EVENT_DELIVERY_FAILED, "reason": alert})


def log_check_failed(*, alert: str) -> None:
    _check_alert(alert)
    logger.error("營運告警的評估失敗", extra={"event": EVENT_CHECK_FAILED, "reason": alert})


def log_check_completed() -> None:
    logger.info("營運告警檢查完成", extra={"event": EVENT_CHECK_COMPLETED})
