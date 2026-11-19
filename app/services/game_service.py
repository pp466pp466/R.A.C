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

修訂（重新生成與編輯最後一輪，rpg-turn-revision）：`begin_revision` 是同一種「開始階段」——驗證、認領、（付費者）預扣、回饋列都在請求內完成，
回傳純資料的 `RevisionPlan`（世界狀態是回合前快照）給 `RevisionRunner`。回合開始時 `begin_turn` 把當下的世界狀態存成該回合的回合前快照。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping

from app.models.rpg_session import (
    OPENING_FALLBACK, OPENING_PENDING, OPENING_READY, REGEN_CATEGORIES, REGEN_KIND_EDIT, REGEN_KIND_REGENERATE, SESSION_ENDED, TURN_COMPLETE,
    MemoryLog, RpgSession,
)
from app.models.script import REVIEW_REJECT
from app.observability import rpg_events, usage_events
from app.repositories.errors import NotRevisableError, SessionEndedError, TurnInProgressError
from app.repositories.session_repository import (
    NOT_REVISABLE_ENDED, NOT_REVISABLE_IN_PROGRESS, NOT_REVISABLE_NO_SNAPSHOT, NOT_REVISABLE_NOT_LAST, OPENING_BUSY, OPENING_CLAIMED, OPENING_DONE, OPENING_TAKEN_OVER,
    REVISION_BUSY, REVISION_CONFLICT, REVISION_TAKEN_OVER, TURN_CREATED, TURN_REPLAYED, SessionRepository,
)
from app.repositories.turn_regeneration_repository import TurnRegenerationRepository
from app.services import notebook_policy, rpg_context, rpg_world
from app.services.access_policy import AccessPolicy
from app.services.current_user import CurrentUser
from app.services.errors import (
    Conflict, InputBlocked, NotFound, NotRevisable, OpeningPending, RevisionConflict, RevisionInvalid, SessionEnded, SystemBusy,
    ValidationError,
)
from app.services.input_cleaning import clean_message, clean_single_line
from app.services.runtime_limits import RuntimeLimits
from app.services.script_rules import check_text_rules, mentions_sexual
from app.services.wallet_service import DEFAULT_MODEL_KEY, WalletService

CLIENT_MSG_ID_MAX = 64            # 與 memory_logs.client_msg_id 的欄位長度一致
ACTION_MAX = 1000                 # 與客服的單則訊息上限一致（假設；RPG 沒有明文上限，見 design Open Questions）
TURN_IN_PROGRESS_MESSAGE = "這個回合仍在處理中，請稍候。"
HOLD_KEY_PREFIX = "turn"
TURNS_PAGE_MAX = 200              # `GET …/turns` 每次最多回傳的筆數（客服的訊息分頁相同）
REASON_MIN = 5                    # 重新生成的理由：NFC 並去除控制字元之後的 Unicode 字元數（rpg-turn-revision；前端從頁面初始狀態讀，不寫死）
REASON_MAX = 50
KIND_REGENERATE = REGEN_KIND_REGENERATE
KIND_EDIT = REGEN_KIND_EDIT
CATEGORIES = REGEN_CATEGORIES     # 回饋分類的列舉（識別字串；繁體中文文案在前端）


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
    revision: int                           # 回合目前的修訂序號（新回合為 1）；`done` 事件帶出
    free_left: int                          # 這個回合還剩幾次免費修訂（重新生成與編輯共用；新回合為設定的免費次數）；`done` 事件帶出
    # 遊戲內「可以嘗試關閉安全模式」提示（safe-mode-upgrade-hint）：開始階段判定的兩個旗標，之後的收尾只據此決定要不要嘗試記錄與送出
    hint_eligible: bool = False             # 玩家設定為安全模式開、存檔的劇本未綁定、緊急煞車未啟用（`AccessPolicy.upgrade_hint_eligible`）
    hint_input: bool = False                # 且玩家這一輪送出的行動被規則層判為性內容（`mentions_sexual`）


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
    revision: int                           # 回合目前的修訂序號；`done` 事件帶出
    free_left: int                          # 這個回合還剩幾次免費修訂（重新生成與編輯共用）；`done` 事件帶出


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


