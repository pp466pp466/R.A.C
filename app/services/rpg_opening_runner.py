"""第 0 幕（開場）的串流與收尾：`OpeningRunner`（見 rpg-opening 規格與 design D12）。

開始階段（`GameService.begin_opening`）已在請求內以條件式更新取得生成權（`pending → generating`，或接手逾時的 `generating`）；
`OpeningRunner` 持有凍結的 `OpeningPlan`，在產生器內串流、審查，並在自己的短交易內寫回。與回合共用 `RunnerBase`
（`StreamRun`、`OutputGuard`、心跳、帳本緩衝），不同之處：

- **不碰錢包**（第 0 幕不是一輪，不扣遊戲幣）、**不寫回合表**；token 與帳本照常計入全站每日用量（`kind=rpg_opening`）。
- 沒有狀態呼叫：通過審查的文字就是結果，寫回 `ready`。
- 被擋一次 → 自動重生一次（`transition(will_retry=true)`）；第二次仍被擋，或生成失敗（主要與備援模型都失敗）→ 寫回 `fallback`，
  內容取劇本的初始對話（沒有則空字串，介面只顯示背景故事）。
- 客戶端在文字完成之前斷線 → 退回 `pending`（不是 `fallback`），部分文字丟棄，下次進入再生成；文字完整之後才斷線則照常寫回 `ready`。
- 所有寫回都是**條件式 UPDATE**（仍是 `generating` 且憑證 `claimed_at` 相同）：被接手而過時的產生器不會覆蓋新的結果，
  寫回失敗（回傳 False）時送出 `opening_superseded` 錯誤。

事件順序：`meta(kind=opening)` → `token`…（`ping` 穿插）→ `done(opening_state=ready|fallback)`。
"""
from __future__ import annotations

from typing import Iterator

from sqlalchemy.orm import Session

from app.models.llm_call_log import KIND_RPG_OPENING
from app.models.rpg_session import OPENING_FALLBACK, OPENING_READY
from app.observability import rpg_events
from app.repositories.session_repository import SessionRepository
from app.services import rpg_prompt
from app.services.game_service import OpeningPlan
from app.services.rpg_runner_base import (
    ATTEMPT_BLOCKED, ATTEMPT_OK, DONE_COMPLETE, EVENT_DONE, EVENT_META, EVENT_TOKEN, EVENT_TRANSITION, AttemptResult, RunnerBase, RunnerDeps,
    SseEvent,
)
from app.services.rpg_stream import StreamRequest


