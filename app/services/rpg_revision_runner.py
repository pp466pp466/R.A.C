"""RPG 修訂（重新生成與編輯最後一輪）的串流與收尾：`RevisionRunner`（見 rpg-turn-revision 規格與 design D3、D4、D6、D10）。

開始階段（`GameService.begin_revision`）已在請求內完成驗證、認領（條件式 UPDATE，`attempt` 加 1）、回饋列與（付費者）預扣；
`RevisionRunner` 持有凍結的 `RevisionPlan`（世界狀態是**回合前快照**、歷史不含目標回合），在產生器內自己管理短交易：串流與狀態呼叫期間沒有
任何資料庫交易（產生器在 `after_request` 之後才執行，不能用請求的工作階段，也不碰 `g`、`current_app`）。與 `TurnRunner` 共用
`RunnerBase`（`StreamRun`、`OutputGuard` 逐句審查、心跳、帳本緩衝）與 `rpg_state_phase`（狀態呼叫與逐項驗證），差別只有收尾與失敗路徑：

- **成功**（文字完整且通過審查、狀態呼叫成功並通過驗證）：單一短交易呼叫 `replace_turn`（輪數與序號不變、`revision` 加 1、認領清除、
  世界狀態以認領時的版本比對寫回、必要時完結存檔）＋`settle`＋回饋列標 `applied`。
- **失敗、被擋、中斷、狀態呼叫失敗、世界版本改變、被接手**：舊內容原樣保留（回合列仍是 `complete`）；釋放自己的認領（`attempt` 相符才生效）、
  退回自己的預扣、回饋列標 `failed` 或 `blocked`——同一個短交易。不消耗免費次數（`regen_count` 只在成功時改變）。被擋的文字不保存。
- 狀態階段失敗＝整個修訂失敗（普通回合可以容忍，是因為文字已送達且不會被收回；修訂的新文字尚未生效，舊內容才是玩家看到的真相）。
- 文字完整之後客戶端才斷線，收尾照常完成（收尾函式不 `yield`，正常路徑與 `GeneratorExit` 路徑共用）；文字未完整就斷線則整個復原。
- **不碰記事簿**：修訂不讀寫記事簿、不觸發抓取（抓取視窗永遠不含最後一輪），Controller 因此不掛 `after_close`。

事件順序（正常）：`meta` → 若干 `token`（`ping` 穿插）→ 一個 `ping`（狀態呼叫前）→ `state` → `done`。被擋時 `transition` 後自動重生一次（沿用同一筆預扣）；
最終被擋以 `done(blocked)` 結束；失敗一律是 `error`（只用既有代碼，不帶原因細節；前端收到後還原舊內容）。

事件內容只有白名單欄位：JSON 原文不會送給前端。日誌只經 `rpg_events`（內部編號、整數與列舉）。行動、理由、劇情、世界狀態、被擋文字都不進日誌。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

from sqlalchemy.orm import Session

from app.models.llm_call_log import KIND_RPG_STREAM
from app.models.rpg_session import REGEN_APPLIED, REGEN_BLOCKED, REGEN_FAILED
from app.observability import rpg_events
from app.repositories.errors import InvalidTurnTransitionError, SessionEndedError, StaleSessionVersionError
from app.repositories.session_repository import SessionRepository
from app.repositories.turn_regeneration_repository import TurnRegenerationRepository
from app.services import rpg_prompt
from app.services.current_user import CurrentUser
from app.services.errors import HoldAlreadyRefunded, HoldAlreadySettled
from app.services.game_service import KIND_EDIT, RevisionPlan, free_left_of
from app.services.rpg_runner_base import (
    ATTEMPT_BLOCKED, ATTEMPT_OK, DONE_BLOCKED, DONE_COMPLETE, DONE_ENDED, EVENT_DONE, EVENT_META, EVENT_PING, EVENT_STATE, EVENT_TRANSITION,
    AttemptResult, RunnerBase, RunnerDeps, SseEvent,
)
from app.services.rpg_state_phase import state_event, state_phase
from app.services.rpg_stream import FAILURE_TIMEOUT, StreamRequest

__all__ = ["RevisionRunner"]


@dataclass
class _Final:
    """`_finalize` 的結果：成功（含 `state` 事件的內容）或失敗（`error` 事件的代碼）。"""

    ok: bool
    error_code: str | None = None
    status: str = DONE_COMPLETE
    coins: int = 0
    state_event: dict[str, Any] = field(default_factory=lambda: {"updated": False, "notices": []})


class RevisionRunner(RunnerBase):
    def __init__(self, *, plan: RevisionPlan, current: CurrentUser, deps: RunnerDeps, marker: str) -> None:
        super().__init__(
            user_id=plan.user_id, kind=KIND_RPG_STREAM, deps=deps, marker=marker, tier=plan.tier, characters=plan.characters,
            adult_age=plan.adult_age,
        )
        self._plan = plan
        self._current = current
        self._final: _Final | None = None
        self._settled = False                 # 已替換成功，或已釋放認領／退款：之後的斷線與錯誤不再動資料

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
        yield self._send(EVENT_META, {
            "kind": plan.kind, "turn_id": plan.seq, "client_msg_id": plan.client_msg_id, "attempt": plan.attempt, "revision": plan.expected_revision,
            "replayed": False,
        })
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
                    yield self._send(EVENT_DONE, self._done_data(DONE_BLOCKED, applied=False, coins=self._balance()))
                    return
                continue
            code = self._record_failure(result.outcome)
            yield self._error(code)
            return

        yield self._send(EVENT_PING, {})                                      # 開始狀態呼叫之前
        final = self._finalize()
        if not final.ok:
            yield self._error(final.error_code or "internal_error")
            return
        yield self._send(EVENT_STATE, final.state_event)
        yield self._send(EVENT_DONE, self._done_data(final.status, applied=True, coins=final.coins))

    def _done_data(self, status: str, *, applied: bool, coins: int) -> dict[str, Any]:
        """`done` 事件：成功時 `revision` 加 1、已用免費次數加 1（編輯則歸零）；被擋時兩者都維持修訂前的值。"""
        plan = self._plan
        if applied:
            revision = plan.expected_revision + 1
            free_left = free_left_of(plan.free_limit, 0 if plan.kind == KIND_EDIT else plan.regen_count + 1)
        else:
            revision = plan.expected_revision
            free_left = free_left_of(plan.free_limit, plan.regen_count)
        return {"status": status, "turn_number": plan.turn_number, "coins": coins, "revision": revision, "free_left": free_left}

    # ---- 被擋、失敗與中斷 -------------------------------------------------------------------------

    def _record_blocked(self, categories: tuple[str, ...], *, final: bool) -> None:
        """輸出被審查擋下。第一次：只記日誌（被擋的文字不保存，沒有地方放，也不需要）；第二次（最終）：釋放認領、退回預扣、回饋列標 `blocked`。"""
        plan = self._plan
        if final:
            self._abort(result=REGEN_BLOCKED, reason="blocked")
        rpg_events.log_turn_blocked(
            user_id=plan.user_id, session_id=plan.session_id, attempt=plan.attempt, categories=categories, auto_retry=True, final=final,
        )

    def _record_failure(self, outcome) -> str:
        """模型失敗（沒有可用的完整文字）：釋放認領、退回預扣、回饋列標 `failed`，舊內容不動。回傳 `error` 事件的代碼。"""
        timed_out = bool(outcome and outcome.failure == FAILURE_TIMEOUT)
        self._abort(result=REGEN_FAILED, reason="timeout" if timed_out else "model_failed")
        return "generation_timeout" if timed_out else "generation_failed"

    def _abort(self, *, result: str, reason: str) -> None:
        """失敗收尾（舊內容不動），同一個短交易：釋放自己的認領（`attempt` 相符才生效，被接手者不會清掉新的認領）、退回自己的預扣
        （免費者沒有預扣）、回饋列標結果（只有 `pending` 能轉出，被接手時已標為 `failed` 的列不被改寫）。只做一次。"""
        if self._settled:
            return
        plan = self._plan

        def work(session: Session) -> None:
            SessionRepository(session).release_revision(plan.user_id, plan.session_public_id, plan.client_msg_id, attempt=plan.attempt)
            if plan.hold_key is not None:
                try:
                    self._deps.wallet_factory(session).refund(self._current, plan.hold_key)
                except HoldAlreadySettled:                                    # 不會發生（成功路徑才結算，且同一個 Runner 只收尾一次）；容忍以免卡住釋放
                    pass
            TurnRegenerationRepository(session).mark_result(
                plan.user_id, plan.session_public_id, turn_seq=plan.seq, attempt=plan.attempt, result=result, now=self._deps.clock(),
            )

        self._tx(work)
        self._settled = True
        rpg_events.log_revision_failed(user_id=plan.user_id, session_id=plan.session_id, kind=plan.kind, attempt=plan.attempt, reason=reason)

    # ---- 斷線與非預期錯誤 -------------------------------------------------------------------------

    def _on_close(self) -> None:
        """客戶端斷線（產生器被關閉）。文字已完整且通過審查：收尾照常完成（成功即算用掉）；否則釋放認領並退款，舊內容不變。不得 `yield`。"""
        if self._final is not None or self._settled:
            return
        try:
            if self._narration is not None:
                self._finalize()
            else:
                self._abort(result=REGEN_FAILED, reason="disconnected")
        except Exception:                                                    # noqa: BLE001 — 斷線後沒有人可以通知；收尾意外失敗時再試著釋放一次
            try:
                self._abort(result=REGEN_FAILED, reason="internal")
            except Exception:                                                # noqa: BLE001 — 認領與預扣由逾時規則釋放
                pass

    def _on_error(self) -> None:
        if self._final is not None or self._settled:
            return
        try:
            self._abort(result=REGEN_FAILED, reason="internal")
        except Exception:                                                    # noqa: BLE001
            pass

    # ---- 收尾（不 yield）---------------------------------------------------------------------------

    def _finalize(self) -> _Final:
        """文字完整之後：狀態呼叫、驗證、一個短交易替換內容並結算。可重複呼叫（只做一次）。任何一步失敗都保留舊內容並釋放。"""
        if self._final is not None:
            return self._final
        assert self._narration is not None
        plan, narration = self._plan, self._narration
        self._tx(lambda session: None)                                         # 串流的帳本先寫入（狀態呼叫的帳本由 `LlmService` 自己寫，順序才與實際一致）
        state = state_phase(plan, self._deps, self._marker, narration)
        if not state.ok:                                                       # 狀態呼叫失敗或驗證失敗＝修訂失敗：新文字不生效
            rpg_events.log_state_failed(
                user_id=plan.user_id, session_id=plan.session_id, turn_number=plan.turn_number, reason=state.degraded or "unavailable",
            )
            return self._fail(reason="state_failed", code="generation_failed")
        is_edit = plan.kind == KIND_EDIT
        structured = {"state": {"ok": True, "dropped": state.dropped}}

        def work(session: Session):
            finish = SessionRepository(session).replace_turn(
                plan.user_id, plan.session_public_id, plan.client_msg_id, expected_revision=plan.expected_revision, attempt=plan.attempt,
                llm_raw_response=narration, structured_result=structured, now=self._deps.clock(), user_action=plan.action if is_edit else None,
                reset_regen_count=is_edit, world_changes=state.world.to_fields() if state.world else None, expected_version=plan.version,
                end_story=state.ending,
            )
            if finish is None:                                                # 不是自己的存檔：開始階段已驗證過，不會發生
                raise RuntimeError("修訂收尾找不到存檔")
            wallet = self._deps.wallet_factory(session)
            if plan.hold_key is not None:
                try:
                    wallet.settle(self._current, plan.hold_key)
                except HoldAlreadyRefunded:                                   # 逾時規則已先退回：新內容已生效，不再追討（與 `TurnRunner` 相同）
                    pass
            TurnRegenerationRepository(session).mark_result(
                plan.user_id, plan.session_public_id, turn_seq=plan.seq, attempt=plan.attempt, result=REGEN_APPLIED, now=self._deps.clock(),
            )
            return finish.world_updated, finish.ended, wallet.balance(plan.user_id)         # 只帶純值出工作階段

        try:
            world_updated, ended, coins = self._tx(work)
        except InvalidTurnTransitionError:                                     # 認領已被新的嘗試接手（或回合已被別人修訂）：我的結果作廢
            return self._fail(reason="superseded", code="turn_superseded")
        except (StaleSessionVersionError, SessionEndedError):                  # 期間世界版本改變（或存檔已完結）：整個修訂失敗，舊內容保留
            return self._fail(reason="state_failed", code="generation_failed")
        self._settled = True
        rpg_events.log_revision_completed(
            user_id=plan.user_id, session_id=plan.session_id, kind=plan.kind, attempt=plan.attempt, chars=len(narration),
            latency_ms=round((self._deps.monotonic() - self._began) * 1000), world_updated=world_updated, ended=ended,
        )
        if ended:
            rpg_events.log_session_ended(
                user_id=plan.user_id, session_id=plan.session_id, turn_number=plan.turn_number, reason="age" if plan.ending_due else "proposed",
            )
        self._final = _Final(True, status=DONE_ENDED if ended else DONE_COMPLETE, coins=coins, state_event=state_event(state, world_updated))
        return self._final

    def _fail(self, *, reason: str, code: str) -> _Final:
        """收尾階段的失敗：釋放認領、退款、回饋列標 `failed`（`_abort`），並記下結果讓重複呼叫不再動資料。"""
        self._abort(result=REGEN_FAILED, reason=reason)
        self._final = _Final(False, error_code=code)
        return self._final
