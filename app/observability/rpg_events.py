"""RPG 回合引擎與第 0 幕的結構化日誌事件（見 service-observability 規格「RPG 回合與第 0 幕事件只含白名單欄位」與 rpg-turn-engine 的 design D15）。

欄位限於白名單內的「事件名稱、內部使用者編號、存檔的內部整數編號、整數的輪數／嘗試序號／字數／延遲／首字時間／丟棄項目數、
固定列舉的結束狀態／結束原因／被擋類別／第 0 幕狀態、模型名稱」。**絕不含**玩家行動、劇情原文、轉場文字、NPC 名稱與特質、地點、
目標、第 0 幕文字、被擋的文字、冪等鍵、存檔對外識別碼、標記字串或任何玩家文字——這裡的函式根本沒有接收這些的參數，
所以不是「記得不要放」，而是「放不進來」。列舉以外的值與不是整數的數字會立刻出錯，日誌裡因此不會出現自由文字。
"""
from __future__ import annotations

import logging
from typing import Iterable

from app.models.script import REVIEW_CATEGORIES
from app.models.user import CONTENT_TIERS

logger = logging.getLogger("app.rpg")

EVENT_TURN_STARTED = "rpg.turn_started"
EVENT_TURN_COMPLETED = "rpg.turn_completed"
EVENT_TURN_BLOCKED = "rpg.turn_blocked"
EVENT_TURN_INTERRUPTED = "rpg.turn_interrupted"
EVENT_TURN_FAILED = "rpg.turn_failed"
EVENT_STATE_FAILED = "rpg.state_failed"
EVENT_SESSION_ENDED = "rpg.session_ended"
EVENT_OPENING_STARTED = "rpg.opening_started"
EVENT_OPENING_READY = "rpg.opening_ready"
EVENT_OPENING_FALLBACK = "rpg.opening_fallback"
EVENT_OPENING_RESET = "rpg.opening_reset"
EVENT_OPENING_TAKEN_OVER = "rpg.opening_taken_over"

EVENT_NAMES = frozenset(
    value for name, value in dict(globals()).items() if name.startswith("EVENT_") and isinstance(value, str)
)

# ---- 固定列舉 ---------------------------------------------------------------------------------------

START_NEW = "new"                      # 新增的回合
START_RESTARTED = "restarted"          # 中斷、被擋或逾時之後重送，接手同一筆
STARTS = (START_NEW, START_RESTARTED)

# 被擋的類別：審查層的類別（規則層）加上洩漏類別（輸出審查）
BLOCK_CATEGORIES = (*REVIEW_CATEGORIES, "setting_leak", "marker_leak")

# 回合沒有完成的原因（中斷：文字尚未完成時客戶端斷線、逾時；失敗：模型沒有任何可用的文字）
INTERRUPT_REASONS = ("disconnected", "timeout", "model_failed")
FAIL_REASONS = ("timeout", "transient", "blocked_by_provider", "config", "no_provider", "budget", "internal")

# 狀態呼叫的降級原因（`LlmService` 的列舉）與結果
STATE_DEGRADED = ("validation", "transient", "blocked", "config", "budget", "unavailable")

END_REASONS = ("proposed", "age")      # 存檔完結：模型提議（Service 確認為自訂結局）、壽終年齡

OPENING_END_REASONS = ("disconnected", "failed", "blocked")
OPENING_STATES = ("pending", "generating", "ready", "fallback")


