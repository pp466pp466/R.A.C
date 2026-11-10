"""RPG 回合的「開始階段」（見 rpg-turn-engine 規格與 design D1、D2、D7、D8）。

`GameService` 在**請求的工作階段**內完成回合開始前的一切，並回傳純資料的 `TurnPlan`（凍結的上下文、鍵、模型、版本…）；
串流與收尾由 `TurnRunner` 在自己的短交易內做，產生器不再讀取任何資料，所以串流與狀態呼叫期間沒有交易。

開始階段的順序（便宜、沒有副作用的檢查在前，讓失敗不會留下回合列或預扣）：

1. 帳號與年齡閘門（`AccessPolicy.require_chat_access`）；
2. 驗證 `client_msg_id`（1 至 64 字）與行動文字（NFC、去除控制字元、1 至 1000 字；空白行動在這裡擋下）；
3. 擁有者（非擁有者與不存在同為 404）；
4. 已 `complete` 的重送：直接回傳既有結果（`TurnReplay`），不碰錢包、不看存檔是否已完結（結局回合的重送要拿得到結果）；
5. 存檔已完結 → `session_ended`；第 0 幕尚未完成（`pending`／`generating`）→ `opening_pending`（空值的舊存檔視同 `fallback`，不受限）；
6. 行動的規則層：只有必須拒絕的類別才擋（422 `input_blocked`，只帶類別）；「分級不符」與有疑慮者一律放行（輸出層依模式處理，
   系統不得自動調高玩家的模式）；
7. 全站每日 token 上限（503 `system_busy`）；
8. `start_turn`（取得存檔列鎖、冪等、單一進行中、逾時接手）→ `hold`（鍵含 `attempt`）——兩者在同一個 SAVEPOINT 內，
   餘額不足時整段復原，**不留下新的回合列**，請求仍可回傳 409。

行動文字、劇情、世界狀態都是個資：這裡只記錄 `rpg_events` 的白名單事件（內部編號、整數與列舉）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

from app.models.rpg_session import (
    OPENING_FALLBACK, OPENING_PENDING, OPENING_READY, SESSION_ENDED, TURN_COMPLETE, MemoryLog, RpgSession,
)
from app.models.script import REVIEW_REJECT
from app.observability import rpg_events, usage_events
from app.repositories.errors import SessionEndedError, TurnInProgressError
from app.repositories.session_repository import (
    OPENING_BUSY, OPENING_CLAIMED, OPENING_DONE, OPENING_TAKEN_OVER, TURN_CREATED, TURN_REPLAYED, SessionRepository,
)
from app.services import rpg_context, rpg_world
from app.services.access_policy import AccessPolicy
from app.services.current_user import CurrentUser
from app.services.errors import (
    Conflict, InputBlocked, NotFound, OpeningPending, SessionEnded, SystemBusy, ValidationError,
)
from app.services.input_cleaning import clean_message
from app.services.runtime_limits import RuntimeLimits
from app.services.script_rules import check_text_rules
from app.services.wallet_service import DEFAULT_MODEL_KEY, WalletService

CLIENT_MSG_ID_MAX = 64            # 與 memory_logs.client_msg_id 的欄位長度一致
ACTION_MAX = 1000                 # 與客服的單則訊息上限一致（假設；RPG 沒有明文上限，見 design Open Questions）
TURN_IN_PROGRESS_MESSAGE = "這個回合仍在處理中，請稍候。"
HOLD_KEY_PREFIX = "turn"


def hold_key(session_public_id: str, client_msg_id: str, attempt: int) -> str:
    """遊戲幣預扣的冪等鍵：`turn:{存檔識別碼}:{client_msg_id}:{attempt}`（最長約 110 字，小於錢包的 180 字上限）。
    `attempt` 隨每次重新開始同一個 `client_msg_id` 加 1，使退回過的預扣不會被同一個鍵撿回（錢包對同一個鍵一律回傳既有預扣）。"""
    return f"{HOLD_KEY_PREFIX}:{session_public_id}:{client_msg_id}:{attempt}"


@dataclass(frozen=True)
class TurnPlan:
    """回合開始時凍結的一切：產生器只用它，不再讀取資料庫。內容都是純資料（沒有 ORM 物件、工作階段或請求物件）。"""

    user_id: int
    session_id: int                         # 內部整數編號（只給日誌與帳本）
    session_public_id: str
    seq: int                                # 回合序號（後端指派；事件的 turn_id）
    client_msg_id: str
    attempt: int
    start: str                              # `rpg_events.START_NEW`／`START_RESTARTED`
    turn_number: int                        # 預期的輪數（已完成的輪數加 1）；實際在完成時指派
    action: str                             # 清理後的行動
    hold_key: str
    cost: int
    version: int                            # 存檔的世界狀態版本（寫回時比對）
    tier: str                               # 生效的內容模式
    model: str                              # 首選模型
    adult_age: int
    max_months: int                         # 每回合經過月數上限（現讀自設定表）
    ending_due: bool
    script: Mapping[str, Any]
    protagonist: Mapping[str, Any]
    world: rpg_world.WorldState
    characters: tuple[rpg_world.CharacterAge, ...]         # 審查用的在場角色與目前年齡
    history: tuple[rpg_context.HistoryTurn, ...]           # 最近 10 個完成的回合
    system_instruction: str
    contents: tuple[Any, ...]


@dataclass(frozen=True)
class TurnReplay:
    """已完成的回合的重送結果：不呼叫模型、不碰錢包。"""

    session_public_id: str
    seq: int
    client_msg_id: str
    attempt: int
    turn_number: int | None
    story: str
    ended: bool                             # 存檔是否已完結（結局回合）


@dataclass(frozen=True)
class TurnBegin:
    """`begin_turn` 的結果：要生成（`plan`），或已完成的重送（`replay`），兩者恰有一個。"""

    plan: TurnPlan | None = None
    replay: TurnReplay | None = None


@dataclass(frozen=True)
class OpeningPlan:
    """第 0 幕取得生成權之後凍結的一切（見 `OpeningRunner`）。`claimed_at` 是這次生成的憑證，寫回時必須相同。"""

    user_id: int
    session_id: int
    session_public_id: str
    claimed_at: datetime
    taken_over: bool
    tier: str
    model: str
    adult_age: int
    script: Mapping[str, Any]
    protagonist: Mapping[str, Any]
    characters: tuple[rpg_world.CharacterAge, ...]
    fallback_text: str                      # 劇本的初始對話（沒有則空字串，介面只顯示背景故事）
    system_instruction: str
    contents: tuple[Any, ...]


@dataclass(frozen=True)
class OpeningDone:
    """第 0 幕已經完成（`ready` 或 `fallback`）：只回傳既有文字，不重新生成。"""

    state: str
    text: str


@dataclass(frozen=True)
class OpeningBegin:
    """`begin_opening` 的結果：要生成（`plan`）或已完成（`done`），兩者恰有一個。"""

    plan: OpeningPlan | None = None
    done: OpeningDone | None = None


OPENING_IN_PROGRESS_MESSAGE = "故事的開場正在準備中，請稍候再看。"


def clean_action(value: object) -> str:
    """行動文字的驗證：必須是文字，NFC、去除控制字元，1 至 `ACTION_MAX` 字；否則 `ValidationError`（欄位 `action`）。"""
    if not isinstance(value, str):
        raise ValidationError(fields={"action": "必須是文字。"})
    text = clean_message(value)
    if text is None:
        raise ValidationError(fields={"action": "包含無法儲存的字元。"})
    if not text:
        raise ValidationError(fields={"action": "請輸入你的行動。"})
    if len(text) > ACTION_MAX:
        raise ValidationError(fields={"action": f"行動最多 {ACTION_MAX} 字。"})
    return text


def clean_client_msg_id(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= CLIENT_MSG_ID_MAX:
        raise ValidationError(fields={"client_msg_id": f"必須是 1 至 {CLIENT_MSG_ID_MAX} 字的文字。"})
    return value


def history_of(turns: list[MemoryLog]) -> tuple[rpg_context.HistoryTurn, ...]:
    return tuple(
        rpg_context.HistoryTurn(turn_number=turn.turn_number, status=turn.turn_status, action=turn.user_action, narration=turn.llm_raw_response)
        for turn in turns
    )


class GameService:
    def __init__(
        self,
        *,
        sessions: SessionRepository,
        wallet: WalletService,
        policy: AccessPolicy,
        limits: RuntimeLimits,
        budget_exhausted: Callable[[], bool],
        marker: str,
        model: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._sessions = sessions
        self._wallet = wallet
        self._policy = policy
        self._limits = limits
        self._budget_exhausted = budget_exhausted
        self._marker = marker
        self._model = model
        self._clock = clock

    # ---- 回合 -----------------------------------------------------------------------------------

    def begin_turn(self, current: CurrentUser, session_public_id: str, *, client_msg_id: object, action: object) -> TurnBegin:
        """開始一個回合（見模組說明的順序）。回傳 `TurnBegin(plan=…)` 或已完成的重送 `TurnBegin(replay=…)`。"""
        self._policy.require_chat_access(current)
        msg_id = clean_client_msg_id(client_msg_id)
        text = clean_action(action)

        save = self._sessions.get_owned(current.id, session_public_id)
        if save is None:
            raise NotFound()
        existing = self._sessions.get_turn(current.id, session_public_id, msg_id)
        if existing is not None and existing.turn_status == TURN_COMPLETE:
            return TurnBegin(replay=self._replay(save, existing))

        if save.status == SESSION_ENDED:
            raise SessionEnded()
        if save.opening_state is not None and save.opening_state not in (OPENING_READY, OPENING_FALLBACK):
            raise OpeningPending()

        tier = self._policy.effective_tier(current, save_tier=save.content_tier)
        adult_age = self._policy.adult_age
        rules = check_text_rules([text], tier=tier, adult_age=adult_age)
        if rules.verdict == REVIEW_REJECT:
            raise InputBlocked(rules.categories)
        if self._budget_exhausted():
            usage_events.log_denied(user_id=current.id, reason=usage_events.DENIED_GLOBAL_BUDGET)
            raise SystemBusy()

        stale_after = self._limits.coin_hold_timeout()
        max_months = self._limits.rpg_max_months_per_turn()
        try:
            with self._sessions.savepoint():                      # 餘額不足時連同新的回合列一起復原
                started = self._sessions.start_turn(
                    current.id, session_public_id, client_msg_id=msg_id, user_action=text, now=self._clock(), stale_after_seconds=stale_after,
                )
                if started is None:
                    raise NotFound()
                if started.outcome == TURN_REPLAYED:                # 並行的另一個請求剛完成同一個回合
                    return TurnBegin(replay=self._replay(save, started.turn))
                turn = started.turn
                attempt, seq = turn.attempt, turn.seq
                key = hold_key(session_public_id, msg_id, attempt)
                held = self._wallet.hold(current, key=key, session_public_id=session_public_id, model_key=DEFAULT_MODEL_KEY)
        except SessionEndedError as exc:
            raise SessionEnded() from exc
        except TurnInProgressError as exc:
            raise Conflict(TURN_IN_PROGRESS_MESSAGE, code="turn_in_progress") from exc

        plan = self._plan(
            current, save, tier=tier, adult_age=adult_age, max_months=max_months, action=text, client_msg_id=msg_id, attempt=attempt, seq=seq,
            start=rpg_events.START_NEW if started.outcome == TURN_CREATED else rpg_events.START_RESTARTED, key=key, cost=held.cost,
        )
        rpg_events.log_turn_started(
            user_id=current.id, session_id=save.id, turn_number=plan.turn_number, attempt=attempt, start=plan.start, tier=tier, model=self._model,
        )
        return TurnBegin(plan=plan)

    # ---- 第 0 幕 --------------------------------------------------------------------------------

    def begin_opening(self, current: CurrentUser, session_public_id: str) -> OpeningBegin:
        """第 0 幕（開場）的開始階段：閘門 → 擁有者 → 已完成就直接回傳既有文字 → 全站預算 → 條件式搶佔生成權。

        - `ready`／`fallback`（以及遷移之前建立、狀態為空值的舊存檔）：回傳 `OpeningDone`，不呼叫模型、不重抽；
        - `pending`：搶佔成功者取得生成權（`OpeningPlan`），同時只有一個請求能生成；
        - `generating` 且未逾 90 秒：另一個請求正在生成 → 409 `opening_in_progress`；逾時者可被接手；
        - 不碰錢包、不寫回合表。
        """
        self._policy.require_chat_access(current)
        save = self._sessions.get_owned(current.id, session_public_id)
        if save is None:
            raise NotFound()
        script = save.script_snapshot_json if isinstance(save.script_snapshot_json, dict) else {}
        fallback_text = script.get("opening") if isinstance(script.get("opening"), str) else ""
        if save.opening_state is None:                                    # 舊存檔：視同 fallback，不生成
            return OpeningBegin(done=OpeningDone(OPENING_FALLBACK, save.opening_text or fallback_text))
        if save.opening_state in (OPENING_READY, OPENING_FALLBACK):
            return OpeningBegin(done=OpeningDone(save.opening_state, save.opening_text or ""))

        if save.opening_state == OPENING_PENDING and self._budget_exhausted():
            usage_events.log_denied(user_id=current.id, reason=usage_events.DENIED_GLOBAL_BUDGET)
            raise SystemBusy()
        now = self._clock()
        claimed = self._sessions.claim_opening(current.id, session_public_id, now=now)
        if claimed is None:
            raise NotFound()
        outcome, save = claimed
        if outcome == OPENING_BUSY:
            raise Conflict(OPENING_IN_PROGRESS_MESSAGE, code="opening_in_progress")
        if outcome == OPENING_DONE:                                       # 並行的另一個請求剛完成
            return OpeningBegin(done=OpeningDone(save.opening_state or OPENING_FALLBACK, save.opening_text or ""))
        assert outcome in (OPENING_CLAIMED, OPENING_TAKEN_OVER)
        if outcome == OPENING_TAKEN_OVER and self._budget_exhausted():     # 接手了卻沒有預算：交還生成權
            self._sessions.reset_opening(current.id, session_public_id, claimed_at=now)
            usage_events.log_denied(user_id=current.id, reason=usage_events.DENIED_GLOBAL_BUDGET)
            raise SystemBusy()

        tier = self._policy.effective_tier(current, save_tier=save.content_tier)
        protagonist = rpg_world.protagonist_from_session(save.protagonist_json, save.player_name, save.character_age)
        world = rpg_world.WorldState.from_fields(
            entities=save.entities_json, goals=save.goals_json, milestone_progress=save.milestone_progress_json,
            elapsed_months=save.elapsed_months, current_location=save.current_location, status_summary=save.status_summary,
        )
        system, contents = rpg_context.build_opening_context(script=script, protagonist=protagonist, tier=tier, marker=self._marker)
        if outcome == OPENING_TAKEN_OVER:
            rpg_events.log_opening_taken_over(user_id=current.id, session_id=save.id)
        rpg_events.log_opening_started(user_id=current.id, session_id=save.id, tier=tier, model=self._model)
        return OpeningBegin(plan=OpeningPlan(
            user_id=current.id, session_id=save.id, session_public_id=save.public_id, claimed_at=now, taken_over=outcome == OPENING_TAKEN_OVER,
            tier=tier, model=self._model, adult_age=self._policy.adult_age, script=script, protagonist=protagonist,
            characters=tuple(rpg_world.known_characters(world, script, protagonist)), fallback_text=fallback_text,
            system_instruction=system, contents=tuple(contents),
        ))

    # ---- 內部 -----------------------------------------------------------------------------------

    def _replay(self, save: RpgSession, turn: MemoryLog) -> TurnReplay:
        return TurnReplay(
            session_public_id=save.public_id, seq=turn.seq, client_msg_id=turn.client_msg_id, attempt=turn.attempt,
            turn_number=turn.turn_number, story=turn.llm_raw_response, ended=save.status == SESSION_ENDED,
        )

    def _plan(
        self, current: CurrentUser, save: RpgSession, *, tier: str, adult_age: int, max_months: int, action: str, client_msg_id: str,
        attempt: int, seq: int, start: str, key: str, cost: int,
    ) -> TurnPlan:
        recent = self._sessions.recent_complete_turns(current.id, save.public_id) or []
        history = history_of(recent)
        completed = self._sessions.count_complete_turns(current.id, save.public_id) or 0
        turn_number = completed + 1
        world = rpg_world.WorldState.from_fields(
            entities=save.entities_json, goals=save.goals_json, milestone_progress=save.milestone_progress_json,
            elapsed_months=save.elapsed_months, current_location=save.current_location, status_summary=save.status_summary,
        )
        script = save.script_snapshot_json if isinstance(save.script_snapshot_json, dict) else {}
        protagonist = rpg_world.protagonist_from_session(save.protagonist_json, save.player_name, save.character_age)
        ending_due = rpg_world.compute_ending_due(script, world, protagonist)
        opening = save.opening_text if save.opening_state in (OPENING_READY, OPENING_FALLBACK) else None
        system, contents = rpg_context.build_turn_context(
            script=script, world=world, protagonist=protagonist, history=history, opening_text=opening, notebook=save.notebook_text or "",
            action=action, tier=tier, turn_number=turn_number, ending_due=ending_due, marker=self._marker,
        )
        return TurnPlan(
            user_id=current.id, session_id=save.id, session_public_id=save.public_id, seq=seq, client_msg_id=client_msg_id, attempt=attempt,
            start=start, turn_number=turn_number, action=action, hold_key=key, cost=cost, version=save.version, tier=tier, model=self._model,
            adult_age=adult_age, max_months=max_months, ending_due=ending_due, script=script, protagonist=protagonist, world=world,
            characters=tuple(rpg_world.known_characters(world, script, protagonist)), history=history, system_instruction=system,
            contents=tuple(contents),
        )