@dataclass(frozen=True)
class RevisionPlan:
    """修訂（重新生成或編輯最後一輪）認領成功之後凍結的一切：`RevisionRunner` 只用它，不再讀取資料庫。內容都是純資料。

    世界狀態 `world` 是**回合前快照**（不是目前存檔），`history` 不含目標回合與其後的回合，`turn_number` 是目標回合原本的輪數。
    **沒有理由與分類**：它們只存入回饋表，不屬於任何會進模型的內容（也不需要被串流階段使用）；`action` 是這次生成使用的行動
    （重新生成沿用原行動，編輯使用清理後的新行動），日誌不記錄它。"""

    user_id: int
    session_id: int                         # 內部整數編號（只給日誌與帳本）
    session_public_id: str
    kind: str                               # `KIND_REGENERATE` 或 `KIND_EDIT`
    seq: int                                # 回合序號（修訂不改變）
    client_msg_id: str
    attempt: int                            # 認領後的嘗試序號（預扣鍵、回饋列、替換與釋放的憑證）
    expected_revision: int                  # 認領時的回合 revision（替換時比對；成功後加 1）
    regen_count: int                        # 認領時這個回合已成功修訂（重新生成或編輯）的次數
    free_limit: int                         # 認領當下設定的免費次數（`done` 的 `free_left` 由它與 `regen_count` 算出）
    taken_over: bool                        # 這次接手了逾時的舊認領
    is_free: bool                           # 免費的修訂（重新生成或編輯，不預扣、不檢查餘額）
    hold_key: str | None                    # 預扣鍵；免費者為 `None`
    cost: int                               # 預扣的幣數（免費為 0）
    turn_number: int                        # 目標回合的輪數
    action: str                             # 這次生成使用的行動
    version: int                            # 認領時的存檔世界版本（寫回時比對）
    tier: str
    model: str
    adult_age: int
    max_months: int
    ending_due: bool
    script: Mapping[str, Any]
    protagonist: Mapping[str, Any]
    world: rpg_world.WorldState             # 回合前快照
    characters: tuple[rpg_world.CharacterAge, ...]
    history: tuple[rpg_context.HistoryTurn, ...]           # 不含目標回合的最近 10 個完成回合
    system_instruction: str
    contents: tuple[Any, ...]


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


