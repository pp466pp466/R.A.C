"""劇本創作、審查與營運限制的結構化日誌事件（見 service-observability 規格「劇本、暱稱與審查事件只含白名單欄位」）。

欄位限於白名單內的「事件名稱、內部使用者編號、固定列舉的類別或結果、整數長度、模型名稱與嘗試次數」。**絕不含**暱稱、
短代號、筆名、劇本的任何文字、標題、標籤、申訴內容或模型原文——這裡的函式根本沒有接收這些的參數，所以不是「記得不要放」，
而是「放不進來」。類別與原因都是固定列舉，傳入列舉以外的值會立刻出錯，日誌裡因此不會出現自由文字。
"""
from __future__ import annotations

import logging
from typing import Iterable

from app.models.script import REVIEW_CATEGORIES, REVIEW_VERDICTS

logger = logging.getLogger("app.script")

EVENT_NICKNAME_SET = "account.nickname_set"
EVENT_CREATED = "script.created"
EVENT_UPDATED = "script.updated"
EVENT_VERSION_CREATED = "script.version_created"
EVENT_DELETED = "script.deleted"
EVENT_WITHDRAWN = "script.withdrawn"
EVENT_AUTHOR_NAME_REFRESHED = "script.author_name_refreshed"
EVENT_SUBMITTED = "script.submitted"
EVENT_AUTO_REJECTED = "script.auto_rejected"
EVENT_REVIEW = "script.review"
EVENT_REVIEW_UNAVAILABLE = "script.review_unavailable"
EVENT_PUBLISH_SUSPENDED = "script.publish_suspended"
EVENT_PUBLISH_LIFTED = "script.publish_lifted"
EVENT_LIMIT_INVALID = "limits.invalid"
EVENT_LIMIT_CHANGED = "limits.changed"

EVENT_NAMES = frozenset(
    value for name, value in dict(globals()).items() if name.startswith("EVENT_") and isinstance(value, str)
)

# 拒絕發生的階段與層級（固定列舉）
STAGES = ("save", "submit", "open")
LAYERS = ("rules", "model")
# 審查不可用的原因（固定列舉）
UNAVAILABLE_REASONS = ("not_configured", "budget", "degraded", "blocked")


def _info(event: str, message: str, **fields: object) -> None:
    logger.info(message, extra={"event": event, **fields})


def log_nickname_set(*, user_id: int) -> None:
    _info(EVENT_NICKNAME_SET, "暱稱已設定", user_id=user_id)


def log_created(*, user_id: int, total_chars: int) -> None:
    _info(EVENT_CREATED, "劇本已建立", user_id=user_id, count=total_chars)


def log_updated(*, user_id: int, total_chars: int) -> None:
    _info(EVENT_UPDATED, "劇本草稿已更新", user_id=user_id, count=total_chars)


def log_version_created(*, user_id: int, total_chars: int) -> None:
    _info(EVENT_VERSION_CREATED, "劇本新草稿已建立", user_id=user_id, count=total_chars)


def log_deleted(*, user_id: int) -> None:
    _info(EVENT_DELETED, "劇本草稿已刪除", user_id=user_id)


def log_withdrawn(*, user_id: int) -> None:
    _info(EVENT_WITHDRAWN, "劇本已撤回送審", user_id=user_id)


def log_author_name_refreshed(*, user_id: int) -> None:
    _info(EVENT_AUTHOR_NAME_REFRESHED, "劇本筆名已重新取用", user_id=user_id)


def log_submitted(*, user_id: int, flagged: bool) -> None:
    _info(EVENT_SUBMITTED, "劇本已送審", user_id=user_id, outcome="flag" if flagged else "pass")


def log_auto_rejected(*, user_id: int, stage: str, layer: str, categories: Iterable[str]) -> None:
    """自動拒絕：階段（儲存、送審、開存檔）、層級（規則層、模型層）與類別列舉；沒有任何內容。"""
    if stage not in STAGES or layer not in LAYERS:
        raise ValueError(f"未知的階段或層級：{stage!r}、{layer!r}")
    clean = sorted(set(categories))
    if not clean or any(category not in REVIEW_CATEGORIES for category in clean):
        raise ValueError(f"類別必須是固定列舉：{clean!r}")
    logger.warning(
        "劇本被自動拒絕",
        extra={"event": EVENT_AUTO_REJECTED, "user_id": user_id, "kind": stage, "outcome": layer, "reason": ",".join(clean)},
    )


def log_review(*, user_id: int, model: str | None, verdict: str, attempt: int) -> None:
    """模型層審查的結果（固定列舉）與使用的模型。"""
    if verdict not in REVIEW_VERDICTS:
        raise ValueError(f"未知的審查結果：{verdict!r}")
    _info(EVENT_REVIEW, "劇本模型層審查", user_id=user_id, model=model, outcome=verdict, attempt=attempt)


def log_review_unavailable(*, user_id: int, reason: str) -> None:
    if reason not in UNAVAILABLE_REASONS:
        raise ValueError(f"未知的原因：{reason!r}")
    logger.warning("劇本審查不可用", extra={"event": EVENT_REVIEW_UNAVAILABLE, "user_id": user_id, "reason": reason})


def log_publish_suspended(*, user_id: int) -> None:
    logger.warning("作者因連續被自動拒絕而被暫停發佈", extra={"event": EVENT_PUBLISH_SUSPENDED, "user_id": user_id})


def log_publish_lifted(*, user_id: int) -> None:
    _info(EVENT_PUBLISH_LIFTED, "管理員解除暫停發佈", user_id=user_id)


def log_limit_invalid(*, key: str) -> None:
    """資料庫中的限制缺值或不合法，改用保底預設；只記鍵名（固定列舉）。"""
    logger.warning("營運限制缺值或不合法，使用保底預設", extra={"event": EVENT_LIMIT_INVALID, "reason": key})


def log_limit_changed(*, key: str) -> None:
    _info(EVENT_LIMIT_CHANGED, "營運限制已修改", reason=key)
