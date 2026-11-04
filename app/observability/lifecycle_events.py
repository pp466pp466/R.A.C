"""帳號生命週期與期限清除的結構化日誌事件（見 service-observability 規格「帳號生命週期事件的記錄」）。

欄位限於白名單內的「事件名稱、內部使用者編號、固定列舉的原因、整數筆數」：
- `account.deletion_requested`、`account.deletion_cancelled`、`account.deleted_by_request`：使用者申請、撤銷與被抹除。
- `account.exported`：匯出個人資料，`reason` 是格式（`json` 或 `csv`）。
- `account.deletion_replayed`：備份還原後重新抹除了這個帳號（只有內部編號；預覽不記）。
- `retention.purged`：期限清除某一類完成，`reason` 是類別、`count` 是筆數；`retention.purge_failed`：某一類失敗（警告層級）。
**絕不含** email、名稱、生日、年齡區間、密碼、確認文字、任何對話或模型原文——函式根本沒有接收這些的參數。
列舉以外的值會立刻出錯，所以日誌裡不會出現自由文字。（既有的閒置清除事件 `account.inactive_purged` 在 `security_events.py`。）
"""
from __future__ import annotations

import logging

logger = logging.getLogger("app.lifecycle")

EVENT_DELETION_REQUESTED = "account.deletion_requested"
EVENT_DELETION_CANCELLED = "account.deletion_cancelled"
EVENT_DELETED_BY_REQUEST = "account.deleted_by_request"
EVENT_EXPORTED = "account.exported"
EVENT_DELETION_REPLAYED = "account.deletion_replayed"
EVENT_RETENTION_PURGED = "retention.purged"
EVENT_RETENTION_FAILED = "retention.purge_failed"

EXPORT_FORMATS = ("json", "csv")

CATEGORY_CONVERSATIONS = "conversations"
CATEGORY_LEDGER = "ledger"
CATEGORY_ERROR_DETAIL = "error_detail"
CATEGORY_AUTH_TOKENS = "auth_tokens"
CATEGORY_ADMIN_AUDIT = "admin_audit"
CATEGORY_LOCK_STRIKES = "lock_strikes"
CATEGORY_SESSIONS = "sessions"                 # RPG 存檔（連同回合與記事簿）
CATEGORY_TURN_CONTENT = "turn_content"         # 中斷或被擋回合的部分劇情內容
RETENTION_CATEGORIES = (
    CATEGORY_CONVERSATIONS, CATEGORY_LEDGER, CATEGORY_ERROR_DETAIL, CATEGORY_AUTH_TOKENS, CATEGORY_ADMIN_AUDIT,
    CATEGORY_LOCK_STRIKES, CATEGORY_SESSIONS, CATEGORY_TURN_CONTENT,
)


def log_deletion_requested(*, user_id: int) -> None:
    logger.info("帳號：申請刪除", extra={"event": EVENT_DELETION_REQUESTED, "user_id": user_id})


def log_deletion_cancelled(*, user_id: int) -> None:
    logger.info("帳號：撤銷刪除", extra={"event": EVENT_DELETION_CANCELLED, "user_id": user_id})


def log_deleted_by_request(*, user_id: int) -> None:
    logger.info("帳號：寬限期滿，已抹除", extra={"event": EVENT_DELETED_BY_REQUEST, "user_id": user_id})


def log_deletion_replayed(*, user_id: int) -> None:
    logger.info("帳號：還原後重新抹除", extra={"event": EVENT_DELETION_REPLAYED, "user_id": user_id})


def log_exported(*, user_id: int, fmt: str) -> None:
    if fmt not in EXPORT_FORMATS:
        raise ValueError(f"未知的匯出格式：{fmt!r}")
    logger.info("帳號：匯出個人資料", extra={"event": EVENT_EXPORTED, "user_id": user_id, "reason": fmt})


def log_retention_purged(*, category: str, count: int) -> None:
    if category not in RETENTION_CATEGORIES:
        raise ValueError(f"未知的清除類別：{category!r}")
    logger.info("期限清除：完成一類", extra={"event": EVENT_RETENTION_PURGED, "reason": category, "count": int(count)})


def log_retention_failed(*, category: str) -> None:
    if category not in RETENTION_CATEGORIES:
        raise ValueError(f"未知的清除類別：{category!r}")
    logger.warning("期限清除：一類失敗", extra={"event": EVENT_RETENTION_FAILED, "reason": category})
