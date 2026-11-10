"""RPG 回合的串流與收尾：`TurnRunner`（見 rpg-turn-stream、rpg-turn-engine、rpg-output-review 規格與 design D2、D4 至 D9）。

`TurnRunner` 持有開始階段凍結的 `TurnPlan`，產生 Server-Sent Events（`events()` 是產生器，Controller 逐個 `encode()` 送出）。
**自己管理短交易**：串流與狀態呼叫期間沒有任何資料庫交易（產生器在 `after_request` 之後才執行，不能用請求的工作階段，也不碰 `g`、
`current_app`）；每個寫入階段各開一個新的工作階段、提交、關閉。

事件順序（正常）：`meta` → 若干 `token`（`ping` 穿插）→ 一個 `ping`（狀態呼叫前）→ `state` → `done`。

    串流階段 ── OutputGuard 逐句審查（保留一句緩衝，通過才釋放）
      ├ 命中且是第一次 ─► 記下被擋文字、`transition(will_retry=true)`、同一筆預扣重新串流（新的 StreamRun，期限重新計算）
      ├ 命中且是第二次 ─► 回合 `blocked`、退回預扣、`transition(will_retry=false)`、`done(blocked)`
      ├ 模型失敗／逾時 ─► 回合 `interrupted`、退回預扣、`error`
      ├ 客戶端斷線（`GeneratorExit`）─► 上游關閉、回合 `interrupted`、退回預扣
      └ 文字完整且通過 ─► 狀態階段
    狀態階段 ── 一個 `ping`；`LlmService.run_structured(kind="rpg_state")`；`apply_proposal` 逐項驗證；
                短交易：完成回合（指派輪數）＋寫入世界狀態（版本比對）＋結算預扣＋必要時完結存檔。
                狀態呼叫失敗、版本衝突都不影響文字：回合照常完成並結算，世界狀態不變（`state.updated = false`）。
                文字完整之後客戶端才斷線，收尾照常完成（收尾函式不 `yield`，正常路徑與 `GeneratorExit` 路徑共用）。

事件內容只有白名單欄位：JSON 原文不會送給前端（模型的結構化輸出只經 `apply_proposal` 驗證後以 `state` 送出玩家可見的欄位）。
日誌只經 `rpg_events`（內部編號、整數與列舉）。行動、劇情、世界狀態、被擋文字都不進日誌。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterator

from sqlalchemy.orm import Session

from app.models.llm_call_log import KIND_RPG_STATE, KIND_RPG_STREAM
from app.observability import rpg_events
from app.repositories.errors import InvalidTurnTransitionError
from app.repositories.session_repository import SessionRepository
from app.services import rpg_context, rpg_prompt, rpg_world
from app.services.current_user import CurrentUser
from app.services.errors import HoldAlreadyRefunded
from app.services.game_service import TurnPlan
from app.services.rpg_runner_base import (
    ATTEMPT_BLOCKED, ATTEMPT_OK, DONE_BLOCKED, DONE_COMPLETE, DONE_ENDED, ERROR_MESSAGES, EVENT_DONE, EVENT_META, EVENT_PING, EVENT_STATE,
    EVENT_TRANSITION, PING_SECONDS, AttemptResult, RunnerBase, RunnerDeps, SseEvent,
)
from app.services.rpg_stream import FAILURE_BLOCKED, FAILURE_CONFIG, FAILURE_TIMEOUT, StreamRequest

__all__ = ["TurnRunner", "RunnerDeps", "SseEvent", "ERROR_MESSAGES", "PING_SECONDS"]


@dataclass
class _StateOutcome:
    ok: bool
    world: rpg_world.WorldState | None = None
    notices: tuple[rpg_world.Notice, ...] = ()
    dropped: int = 0
    ending: bool = False
    degraded: str | None = None


@dataclass
class _Final:
    status: str
    turn_number: int | None
    coins: int
    state_event: dict[str, Any]
    superseded: bool = False


def _state_event(state: _StateOutcome, updated: bool) -> dict[str, Any]:
    """`state` 事件：驗證後的玩家可見欄位與通知。沒有寫入世界狀態時只有 `updated: false`。"""
    if not updated or state.world is None:
        return {"updated": False, "notices": []}
    world = state.world
    return {
        "updated": True, "elapsed_months": world.elapsed_months, "location": world.current_location, "summary": world.status_summary,
        "goals": [goal["text"] for goal in world.goals.get("active", []) if isinstance(goal.get("text"), str)],
        "notices": [notice.to_dict() for notice in state.notices],
    }


class TurnRunner(RunnerBase):
    def __init__(self, *, plan: TurnPlan, current: CurrentUser, deps: RunnerDeps, marker: str) -> None:
        super().__init__(
            user_id=plan.user_id, kind=KIND_RPG_STREAM, deps=deps, marker=marker, tier=plan.tier, characters=plan.characters,
            adult_age=plan.adult_age,
        )
        self._plan = plan
        self._current = current
        self._final: _Final | None = None
        self._ended_unfinished = False

    # ---- 對外 ---------------------------------------------------------------------------------

    def events(self) -> Iterator[SseEvent]:
        """事件產生器。呼叫端（Controller）逐個送出；客戶端斷線時 WSGI 伺服器會 `close()` 這個產生器。"""
        try:
            yield from self._run()
        except GeneratorExit:
            self._on_close()
            raise
        except Exception:                                                    # noqa: BLE001 — 事件只有列舉，不帶例外內容
            self._on_error()
            yield self._error("internal_error")

    # ---- 產生器本體 -----------------------------------------------------------------------------

    def _run(self) -> Iterator[SseEvent]:
        plan = self._plan
        yield self._send(EVENT_META, {"turn_id": plan.seq, "client_msg_id": plan.client_msg_id, "attempt": plan.attempt, "replayed": False})
        request = StreamRequest(
            model=plan.model, system_instruction=plan.system_instruction, contents=plan.contents,
            max_output_tokens=rpg_prompt.NARRATION_MAX_OUTPUT_TOKENS,
        )
        blocked_count = 0
        while True:
            result: AttemptResult = yield from self._attempt(request)
            if result.kind == ATTEMPT_OK:
                break
            if result.kind == ATTEMPT_BLOCKED:
                blocked_count += 1
                last = blocked_count >= 2
                self._record_blocked(result.categories, final=last)
                yield self._send(EVENT_TRANSITION, {
                    "text": rpg_prompt.transition_text(plan.seq, plan.attempt, blocked_count - 1), "will_retry": not last,
                })
                if last:
                    yield self._send(EVENT_DONE, {"status": DONE_BLOCKED, "turn_number": None, "coins": self._balance()})
                    return
                continue
            code = self._record_failure(result.outcome)
            yield self._error(code)
            return

        yield self._send(EVENT_PING, {})                                      # 開始狀態呼叫之前
        final = self._finalize()
        if final.superseded:
            yield self._error("turn_superseded")
            return
        yield self._send(EVENT_STATE, final.state_event)
        yield self._send(EVENT_DONE, {"status": final.status, "turn_number": final.turn_number, "coins": final.coins})

    # ---- 被擋、失敗與中斷 -------------------------------------------------------------------------

    def _record_blocked(self, categories: tuple[str, ...], *, final: bool) -> None:
        plan = self._plan
        structured = {"blocked": {"categories": list(categories), "auto_retry": True}}

        def work(session: Session) -> None:
            repo = SessionRepository(session)
            if final:
                repo.block_turn(
                    plan.user_id, plan.session_public_id, plan.client_msg_id, partial_response=self._received, attempt=plan.attempt,
                    structured_result=structured,
                )
                self._deps.wallet_factory(session).refund(self._current, plan.hold_key)
            else:
                repo.save_blocked_text(
                    plan.user_id, plan.session_public_id, plan.client_msg_id, text=self._received, structured_result=structured, attempt=plan.attempt,
                )

        self._tx(work)
        self._ended_unfinished = final
        rpg_events.log_turn_blocked(
            user_id=plan.user_id, session_id=plan.session_id, attempt=plan.attempt, categories=categories, auto_retry=True, final=final,
        )

    def _record_failure(self, outcome) -> str:
        """模型失敗（沒有可用的完整文字）：回合 `interrupted`（保留已收到的片段）、退回預扣。回傳 `error` 事件的代碼。"""
        plan = self._plan
        started = bool(outcome and outcome.text_started)
        failure = outcome.failure if outcome else None
        reason = {
            FAILURE_TIMEOUT: "timeout", FAILURE_BLOCKED: "blocked_by_provider", FAILURE_CONFIG: "config",
        }.get(failure, "transient")
        self._interrupt(partial=self._received if started else "")
        if started:
            rpg_events.log_turn_interrupted(
                user_id=plan.user_id, session_id=plan.session_id, attempt=plan.attempt,
                reason="timeout" if reason == "timeout" else "model_failed", chars=len(self._received),
            )
        else:
            rpg_events.log_turn_failed(
                user_id=plan.user_id, session_id=plan.session_id, attempt=plan.attempt, reason=reason, model=outcome.model if outcome else self._plan.model,
            )
        return "generation_timeout" if reason == "timeout" else "generation_failed"

    def _interrupt(self, *, partial: str) -> None:
        """回合 `interrupted`、退回預扣（同一個交易）。回合已被新的嘗試接手（嘗試序號不符）時只退回自己的預扣。"""
        plan = self._plan
        if self._ended_unfinished:
            return

        def work(session: Session) -> None:
            repo = SessionRepository(session)
            try:
                with repo.savepoint():
                    repo.interrupt_turn(
                        plan.user_id, plan.session_public_id, plan.client_msg_id, partial_response=partial, attempt=plan.attempt,
                    )
            except InvalidTurnTransitionError:
                pass
            self._deps.wallet_factory(session).refund(self._current, plan.hold_key)

        self._tx(work)
        self._ended_unfinished = True

    # ---- 斷線與非預期錯誤 -------------------------------------------------------------------------

    def _on_close(self) -> None:
        """客戶端斷線（產生器被關閉）。文字已完整且通過審查：收尾照常完成並結算；否則回合中斷、退回預扣。不得 `yield`。"""
        if self._final is not None or self._ended_unfinished:
            return
        try:
            if self._narration is not None:
                self._finalize()
                return
            plan = self._plan
            self._interrupt(partial=self._received)
            rpg_events.log_turn_interrupted(
                user_id=plan.user_id, session_id=plan.session_id, attempt=plan.attempt, reason="disconnected", chars=len(self._received),
            )
        except Exception:                                                    # noqa: BLE001 — 斷線後沒有人可以通知；預扣由逾時規則退回
            pass

    def _on_error(self) -> None:
        if self._final is not None or self._ended_unfinished:
            return
        try:
            self._interrupt(partial=self._received)
            plan = self._plan
            rpg_events.log_turn_failed(user_id=plan.user_id, session_id=plan.session_id, attempt=plan.attempt, reason="internal", model=self._plan.model)
        except Exception:                                                    # noqa: BLE001
            pass

    # ---- 收尾（不 yield）---------------------------------------------------------------------------

    def _finalize(self) -> _Final:
        """文字完整之後：狀態呼叫、驗證、一個短交易完成回合並結算。可重複呼叫（只做一次）。"""
        if self._final is not None:
            return self._final
        assert self._narration is not None
        plan, narration = self._plan, self._narration
        self._tx(lambda session: None)                                         # 串流的帳本先寫入（狀態呼叫的帳本由 `LlmService` 自己寫，順序才與實際一致）
        state = self._state_phase(narration)
        structured = {"state": {"ok": state.ok, "dropped": state.dropped}}

        def work(session: Session):
            finish = SessionRepository(session).finish_turn(
                plan.user_id, plan.session_public_id, plan.client_msg_id, llm_raw_response=narration, structured_result=structured,
                now=self._deps.clock(), attempt=plan.attempt, world_changes=state.world.to_fields() if state.ok and state.world else None,
                expected_version=plan.version, end_story=state.ending,
            )
            wallet = self._deps.wallet_factory(session)
            try:
                wallet.settle(self._current, plan.hold_key)
            except HoldAlreadyRefunded:                                       # 逾時規則已先退回：文字已送達，不再追討
                pass
            if finish is None:                                                # 不是自己的存檔：開始階段已驗證過，不會發生
                raise RuntimeError("回合收尾找不到存檔")
            return finish.turn.turn_number, finish.world_updated, finish.ended, wallet.balance(plan.user_id)       # 只帶純值出工作階段

        try:
            turn_number, world_updated, ended, coins = self._tx(work)
        except InvalidTurnTransitionError:
            self._final = _Final(DONE_COMPLETE, None, 0, {"updated": False, "notices": []}, superseded=True)
            return self._final
        elapsed_ms = round((self._deps.monotonic() - self._began) * 1000)
        ok = self._outcome_ok
        rpg_events.log_turn_completed(
            user_id=plan.user_id, session_id=plan.session_id, turn_number=turn_number or plan.turn_number, attempt=plan.attempt,
            chars=len(narration), latency_ms=elapsed_ms, first_chunk_ms=ok.first_chunk_ms if ok else None,
            model=ok.model if ok else plan.model, finish_reason=ok.finish_reason if ok else None, dropped=state.dropped, state_ok=state.ok, world_updated=world_updated, ended=ended,
        )
        if not state.ok:
            rpg_events.log_state_failed(
                user_id=plan.user_id, session_id=plan.session_id, turn_number=turn_number or plan.turn_number, reason=state.degraded or "unavailable",
            )
        if ended:
            rpg_events.log_session_ended(
                user_id=plan.user_id, session_id=plan.session_id, turn_number=turn_number or plan.turn_number,
                reason="age" if plan.ending_due else "proposed",
            )
        self._final = _Final(
            DONE_ENDED if ended else DONE_COMPLETE, turn_number, coins, _state_event(state, world_updated),
        )
        return self._final

    def _state_phase(self, narration: str) -> _StateOutcome:
        """狀態呼叫（結構化輸出，`kind=rpg_state`）並逐項驗證。任何失敗都回傳 `ok=False`（文字照常完成，世界狀態不變）。"""
        plan = self._plan
        factory = self._deps.llm_factory
        failed = _StateOutcome(False, ending=plan.ending_due, degraded="unavailable")       # 壽終年齡由 Service 判定，狀態呼叫失敗也照樣完結
        if factory is None:
            return failed
        system, contents = rpg_context.build_state_context(
            script=plan.script, world=plan.world, protagonist=plan.protagonist, action=plan.action, narration=narration,
            turn_number=plan.turn_number, ending_due=plan.ending_due, marker=self._marker,
        )
        try:
            with self._deps.session_factory() as session:
                llm = factory(session)
                result = llm.run_structured(
                    kind=KIND_RPG_STATE, user_id=plan.user_id, conversation_id=None, system_instruction=system, contents=contents,
                    schema=rpg_prompt.STATE_SCHEMA, rules=rpg_prompt.state_rules(self._marker), model=plan.model,
                )
                session.commit()
        except Exception:                                                    # noqa: BLE001 — 狀態呼叫不得拖垮已送達的文字
            return failed
        if not result.ok:
            failed.degraded = result.degraded if result.degraded in rpg_events.STATE_DEGRADED else "unavailable"
            return failed
        proposal = rpg_world.apply_proposal(
            plan.world, result.value, rpg_world.ProposalContext(
                script=plan.script, protagonist=plan.protagonist, turn_number=plan.turn_number, action=plan.action, narration=narration,
                tier=plan.tier, max_months=plan.max_months, ending_due=plan.ending_due, adult_age=plan.adult_age, markers=(self._marker,),
            ),
        )
        return _StateOutcome(True, proposal.world, proposal.notices, proposal.dropped, proposal.ending)

