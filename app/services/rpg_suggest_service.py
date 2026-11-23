"""RPG 行動建議（見 rpg-suggestions 規格與 rpg-story-view 的 design D5、D6）。

玩家點建議圖示才呼叫：一次獨立的非串流結構化呼叫（用途 `rpg_suggest`，沿用 RPG 模型、備援與單次請求總預算）。**預設不產生**：載入存檔、
第 0 幕與回合完成都不會呼叫；這個服務只有玩家主動請求時才執行。不碰錢包（不預扣、不扣款、不用免費次數），但每次模型呼叫都寫入用量帳本、
計入全站每日 token 上限；管理員行為相同。

`suggest` 的順序（每一步失敗就停下；便宜、沒有副作用的檢查在前）：
1. 帳號與年齡閘門（`require_chat_access`）；2. 擁有者（非擁有者與不存在同為 404）；3. 存檔已完結 → `session_ended`；
4. 第 0 幕尚未完成 → `opening_pending`；5. 有進行中（含修訂中）的回合 → `turn_in_progress`（`SessionRepository.turn_in_progress`，
   第一輪正在串流而尚無完成回合時也成立）；
6. 保存的錨點等於目前的錨點 → 直接回傳保存的建議（`reused`，**不呼叫模型**）；7. 沒有 RPG 模型供應商 → 503；
8. 全站每日 token 上限 → 503 `system_busy`；9. 組裝上下文（只有玩家看得到的資訊，`rpg_context.build_suggest_context`；生效模式現算）並呼叫模型
   （結構化輸出，驗證失敗同模型重試一次，暫時性錯誤切備援）；10. 逐項驗證（`rpg_suggest_prompt.clean_items`，無效者丟棄）；
11. `SessionRepository.save_suggestions` 在鎖內重新比對錨點後寫入：錨點已改變（呼叫期間有新回合或修訂成功）→ 409 `suggestions_stale`、
    不保存；並行的另一個請求先寫入 → 回傳已存的那一組；不改 `version`、不改世界狀態。

**失敗不保存、不佔每個畫面一次的次數**：模型失敗或沒有任何有效項目回 503 `suggestions_unavailable`，玩家可再按圖示重試。
模型呼叫之後立刻提交目前的交易（`release`），讓最後一筆帳本在後面的 4xx／5xx 之後仍然保留（呼叫過模型就是用過 token）。
日誌只記內部編號、項數、結果列舉與呼叫次數（`rpg_events`），絕不記錄建議、劇情、行動或角色卡的文字。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from app.models.llm_call_log import KIND_RPG_SUGGEST
from app.models.rpg_session import OPENING_FALLBACK, OPENING_READY, SESSION_ENDED
from app.observability import rpg_events, usage_events
from app.repositories.session_repository import SUGGESTIONS_ENDED, SUGGESTIONS_REUSED, SUGGESTIONS_STALE, SessionRepository
from app.services import rpg_context, rpg_world
from app.services.access_policy import AccessPolicy
from app.services.current_user import CurrentUser
from app.services.errors import (
    ActionSuggestionsStale, ActionSuggestionsUnavailable, Conflict, NotFound, OpeningPending, ServiceUnavailable, SessionEnded, SystemBusy,
)
from app.services.game_service import TURN_IN_PROGRESS_MESSAGE, history_of, opening_view, world_of
from app.services.llm_service import (
    DEGRADED_BLOCKED, DEGRADED_BUDGET, DEGRADED_CONFIG, DEGRADED_TRANSIENT, DEGRADED_VALIDATION, LlmService,
)
from app.services.rpg_suggest_prompt import SUGGEST_SCHEMA, clean_items, items_of, suggest_rules
from app.services.runtime_limits import RuntimeLimits

NO_PROVIDER_MESSAGE = "故事引擎目前無法使用，請稍後再試。"

# `LlmService` 的降級原因 -> 事件的原因列舉（沒有對應者視為暫時性錯誤）
_FAIL_REASONS = {
    DEGRADED_VALIDATION: "validation", DEGRADED_TRANSIENT: "transient", DEGRADED_BLOCKED: "blocked", DEGRADED_CONFIG: "config",
    DEGRADED_BUDGET: "budget",
}


@dataclass(frozen=True)
class Suggestions:
    """端點的結果：1 至 3 項建議（純文字）與是否為已保存的結果（沒有呼叫模型，或並行的另一個請求先寫入）。"""

    items: tuple[str, ...]
    reused: bool


class SuggestionService:
    def __init__(
        self,
        *,
        sessions: SessionRepository,
        policy: AccessPolicy,
        limits: RuntimeLimits,
        llm: LlmService | None,
        budget_exhausted: Callable[[], bool],
        marker: str,
        model: str,
        clock: Callable[[], datetime],
        release: Callable[[], None],
    ) -> None:
        self._sessions = sessions
        self._policy = policy
        self._limits = limits
        self._llm = llm
        self._budget_exhausted = budget_exhausted
        self._marker = marker
        self._model = model
        self._clock = clock
        self._release = release

    def suggest(self, current: CurrentUser, session_public_id: str) -> Suggestions:
        self._policy.require_chat_access(current)
        save = self._sessions.get_owned(current.id, session_public_id)
        if save is None:
            raise NotFound()
        session_id = save.id
        if save.status == SESSION_ENDED:
            raise SessionEnded()
        if save.opening_state is not None and save.opening_state not in (OPENING_READY, OPENING_FALLBACK):
            raise OpeningPending()
        busy = self._sessions.turn_in_progress(
            current.id, session_public_id, now=self._clock(), stale_after_seconds=self._limits.coin_hold_timeout(),
        )
        if busy is None:
            raise NotFound()
        if busy:
            raise Conflict(TURN_IN_PROGRESS_MESSAGE, code="turn_in_progress")
        state = self._sessions.suggestion_state(current.id, session_public_id)
        if state is None:
            raise NotFound()
        if state.items:                                                     # 同一個畫面最多產生一次：重用，不呼叫模型
            rpg_events.log_suggestion_served(user_id=current.id, session_id=session_id, count=len(state.items), outcome="reused", model=None)
            return Suggestions(state.items, True)
        if self._llm is None:
            rpg_events.log_suggestion_failed(user_id=current.id, session_id=session_id, reason="no_provider", calls=0)
            raise ServiceUnavailable(NO_PROVIDER_MESSAGE)
        if self._budget_exhausted():
            usage_events.log_denied(user_id=current.id, reason=usage_events.DENIED_GLOBAL_BUDGET)
            raise SystemBusy()

        # 生效模式與上下文都在呼叫之前算好（每次現算，不取自開存檔時的值）；呼叫之後不再讀取存檔
        script = save.script_snapshot_json if isinstance(save.script_snapshot_json, dict) else {}
        tier = self._policy.save_mode(current, script)
        adult_age = self._policy.adult_age
        protagonist = rpg_world.protagonist_from_session(save.protagonist_json, save.player_name, save.character_age)
        recent = self._sessions.recent_complete_turns(current.id, session_public_id, limit=rpg_context.SUGGEST_TURNS) or []
        opening = opening_view(save, script).get("text")
        system, contents = rpg_context.build_suggest_context(
            script=script, world=world_of(save), protagonist=protagonist, history=history_of(recent),
            opening_text=opening if isinstance(opening, str) else None, tier=tier, marker=self._marker,
        )
        outcome = self._llm.run_structured(
            kind=KIND_RPG_SUGGEST, user_id=current.id, conversation_id=None, system_instruction=system, contents=contents,
            schema=SUGGEST_SCHEMA, rules=suggest_rules(self._marker, tier=tier, adult_age=adult_age), model=self._model,
        )
        self._release()                                                     # 最後一筆帳本只寫入、不提交：先提交，之後的錯誤回應才不會把它回復
        if not outcome.ok:
            raise self._unavailable(current.id, session_id, _FAIL_REASONS.get(outcome.degraded or "", "transient"), outcome.attempts)
        items = clean_items(items_of(outcome.value), marker=self._marker, tier=tier, adult_age=adult_age)
        if not items:                                                       # 語意規則已要求至少一個有效項；防禦：沒有有效項一律視為失敗
            raise self._unavailable(current.id, session_id, "empty", outcome.attempts)

        saved = self._sessions.save_suggestions(current.id, session_public_id, seq=state.seq, revision=state.revision, items=items)
        if saved is None:
            raise NotFound()
        if saved.outcome == SUGGESTIONS_ENDED:
            raise SessionEnded()
        if saved.outcome == SUGGESTIONS_STALE:                              # 呼叫期間劇情已前進：這組建議對應舊畫面，不保存也不回傳
            rpg_events.log_suggestion_failed(user_id=current.id, session_id=session_id, reason="stale", calls=outcome.attempts)
            raise ActionSuggestionsStale()
        reused = saved.outcome == SUGGESTIONS_REUSED                        # 並行的另一個請求先寫入：回傳已存的那一組
        rpg_events.log_suggestion_served(
            user_id=current.id, session_id=session_id, count=len(saved.items), outcome="reused" if reused else "generated", model=outcome.model,
        )
        return Suggestions(saved.items, reused)

    @staticmethod
    def _unavailable(user_id: int, session_id: int, reason: str, calls: int) -> ActionSuggestionsUnavailable:
        rpg_events.log_suggestion_failed(user_id=user_id, session_id=session_id, reason=reason, calls=calls)
        return ActionSuggestionsUnavailable()