def clean_expected_revision(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError(fields={"expected_revision": "必須是不小於 1 的整數。"})
    return value


def revision_field_problems(category: object, reason: object) -> tuple[str | None, str | None, dict[str, str]]:
    """重新生成的分類與理由的驗證。回傳（分類, 清理後的理由, 欄位問題）；理由以 NFC、去除控制字元與頭尾空白之後的 Unicode 字元數計，
    須為 `REASON_MIN` 至 `REASON_MAX` 字。有問題的欄位都列出（不回顯玩家填的內容）。"""
    problems: dict[str, str] = {}
    chosen = category if isinstance(category, str) and category in CATEGORIES else None
    if chosen is None:
        problems["category"] = "請選擇一個分類。"
    text: str | None = None
    if not isinstance(reason, str):
        problems["reason"] = "請簡短說明原因。"
    else:
        text = clean_single_line(reason)
        if text is None:
            problems["reason"] = "包含無法儲存的字元。"
        elif len(text) < REASON_MIN:
            problems["reason"] = f"請簡短說明原因，至少 {REASON_MIN} 字。"
        elif len(text) > REASON_MAX:
            problems["reason"] = f"原因最多 {REASON_MAX} 字。"
    return chosen, text if "reason" not in problems else None, problems


def world_of(save: RpgSession) -> rpg_world.WorldState:
    """存檔目前的世界狀態（六個欄位的互轉由 `WorldState` 負責）。"""
    return rpg_world.WorldState.from_fields(
        entities=save.entities_json, goals=save.goals_json, milestone_progress=save.milestone_progress_json,
        elapsed_months=save.elapsed_months, current_location=save.current_location, status_summary=save.status_summary,
    )


def world_from_snapshot(snapshot: Mapping[str, Any]) -> rpg_world.WorldState:
    """回合前快照（`WorldState.to_fields()` 的六欄）還原成 `WorldState`。"""
    return rpg_world.WorldState.from_fields(
        entities=snapshot.get("entities_json"), goals=snapshot.get("goals_json"), milestone_progress=snapshot.get("milestone_progress_json"),
        elapsed_months=snapshot.get("elapsed_months", 0), current_location=snapshot.get("current_location", ""),
        status_summary=snapshot.get("status_summary", ""),
    )


def free_left_of(free_limit: int, regen_count: int) -> int:
    """這個回合還剩幾次免費修訂（重新生成與編輯共用）。"""
    return max(0, int(free_limit) - int(regen_count))


def opening_view(save: RpgSession, script: Mapping[str, Any]) -> dict[str, Any]:
    """第 0 幕的玩家視圖：狀態與文字（尚未完成時沒有文字）。遷移之前建立、狀態為空值的舊存檔視同 `fallback`（劇本的初始對話）。"""
    if save.opening_state is None:
        fallback = script.get("opening") if isinstance(script.get("opening"), str) else ""
        return {"state": OPENING_FALLBACK, "text": save.opening_text or fallback}
    if save.opening_state in (OPENING_READY, OPENING_FALLBACK):
        return {"state": save.opening_state, "text": save.opening_text or ""}
    return {"state": save.opening_state, "text": None}


def world_view(save: RpgSession) -> dict[str, Any]:
    """世界狀態的玩家視圖：經過月數、地點、摘要、進行中的目標、去過的地點與主角目前的年齡。
    NPC 的角色卡（特質、隱藏的歸屬、弧線階段）與錨定事實是模型的上下文，不在這裡。"""
    world = rpg_world.WorldState.from_fields(
        entities=save.entities_json, goals=save.goals_json, milestone_progress=save.milestone_progress_json,
        elapsed_months=save.elapsed_months, current_location=save.current_location, status_summary=save.status_summary,
    )
    protagonist = rpg_world.protagonist_from_session(save.protagonist_json, save.player_name, save.character_age)
    return {
        "elapsed_months": world.elapsed_months, "location": world.current_location, "summary": world.status_summary,
        "goals": [goal["text"] for goal in world.goals.get("active", []) if isinstance(goal.get("text"), str)],
        "places": list(world.entities.get("locations", [])),
        "protagonist_age": rpg_world.protagonist_age(world.entities, protagonist, world.elapsed_months),
    }


def turn_view(turn: MemoryLog) -> dict[str, Any]:
    """一個回合的玩家視圖。只有 `complete` 才含劇情；被擋與中斷的回合不含部分內容（被擋的文字永遠不離開伺服器）。"""
    complete = turn.turn_status == TURN_COMPLETE
    return {
        "seq": turn.seq, "client_msg_id": turn.client_msg_id, "turn_number": turn.turn_number, "status": turn.turn_status,
        "action": turn.user_action, "story": turn.llm_raw_response if complete else None, "revision": turn.revision,
    }


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
        regenerations: TurnRegenerationRepository,
        wallet: WalletService,
        policy: AccessPolicy,
        limits: RuntimeLimits,
        budget_exhausted: Callable[[], bool],
        marker: str,
        model: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._sessions = sessions
        self._regenerations = regenerations
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

        tier = self._policy.save_mode(current, save.script_snapshot_json)
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
                    state_before=lambda: world_of(save).to_fields(),   # 取得存檔列鎖之後才讀：與稍後凍結的 TurnPlan 是同一份世界
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

        # 提示的前提在開始階段一次算好（純規則層，不呼叫模型）：不影響這個回合的任何內容、預扣或上下文
        hint_eligible = self._policy.upgrade_hint_eligible(current, save.script_snapshot_json)
        plan = self._plan(
            current, save, tier=tier, adult_age=adult_age, max_months=max_months, action=text, client_msg_id=msg_id, attempt=attempt, seq=seq,
            start=rpg_events.START_NEW if started.outcome == TURN_CREATED else rpg_events.START_RESTARTED, key=key, cost=held.cost,
            revision=turn.revision, hint_eligible=hint_eligible, hint_input=hint_eligible and mentions_sexual(text),
        )
        rpg_events.log_turn_started(
            user_id=current.id, session_id=save.id, turn_number=plan.turn_number, attempt=attempt, start=plan.start, tier=tier, model=self._model,
        )
        return TurnBegin(plan=plan)

    # ---- 修訂：重新生成與編輯最後一輪 -----------------------------------------------------------------

    def begin_revision(
        self, current: CurrentUser, session_public_id: str, client_msg_id: object, *, kind: str, expected_revision: object,
        category: object = None, reason: object = None, action: object = None,
    ) -> RevisionPlan:
        """開始一次修訂（見 rpg-turn-revision 規格與 design D1、D2、D3、D4、D5）。順序（便宜、沒有副作用的檢查在前）：

        1. 帳號與年齡閘門；2. 驗證本文（`expected_revision`；重新生成的分類與理由，編輯的 `clean_action`）；
        3. 擁有者與回合（不存在或非擁有者 404）；4. `revision`（`revision_conflict`）；5. 可修訂（`not_revisable` 帶原因；
           同一回合已有未逾時的認領為 `turn_in_progress`）；6. 編輯：新行動須不同於原行動（422），再過規則層（`input_blocked`）；
        7. 全站每日 token 上限（`system_busy`）；
        8. 同一個 SAVEPOINT：認領（條件式 UPDATE，`attempt` 加 1）→ 接手時把舊的 `pending` 回饋標為 `failed` → 回饋列 `pending` →
           （免費次數用完之後的修訂，重新生成與編輯共用同一組次數）`wallet.hold`，鍵含認領後的 `attempt`；免費者不預扣、不檢查餘額；
           餘額不足時整段復原，**不留認領、不留回饋列**。管理員超過免費次數後照樣呼叫 `wallet.hold`，預扣金額為 0、不會餘額不足。

        回傳純資料的 `RevisionPlan`：世界狀態是回合前快照，歷史不含目標回合。理由與分類只寫入回饋表，不在計畫裡，也不進模型。"""
        self._policy.require_chat_access(current)
        if kind not in (KIND_REGENERATE, KIND_EDIT):
            raise ValueError(f"未知的修訂種類：{kind!r}")
        msg_id = clean_client_msg_id(client_msg_id)
        expected = clean_expected_revision(expected_revision)
        chosen_category: str | None = None
        reason_text: str | None = None
        new_action: str | None = None
        if kind == KIND_REGENERATE:
            chosen_category, reason_text, problems = revision_field_problems(category, reason)
            if problems:
                raise RevisionInvalid(problems)
        else:
            new_action = clean_action(action)

        save = self._sessions.get_owned(current.id, session_public_id)
        if save is None:
            raise NotFound()
        original = self._sessions.get_turn(current.id, session_public_id, msg_id)
        if original is None:
            raise NotFound()
        if original.revision != expected:
            raise RevisionConflict()

        now = self._clock()
        stale_after = self._limits.coin_hold_timeout()
        state = self._sessions.revision_state(current.id, session_public_id, now=now, stale_after_seconds=stale_after)
        if state is None:
            raise NotFound()
        if state.client_msg_id != msg_id:                          # 目標不是最後一個完成的回合（被擋、中斷與生成中的嘗試也不是）
            raise NotRevisable(NOT_REVISABLE_ENDED if save.status == SESSION_ENDED else NOT_REVISABLE_NOT_LAST)
        if not state.revisable:
            claimed_at = original.revising_started_at
            if state.reason == NOT_REVISABLE_IN_PROGRESS and claimed_at is not None and claimed_at >= now - timedelta(seconds=stale_after):
                raise Conflict(TURN_IN_PROGRESS_MESSAGE, code="turn_in_progress")          # 同一回合已有未逾時的認領
            raise NotRevisable(state.reason or NOT_REVISABLE_NOT_LAST)

        tier = self._policy.save_mode(current, save.script_snapshot_json)
        if kind == KIND_EDIT:
            if new_action == original.user_action:
                raise RevisionInvalid({"action": "新的行動需要和原本的不同。"})
            rules = check_text_rules([new_action], tier=tier, adult_age=self._policy.adult_age)
            if rules.verdict == REVIEW_REJECT:
                raise InputBlocked(rules.categories)
        if self._budget_exhausted():
            usage_events.log_denied(user_id=current.id, reason=usage_events.DENIED_GLOBAL_BUDGET)
            raise SystemBusy()

        free_limit = self._limits.rpg_free_regenerations()
        try:
            with self._sessions.savepoint():                      # 餘額不足（或任何失敗）時連同認領與回饋列一起復原
                claim = self._sessions.claim_revision(
                    current.id, session_public_id, msg_id, expected_revision=expected, now=now, stale_after_seconds=stale_after,
                )
                if claim is None:
                    raise NotFound()
                if claim.outcome == REVISION_CONFLICT:
                    raise RevisionConflict()
                if claim.outcome == REVISION_BUSY:
                    raise Conflict(TURN_IN_PROGRESS_MESSAGE, code="turn_in_progress")
                turn = claim.turn
                if not isinstance(turn.state_before_json, Mapping):        # 認領已確認有快照；防禦資料損壞（整段復原，不留認領）
                    raise NotRevisable(NOT_REVISABLE_NO_SNAPSHOT)
                attempt = turn.attempt
                taken_over = claim.outcome == REVISION_TAKEN_OVER
                is_free = turn.regen_count < free_limit               # 重新生成與編輯共用同一組免費次數（每回合前 N 次）
                if taken_over:                                    # 舊執行者的回饋列先標為失敗，新的一列才不會被一起標記
                    self._regenerations.fail_pending_for_turn(current.id, session_public_id, turn_seq=turn.seq, now=now)
                self._regenerations.add_pending(
                    current.id, session_public_id, turn_seq=turn.seq, attempt=attempt, kind=kind, category=chosen_category, reason=reason_text,
                    is_free=is_free,
                )
                key: str | None = None
                cost = 0
                if not is_free:
                    key = hold_key(session_public_id, msg_id, attempt)
                    cost = self._wallet.hold(current, key=key, session_public_id=session_public_id, model_key=DEFAULT_MODEL_KEY).cost
        except NotRevisableError as exc:
            raise NotRevisable(exc.reason) from exc
        except TurnInProgressError as exc:
            raise Conflict(TURN_IN_PROGRESS_MESSAGE, code="turn_in_progress") from exc

        plan = self._revision_plan(
            current, save, turn, tier=tier, action=new_action if kind == KIND_EDIT else turn.user_action, kind=kind, taken_over=taken_over,
            is_free=is_free, key=key, cost=cost, free_limit=free_limit,
        )
        rpg_events.log_revision_started(user_id=current.id, session_id=save.id, kind=kind, attempt=attempt, is_free=is_free, cost=cost)
        return plan

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

        tier = self._policy.save_mode(current, save.script_snapshot_json)
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

    # ---- 唯讀 -----------------------------------------------------------------------------------

    def balance(self, current: CurrentUser) -> int:
        """目前的遊戲幣合計（純讀取；重送事件的 `done.coins` 用）。"""
        return self._wallet.balance(current.id)

    def session_snapshot(self, current: CurrentUser, session_public_id: str) -> dict[str, Any]:
        """存檔狀態視圖：對外識別碼、狀態、第 0 幕、世界狀態的玩家視圖、記事簿用量摘要與已完成輪數。已完結的存檔也可以讀。

        沒有內部編號、來源版本與劇本複本全文（只有標題）；世界狀態只含玩家可以知道的欄位（NPC 的特質與隱藏的歸屬不在其中）；
        記事簿只有版本、字數、容量與是否已滿，**沒有全文**（全文只走 `NotebookService.view`）。
        `last_turn` 是最後一輪的修訂資訊（沒有完成的回合時為空）：`client_msg_id`、`seq`、`revision`、是否可修訂與原因、剩餘免費次數與兩種操作的幣價。"""
        self._policy.require_chat_access(current)
        save = self._sessions.get_owned(current.id, session_public_id)
        if save is None:
            raise NotFound()
        script = save.script_snapshot_json if isinstance(save.script_snapshot_json, dict) else {}
        return {
            "id": save.public_id, "title": script.get("title"), "content_tier": self._policy.save_mode(current, script), "status": save.status,
            "ended": save.status == SESSION_ENDED, "protagonist": save.protagonist_json, "version": save.version,
            "completed_turns": self._sessions.count_complete_turns(current.id, session_public_id) or 0,
            "opening": opening_view(save, script), "world": world_view(save),
            "notebook": notebook_policy.summary_of(save.notebook_text, save.notebook_version, current.member_tier),
            "last_turn": self._last_turn_view(current, save.public_id),
        }

    def turn_views(
        self, current: CurrentUser, session_public_id: str, *, since_seq: object = 0, limit: object = TURNS_PAGE_MAX,
    ) -> tuple[list[dict[str, Any]], bool]:
        """序號大於 `since_seq` 的回合（遞增），以及是否還有更多。只有 `complete` 的回合含劇情；被擋、中斷與生成中的回合
        只標示狀態，不含任何部分內容。已完結的存檔也可以讀。"""
        self._policy.require_chat_access(current)
        if isinstance(since_seq, bool) or not isinstance(since_seq, int) or since_seq < 0:
            raise ValidationError(fields={"since_seq": "必須是不小於 0 的整數。"})
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= TURNS_PAGE_MAX:
            raise ValidationError(fields={"limit": f"每次筆數須為 1 至 {TURNS_PAGE_MAX}。"})
        turns = self._sessions.list_turns(current.id, session_public_id, since_seq=since_seq, limit=limit + 1)
        if turns is None:
            raise NotFound()
        return [turn_view(turn) for turn in turns[:limit]], len(turns) > limit

    # ---- 內部 -----------------------------------------------------------------------------------

    def _replay(self, save: RpgSession, turn: MemoryLog) -> TurnReplay:
        return TurnReplay(
            session_public_id=save.public_id, seq=turn.seq, client_msg_id=turn.client_msg_id, attempt=turn.attempt,
            turn_number=turn.turn_number, story=turn.llm_raw_response, ended=save.status == SESSION_ENDED, revision=turn.revision,
            free_left=free_left_of(self._limits.rpg_free_regenerations(), turn.regen_count),
        )

    def _revision_plan(
        self, current: CurrentUser, save: RpgSession, turn: MemoryLog, *, tier: str, action: str, kind: str, taken_over: bool, is_free: bool,
        key: str | None, cost: int, free_limit: int,
    ) -> RevisionPlan:
        """認領之後凍結修訂的上下文：回合前快照的世界、不含目標回合的歷史、目標回合原本的輪數。`save` 與 `turn` 已被認領的條件式
        UPDATE 標為過期，這裡讀到的是鎖內最新的值（`version` 因此與寫回時比對的版本一致）。"""
        world = world_from_snapshot(turn.state_before_json)
        script = save.script_snapshot_json if isinstance(save.script_snapshot_json, dict) else {}
        protagonist = rpg_world.protagonist_from_session(save.protagonist_json, save.player_name, save.character_age)
        ending_due = rpg_world.compute_ending_due(script, world, protagonist)
        history = history_of(self._sessions.complete_turns_before(current.id, save.public_id, turn.turn_number, limit=rpg_context.HISTORY_TURNS) or [])
        opening = save.opening_text if save.opening_state in (OPENING_READY, OPENING_FALLBACK) else None
        system, contents = rpg_context.build_turn_context(
            script=script, world=world, protagonist=protagonist, history=history, opening_text=opening, notebook=save.notebook_text or "",
            action=action, tier=tier, turn_number=turn.turn_number, ending_due=ending_due, marker=self._marker,
        )
        return RevisionPlan(
            user_id=current.id, session_id=save.id, session_public_id=save.public_id, kind=kind, seq=turn.seq, client_msg_id=turn.client_msg_id,
            attempt=turn.attempt, expected_revision=turn.revision, regen_count=turn.regen_count, free_limit=free_limit, taken_over=taken_over,
            is_free=is_free, hold_key=key, cost=cost, turn_number=turn.turn_number, action=action, version=save.version, tier=tier,
            model=self._model, adult_age=self._policy.adult_age, max_months=self._limits.rpg_max_months_per_turn(), ending_due=ending_due,
            script=script, protagonist=protagonist, world=world, characters=tuple(rpg_world.known_characters(world, script, protagonist)),
            history=history, system_instruction=system, contents=tuple(contents),
        )

    def _last_turn_view(self, current: CurrentUser, session_public_id: str) -> dict[str, Any] | None:
        """最後一輪的修訂資訊（玩家端 `last_turn`）：與 `begin_revision` 用同一個判斷（`revision_state`），畫面與端點不會各說各話。
        沒有完成的回合時為 `None`。沒有快照、認領與內部編號。還有免費次數時，重新生成與編輯的幣價都是 0（兩種操作共用次數）；管理員（`is_active_admin`）永遠是 0。"""
        state = self._sessions.revision_state(
            current.id, session_public_id, now=self._clock(), stale_after_seconds=self._limits.coin_hold_timeout(),
        )
        if state is None or state.client_msg_id is None:
            return None
        free_left = free_left_of(self._limits.rpg_free_regenerations(), state.regen_count or 0)
        cost = 0 if current.is_active_admin else self._limits.coin_turn_cost(DEFAULT_MODEL_KEY)     # 管理員不消耗遊戲幣：費用為 0
        return {
            "client_msg_id": state.client_msg_id, "seq": state.seq, "revision": state.revision, "revisable": state.revisable,
            "reason": state.reason, "free_left": free_left, "regenerate_cost": 0 if free_left > 0 else cost,
            "edit_cost": 0 if free_left > 0 else cost,
        }

    def _plan(
        self, current: CurrentUser, save: RpgSession, *, tier: str, adult_age: int, max_months: int, action: str, client_msg_id: str,
        attempt: int, seq: int, start: str, key: str, cost: int, revision: int, hint_eligible: bool = False, hint_input: bool = False,
    ) -> TurnPlan:
        recent = self._sessions.recent_complete_turns(current.id, save.public_id) or []
        history = history_of(recent)
        completed = self._sessions.count_complete_turns(current.id, save.public_id) or 0
        turn_number = completed + 1
        world = world_of(save)
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
            contents=tuple(contents), revision=revision, free_left=free_left_of(self._limits.rpg_free_regenerations(), 0),
            hint_eligible=hint_eligible, hint_input=hint_input,
        )
