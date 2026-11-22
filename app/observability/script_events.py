"""劇本創作、審查、審核（核可、退回、下架、恢復上架）、檢舉與營運限制的結構化日誌事件（見 service-observability 規格「劇本、暱稱與審查事件只含白名單欄位」
與「檢舉與匿名保留事件只含白名單欄位」）。

欄位限於白名單內的「事件名稱、內部使用者編號、固定列舉的類別或結果、整數長度、模型名稱與嘗試次數」。**絕不含**暱稱、
短代號、筆名、劇本的任何文字、標題、標籤、申訴內容或模型原文——這裡的函式根本沒有接收這些的參數，所以不是「記得不要放」，
而是「放不進來」。類別與原因都是固定列舉，傳入列舉以外的值會立刻出錯，日誌裡因此不會出現自由文字。

**檢舉事件**（rpg-script-reports）：只有內部使用者編號、整數（理由數量放在 `count`、自寫文字的字數放在 `chars`、結案筆數放在 `count`）與
固定列舉的結果（`outcome`：`removed`／`superseded`）。**沒有**理由陣列的內容、自寫文字、版本與劇本的對外識別碼、標題與筆名，
也沒有被檢舉作者的任何識別——函式沒有接收這些的參數。沿用既有的日誌欄位，沒有新增白名單欄位。
"""
from __future__ import annotations

import logging
from typing import Iterable

from app.models.script import DECISION_CATEGORIES, REMOVERS, REVIEW_CATEGORIES, REVIEW_VERDICTS
from app.models.script_report import CLOSED_REMOVED, CLOSED_SUPERSEDED

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
EVENT_APPROVED = "script.approved"
EVENT_REJECTED = "script.rejected"
EVENT_REMOVED = "script.removed"
EVENT_RESTORED = "script.restored"
EVENT_REPORT_SUBMITTED = "script.report_submitted"
EVENT_REPORT_LIMITED = "script.report_limited"
EVENT_REPORTS_DISMISSED = "script.reports_dismissed"
EVENT_REPORTS_AUTO_CLOSED = "script.reports_auto_closed"
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
# 系統自動結案的結果（固定列舉）：版本被下架或被新版本取代；「不成立」是管理員的決定，另有事件
AUTO_CLOSED_RESULTS = (CLOSED_REMOVED, CLOSED_SUPERSEDED)


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


def log_approved(*, user_id: int) -> None:
    """管理員核可一份劇本（`user_id` 是執行的管理員；沒有任何劇本文字與備註）。"""
    _info(EVENT_APPROVED, "管理員核可劇本", user_id=user_id)


def log_rejected(*, user_id: int, category: str) -> None:
    """管理員人工退回：固定類別（審查類別加 `other_policy`）；函式沒有備註參數，備註放不進日誌。"""
    _check_category(category)
    _info(EVENT_REJECTED, "管理員退回劇本", user_id=user_id, reason=category)


def log_removed(*, user_id: int, remover: str, category: str | None = None) -> None:
    """劇本下架：下架者（`author`／`admin`）與固定類別（作者自行下架沒有類別）；`user_id` 是執行下架的人。"""
    _check_remover(remover)
    if category is not None:
        _check_category(category)
    _info(EVENT_REMOVED, "劇本下架", user_id=user_id, kind=remover, reason=category)


def log_restored(*, user_id: int, remover: str) -> None:
    """劇本恢復上架：`remover` 是原本的下架者，也就是這次的恢復者（作者恢復自己下架的、管理員恢復自己下架的）。"""
    _check_remover(remover)
    _info(EVENT_RESTORED, "劇本恢復上架", user_id=user_id, kind=remover)


def log_report_submitted(*, user_id: int, reason_count: int, text_length: int) -> None:
    """玩家送出一筆檢舉（`user_id` 是檢舉人）：只有理由的數量與自寫文字的字數（整數）；沒有理由的內容、文字、版本與劇本的任何識別。"""
    _check_count("reason_count", reason_count)
    _check_count("text_length", text_length)
    _info(EVENT_REPORT_SUBMITTED, "玩家檢舉劇本", user_id=user_id, count=reason_count, chars=text_length)


def log_report_limited(*, user_id: int) -> None:
    """檢舉被每日上限擋下（`user_id` 是檢舉人）；不記上限與今日筆數。"""
    logger.warning("檢舉被每日上限擋下", extra={"event": EVENT_REPORT_LIMITED, "user_id": user_id})


def log_reports_dismissed(*, user_id: int, count: int) -> None:
    """管理員把一個版本未結案的檢舉結為「不成立」（`user_id` 是執行的管理員）：只有結案筆數。"""
    _check_count("count", count)
    _info(EVENT_REPORTS_DISMISSED, "管理員結案檢舉（不成立）", user_id=user_id, count=count)


def log_reports_auto_closed(*, count: int, result: str) -> None:
    """版本離開上架（下架或被新版本取代）時系統自動結案：結案筆數與結果（固定列舉）。**只在 `count > 0` 時記錄**：
    沒有未結案的檢舉就不產生事件（這個條件在函式內，呼叫端不可能漏掉）。"""
    if result not in AUTO_CLOSED_RESULTS:
        raise ValueError(f"結案結果必須是固定列舉：{result!r}")
    _check_count("count", count)
    if count > 0:
        _info(EVENT_REPORTS_AUTO_CLOSED, "系統自動結案檢舉", count=count, outcome=result)


def _check_count(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} 必須是非負整數。")


def _check_category(category: str) -> None:
    if category not in DECISION_CATEGORIES:
        raise ValueError(f"類別必須是固定列舉：{category!r}")


def _check_remover(remover: str) -> None:
    if remover not in REMOVERS:
        raise ValueError(f"下架者必須是固定列舉：{remover!r}")


def log_limit_invalid(*, key: str) -> None:
    """資料庫中的限制缺值或不合法，改用保底預設；只記鍵名（固定列舉）。"""
    logger.warning("營運限制缺值或不合法，使用保底預設", extra={"event": EVENT_LIMIT_INVALID, "reason": key})


def log_limit_changed(*, key: str) -> None:
    _info(EVENT_LIMIT_CHANGED, "營運限制已修改", reason=key)