class OpeningRunner(RunnerBase):
    def __init__(self, *, plan: OpeningPlan, deps: RunnerDeps, marker: str) -> None:
        super().__init__(
            user_id=plan.user_id, kind=KIND_RPG_OPENING, deps=deps, marker=marker, tier=plan.tier, characters=plan.characters,
            adult_age=plan.adult_age,
        )
        self._plan = plan
        self._settled = False                 # 已寫回（ready／fallback）或已退回 pending：之後的斷線與錯誤不再動資料

    # ---- 對外 ---------------------------------------------------------------------------------

    def events(self) -> Iterator[SseEvent]:
        """事件產生器。客戶端斷線時 WSGI 伺服器會 `close()` 這個產生器。"""
        try:
            yield from self._run()
        except GeneratorExit:
            self._on_close()
            raise
        except Exception:                                                    # noqa: BLE001 — 事件只有列舉，不帶例外內容
            self._reset()
            yield self._error("internal_error")

    # ---- 產生器本體 -----------------------------------------------------------------------------

    def _run(self) -> Iterator[SseEvent]:
        plan = self._plan
        yield self._send(EVENT_META, {"kind": "opening", "replayed": False})
        request = StreamRequest(
            model=plan.model, system_instruction=plan.system_instruction, contents=plan.contents,
            max_output_tokens=rpg_prompt.OPENING_MAX_OUTPUT_TOKENS,
        )
        blocked_count = 0
        while True:
            result: AttemptResult = yield from self._attempt(request)
            if result.kind == ATTEMPT_OK:
                if not self._write(OPENING_READY, result.narration):
                    yield self._error("opening_superseded")
                    return
                ok = result.outcome
                rpg_events.log_opening_ready(
                    user_id=plan.user_id, session_id=plan.session_id, chars=len(result.narration),
                    latency_ms=round((self._deps.monotonic() - self._began) * 1000), first_chunk_ms=ok.first_chunk_ms if ok else None,
                    model=ok.model if ok else plan.model,
                )
                yield self._done(OPENING_READY)
                return
            if result.kind == ATTEMPT_BLOCKED:
                blocked_count += 1
                if blocked_count < 2:
                    yield self._send(EVENT_TRANSITION, {"text": rpg_prompt.transition_text(0, 1, 0), "will_retry": True})
                    continue
                rpg_events.log_opening_fallback(
                    user_id=plan.user_id, session_id=plan.session_id, reason="blocked", categories=result.categories,
                )
                yield from self._fallback(discard=True, blocked_count=blocked_count)
                return
            # 失敗：主要與備援模型都沒有產生可用的完整文字
            started = bool(result.outcome and result.outcome.text_started)
            rpg_events.log_opening_fallback(user_id=plan.user_id, session_id=plan.session_id, reason="failed")
            yield from self._fallback(discard=started, blocked_count=0)
            return

    def _fallback(self, *, discard: bool, blocked_count: int) -> Iterator[SseEvent]:
        """改用劇本的初始對話：先寫回（條件式），再通知客戶端。已經送出部分文字時先送 `transition` 讓前端丟棄。"""
        plan = self._plan
        if not self._write(OPENING_FALLBACK, plan.fallback_text):
            yield self._error("opening_superseded")
            return
        if discard:
            yield self._send(EVENT_TRANSITION, {"text": rpg_prompt.transition_text(0, 1, max(blocked_count - 1, 0)), "will_retry": False})
        if plan.fallback_text:
            yield self._send(EVENT_TOKEN, {"text": plan.fallback_text})
        yield self._done(OPENING_FALLBACK)

    def _done(self, state: str) -> SseEvent:
        return self._send(EVENT_DONE, {"status": DONE_COMPLETE, "turn_number": None, "coins": self._balance(), "opening_state": state})

    # ---- 寫回 ---------------------------------------------------------------------------------

    def _write(self, state: str, text: str) -> bool:
        """條件式寫回 `ready`／`fallback`；憑證不符（已被接手）或已不是生成中則回傳 False。"""
        plan = self._plan

        def work(session: Session) -> bool:
            return bool(SessionRepository(session).finish_opening(plan.user_id, plan.session_public_id, claimed_at=plan.claimed_at, state=state, text=text))

        written = self._tx(work)
        self._settled = True
        return written

    def _reset(self) -> None:
        """退回 `pending`、丟棄部分文字（條件同寫回）。重複呼叫無害。"""
        if self._settled:
            return
        plan = self._plan
        try:
            reset = self._tx(lambda session: SessionRepository(session).reset_opening(plan.user_id, plan.session_public_id, claimed_at=plan.claimed_at))
        except Exception:                                                    # noqa: BLE001 — 退回失敗時，逾時規則（90 秒）讓下一個請求接手
            return
        self._settled = True
        if reset:
            rpg_events.log_opening_reset(user_id=plan.user_id, session_id=plan.session_id)

    def _on_close(self) -> None:
        """客戶端斷線。文字已完整且通過審查：照常寫回 `ready`；否則退回 `pending`。不得 `yield`。"""
        if self._settled:
            return
        try:
            if self._narration is not None:
                self._write(OPENING_READY, self._narration)
            else:
                self._reset()
        except Exception:                                                    # noqa: BLE001
            pass
