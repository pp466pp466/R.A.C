"""回合與第 0 幕共用的串流執行基礎（見 rpg-turn-stream 規格與 design D2、D4、D5、D12）。

`TurnRunner`（回合）與 `OpeningRunner`（第 0 幕）都是「在產生器內自己管理短交易」的串流執行者：串流與狀態呼叫期間沒有資料庫交易
（產生器在 `after_request` 之後才執行，不能用請求的工作階段，也不碰 `g`、`current_app`），每個寫入階段各開一個新的工作階段、提交、關閉。
這裡放兩者共用的東西：

- 事件型別 `SseEvent`（`event:` 一行、單行 JSON 的 `data:` 一行、空行）與錯誤訊息；
- `RunnerDeps`：產生器需要的一切（工作階段工廠、錢包與模型服務工廠、供應商、設定、時鐘），全部在 Controller 建立 Response 之前取出；
- `RunnerBase`：事件小工具、心跳（沒有其他輸出時每 15 秒一個 `ping`）、串流帳本的緩衝與短交易（`_tx`）、斷路查詢；
- `_attempt`：**一次串流嘗試**——開一個 `StreamRun`、以 `OutputGuard` 逐句審查（保留一句緩衝，通過才釋放並送出 `token`）、
  命中時中止上游，回傳 `AttemptResult`（成功、被擋或失敗）。被擋後要不要重生、失敗要怎麼收尾由各自的 Runner 決定。

事件內容只有白名單欄位；日誌只經 `rpg_events`（內部編號、整數與列舉）。行動、劇情、世界狀態、被擋文字都不進日誌。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Iterator

from sqlalchemy.orm import Session

from app.config import LlmSettings
from app.llm.types import StreamingLLMProvider, StreamText
from app.repositories.llm_call_log_repository import LlmCallLogRepository
from app.services.llm_service import LlmService
from app.services.rpg_output_guard import OutputGuard
from app.services.rpg_stream import (
    ABORT_OUTPUT_BLOCKED, StreamLedgerEntry, StreamOutcome, StreamRequest, StreamRunner, write_ledger_entry,
)
from app.services.rpg_world import CharacterAge
from app.services.wallet_service import WalletService

PING_SECONDS = 15.0                  # 沒有其他輸出時每隔多久送一個 ping

# 事件名稱
EVENT_META = "meta"
EVENT_TOKEN = "token"
EVENT_PING = "ping"
EVENT_TRANSITION = "transition"
EVENT_STATE = "state"
EVENT_DONE = "done"
EVENT_ERROR = "error"

# 結束狀態（`done` 事件）
DONE_COMPLETE = "complete"
DONE_BLOCKED = "blocked"
DONE_ENDED = "ended"

# 錯誤代碼與繁體中文訊息（只有類別，不帶任何原因細節）
ERROR_MESSAGES = {
    "generation_timeout": "故事生成逾時了，這一輪沒有扣除遊戲幣，請再試一次。",
    "generation_failed": "故事暫時無法生成，這一輪沒有扣除遊戲幣，請稍後再試。",
    "turn_superseded": "這個回合已經被新的嘗試取代，請重新整理後再試。",
    "opening_superseded": "故事的開場已經由另一次嘗試完成，請重新整理後再看。",
    "internal_error": "發生了預期之外的問題，這一輪沒有扣除遊戲幣，請稍後再試。",
}

# `AttemptResult.kind`
ATTEMPT_OK = "ok"
ATTEMPT_BLOCKED = "blocked"
ATTEMPT_FAILED = "failed"


@dataclass(frozen=True)
class SseEvent:
    name: str
    data: dict[str, Any]

    def encode(self) -> str:
        """`event:` 一行、單行 JSON 的 `data:` 一行、空行。"""
        return f"event: {self.name}\ndata: {json.dumps(self.data, ensure_ascii=False, separators=(',', ':'))}\n\n"


@dataclass(frozen=True)
class RunnerDeps:
    """產生器需要的一切；全部在 Controller 建立 Response 之前就從應用程式取出（產生器內不使用 `g`、`current_app`）。"""

    session_factory: Callable[[], Session]
    wallet_factory: Callable[[Session], WalletService]
    llm_factory: Callable[[Session], LlmService] | None          # 狀態呼叫用；沒有設定時狀態階段一律視為不可用
    provider: StreamingLLMProvider
    settings: LlmSettings
    clock: Callable[[], datetime]
    monotonic: Callable[[], float] = time.monotonic
    ping_seconds: float = PING_SECONDS


@dataclass(frozen=True)
class AttemptResult:
    """一次串流嘗試的結果：成功（`narration` 是完整、通過審查的文字）、被擋（`categories`）或失敗（`outcome` 說明原因）。
    `received` 是這次嘗試迄今收到的全部文字（含尚未釋放與命中的那句）。"""

    kind: str
    outcome: StreamOutcome | None
    narration: str = ""
    categories: tuple[str, ...] = ()
    received: str = ""


class RunnerBase:
    def __init__(
        self, *, user_id: int, kind: str, deps: RunnerDeps, marker: str, tier: str, characters: Iterable[CharacterAge], adult_age: int,
    ) -> None:
        self._user_id = user_id
        self._deps = deps
        self._marker = marker
        self._tier = tier
        self._characters = tuple(characters)
        self._adult_age = adult_age
        self._pending: list[tuple[StreamLedgerEntry, datetime]] = []        # 尚未寫入的串流帳本（下一個短交易一併寫入）
        self._stream = StreamRunner(
            provider=deps.provider, settings=deps.settings, kind=kind, record=self._record_ledger, breaker_open=self._breaker_open,
            user_id=user_id, conversation_id=None, monotonic=deps.monotonic,
        )
        self._began = deps.monotonic()
        self._last_sent = self._began
        self._received = ""                                                  # 迄今收到的全部文字（含尚未釋放與命中的那句）
        self._narration: str | None = None                                   # 完整且通過審查的文字；有值代表可以收尾
        self._outcome_ok: StreamOutcome | None = None                        # 成功嘗試的結果（模型、首字時間、結束原因）

    # ---- 事件小工具 -----------------------------------------------------------------------------

    def _send(self, name: str, data: dict[str, Any]) -> SseEvent:
        self._last_sent = self._deps.monotonic()
        return SseEvent(name, data)

    def _error(self, code: str) -> SseEvent:
        return self._send(EVENT_ERROR, {"code": code, "message": ERROR_MESSAGES[code]})

    def _ping_due(self) -> bool:
        return self._deps.monotonic() - self._last_sent >= self._deps.ping_seconds

    # ---- 帳本與短交易 ---------------------------------------------------------------------------

    def _record_ledger(self, entry: StreamLedgerEntry) -> None:
        self._pending.append((entry, self._deps.clock()))

    def _breaker_open(self, model: str) -> bool:
        """近期這個模型是否逾時或伺服器錯誤（帳本查詢，多個 worker 看到同一份真相）；查詢失敗時視為未開啟，不影響生成。"""
        try:
            with self._deps.session_factory() as session:
                since = self._deps.clock() - timedelta(seconds=self._deps.settings.breaker_seconds)
                return LlmCallLogRepository(session).model_unhealthy_since(model, since)
        except Exception:                                                    # noqa: BLE001
            return False

    def _tx(self, work: Callable[[Session], Any]) -> Any:
        """一個短交易：執行 `work`、寫入待寫的串流帳本、提交；失敗整個回復（帳本保留到下一個交易）。"""
        with self._deps.session_factory() as session:
            try:
                result = work(session)
                flushed = len(self._pending)
                ledger = LlmCallLogRepository(session)
                for entry, at in self._pending[:flushed]:
                    write_ledger_entry(ledger, entry, at)
                session.commit()
            except Exception:
                session.rollback()
                raise
        del self._pending[:flushed]
        return result

    def _balance(self) -> int:
        try:
            return self._tx(lambda session: self._deps.wallet_factory(session).balance(self._user_id))
        except Exception:                                                    # noqa: BLE001 — 餘額只是資訊；失敗不影響結果
            return 0

    # ---- 一次串流嘗試 ---------------------------------------------------------------------------

    def _attempt(self, request: StreamRequest) -> Iterator[SseEvent]:
        """開一個 `StreamRun`、逐句審查並送出 `token`；回傳 `AttemptResult`（用 `result = yield from self._attempt(...)` 取得）。

        上游串流一定關閉（提前離開、斷線與例外都一樣）。文字完整且通過審查時，**先**設定 `self._narration` 再送出最後的 `token`，
        所以最後一個事件送出時客戶端才斷線，收尾也照常完成。"""
        guard = OutputGuard(tier=self._tier, characters=self._characters, marker=self._marker, adult_age=self._adult_age)
        run = self._stream.start(request)
        released: list[str] = []
        hit: tuple[str, ...] = ()
        iterator = iter(run)
        try:
            for item in iterator:
                if isinstance(item, StreamText):
                    result = guard.feed(item.text)
                    self._received = guard.received_text
                    if result.release:
                        released.append(result.release)
                        yield self._send(EVENT_TOKEN, {"text": result.release})
                    if result.blocked:
                        hit = result.categories
                        break
                if self._ping_due():
                    yield self._send(EVENT_PING, {})
        finally:
            if hit:
                run.abort(ABORT_OUTPUT_BLOCKED, hit[0])
            iterator.close()
        self._received = guard.received_text
        outcome = run.outcome
        if not hit and outcome is not None and outcome.ok:
            tail = guard.finish()
            if tail.release:
                released.append(tail.release)
            if not tail.blocked:
                self._outcome_ok = outcome
                self._narration = "".join(released)
                if tail.release:
                    yield self._send(EVENT_TOKEN, {"text": tail.release})
                return AttemptResult(ATTEMPT_OK, outcome, narration=self._narration, received=self._received)
            hit = tail.categories
            if tail.release:
                yield self._send(EVENT_TOKEN, {"text": tail.release})
        if hit:
            return AttemptResult(ATTEMPT_BLOCKED, outcome, categories=hit, received=self._received)
        return AttemptResult(ATTEMPT_FAILED, outcome, received=self._received)
