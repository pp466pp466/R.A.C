"""管理員後台的結構化日誌事件（見 service-observability 規格「後台事件只含固定列舉與內部編號」）。

三種事件，欄位限於白名單內的「事件名稱、內部使用者編號、固定列舉的原因」：
- `admin.access_denied`：非管理員碰了後台（原因固定為 `not_admin`；資訊層級，可用來察覺有人在試探）。
- `admin.reauth_failed`：危險操作重新輸入密碼失敗（原因是嘗試的動作；警告層級）。
- `admin.action`：改變資料的操作成功（原因是動作名稱；資訊層級）。
**絕不含** email、顯示名稱、訊息文字、管理員填寫的原因或任何請求內容——函式根本沒有接收這些的參數。原因是固定列舉，
傳入列舉以外的值會立刻出錯，所以日誌裡不會出現自由文字。與稽核資料表（`admin_audit_logs`）不同：日誌供告警與除錯，
稽核供「誰對誰做了什麼」的追溯。
"""
from __future__ import annotations

import logging

from app.models.admin_audit_log import (
    ACTION_ACCOUNT_UNLOCK, ACTION_CONVERSATION_DELETE, ACTION_USER_CHANGE_TIER, ACTION_USER_SUSPEND,
    ACTION_USER_UNSUSPEND, AUDIT_ACTIONS,
)

logger = logging.getLogger("app.admin")

EVENT_ACCESS_DENIED = "admin.access_denied"
EVENT_REAUTH_FAILED = "admin.reauth_failed"
EVENT_ACTION = "admin.action"

REASON_NOT_ADMIN = "not_admin"

# 需要重新輸入密碼的動作（高風險操作）
DANGEROUS_ACTIONS = (
    ACTION_USER_SUSPEND, ACTION_USER_UNSUSPEND, ACTION_USER_CHANGE_TIER, ACTION_CONVERSATION_DELETE, ACTION_ACCOUNT_UNLOCK,
)


def log_access_denied(*, user_id: int | None) -> None:
    logger.info("後台：非管理員被擋下", extra={"event": EVENT_ACCESS_DENIED, "user_id": user_id, "reason": REASON_NOT_ADMIN})


def log_reauth_failed(*, user_id: int, action: str) -> None:
    if action not in DANGEROUS_ACTIONS:
        raise ValueError(f"不是需要重新輸入密碼的動作：{action!r}")
    logger.warning("後台：重新輸入密碼失敗", extra={"event": EVENT_REAUTH_FAILED, "user_id": user_id, "reason": action})


def log_action(*, user_id: int, action: str) -> None:
    if action not in AUDIT_ACTIONS:
        raise ValueError(f"未知的後台動作：{action!r}")
    logger.info("後台：管理員操作", extra={"event": EVENT_ACTION, "user_id": user_id, "reason": action})
