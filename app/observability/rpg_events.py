"""RPG 回合引擎與第 0 幕的結構化日誌事件（見 service-observability 規格「RPG 回合與第 0 幕事件只含白名單欄位」與 rpg-turn-engine 的 design D15）。

欄位限於白名單內的「事件名稱、內部使用者編號、存檔的內部整數編號、整數的輪數／嘗試序號／字數／延遲／首字時間／丟棄項目數、
固定列舉的結束狀態／結束原因／被擋類別／第 0 幕狀態、模型名稱」。**絕不含**玩家行動、劇情原文、轉場文字、NPC 名稱與特質、地點、
目標、第 0 幕文字、被擋的文字、冪等鍵、存檔對外識別碼、標記字串或任何玩家文字——這裡的函式根本沒有接收這些的參數，
所以不是「記得不要放」，而是「放不進來」。列舉以外的值與不是整數的數字會立刻出錯，日誌裡因此不會出現自由文字。
"""
from __future__ import annotations

import logging
from typing import Iterable

from app.models.rpg_session import REGEN_KINDS
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
EVENT_NOTEBOOK_CAPTURED = "rpg.notebook_captured"
EVENT_NOTEBOOK_SKIPPED = "rpg.notebook_skipped"
EVENT_NOTEBOOK_ABANDONED = "rpg.notebook_abandoned"
EVENT_NOTEBOOK_FAILED = "rpg.notebook_failed"
EVENT_NOTEBOOK_SAVED = "rpg.notebook_saved"
EVENT_NOTEBOOK_SAVE_REJECTED = "rpg.notebook_save_rejected"
EVENT_REVISION_STARTED = "rpg.revision_started"
EVENT_REVISION_COMPLETED = "rpg.revision_completed"
EVENT_REVISION_FAILED = "rpg.revision_failed"

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

# 記事簿（rpg-notebook 的 design D7；`failed` 的 `validation`、`blocked` 是實作時補上的，與回合的 FAIL_REASONS 對應）
NOTEBOOK_SKIP_REASONS = ("full", "empty", "budget")                  # 已滿（推進標記）、沒有新條目（推進標記）、全站預算用盡（不推進）
NOTEBOOK_ABANDON_REASONS = ("blocked", "too_long", "stale")          # 規則層／洩漏未通過、精簡後仍超過上限、鎖內發現超過上限（皆推進標記）
NOTEBOOK_FAIL_REASONS = ("transient", "timeout", "config", "unavailable", "validation", "blocked")    # 皆不推進標記，由後續回合補做
NOTEBOOK_REJECT_REASONS = ("too_long", "blocked", "conflict", "ended")                                  # 玩家儲存被拒

# 重新生成與編輯最後一輪（rpg-turn-revision 的 design D10）。**不記錄**理由、分類、行動、劇情與快照：函式沒有接收它們的參數
REVISION_FAIL_REASONS = ("blocked", "timeout", "model_failed", "state_failed", "superseded", "disconnected", "internal")


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


# ---- 記事簿（見 rpg-notebook-capture 與 rpg-notebook 規格；欄位只有內部編號、整數與列舉，**沒有任何記事簿、條目、行動或劇情文字**）---------


def log_notebook_captured(
    *, user_id: int, session_id: int, boundary: int, entries: int, longest: int, added: int, total: int, version: int, over_entries: int,
    over_chars: int, calls: int, latency_ms: int, model: str | None, categories: Iterable[str] = (),
) -> None:
    """AI 追加成功：處理到的邊界、實際追加的條數、最長一條的字數、追加的字數（含接在既有內容之後的換行）、追加後的總字數與記事簿版本。

    `over_entries`、`over_chars` 是**超出提示詞要求**（每次 5 條、每條 50 字）的條數與字數（逐條超出的字數加總），條目仍完整寫入、不丟不截，
    記下來只為了調整提示詞。`calls` 是模型呼叫次數（含重試與精簡）。`categories` 是放行但記錄的類別（`other_concern`）。"""
    fields: dict[str, object] = {}
    cats = _categories(categories)
    if cats:
        fields["categories"] = cats
    _info(
        EVENT_NOTEBOOK_CAPTURED, "RPG 記事簿追加", user_id=user_id, session_id=_integer("session_id", session_id), boundary=_integer("boundary", boundary),
        entries=_integer("entries", entries), longest=_integer("longest", longest), added=_integer("added", added), total=_integer("total", total),
        version=_integer("version", version), over_entries=_integer("over_entries", over_entries), over_chars=_integer("over_chars", over_chars),
        attempt=_integer("calls", calls), latency_ms=_integer("latency_ms", latency_ms), model=model, outcome="captured", **fields,
    )


def log_notebook_skipped(
    *, user_id: int, session_id: int, boundary: int, reason: str, total: int | None = None, calls: int = 0, latency_ms: int | None = None,
    model: str | None = None,
) -> None:
    """這個視窗沒有追加也不算失敗：記事簿已滿（`full`，沒有呼叫模型，推進標記）、沒有新條目（`empty`，推進標記）、全站預算用盡（`budget`，不推進標記）。"""
    _info(
        EVENT_NOTEBOOK_SKIPPED, "RPG 記事簿略過", user_id=user_id, session_id=_integer("session_id", session_id), boundary=_integer("boundary", boundary),
        reason=_choice("原因", reason, NOTEBOOK_SKIP_REASONS), total=_optional_integer("total", total), attempt=_integer("calls", calls),
        latency_ms=_optional_integer("latency_ms", latency_ms), model=model, outcome="skipped",
    )


