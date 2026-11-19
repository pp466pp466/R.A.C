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
    狀態階段 ── （共用模組 `rpg_state_phase`，修訂的 `RevisionRunner` 也用它）一個 `ping`；`LlmService.run_structured(kind="rpg_state")`；`apply_proposal` 逐項驗證；
                短交易：完成回合（指派輪數）＋寫入世界狀態（版本比對）＋結算預扣＋必要時完結存檔。
                狀態呼叫失敗、版本衝突都不影響文字：回合照常完成並結算，世界狀態不變（`state.updated = false`）。
                文字完整之後客戶端才斷線，收尾照常完成（收尾函式不 `yield`，正常路徑與 `GeneratorExit` 路徑共用）。

事件內容只有白名單欄位：JSON 原文不會送給前端（模型的結構化輸出只經 `apply_proposal` 驗證後以 `state` 送出玩家可見的欄位）。
日誌只經 `rpg_events`（內部編號、整數與列舉）。行動、劇情、世界狀態、被擋文字都不進日誌。

遊戲內提示（safe-mode-upgrade-hint）：開始階段已判定 `plan.hint_eligible`（玩家設定為安全模式開、劇本未綁定、緊急煞車未啟用）與
`plan.hint_input`（行動被規則層判為性內容）；輸出端的條件是**這個回合曾有嘗試因性內容被審查層擋下**（類別 `tier_mismatch`，安全模式的尺度）。
成功完成的回合才嘗試：在收尾的同一個短交易內以 `sessions.safe_hint_at is null` 的條件式更新搶先記錄，更新 1 列才在 `state` 事件的
`notices` 加入 `safe_mode_hint`（沒有文字），所以每個存檔最多一次；被擋、中斷、失敗與被取代的回合沒有紀錄；斷線後才補做的收尾不記錄
（玩家看不到，留給下一次）。提示不改變回合的內容、玩家的設定、預扣的遊戲幣與模型的上下文，也不新增事件種類。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterator

from sqlalchemy.orm import Session

from app.models.llm_call_log import KIND_RPG_STREAM
from app.models.script import CATEGORY_TIER_MISMATCH
from app.observability import rpg_events
from app.repositories.errors import InvalidTurnTransitionError
from app.repositories.session_repository import SessionRepository
from app.services import rpg_prompt
from app.services.current_user import CurrentUser
from app.services.errors import HoldAlreadyRefunded
from app.services.game_service import TurnPlan
from app.services.rpg_notebook_capture import CaptureTarget
from app.services.rpg_runner_base import (
    ATTEMPT_BLOCKED, ATTEMPT_OK, DONE_BLOCKED, DONE_COMPLETE, DONE_ENDED, ERROR_MESSAGES, EVENT_DONE, EVENT_META, EVENT_PING, EVENT_STATE,
    EVENT_TRANSITION, PING_SECONDS, AttemptResult, RunnerBase, RunnerDeps, SseEvent,
)
from app.services.rpg_state_phase import state_event, state_phase, with_safe_mode_hint
from app.services.rpg_stream import FAILURE_BLOCKED, FAILURE_CONFIG, FAILURE_TIMEOUT, StreamRequest

__all__ = ["TurnRunner", "RunnerDeps", "SseEvent", "ERROR_MESSAGES", "PING_SECONDS"]


@dataclass
class _Final:
    status: str
    turn_number: int | None
    coins: int
    state_event: dict[str, Any]
    superseded: bool = False


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
        self._capture_due = False
        self._sexual_output_blocked = False                                  # 這個回合曾有嘗試因性內容被審查層擋下（提示的輸出條件）

    # ---- 對外 ---------------------------------------------------------------------------------

    def capture_target(self) -> CaptureTarget | None:
        """回應關閉之後要不要抓取記事簿（rpg-notebook 的 design D1）：`done` 已被伺服器取走（`yield` 返回）、回合為 `complete`
        （不是 `ended`、被擋、中斷、失敗或被取代）時回傳抓取對象（純資料），否則 `None`。`done` 寫入時客戶端斷線（`GeneratorExit`）、
        從頭就是已完成回合的重送（不經過本類別）都不會設定。每個正常完成的回合都會成立，是否真有待抓取的視窗由 `NotebookCapturer` 判斷。"""
        if not self._capture_due:
            return None
        plan = self._plan
        return CaptureTarget(
            user_id=plan.user_id, session_id=plan.session_id, session_public_id=plan.session_public_id, member_tier=self._current.member_tier,
            content_tier=plan.tier, adult_age=plan.adult_age,
        )

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
                if CATEGORY_TIER_MISMATCH in result.categories:
                    self._sexual_output_blocked = True
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
        final = self._finalize(allow_hint=True)
        if final.superseded:
            yield self._error("turn_superseded")
            return
        yield self._send(EVENT_STATE, final.state_event)
        yield self._send(EVENT_DONE, {
            "status": final.status, "turn_number": final.turn_number, "coins": final.coins, "revision": plan.revision, "free_left": plan.free_left,
        })
        if final.status == DONE_COMPLETE:                                     # 執行到這裡表示 `done` 已被取走；寫入失敗時是 `GeneratorExit`，到不了這一行
            self._capture_due = True

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
                self._finalize(allow_hint=False)                              # 玩家已經看不到：不記錄提示，留給下一次
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

    def _hint_reason(self) -> str | None:
        """這個回合要嘗試記錄提示的原因（`input` 優先於 `output`）；沒有資格或沒有觸發條件時 `None`。"""
        plan = self._plan
        if not plan.hint_eligible:
            return None
        if plan.hint_input:
            return "input"
        return "output" if self._sexual_output_blocked else None

    def _finalize(self, *, allow_hint: bool = True) -> _Final:
        """文字完整之後：狀態呼叫、驗證、一個短交易完成回合並結算。可重複呼叫（只做一次）。
        `allow_hint` 只在玩家還連著線時為真：斷線後補做的收尾不記錄提示（記了也送不到）。"""
        if self._final is not None:
            return self._final
        assert self._narration is not None
        plan, narration = self._plan, self._narration
        self._tx(lambda session: None)                                         # 串流的帳本先寫入（狀態呼叫的帳本由 `LlmService` 自己寫，順序才與實際一致）
        state = state_phase(plan, self._deps, self._marker, narration)
        structured = {"state": {"ok": state.ok, "dropped": state.dropped}}
        hint_reason = self._hint_reason() if allow_hint else None

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
            # 提示：與回合收尾同一個交易，條件式更新搶先記錄；回合沒有完成（交易失敗）就不會留下紀錄
            hinted = hint_reason is not None and SessionRepository(session).claim_safe_hint(plan.user_id, plan.session_public_id, now=self._deps.clock())
            return finish.turn.turn_number, finish.world_updated, finish.ended, wallet.balance(plan.user_id), hinted       # 只帶純值出工作階段

        try:
            turn_number, world_updated, ended, coins, hinted = self._tx(work)
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
        event = state_event(state, world_updated)
        if hinted and hint_reason is not None:
            event = with_safe_mode_hint(event)
            rpg_events.log_safe_mode_hint(user_id=plan.user_id, session_id=plan.session_id, reason=hint_reason)
        self._final = _Final(DONE_ENDED if ended else DONE_COMPLETE, turn_number, coins, event)
        return self._final

