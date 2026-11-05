"""玩家主角的結構化日誌事件（見 service-observability 規格「劇本、暱稱與審查事件只含白名單欄位」與 rpg-character-creation 的 design D8）。

欄位限於白名單內的「事件名稱、內部使用者編號、固定列舉的類別或結果、整數、模型名稱與嘗試次數」。**絕不含**主角的任何文字
（角色卡、錨定事實）、玩家的自我介紹或模型原文——這裡的函式根本沒有接收這些的參數，所以不是「記得不要放」，而是「放不進來」。
類別、階段、層級與原因都是固定列舉，傳入列舉以外的值會立刻出錯，日誌裡因此不會出現自由文字。
"""
from __future__ import annotations

import logging
from typing import Iterable

from app.models.script import REVIEW_CATEGORIES, REVIEW_VERDICTS
from app.models.user import CONTENT_TIERS

logger = logging.getLogger("app.protagonist")

EVENT_SUGGESTED = "protagonist.suggested"
EVENT_SUGGEST_UNAVAILABLE = "protagonist.suggest_unavailable"
EVENT_SUGGEST_LIMITED = "protagonist.suggest_limited"
EVENT_REJECTED = "protagonist.rejected"
EVENT_REVIEW = "protagonist.review"
EVENT_REVIEW_UNAVAILABLE = "protagonist.review_unavailable"
EVENT_SESSION_STARTED = "session.started"

EVENT_NAMES = frozenset(
    value for name, value in dict(globals()).items() if name.startswith("EVENT_") and isinstance(value, str)
)

# 固定列舉
STAGES = ("suggest", "start")                                      # 被拒絕的階段：生成預覽、開存檔
LAYERS = ("rules", "model")
UNAVAILABLE_REASONS = ("not_configured", "budget", "degraded", "blocked")
ANCHOR_OUTCOMES = ("anchored", "skipped")


def _info(event: str, message: str, **fields: object) -> None:
    logger.info(message, extra={"event": event, **fields})


def log_suggested(*, user_id: int, model: str | None, attempt: int, remaining: int) -> None:
    """一次生成預覽成功：使用的模型、第幾次嘗試與今日剩餘次數（整數）。"""
    _info(EVENT_SUGGESTED, "主角生成預覽", user_id=user_id, model=model, attempt=attempt, count=remaining)


def log_suggest_unavailable(*, user_id: int, reason: str) -> None:
    if reason not in UNAVAILABLE_REASONS:
        raise ValueError(f"未知的原因：{reason!r}")
    logger.warning("主角生成不可用", extra={"event": EVENT_SUGGEST_UNAVAILABLE, "user_id": user_id, "reason": reason})


def log_suggest_limited(*, user_id: int, used: int, limit: int) -> None:
    logger.info("主角生成次數已達今日上限", extra={"event": EVENT_SUGGEST_LIMITED, "user_id": user_id, "used": used, "limit": limit})


def log_rejected(*, user_id: int, stage: str, layer: str, categories: Iterable[str]) -> None:
    """主角被自動拒絕：階段（生成預覽、開存檔）、層級（規則層、模型層）與類別列舉；沒有任何內容。"""
    if stage not in STAGES or layer not in LAYERS:
        raise ValueError(f"未知的階段或層級：{stage!r}、{layer!r}")
    clean = sorted(set(categories))
    if not clean or any(category not in REVIEW_CATEGORIES for category in clean):
        raise ValueError(f"類別必須是固定列舉：{clean!r}")
    logger.warning(
        "主角被自動拒絕",
        extra={"event": EVENT_REJECTED, "user_id": user_id, "kind": stage, "outcome": layer, "reason": ",".join(clean)},
    )


def log_review(*, user_id: int, model: str | None, verdict: str, attempt: int) -> None:
    """主角模型層審查的結果（固定列舉）與使用的模型。"""
    if verdict not in REVIEW_VERDICTS:
        raise ValueError(f"未知的審查結果：{verdict!r}")
    _info(EVENT_REVIEW, "主角模型層審查", user_id=user_id, model=model, outcome=verdict, attempt=attempt)


def log_review_unavailable(*, user_id: int, reason: str) -> None:
    if reason not in UNAVAILABLE_REASONS:
        raise ValueError(f"未知的原因：{reason!r}")
    logger.warning("主角審查不可用", extra={"event": EVENT_REVIEW_UNAVAILABLE, "user_id": user_id, "reason": reason})


def log_session_started(*, user_id: int, tier: str, anchor: str) -> None:
    """開出存檔：內容模式與是否帶有錨定事實（固定列舉）；存檔本身的內部編號不記（沒有需要）。"""
    if tier not in CONTENT_TIERS or anchor not in ANCHOR_OUTCOMES:
        raise ValueError(f"未知的內容模式或錨定事實結果：{tier!r}、{anchor!r}")
    _info(EVENT_SESSION_STARTED, "開存檔", user_id=user_id, kind=tier, outcome=anchor)