def log_notebook_abandoned(
    *, user_id: int, session_id: int, boundary: int, reason: str, entries: int, total: int, calls: int, latency_ms: int, model: str | None,
    categories: Iterable[str] = (),
) -> None:
    """放棄這個視窗並推進標記：條目未通過規則層或含洩漏（`blocked`，附類別，沒有被擋的文字）、精簡後仍超過上限（`too_long`）、
    鎖內發現追加後會超過上限（`stale`）。`total` 是追加後會有的字數。"""
    fields: dict[str, object] = {}
    cats = _categories(categories)
    if cats:
        fields["categories"] = cats
    _warning(
        EVENT_NOTEBOOK_ABANDONED, "RPG 記事簿放棄", user_id=user_id, session_id=_integer("session_id", session_id), boundary=_integer("boundary", boundary),
        reason=_choice("原因", reason, NOTEBOOK_ABANDON_REASONS), entries=_integer("entries", entries), total=_integer("total", total),
        attempt=_integer("calls", calls), latency_ms=_integer("latency_ms", latency_ms), model=model, outcome="abandoned", **fields,
    )


def log_notebook_failed(
    *, user_id: int, session_id: int, boundary: int | None, reason: str, calls: int, latency_ms: int, model: str | None,
) -> None:
    """抓取失敗，標記**不**推進，由後續回合補做（暫時性失敗、預算逾時、設定錯誤、驗證失敗、供應商擋下、非預期的錯誤）。"""
    _warning(
        EVENT_NOTEBOOK_FAILED, "RPG 記事簿抓取失敗", user_id=user_id, session_id=_integer("session_id", session_id),
        boundary=_optional_integer("boundary", boundary), reason=_choice("原因", reason, NOTEBOOK_FAIL_REASONS), attempt=_integer("calls", calls),
        latency_ms=_integer("latency_ms", latency_ms), model=model, outcome="failed",
    )


def log_notebook_saved(*, user_id: int, session_id: int, version: int, total: int) -> None:
    """玩家儲存了記事簿：新的版本與字數。"""
    _info(
        EVENT_NOTEBOOK_SAVED, "RPG 記事簿玩家儲存", user_id=user_id, session_id=_integer("session_id", session_id), version=_integer("version", version),
        total=_integer("total", total), outcome="saved",
    )


def log_notebook_save_rejected(
    *, user_id: int, session_id: int, reason: str, total: int | None = None, categories: Iterable[str] = (),
) -> None:
    """玩家的儲存被拒：太長（`too_long`）、規則層擋下（`blocked`，附類別，沒有被擋的內容）、版本衝突（`conflict`）、存檔已完結（`ended`）。
    `total` 是送出的字數。"""
    fields: dict[str, object] = {}
    cats = _categories(categories)
    if cats:
        fields["categories"] = cats
    _info(
        EVENT_NOTEBOOK_SAVE_REJECTED, "RPG 記事簿玩家儲存被拒", user_id=user_id, session_id=_integer("session_id", session_id),
        reason=_choice("原因", reason, NOTEBOOK_REJECT_REASONS), total=_optional_integer("total", total), outcome="rejected", **fields,
    )


# ---- 重新生成與編輯最後一輪 -----------------------------------------------------------------------------


def log_revision_started(*, user_id: int, session_id: int, kind: str, attempt: int, is_free: bool, cost: int) -> None:
    """修訂已認領（付費者已預扣）：種類（重新生成或編輯）、認領後的嘗試序號、是否免費、預扣的幣數（免費為 0）。沒有理由與行動。"""
    if not isinstance(is_free, bool):
        raise ValueError(f"is_free 必須是布林：{is_free!r}")
    _info(
        EVENT_REVISION_STARTED, "RPG 修訂開始", user_id=user_id, session_id=_integer("session_id", session_id),
        kind=_choice("種類", kind, REGEN_KINDS), attempt=_integer("attempt", attempt), coins=_integer("cost", cost),
        outcome="free" if is_free else "paid",
    )


def log_revision_completed(
    *, user_id: int, session_id: int, kind: str, attempt: int, chars: int, latency_ms: int, world_updated: bool, ended: bool,
) -> None:
    """修訂成功（新回應已替換舊回應並結算）：字數、延遲、世界狀態是否有變更、存檔是否因此完結。"""
    if not isinstance(world_updated, bool) or not isinstance(ended, bool):
        raise ValueError("world_updated 與 ended 必須是布林")
    _info(
        EVENT_REVISION_COMPLETED, "RPG 修訂完成", user_id=user_id, session_id=_integer("session_id", session_id),
        kind=_choice("種類", kind, REGEN_KINDS), attempt=_integer("attempt", attempt), chars=_integer("chars", chars),
        latency_ms=_integer("latency_ms", latency_ms), outcome="ended" if ended else "complete",
        reason="world_updated" if world_updated else "world_unchanged",
    )


def log_revision_failed(*, user_id: int, session_id: int, kind: str, attempt: int, reason: str) -> None:
    """修訂沒有完成（被擋、逾時、模型失敗、狀態呼叫失敗或世界版本改變、被接手、斷線、非預期錯誤）：保留舊內容、已退款或釋放認領。"""
    _warning(
        EVENT_REVISION_FAILED, "RPG 修訂失敗", user_id=user_id, session_id=_integer("session_id", session_id),
        kind=_choice("種類", kind, REGEN_KINDS), attempt=_integer("attempt", attempt), reason=_choice("原因", reason, REVISION_FAIL_REASONS),
        outcome="failed",
    )
