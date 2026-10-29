"""用量事件的結構化日誌（見 service-observability 規格與 AGENTS.md 第 11 節）。

三種事件，欄位限於白名單內的「分類與整數」：事件名稱、內部使用者編號、固定列舉的原因代碼，以及警告事件的用量與上限。
**絕不含**訊息文字、回覆、email 或機密——函式根本沒有接收這些的參數。拒絕事件的原因是固定列舉，傳入其他值會立刻出錯，
所以日誌裡不會出現自由文字。

- `usage.denied`：一則訊息因用量限制被拒絕（資訊層級；使用者一直重試會很多筆，但每筆都很小）。
- `usage.global_budget_warning`：全站今日用量越過上限的 80%（警告層級）。沒有跨 worker 的共享狀態可以「每日只記一次」，
  所以越過門檻後的每個請求都會記一筆，由告警層（C-4c）以時間窗去重。`reason` 指出是 `tokens` 還是 `calls`。
- `cs.chat_suspended`：使用者因一日被安全過濾阻擋達 3 次而被暫停（警告層級，供管理員之後在後台查看）。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("app.usage")

EVENT_DENIED = "usage.denied"
EVENT_BUDGET_WARNING = "usage.global_budget_warning"
EVENT_SUSPENDED = "cs.chat_suspended"

DENIED_RATE_LIMITED = "rate_limited"
DENIED_DAILY_QUOTA = "daily_quota"
DENIED_CONVERSATION_LIMIT = "conversation_limit"
DENIED_SUSPENDED = "suspended"
DENIED_GLOBAL_BUDGET = "global_budget"
DENIED_REASONS = (
    DENIED_RATE_LIMITED, DENIED_DAILY_QUOTA, DENIED_CONVERSATION_LIMIT, DENIED_SUSPENDED, DENIED_GLOBAL_BUDGET,
)

BUDGET_TOKENS = "tokens"
BUDGET_CALLS = "calls"
BUDGET_RESOURCES = (BUDGET_TOKENS, BUDGET_CALLS)

SUSPENDED_SAFETY_BLOCKS = "safety_blocks"


def log_denied(*, user_id: int | None, reason: str) -> None:
    if reason not in DENIED_REASONS:
        raise ValueError(f"未知的拒絕原因：{reason!r}")
    logger.info("用量限制：訊息被拒絕", extra={"event": EVENT_DENIED, "user_id": user_id, "reason": reason})


def log_budget_warning(*, resource: str, used: int, limit: int) -> None:
    if resource not in BUDGET_RESOURCES:
        raise ValueError(f"未知的用量種類：{resource!r}")
    logger.warning(
        "全站每日用量已達上限的 80%",
        extra={"event": EVENT_BUDGET_WARNING, "reason": resource, "used": int(used), "limit": int(limit)},
    )


def log_suspended(*, user_id: int) -> None:
    logger.warning(
        "客服對話因反覆被內容安全過濾阻擋而暫停",
        extra={"event": EVENT_SUSPENDED, "user_id": user_id, "reason": SUSPENDED_SAFETY_BLOCKS},
    )