def _integer(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必須是整數：{value!r}")
    return value


def _optional_integer(name: str, value: object) -> int | None:
    return None if value is None else _integer(name, value)


def _choice(name: str, value: object, allowed: Iterable[str]) -> str:
    if value not in tuple(allowed):
        raise ValueError(f"未知的{name}：{value!r}")
    return value                                                                  # type: ignore[return-value]


def _categories(values: Iterable[str]) -> str:
    """被擋類別：固定列舉，排序後以逗號相連（日誌的欄位是單一字串）。"""
    return ",".join(sorted(_choice("類別", value, BLOCK_CATEGORIES) for value in values))


def _info(event: str, message: str, **fields: object) -> None:
    logger.info(message, extra={"event": event, **fields})


def _warning(event: str, message: str, **fields: object) -> None:
    logger.warning(message, extra={"event": event, **fields})


# ---- 回合 -------------------------------------------------------------------------------------------


def log_turn_started(*, user_id: int, session_id: int, turn_number: int, attempt: int, start: str, tier: str, model: str | None) -> None:
    """回合開始（已預扣）：預期輪數、嘗試序號、是新回合或重送、內容模式與首選模型。"""
    _info(
        EVENT_TURN_STARTED, "RPG 回合開始", user_id=user_id, session_id=_integer("session_id", session_id),
        turn_number=_integer("turn_number", turn_number), attempt=_integer("attempt", attempt), outcome=_choice("開始方式", start, STARTS),
        reason=_choice("內容模式", tier, CONTENT_TIERS), model=model,
    )


def log_turn_completed(
    *, user_id: int, session_id: int, turn_number: int, attempt: int, chars: int, latency_ms: int, first_chunk_ms: int | None,
    model: str | None, finish_reason: str | None, dropped: int, state_ok: bool, world_updated: bool, ended: bool,
) -> None:
    """回合完成並結算：字數、延遲、首字時間、模型、狀態呼叫是否成功、被丟棄的提議項目數、世界狀態是否寫入、存檔是否完結。"""
    outcome = "ended" if ended else "complete"
    _info(
        EVENT_TURN_COMPLETED, "RPG 回合完成", user_id=user_id, session_id=_integer("session_id", session_id),
        turn_number=_integer("turn_number", turn_number), attempt=_integer("attempt", attempt), outcome=outcome,
        chars=_integer("chars", chars), latency_ms=_integer("latency_ms", latency_ms),
        first_chunk_ms=_optional_integer("first_chunk_ms", first_chunk_ms), model=model, finish_reason=finish_reason,
        dropped=_integer("dropped", dropped), reason="state_ok" if state_ok else "state_failed" if not world_updated else "state_stale",
    )


def log_turn_blocked(*, user_id: int, session_id: int, attempt: int, categories: Iterable[str], auto_retry: bool, final: bool) -> None:
    """輸出被審查擋下：類別（列舉）、是否將自動重生、是否為最終（第二次）被擋。沒有被擋的文字。"""
    _warning(
        EVENT_TURN_BLOCKED, "RPG 回合輸出被擋", user_id=user_id, session_id=_integer("session_id", session_id),
        attempt=_integer("attempt", attempt), categories=_categories(categories),
        outcome="blocked" if final else "retrying", reason="auto_retry" if auto_retry else "no_retry",
    )


def log_turn_interrupted(*, user_id: int, session_id: int, attempt: int, reason: str, chars: int) -> None:
    _warning(
        EVENT_TURN_INTERRUPTED, "RPG 回合中斷", user_id=user_id, session_id=_integer("session_id", session_id),
        attempt=_integer("attempt", attempt), reason=_choice("原因", reason, INTERRUPT_REASONS), chars=_integer("chars", chars),
    )


def log_turn_failed(*, user_id: int, session_id: int, attempt: int, reason: str, model: str | None) -> None:
    _warning(
        EVENT_TURN_FAILED, "RPG 回合失敗", user_id=user_id, session_id=_integer("session_id", session_id),
        attempt=_integer("attempt", attempt), reason=_choice("原因", reason, FAIL_REASONS), model=model,
    )


def log_state_failed(*, user_id: int, session_id: int, turn_number: int, reason: str) -> None:
    """狀態呼叫失敗：文字已送達、回合仍完成，世界狀態不變。"""
    _warning(
        EVENT_STATE_FAILED, "RPG 狀態呼叫失敗", user_id=user_id, session_id=_integer("session_id", session_id),
        turn_number=_integer("turn_number", turn_number), reason=_choice("原因", reason, STATE_DEGRADED),
    )


def log_session_ended(*, user_id: int, session_id: int, turn_number: int, reason: str) -> None:
    _info(
        EVENT_SESSION_ENDED, "RPG 存檔完結", user_id=user_id, session_id=_integer("session_id", session_id),
        turn_number=_integer("turn_number", turn_number), reason=_choice("原因", reason, END_REASONS),
    )


# ---- 第 0 幕 ----------------------------------------------------------------------------------------


def log_opening_started(*, user_id: int, session_id: int, tier: str, model: str | None) -> None:
    _info(
        EVENT_OPENING_STARTED, "RPG 第 0 幕開始生成", user_id=user_id, session_id=_integer("session_id", session_id),
        reason=_choice("內容模式", tier, CONTENT_TIERS), model=model,
    )


def log_opening_ready(*, user_id: int, session_id: int, chars: int, latency_ms: int, first_chunk_ms: int | None, model: str | None) -> None:
    _info(
        EVENT_OPENING_READY, "RPG 第 0 幕完成", user_id=user_id, session_id=_integer("session_id", session_id), chars=_integer("chars", chars),
        latency_ms=_integer("latency_ms", latency_ms), first_chunk_ms=_optional_integer("first_chunk_ms", first_chunk_ms), model=model,
        outcome="ready",
    )


def log_opening_fallback(*, user_id: int, session_id: int, reason: str, categories: Iterable[str] = ()) -> None:
    """第 0 幕改用劇本的初始對話：生成失敗或兩次被擋（被擋時附類別）。"""
    fields: dict[str, object] = {"reason": _choice("原因", reason, ("failed", "blocked")), "outcome": "fallback"}
    cats = _categories(categories)
    if cats:
        fields["categories"] = cats
    _warning(EVENT_OPENING_FALLBACK, "RPG 第 0 幕退回初始對話", user_id=user_id, session_id=_integer("session_id", session_id), **fields)


def log_opening_reset(*, user_id: int, session_id: int) -> None:
    """客戶端斷線：第 0 幕退回待生成，部分文字丟棄。"""
    _info(
        EVENT_OPENING_RESET, "RPG 第 0 幕退回待生成", user_id=user_id, session_id=_integer("session_id", session_id),
        reason="disconnected", outcome="pending",
    )


def log_opening_taken_over(*, user_id: int, session_id: int) -> None:
    _warning(
        EVENT_OPENING_TAKEN_OVER, "RPG 第 0 幕生成權被接手", user_id=user_id, session_id=_integer("session_id", session_id),
        outcome="generating",
    )
