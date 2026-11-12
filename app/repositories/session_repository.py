"""RPG 存檔與回合的資料存取（見 rpg-sessions 規格、AGENTS.md 第 6、7、10、11 節、rpg-data-and-script 的 design D3、D4、D11）。

只用 ORM 表達式，不提交交易（請求結束時統一提交）。

**擁有者隔離**：所有方法的第一個參數是 `user_id`，查詢一律帶 `RpgSession.user_id == user_id`；別人的存檔與不存在的存檔得到相同的結果
（`None`），Service 據此回 404。沒有只憑識別碼取得存檔或回合的方法，管理員也一樣（沒有專用路徑；管理員預設看不到存檔內容）。

**回合**
- `(session_id, client_msg_id)` 與 `(session_id, seq)` 由資料庫唯一約束保證。開始回合先對存檔那一列做條件式 UPDATE
  （同時確認存檔未完結、更新 `last_active_at`）：這個 UPDATE 取得該列的鎖並持有到交易結束，所以同一存檔的並行請求被串行化，
  之後「讀既有回合、`seq` 取目前最大值加 1、寫入」不會互相搶；仍保留撞到唯一約束時重試一次的保險。
- 重送：`complete` 直接回傳既有回合；`interrupted`／`blocked` 重設為 `streaming` 並覆蓋該筆（`attempt` 加 1）；`streaming` 未逾時拋出
  `TurnInProgressError`，逾時（`updated_at` 早於傳入的逾時秒數）視同中斷而接手（`attempt` 同樣加 1）。
- 同一存檔同時只能有一個未逾時的 `streaming` 回合：其他編號的回合開始時拋出 `TurnInProgressError`；其他編號且已逾時的 `streaming` 回合被標為 `interrupted`。
- `attempt`（嘗試序號，從 1 起）是遊戲幣預扣冪等鍵的一部分；完成、中斷與被擋可帶 `attempt`，使被接手而過時的產生器無法改寫新的嘗試。
- 輪數（`turn_number`）只在回合完成時指派（已完成的回合數加 1），所以中斷與被擋的回合不計輪、不佔輪數。

**第 0 幕**（`opening_state`）：搶佔、接手、完成與退回都是帶 `user_id` 與狀態條件的單一 UPDATE；搶佔時的 `opening_started_at` 就是該次生成的憑證，
寫回時必須相同，所以被接手而過時的產生器不會覆蓋新的結果。空值（遷移之前的存檔）視同 `fallback`，永遠不生成。

**記事簿**（rpg-notebook）：寫入只有四個方法，全部是帶 `user_id` 與狀態條件的單一 UPDATE，且**只有進行中的存檔會被改動**（已完結的存檔
記事簿不可變）。`lock_notebook_for_capture` 對自己的存檔做一次「條件成立才生效」的 UPDATE 來取得該列的鎖（持有到交易結束，與 `start_turn`
相同的做法），鎖內讀到的文字與版本才是追加的依據；`append_notebook` 在同一個交易內以這份最新文字算出的完整新文字寫入（版本加 1、
已處理到的輪次設為邊界），標記條件使並行的重複抓取只有一次生效；`advance_notebook_marker` 只推進標記（已滿、放棄與空結果），不改文字、不加版本、
只往前；`save_notebook` 是玩家的儲存，以 `notebook_version` 條件式寫入，不碰標記。AI 追加與玩家儲存都使版本加 1，所以任一方都能偵測到對方。
`turns_in_range` 取抓取視窗的完成回合。記事簿是玩家文字衍生的個資：這裡不記錄任何日誌。

**修訂**（rpg-turn-revision）：重新生成與編輯只針對「最後一輪」（存檔最後一個 `complete` 的回合）。修訂期間回合列仍是 `complete`，
`revising_started_at`（認領時間，逾時視同沒有人在修訂）表示修訂中。`claim_revision` 先取得存檔那一列的鎖（與 `start_turn` 相同，所以認領與
開始新回合被串行化），在鎖內判斷 `revision`、可否修訂，再以單一條件式 UPDATE 認領（`attempt` 加 1）；`release_revision` 與 `replace_turn`
都以 `attempt` 比對，所以被接手的舊執行者什麼都改不了。`replace_turn` 在一個 SAVEPOINT 內替換內容、`revision` 加 1、寫回世界狀態與完結：
任何一步失敗整個復原（保留舊內容）。回合前的世界快照（`state_before_json`）由 `start_turn` 寫入，下一輪完成（`complete_turn` 指派輪數）時
清掉前一輪的快照，所以全存檔最多一個回合有快照。快照與回合內容一樣是玩家文字衍生的個資：不記錄日誌。

**已完結的存檔**：`end_session` 之後不得再開始回合或改動世界狀態；沒有任何方法把狀態改回進行中（不可恢復）。
玩家自行封存（`is_archived`）與劇情完結是兩件事，`set_archived` 對已完結的存檔也有效。

存檔與回合的內容（行動、劇情原文、記事簿、世界狀態、劇本複本）是個資：這裡不記錄任何日誌。
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterator, Mapping, Sequence

import sqlalchemy as sa
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.models.rpg_session import (
    OPENING_FALLBACK, OPENING_GENERATING, OPENING_PENDING, OPENING_READY, SESSION_ACTIVE, SESSION_ENDED, TURN_BLOCKED,
    TURN_COMPLETE, TURN_INTERRUPTED, TURN_STREAMING, MemoryLog, RpgSession,
)
from app.repositories.base import BaseRepository
from app.repositories.errors import (
    InvalidTurnTransitionError, NotRevisableError, SessionEndedError, StaleNotebookVersionError, StaleSessionVersionError,
    TurnInProgressError,
)

# `start_turn` 的結果
TURN_CREATED = "created"          # 新增了一個回合
TURN_RESTARTED = "restarted"      # 重送了中斷或被擋的回合：該筆被重設為生成中
TURN_REPLAYED = "replayed"        # 重送了已完成的回合：回傳既有的回合，沒有新增或改動

# `claim_opening` 的結果
OPENING_CLAIMED = "claimed"            # 取得生成權（pending → generating）
OPENING_TAKEN_OVER = "taken_over"      # 接手了停在 generating 太久的生成
OPENING_BUSY = "busy"                  # 另一個請求正在生成且未逾時
OPENING_DONE = "done"                  # 已經是 ready 或 fallback（或遷移之前的舊存檔）：不再生成

# `claim_revision` 的結果
REVISION_CLAIMED = "claimed"           # 取得修訂權（沒有人在修訂）
REVISION_TAKEN_OVER = "taken_over"     # 接手了逾時未完成的認領（舊執行者之後的替換與釋放都會失敗）
REVISION_CONFLICT = "conflict"         # `expected_revision` 與後端不一致（回合已被修訂過）
REVISION_BUSY = "busy"                 # 同一回合已有未逾時的認領

# 不可修訂的原因（`NotRevisableError.reason`、`RevisionState.reason`）；判斷順序即這裡的順序
NOT_REVISABLE_ENDED = "ended"              # 存檔已完結（含結局那一輪）
NOT_REVISABLE_NOT_LAST = "not_last"        # 不是最後一個完成的回合（被擋、中斷與生成中的嘗試也不是）
NOT_REVISABLE_IN_PROGRESS = "in_progress"  # 存檔有未逾時的生成中回合或修訂
NOT_REVISABLE_NO_SNAPSHOT = "no_snapshot"  # 沒有回合前快照（遷移之前的回合，或已被下一輪清除）
NOT_REVISABLE_REASONS = (NOT_REVISABLE_ENDED, NOT_REVISABLE_NOT_LAST, NOT_REVISABLE_IN_PROGRESS, NOT_REVISABLE_NO_SNAPSHOT)

# 預設的逾時秒數：與 `coin.hold_timeout_seconds` 的保底預設相同；呼叫端（GameService）每次現讀設定後傳入
DEFAULT_TURN_STALE_SECONDS = 180
OPENING_STALE_SECONDS = 90             # `generating` 超過這麼久未完成，視同生成者已不在

# `update_world_state` 可以寫入的欄位（對應存檔的欄位名稱）
WORLD_STATE_FIELDS = (
    "current_location", "status_summary", "entities_json", "goals_json", "milestone_progress_json", "elapsed_months",
)


@dataclass(frozen=True)
class TurnStart:
    turn: MemoryLog
    outcome: str


@dataclass(frozen=True)
class TurnFinish:
    """`finish_turn` 的結果：完成的回合、世界狀態是否寫入（版本不一致或存檔已完結時為 False）、存檔是否在這次轉為已完結。"""

    turn: MemoryLog
    world_updated: bool
    ended: bool


@dataclass(frozen=True)
class RevisionClaim:
    """`claim_revision` 的結果：認領後（或未認領時目前）的回合，與 `REVISION_*` 之一。只有 `claimed` 與 `taken_over` 可以開始修訂。"""

    turn: MemoryLog
    outcome: str


@dataclass(frozen=True)
class RevisionState:
    """最後一輪的修訂狀態（`revision_state`）。存檔還沒有任何完成的回合時，回合欄位都是 `None`、`revisable` 為假、`reason` 為 `None`。
    不可修訂時 `reason` 為 `NOT_REVISABLE_REASONS` 之一；未逾時的認領視為 `in_progress`。"""

    client_msg_id: str | None
    seq: int | None
    turn_number: int | None
    revision: int | None
    regen_count: int | None
    revisable: bool
    reason: str | None


@dataclass(frozen=True)
class NotebookLock:
    """`lock_notebook_for_capture` 取得列鎖之後，在鎖內讀到的記事簿狀態：文字、版本與已處理到的輪次。
    鎖持有到呼叫端的交易結束；之後的追加必須以這份文字為基礎重新計算。"""

    text: str
    version: int
    upto_turn: int


def _exported_protagonist(stored: Any) -> dict[str, Any] | None:
    """匯出的主角：角色卡的各欄位（不含保留的圖片欄位）與錨定事實；沒有主角資料的舊存檔回傳 `None`。"""
    if not isinstance(stored, dict) or not isinstance(stored.get("character"), dict):
        return None
    character = {key: value for key, value in stored["character"].items() if key != "image"}
    anchor = stored.get("anchor")
    return {"character": character, "anchor": dict(anchor) if isinstance(anchor, dict) else None}


class SessionRepository(BaseRepository[RpgSession]):
    model = RpgSession

    # ---- 存檔 ---------------------------------------------------------------------

    def create(
        self, user_id: int, *, source_version_id: int | None, snapshot: Mapping[str, Any], snapshot_spec_version: int,
        protagonist: Mapping[str, Any], content_tier: str, now: datetime,
    ) -> RpgSession:
        """開存檔：複製劇本版本標準化後的完整內容，並給所有欄位初始值（版本號 1、經過 0 個月、記事簿為空…）。

        `protagonist` 是 `{"character": {...}, "anchor": {...} 或 None}`（見 `services/protagonist.build_protagonist`）；
        主角姓名與角色年齡欄位**只在這裡**由角色卡寫入，開存檔之後沒有任何方法能修改它們（角色年齡是錨定事實）。"""
        character = protagonist["character"]
        save = RpgSession(
            user_id=user_id, source_version_id=source_version_id, script_snapshot_json=copy.deepcopy(dict(snapshot)),
            snapshot_spec_version=snapshot_spec_version, player_name=character["name"], character_age=character["age"],
            protagonist_json=copy.deepcopy(dict(protagonist)),
            content_tier=content_tier, last_active_at=now,
            entities_json={}, goals_json={"active": [], "archived": []}, milestone_progress_json={},
            opening_state=OPENING_PENDING,
        )
        self._session.add(save)
        self._session.flush()
        return save

    def get_owned(self, user_id: int, public_id: str) -> RpgSession | None:
        return self._session.scalar(select(RpgSession).where(RpgSession.public_id == public_id, RpgSession.user_id == user_id))

    def list_owned(self, user_id: int, *, include_archived: bool = True, limit: int = 50, offset: int = 0) -> list[RpgSession]:
        """自己的存檔，依最後活動由新到舊。"""
        statement = select(RpgSession).where(RpgSession.user_id == user_id)
        if not include_archived:
            statement = statement.where(RpgSession.is_archived == sa.false())          # SQL Server 沒有 `IS 0`
        statement = statement.order_by(RpgSession.last_active_at.desc(), RpgSession.id.desc()).limit(limit).offset(offset)
        return list(self._session.scalars(statement))

    def complete_turn_counts(self, user_id: int, session_ids: Sequence[int]) -> dict[int, int]:
        """自己的各存檔已完成的輪數（只計 `complete` 的回合；被擋、中斷、生成中的不計，第 0 幕不是回合所以也不計），一次查詢。

        以 `sessions.user_id` 為條件（外部連接 `memory_logs`），所以**只有呼叫者自己的存檔**會出現在結果裡：別人的與不存在的
        存檔編號沒有鍵，不會被計入也不會因此洩漏什麼；自己的存檔沒有完整回合時為 0。傳入空清單回傳空字典、不查詢。
        """
        ids = list(dict.fromkeys(session_ids))
        if not ids:
            return {}
        statement = (
            select(RpgSession.id, func.count(MemoryLog.id))
            .outerjoin(MemoryLog, sa.and_(MemoryLog.session_id == RpgSession.id, MemoryLog.turn_status == TURN_COMPLETE))
            .where(RpgSession.user_id == user_id, RpgSession.id.in_(ids))
            .group_by(RpgSession.id)
        )
        return {session_id: count for session_id, count in self._session.execute(statement).all()}

    def _update_owned(self, save: RpgSession, *conditions, **values: Any) -> int:
        """對自己的存檔做條件式 UPDATE，之後讓已載入的物件重新讀取。回傳影響的列數。"""
        result = self._session.execute(
            update(RpgSession).where(RpgSession.id == save.id, RpgSession.user_id == save.user_id, *conditions)
            .values(**values).execution_options(synchronize_session=False)
        )
        self._session.expire(save)
        return result.rowcount

    # ---- 世界狀態（以版本號偵測衝突）--------------------------------------------------------

    def update_world_state(self, user_id: int, public_id: str, *, expected_version: int, **changes: Any) -> RpgSession | None:
        """以預期的版本號更新世界狀態（成功時版本號加 1）。只接受 `WORLD_STATE_FIELDS` 的欄位。

        不是自己的存檔回傳 `None`；已完結拋出 `SessionEndedError`（資料不變）；版本號不一致拋出 `StaleSessionVersionError`（狀態不變）。
        """
        unknown = set(changes) - set(WORLD_STATE_FIELDS)
        if unknown:
            raise ValueError("不是世界狀態的欄位：" + "、".join(sorted(unknown)))
        save = self.get_owned(user_id, public_id)
        if save is None:
            return None
        changed = self._update_owned(
            save, RpgSession.status == SESSION_ACTIVE, RpgSession.version == expected_version,
            version=RpgSession.version + 1, **changes,
        )
        if changed != 1:
            if save.status == SESSION_ENDED:
                raise SessionEndedError
            raise StaleSessionVersionError
        return save

    # ---- 完結與封存 ---------------------------------------------------------------------

    def end_session(self, user_id: int, public_id: str, *, now: datetime) -> bool | None:
        """標記為已完結並記錄完結時間。回傳 `True`；本來就已完結回傳 `False`（完結時間不變）；不是自己的回傳 `None`。
        沒有任何方法能把已完結的存檔改回進行中。"""
        save = self.get_owned(user_id, public_id)
        if save is None:
            return None
        return self._update_owned(save, RpgSession.status == SESSION_ACTIVE, status=SESSION_ENDED, ended_at=now) == 1

    def set_archived(self, user_id: int, public_id: str, archived: bool) -> bool | None:
        """玩家自行封存或取消封存（與劇情完結無關，已完結的存檔也可以）。不是自己的回傳 `None`。"""
        save = self.get_owned(user_id, public_id)
        if save is None:
            return None
        self._update_owned(save, is_archived=archived)
        return True

    # ---- 回合 ---------------------------------------------------------------------------

    def get_turn(self, user_id: int, public_id: str, client_msg_id: str) -> MemoryLog | None:
        save = self.get_owned(user_id, public_id)
        if save is None:
            return None
        return self._turn_by_client_id(save.id, client_msg_id)

    def list_turns(self, user_id: int, public_id: str, *, since_seq: int = 0, limit: int = 200) -> list[MemoryLog] | None:
        """存檔的回合，依序號由小到大（序號大於 `since_seq`）；已完結的存檔也可以讀。不是自己的存檔回傳 `None`。"""
        save = self.get_owned(user_id, public_id)
        if save is None:
            return None
        statement = select(MemoryLog).where(MemoryLog.session_id == save.id, MemoryLog.seq > since_seq).order_by(MemoryLog.seq).limit(limit)
        return list(self._session.scalars(statement))

    def recent_complete_turns(self, user_id: int, public_id: str, *, limit: int = 10) -> list[MemoryLog] | None:
        """最近 `limit` 個「完成」的回合，依輪數由舊到新（回合引擎的視窗用；中斷、被擋與生成中的回合不在其中）。不是自己的存檔回傳 `None`。"""
        save = self.get_owned(user_id, public_id)
        if save is None:
            return None
        statement = (
            select(MemoryLog).where(MemoryLog.session_id == save.id, MemoryLog.turn_status == TURN_COMPLETE, MemoryLog.turn_number.is_not(None))
            .order_by(MemoryLog.turn_number.desc()).limit(limit)
        )
        return list(reversed(list(self._session.scalars(statement))))

    @contextmanager
    def savepoint(self) -> Iterator[Any]:
        """一個 SAVEPOINT 的情境管理員：區塊內拋出例外時，區塊內的所有寫入（回合列、預扣）一併復原，請求仍可回傳 4xx。
        先讓交易開始（一個不影響任何列的 UPDATE）再進入 SAVEPOINT，與錢包的做法相同（SQLite 的驅動在第一個寫入之前不會開始交易）。"""
        self._session.execute(
            update(RpgSession).where(RpgSession.id == -1).values(version=RpgSession.version).execution_options(synchronize_session=False)
        )
        with self._session.begin_nested() as nested:
            yield nested

    def count_complete_turns(self, user_id: int, public_id: str) -> int | None:
        save = self.get_owned(user_id, public_id)
        if save is None:
            return None
        return self._complete_count(save.id)

    def start_turn(
        self, user_id: int, public_id: str, *, client_msg_id: str, user_action: str, now: datetime,
        stale_after_seconds: int = DEFAULT_TURN_STALE_SECONDS, state_before: Mapping[str, Any] | None = None,
    ) -> TurnStart | None:
        """開始一個回合（冪等）。不是自己的存檔回傳 `None`；存檔已完結拋出 `SessionEndedError`。

        - 同一個編號：`complete` 回傳既有回合（`replayed`）；`interrupted`／`blocked` 重設並把 `attempt` 加 1；`streaming` 未逾時拋出
          `TurnInProgressError`，逾時（`updated_at` 早於 `now` 減 `stale_after_seconds`）視同中斷而接手（`attempt` 加 1）。
        - 同一存檔已有「其他編號且未逾時」的 `streaming` 回合：拋出 `TurnInProgressError`（同時只能有一個）；其他編號而逾時者標為 `interrupted`。
        - 同一存檔有未逾時的修訂認領（`claim_revision`）：同樣拋出 `TurnInProgressError`；認領逾時後不再阻擋。
        `stale_after_seconds` 由呼叫端每次現讀（遊戲幣的預扣逾時秒數），這樣預扣與回合同時到期。

        `state_before` 是回合開始前的世界快照（`WorldState.to_fields()` 的六欄），寫入新回合列的 `state_before_json`，供之後修訂這一輪時
        還原；重送被擋或中斷的回合時**覆寫**（沒帶就清為空：舊快照對應的是更早的世界）。重送已完成的回合不改動它。"""
        save = self.get_owned(user_id, public_id)
        if save is None:
            return None
        # 取得存檔那一列的鎖（持有到交易結束）並確認未完結：同一存檔的並行請求從這裡開始被串行化
        if self._update_owned(save, RpgSession.status == SESSION_ACTIVE, last_active_at=now) != 1:
            raise SessionEndedError
        cutoff = now - timedelta(seconds=stale_after_seconds)
        for attempt in (1, 2):
            existing = self._turn_by_client_id(save.id, client_msg_id)
            if existing is not None:
                if existing.turn_status == TURN_COMPLETE:
                    return TurnStart(existing, TURN_REPLAYED)
                self._ensure_no_other_streaming(save.id, except_turn_id=existing.id, cutoff=cutoff, now=now)
                return self._resend(existing, user_action, cutoff=cutoff, now=now, state_before=state_before)
            self._ensure_no_other_streaming(save.id, except_turn_id=None, cutoff=cutoff, now=now)
            try:
                with self._session.begin_nested():
                    turn = MemoryLog(
                        session_id=save.id, client_msg_id=client_msg_id, seq=self._next_seq(save.id),
                        turn_status=TURN_STREAMING, user_action=user_action, llm_raw_response="", attempt=1,
                        created_at=now, updated_at=now,
                    )
                    if state_before is not None:                  # 沒帶就不設定這個屬性：資料庫存 SQL 的 NULL，不是 JSON 的 null
                        turn.state_before_json = copy.deepcopy(dict(state_before))
                    self._session.add(turn)
                    self._session.flush()
                return TurnStart(turn, TURN_CREATED)
            except IntegrityError:
                if attempt == 2:
                    raise
        raise AssertionError("不會走到這裡")           # pragma: no cover

    def complete_turn(
        self, user_id: int, public_id: str, client_msg_id: str, *, llm_raw_response: str, structured_result: Mapping[str, Any] | None,
        now: datetime, attempt: int | None = None,
    ) -> MemoryLog | None:
        """回合完成：指派輪數（已完成的回合數加 1）。不是自己的回傳 `None`；不是生成中（或 `attempt` 不符）拋出 `InvalidTurnTransitionError`。

        同一個交易內，前一輪（輪數少 1）的回合前快照被清為空：新的一輪完成後，前一輪不能再被修訂（`finish_turn` 經由這裡完成回合）。
        被擋與中斷的回合不會走到這裡，所以不清除。"""
        save = self.get_owned(user_id, public_id)
        if save is None:
            return None
        self._update_owned(save, last_active_at=now)             # 取得存檔那一列的鎖，輪數的指派因此不會被並行的完成搶走
        turn = self._turn_by_client_id(save.id, client_msg_id)
        if turn is None:
            return None
        number = self._complete_count(save.id) + 1
        self._finish_turn(
            turn, TURN_COMPLETE, attempt=attempt, turn_number=number, llm_raw_response=llm_raw_response,
            structured_result=dict(structured_result) if structured_result is not None else None,
        )
        previous = self._session.scalar(select(MemoryLog).where(MemoryLog.session_id == save.id, MemoryLog.turn_number == number - 1))
        if previous is not None:
            self._session.execute(                                # 前一輪的快照：SQL 的 NULL（不是 JSON 的 null）
                update(MemoryLog).where(MemoryLog.id == previous.id).values(state_before_json=sa.null())
                .execution_options(synchronize_session=False)
            )
            self._session.expire(previous)                        # 已載入的物件不留著舊快照
        return turn

    def finish_turn(
        self, user_id: int, public_id: str, client_msg_id: str, *, llm_raw_response: str, structured_result: Mapping[str, Any] | None,
        now: datetime, attempt: int | None = None, world_changes: Mapping[str, Any] | None = None, expected_version: int | None = None,
        end_story: bool = False,
    ) -> TurnFinish | None:
        """回合的收尾：完成回合（指派輪數）、寫入世界狀態、必要時轉為已完結——全部在呼叫端的同一個交易內。

        回合一定完成；世界狀態只在 `world_changes` 不是 `None` 且 `expected_version` 與後端一致時寫入，版本不一致（或存檔已被完結）時
        不寫入、回合仍完成（`world_updated=False`，以後端為準）。`end_story` 為真時在回合完成後把存檔轉為已完結。
        不是自己的回傳 `None`；回合不是生成中（或 `attempt` 不符）拋出 `InvalidTurnTransitionError`（整個收尾沒有任何改動）。"""
        turn = self.complete_turn(
            user_id, public_id, client_msg_id, llm_raw_response=llm_raw_response, structured_result=structured_result, now=now,
            attempt=attempt,
        )
        if turn is None:
            return None
        world_updated = False
        if world_changes is not None:
            if expected_version is None:
                raise ValueError("寫入世界狀態必須帶預期的版本號。")
            try:
                world_updated = self.update_world_state(user_id, public_id, expected_version=expected_version, **dict(world_changes)) is not None
            except (StaleSessionVersionError, SessionEndedError):
                world_updated = False
        ended = bool(end_story) and self.end_session(user_id, public_id, now=now) is True
        return TurnFinish(turn, world_updated, ended)

    def interrupt_turn(
        self, user_id: int, public_id: str, client_msg_id: str, *, partial_response: str, attempt: int | None = None
    ) -> MemoryLog | None:
        """串流中斷：保留已收到的片段，不計輪。"""
        return self._end_unfinished(user_id, public_id, client_msg_id, TURN_INTERRUPTED, partial_response, attempt=attempt)

    def block_turn(
        self, user_id: int, public_id: str, client_msg_id: str, *, partial_response: str = "", attempt: int | None = None,
        structured_result: Mapping[str, Any] | None = None,
    ) -> MemoryLog | None:
        """被審查擋下：不計輪，也不進上下文。`structured_result` 通常是 `{"blocked": {"categories": [...], "auto_retry": ...}}`。"""
        return self._end_unfinished(
            user_id, public_id, client_msg_id, TURN_BLOCKED, partial_response, attempt=attempt, structured_result=structured_result
        )

    def save_blocked_text(
        self, user_id: int, public_id: str, client_msg_id: str, *, text: str, structured_result: Mapping[str, Any], attempt: int | None = None,
    ) -> MemoryLog | None:
        """第一次被擋時先把被擋的文字與類別記在仍為 `streaming` 的回合上（自動重生期間；之後的結局由 `block_turn`／`finish_turn` 覆蓋）。
        不改變回合狀態；回合不是生成中（或 `attempt` 不符）拋出 `InvalidTurnTransitionError`。"""
        save = self.get_owned(user_id, public_id)
        if save is None:
            return None
        turn = self._turn_by_client_id(save.id, client_msg_id)
        if turn is None:
            return None
        conditions = [MemoryLog.id == turn.id, MemoryLog.turn_status == TURN_STREAMING]
        if attempt is not None:
            conditions.append(MemoryLog.attempt == attempt)
        result = self._session.execute(
            update(MemoryLog).where(*conditions).values(llm_raw_response=text, structured_result=dict(structured_result))
            .execution_options(synchronize_session=False)
        )
        self._session.expire(turn)
        if result.rowcount != 1:
            raise InvalidTurnTransitionError
        return turn

    # ---- 第 0 幕（條件式 UPDATE：搶佔、接手、完成、退回）-----------------------------------------

    def claim_opening(
        self, user_id: int, public_id: str, *, now: datetime, stale_after_seconds: int = OPENING_STALE_SECONDS
    ) -> tuple[str, RpgSession] | None:
        """嘗試取得第 0 幕的生成權。不是自己的存檔回傳 `None`；否則回傳（結果, 存檔）：
        `claimed`（pending → generating）、`taken_over`（generating 超過 `stale_after_seconds`）、`busy`（另一個請求正在生成且未逾時）、
        `done`（已經是 ready／fallback，或遷移之前的存檔為空值）。只有 `claimed` 與 `taken_over` 的呼叫者可以生成；
        存檔的 `opening_started_at` 就是這次生成的憑證，寫回時必須帶同一個值。"""
        save = self.get_owned(user_id, public_id)
        if save is None:
            return None
        if self._update_owned(
            save, RpgSession.opening_state == OPENING_PENDING, opening_state=OPENING_GENERATING, opening_started_at=now, opening_text=None,
        ) == 1:
            return OPENING_CLAIMED, save
        cutoff = now - timedelta(seconds=stale_after_seconds)
        if self._update_owned(
            save, RpgSession.opening_state == OPENING_GENERATING, RpgSession.opening_started_at < cutoff, opening_started_at=now,
        ) == 1:
            return OPENING_TAKEN_OVER, save
        return (OPENING_BUSY if save.opening_state == OPENING_GENERATING else OPENING_DONE), save

    def finish_opening(
        self, user_id: int, public_id: str, *, claimed_at: datetime, state: str, text: str,
    ) -> bool | None:
        """第 0 幕生成完成：`state` 為 `ready`（文字是生成的開場）或 `fallback`（文字是劇本的初始對話，可為空字串）。
        只有「仍是 generating 且憑證（開始時間）相同」才寫入；已被接手或已完成則不改動並回傳 `False`。不是自己的存檔回傳 `None`。"""
        if state not in (OPENING_READY, OPENING_FALLBACK):
            raise ValueError("完成的第 0 幕必須是 ready 或 fallback。")
        save = self.get_owned(user_id, public_id)
        if save is None:
            return None
        return self._update_owned(
            save, RpgSession.opening_state == OPENING_GENERATING, RpgSession.opening_started_at == claimed_at,
            opening_state=state, opening_text=text,
        ) == 1

    def reset_opening(self, user_id: int, public_id: str, *, claimed_at: datetime) -> bool | None:
        """客戶端斷線：退回 pending、丟棄部分文字與開始時間，下次進入再生成。條件同 `finish_opening`。"""
        save = self.get_owned(user_id, public_id)
        if save is None:
            return None
        return self._update_owned(
            save, RpgSession.opening_state == OPENING_GENERATING, RpgSession.opening_started_at == claimed_at,
            opening_state=OPENING_PENDING, opening_text=None, opening_started_at=None,
        ) == 1

    # ---- 記事簿（抓取的視窗與鎖、AI 追加、玩家儲存；皆為條件式 UPDATE，只改動進行中的存檔）------------------

    def turns_in_range(self, user_id: int, public_id: str, first: int, last: int) -> list[MemoryLog] | None:
        """輪數在 `first` 到 `last`（含）之間的「完成」回合，依輪數由小到大（記事簿抓取的視窗）。中斷、被擋與生成中的回合沒有輪數，
        不在其中。不是自己的存檔回傳 `None`。"""
        save = self.get_owned(user_id, public_id)
        if save is None:
            return None
        statement = (
            select(MemoryLog).where(
                MemoryLog.session_id == save.id, MemoryLog.turn_status == TURN_COMPLETE, MemoryLog.turn_number.is_not(None),
                MemoryLog.turn_number >= first, MemoryLog.turn_number <= last,
            ).order_by(MemoryLog.turn_number)
        )
        return list(self._session.scalars(statement))

    def lock_notebook_for_capture(self, user_id: int, public_id: str, boundary: int) -> NotebookLock | None:
        """取得抓取的寫入鎖：對自己的存檔做一次條件式 UPDATE（存檔進行中，且已處理到的輪次小於 `boundary`），成立就持有該列的鎖到交易結束，
        並回傳鎖內讀到的最新文字、版本與標記。其他請求（重複的抓取、玩家的儲存）在鎖釋放之前不會改動這一列，所以追加是對最新文字做的。
        不成立（不是自己的存檔、已完結、或標記已被推進到 `boundary` 以上）回傳 `None`，什麼都沒有改動。"""
        save = self.get_owned(user_id, public_id)
        if save is None:
            return None
        if self._update_owned(
            save, RpgSession.status == SESSION_ACTIVE, RpgSession.notebook_upto_turn < boundary,
            notebook_upto_turn=RpgSession.notebook_upto_turn,                       # 不改任何值：只為了取得列鎖
        ) != 1:
            return None
        return NotebookLock(text=save.notebook_text or "", version=save.notebook_version, upto_turn=save.notebook_upto_turn)

    def append_notebook(self, user_id: int, public_id: str, *, text: str, boundary: int) -> bool | None:
        """AI 追加：寫入完整的新文字，版本加 1，已處理到的輪次設為 `boundary`。條件同 `lock_notebook_for_capture`（進行中且標記小於
        `boundary`），所以並行的重複抓取只有一次生效。**`text` 必須是以同一個交易內 `lock_notebook_for_capture` 回傳的最新文字算出的**，
        否則會覆蓋玩家剛做的編輯。成功回傳 `True`；條件不成立回傳 `False`（沒有改動）；不是自己的存檔回傳 `None`。"""
        save = self.get_owned(user_id, public_id)
        if save is None:
            return None
        return self._update_owned(
            save, RpgSession.status == SESSION_ACTIVE, RpgSession.notebook_upto_turn < boundary,
            notebook_text=text, notebook_version=RpgSession.notebook_version + 1, notebook_upto_turn=boundary,
        ) == 1

    def advance_notebook_marker(self, user_id: int, public_id: str, boundary: int) -> bool | None:
        """只推進已處理到的輪次（記事簿已滿、放棄與沒有重點時）：不改文字、不加版本。只往前（標記小於 `boundary` 才生效）、只改動進行中的存檔。
        成功回傳 `True`；條件不成立回傳 `False`；不是自己的存檔回傳 `None`。"""
        save = self.get_owned(user_id, public_id)
        if save is None:
            return None
        return self._update_owned(
            save, RpgSession.status == SESSION_ACTIVE, RpgSession.notebook_upto_turn < boundary, notebook_upto_turn=boundary,
        ) == 1

    def save_notebook(self, user_id: int, public_id: str, *, text: str, expected_version: int) -> RpgSession | None:
        """玩家儲存編輯後的記事簿：存檔進行中且 `notebook_version` 等於 `expected_version` 才寫入（版本加 1）；不改已處理到的輪次。
        不是自己的存檔回傳 `None`；已完結拋出 `SessionEndedError`；版本不一致拋出 `StaleNotebookVersionError`（資料都不變）。
        容量與規則層的檢查是 Service 的責任，這裡只保證條件式寫入。"""
        save = self.get_owned(user_id, public_id)
        if save is None:
            return None
        changed = self._update_owned(
            save, RpgSession.status == SESSION_ACTIVE, RpgSession.notebook_version == expected_version,
            notebook_text=text, notebook_version=RpgSession.notebook_version + 1,
        )
        if changed != 1:
            if save.status == SESSION_ENDED:
                raise SessionEndedError                      # 已完結優先於版本衝突：已完結的記事簿沒有「重新載入再編輯」可言
            raise StaleNotebookVersionError
        return save

    # ---- 修訂：重新生成與編輯最後一輪（認領、釋放、替換；皆為條件式 UPDATE）--------------------------------

    def revision_state(
        self, user_id: int, public_id: str, *, now: datetime, stale_after_seconds: int = DEFAULT_TURN_STALE_SECONDS
    ) -> RevisionState | None:
        """最後一輪（存檔最後一個 `complete` 的回合）的修訂狀態；與 `claim_revision` 用同一個判斷，所以畫面與端點不會各說各話。
        不是自己的存檔回傳 `None`；還沒有完成的回合時回傳各欄為空、`revisable` 為假的狀態。
        未逾時的認領也算 `in_progress`（`claim_revision` 則回報 `busy`）。`now` 與 `stale_after_seconds` 用來判斷生成中與認領是否逾時。"""
        save = self.get_owned(user_id, public_id)
        if save is None:
            return None
        last = self._last_complete_turn(save.id)
        if last is None:
            return RevisionState(None, None, None, None, None, False, None)
        reason = self._not_revisable_reason(save.id, last, cutoff=now - timedelta(seconds=stale_after_seconds), include_claim=True)
        return RevisionState(last.client_msg_id, last.seq, last.turn_number, last.revision, last.regen_count, reason is None, reason)

    def claim_revision(
        self, user_id: int, public_id: str, client_msg_id: str, *, expected_revision: int, now: datetime,
        stale_after_seconds: int = DEFAULT_TURN_STALE_SECONDS,
    ) -> RevisionClaim | None:
        """認領修訂權。不是自己的存檔或回合回傳 `None`；否則回傳（回合, 結果）：

        - 先對存檔那一列做條件式 UPDATE 取得列鎖（與 `start_turn` 相同，並行的認領與新回合因此被串行化），之後的判斷都是對鎖內的最新資料；
        - `conflict`：`expected_revision` 與後端的 `revision` 不一致（優先於其他判斷）；
        - 不可修訂拋出 `NotRevisableError`（`ended`、`not_last`、`in_progress`、`no_snapshot`）；
        - `claimed`／`taken_over`：單一條件式 UPDATE（回合為 `complete`、`revision` 相符、沒有未逾時的認領）成功，`revising_started_at` 設為 `now`、
          `attempt` 加 1（預扣鍵因此每次不同）；認領早已逾時（`stale_after_seconds`）者是 `taken_over`，舊執行者之後的 `replace_turn` 與
          `release_revision` 因 `attempt` 不符而失敗；
        - `busy`：同一回合已有未逾時的認領，沒有任何改動（除了存檔的最後活動時間）。
        `stale_after_seconds` 由呼叫端每次現讀（遊戲幣的預扣逾時秒數），認領與預扣因此同時到期。"""
        save = self.get_owned(user_id, public_id)
        if save is None or self._turn_by_client_id(save.id, client_msg_id) is None:
            return None
        self._update_owned(save, RpgSession.status == SESSION_ACTIVE, last_active_at=now)       # 取得列鎖；已完結時不成立，下面回報 ended
        turn = self._fresh_turn(save.id, client_msg_id)
        if turn.revision != expected_revision:
            return RevisionClaim(turn, REVISION_CONFLICT)
        cutoff = now - timedelta(seconds=stale_after_seconds)
        reason = self._not_revisable_reason(save.id, turn, cutoff=cutoff, include_claim=False)
        if reason is not None:
            raise NotRevisableError(reason)
        taking_over = turn.revising_started_at is not None            # 走到這裡的非空認領一定已逾時，否則下面的條件不成立
        result = self._session.execute(
            update(MemoryLog).where(
                MemoryLog.id == turn.id, MemoryLog.turn_status == TURN_COMPLETE, MemoryLog.revision == expected_revision,
                MemoryLog.revising_started_at.is_(None) | (MemoryLog.revising_started_at < cutoff),
            ).values(revising_started_at=now, attempt=MemoryLog.attempt + 1).execution_options(synchronize_session=False)
        )
        self._session.expire(turn)
        if result.rowcount != 1:
            return RevisionClaim(turn, REVISION_BUSY)
        return RevisionClaim(turn, REVISION_TAKEN_OVER if taking_over else REVISION_CLAIMED)

    def release_revision(self, user_id: int, public_id: str, client_msg_id: str, *, attempt: int) -> bool | None:
        """修訂失敗、被擋或中斷時釋放自己的認領（舊內容原樣保留）。只有 `attempt` 相符且仍有認領者才生效，所以已被接手而過時的執行者
        不會清掉新的認領；已經替換成功（認領已清除）者也是 `False`。不是自己的回傳 `None`。"""
        save = self.get_owned(user_id, public_id)
        if save is None:
            return None
        turn = self._turn_by_client_id(save.id, client_msg_id)
        if turn is None:
            return None
        result = self._session.execute(
            update(MemoryLog).where(
                MemoryLog.id == turn.id, MemoryLog.turn_status == TURN_COMPLETE, MemoryLog.attempt == attempt,
                MemoryLog.revising_started_at.is_not(None),
            ).values(revising_started_at=None).execution_options(synchronize_session=False)
        )
        self._session.expire(turn)
        return result.rowcount == 1

    def replace_turn(
        self, user_id: int, public_id: str, client_msg_id: str, *, expected_revision: int, attempt: int, llm_raw_response: str,
        structured_result: Mapping[str, Any] | None, now: datetime, user_action: str | None = None, reset_regen_count: bool = False,
        world_changes: Mapping[str, Any] | None = None, expected_version: int | None = None, end_story: bool = False,
    ) -> TurnFinish | None:
        """修訂成功：以新的回應替換回合的內容。輪數與序號不變、`revision` 加 1、認領清除；`regen_count` 加 1（編輯時 `reset_regen_count`
        為真則歸零）；`user_action` 不是 `None` 時一併替換（編輯）；快照不動（這一輪仍是最後一輪，可以再修訂）。

        條件式 UPDATE 要求回合為 `complete`、`revision` 與 `attempt` 相符且仍有認領；不符（過時的執行者、已被接手、已被別人修訂）拋出
        `InvalidTurnTransitionError`。`world_changes`（`WorldState.to_fields()` 的六欄）以 `expected_version` 比對後寫回，`end_story`
        為真時再轉為已完結。**全部在一個 SAVEPOINT 內：任何一步失敗（包含世界版本不一致 `StaleSessionVersionError`、存檔已完結
        `SessionEndedError`）整個復原，舊內容原樣保留**——這與 `finish_turn` 不同（那裡文字已送達，世界狀態寫不進去仍然完成）。
        不是自己的回傳 `None`。"""
        save = self.get_owned(user_id, public_id)
        if save is None:
            return None
        turn = self._turn_by_client_id(save.id, client_msg_id)
        if turn is None:
            return None
        if world_changes is not None and expected_version is None:
            raise ValueError("寫入世界狀態必須帶預期的版本號。")
        values: dict[str, Any] = {
            "llm_raw_response": llm_raw_response, "structured_result": dict(structured_result) if structured_result is not None else None,
            "revision": MemoryLog.revision + 1, "revising_started_at": None,
            "regen_count": 0 if reset_regen_count else MemoryLog.regen_count + 1, "updated_at": now,
        }
        if user_action is not None:
            values["user_action"] = user_action
        with self.savepoint():
            if self._update_owned(save, RpgSession.status == SESSION_ACTIVE, last_active_at=now) != 1:
                raise SessionEndedError
            result = self._session.execute(
                update(MemoryLog).where(
                    MemoryLog.id == turn.id, MemoryLog.turn_status == TURN_COMPLETE, MemoryLog.revision == expected_revision,
                    MemoryLog.attempt == attempt, MemoryLog.revising_started_at.is_not(None),
                ).values(**values).execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise InvalidTurnTransitionError
            world_updated = False
            if world_changes is not None:
                world_updated = self.update_world_state(user_id, public_id, expected_version=expected_version, **dict(world_changes)) is not None
            ended = bool(end_story) and self.end_session(user_id, public_id, now=now) is True
        self._session.expire(turn)
        return TurnFinish(turn, world_updated, ended)

    def complete_turns_before(self, user_id: int, public_id: str, turn_number: int, limit: int = 10) -> list[MemoryLog] | None:
        """輪數小於 `turn_number` 的最近 `limit` 個「完成」回合，依輪數由舊到新（修訂的上下文歷史：不含目標回合與其後的回合）。
        不是自己的存檔回傳 `None`。"""
        save = self.get_owned(user_id, public_id)
        if save is None:
            return None
        statement = (
            select(MemoryLog).where(
                MemoryLog.session_id == save.id, MemoryLog.turn_status == TURN_COMPLETE, MemoryLog.turn_number.is_not(None),
                MemoryLog.turn_number < turn_number,
            ).order_by(MemoryLog.turn_number.desc()).limit(limit)
        )
        return list(reversed(list(self._session.scalars(statement))))

    # ---- 匯出（個人資料匯出用）-------------------------------------------------------------

    def export_sessions(self, user_id: int, *, page_size: int = 50) -> Iterator[dict[str, Any]]:
        """這位使用者的全部存檔（含已完結與已封存的），由舊到新逐筆產生（以存檔編號為游標分頁）。回合另以 `export_turns` 分頁讀取。

        含劇本標題（取自存檔自己的複本）、狀態、內容模式、主角姓名與角色年齡、主角的角色卡與錨定事實（`protagonist`；遷移之前建立的存檔為空）、所在地點、狀態摘要、各時間、記事簿全文、第 0 幕的狀態與文字（遷移之前的存檔為空）與世界狀態
        （`world_state`，只給 JSON 匯出使用）。**不含**劇本複本本身（那是來源劇本作者的內容）、來源版本編號、版本號、第 0 幕的開始時間與其他內部編號。
        """
        after = 0
        while True:
            saves = list(self._session.scalars(
                select(RpgSession).where(RpgSession.user_id == user_id, RpgSession.id > after).order_by(RpgSession.id).limit(page_size)
            ))
            if not saves:
                return
            for save in saves:
                snapshot = save.script_snapshot_json if isinstance(save.script_snapshot_json, dict) else {}
                yield {
                    "id": save.public_id, "script_title": snapshot.get("title"), "status": save.status, "content_tier": save.content_tier,
                    "player_name": save.player_name, "character_age": save.character_age, "current_location": save.current_location,
                    "status_summary": save.status_summary, "elapsed_months": save.elapsed_months, "is_archived": save.is_archived,
                    "created_at": save.created_at, "last_active_at": save.last_active_at, "ended_at": save.ended_at,
                    "notebook_text": save.notebook_text, "protagonist": _exported_protagonist(save.protagonist_json),
                    "opening_state": save.opening_state, "opening_text": save.opening_text,
                    "world_state": {
                        "entities": save.entities_json, "goals": save.goals_json, "milestone_progress": save.milestone_progress_json,
                    },
                }
            after = saves[-1].id

    def export_turns(self, user_id: int, public_id: str, *, page_size: int = 500) -> Iterator[dict[str, Any]]:
        """一個存檔的全部回合，依序號由小到大逐頁產生。不是自己的存檔不產生任何東西。

        含序號、輪數（只有完成的回合有）、狀態、玩家行動、劇情原文與時間。**不含**前端產生的訊息編號與模型的結構化輸出。
        """
        save = self.get_owned(user_id, public_id)
        if save is None:
            return
        session_id, after = save.id, 0
        while True:
            turns = list(self._session.scalars(
                select(MemoryLog).where(MemoryLog.session_id == session_id, MemoryLog.seq > after).order_by(MemoryLog.seq).limit(page_size)
            ))
            if not turns:
                return
            for turn in turns:
                yield {
                    "seq": turn.seq, "turn_number": turn.turn_number, "status": turn.turn_status, "user_action": turn.user_action,
                    "story": turn.llm_raw_response, "created_at": turn.created_at, "updated_at": turn.updated_at,
                }
            after = turns[-1].seq

    # ---- 內部 ---------------------------------------------------------------------------

    def _turn_by_client_id(self, session_id: int, client_msg_id: str) -> MemoryLog | None:
        return self._session.scalar(select(MemoryLog).where(MemoryLog.session_id == session_id, MemoryLog.client_msg_id == client_msg_id))

    def _fresh_turn(self, session_id: int, client_msg_id: str) -> MemoryLog:
        """讀取回合並覆蓋已載入物件的屬性：取得列鎖之後，別的交易剛提交的改動才不會被身分對應表裡的舊值蓋住。"""
        return self._session.scalar(
            select(MemoryLog).where(MemoryLog.session_id == session_id, MemoryLog.client_msg_id == client_msg_id)
            .execution_options(populate_existing=True)
        )

    def _last_complete_turn(self, session_id: int) -> MemoryLog | None:
        """存檔最後一個「完成」的回合（輪數最大者；被擋、中斷與生成中的嘗試沒有輪數，不是最後一輪）。"""
        return self._session.scalar(
            select(MemoryLog).where(MemoryLog.session_id == session_id, MemoryLog.turn_status == TURN_COMPLETE, MemoryLog.turn_number.is_not(None))
            .order_by(MemoryLog.turn_number.desc()).limit(1).execution_options(populate_existing=True)
        )

    def _not_revisable_reason(self, session_id: int, turn: MemoryLog, *, cutoff: datetime, include_claim: bool) -> str | None:
        """回合不能被修訂的原因（依 `NOT_REVISABLE_REASONS` 的順序），可以修訂則回傳 `None`。`include_claim` 為真時，
        這個回合未逾時的認領也算 `in_progress`（`revision_state` 用）；`claim_revision` 自己處理認領（回報 `busy`）。"""
        status = self._session.scalar(select(RpgSession.status).where(RpgSession.id == session_id))
        if status != SESSION_ACTIVE:
            return NOT_REVISABLE_ENDED
        last = self._session.scalar(
            select(func.max(MemoryLog.turn_number)).where(MemoryLog.session_id == session_id, MemoryLog.turn_status == TURN_COMPLETE)
        )
        if turn.turn_status != TURN_COMPLETE or turn.turn_number is None or turn.turn_number != last:
            return NOT_REVISABLE_NOT_LAST
        if self._session.scalar(
            select(func.count()).select_from(MemoryLog).where(
                MemoryLog.session_id == session_id, MemoryLog.turn_status == TURN_STREAMING, MemoryLog.updated_at >= cutoff,
            )
        ):
            return NOT_REVISABLE_IN_PROGRESS
        if include_claim and turn.revising_started_at is not None and turn.revising_started_at >= cutoff:
            return NOT_REVISABLE_IN_PROGRESS
        if turn.state_before_json is None:
            return NOT_REVISABLE_NO_SNAPSHOT
        return None

    def _next_seq(self, session_id: int) -> int:
        return (self._session.scalar(select(func.max(MemoryLog.seq)).where(MemoryLog.session_id == session_id)) or 0) + 1

    def _complete_count(self, session_id: int) -> int:
        return self._session.scalar(
            select(func.count()).select_from(MemoryLog).where(MemoryLog.session_id == session_id, MemoryLog.turn_status == TURN_COMPLETE)
        ) or 0

    def _ensure_no_other_streaming(self, session_id: int, *, except_turn_id: int | None, cutoff: datetime, now: datetime) -> None:
        """同一存檔同時只能有一個未逾時的 `streaming` 回合，且不能與修訂並行。其他編號未逾時者，或有未逾時的修訂認領，拋出
        `TurnInProgressError`；已逾時的 `streaming` 者標為 `interrupted`（逾時的認領只是不再阻擋，不需要清除）。"""
        if self._session.scalar(
            select(func.count()).select_from(MemoryLog).where(
                MemoryLog.session_id == session_id, MemoryLog.revising_started_at.is_not(None), MemoryLog.revising_started_at >= cutoff,
            )
        ):
            raise TurnInProgressError
        others = [MemoryLog.session_id == session_id, MemoryLog.turn_status == TURN_STREAMING]
        if except_turn_id is not None:
            others.append(MemoryLog.id != except_turn_id)
        if self._session.scalar(select(func.count()).select_from(MemoryLog).where(*others, MemoryLog.updated_at >= cutoff)):
            raise TurnInProgressError
        self._session.execute(
            update(MemoryLog).where(*others, MemoryLog.updated_at < cutoff)
            .values(turn_status=TURN_INTERRUPTED, turn_number=None, updated_at=now).execution_options(synchronize_session=False)
        )

    def _resend(
        self, existing: MemoryLog, user_action: str, *, cutoff: datetime, now: datetime, state_before: Mapping[str, Any] | None = None
    ) -> TurnStart:
        # interrupted、blocked，或逾時的 streaming：重新生成並覆蓋該筆（序號與編號不變，輪數仍是空的，嘗試序號加 1）
        reusable = (
            MemoryLog.turn_status.in_((TURN_INTERRUPTED, TURN_BLOCKED))
            | ((MemoryLog.turn_status == TURN_STREAMING) & (MemoryLog.updated_at < cutoff))
        )
        result = self._session.execute(
            update(MemoryLog).where(MemoryLog.id == existing.id, reusable)
            .values(
                turn_status=TURN_STREAMING, user_action=user_action, llm_raw_response="", structured_result=None, turn_number=None,
                attempt=MemoryLog.attempt + 1, updated_at=now,
                # 回合前的快照一律以這次為準；沒帶就清為空（SQL 的 NULL），舊快照對應的是更早的世界
                state_before_json=copy.deepcopy(dict(state_before)) if state_before is not None else sa.null(),
            )
            .execution_options(synchronize_session=False)
        )
        self._session.expire(existing)
        if result.rowcount != 1:
            raise TurnInProgressError                       # 仍在串流且未逾時（或剛被別人接手）
        return TurnStart(existing, TURN_RESTARTED)

    def _finish_turn(self, turn: MemoryLog, to_status: str, *, attempt: int | None = None, **values: Any) -> None:
        """回合離開生成中：單一條件式 UPDATE，只有 `streaming`（且 `attempt` 相符，若有帶）的回合成立。"""
        conditions = [MemoryLog.id == turn.id, MemoryLog.turn_status == TURN_STREAMING]
        if attempt is not None:
            conditions.append(MemoryLog.attempt == attempt)
        result = self._session.execute(
            update(MemoryLog).where(*conditions).values(turn_status=to_status, **values).execution_options(synchronize_session=False)
        )
        self._session.expire(turn)
        if result.rowcount != 1:
            raise InvalidTurnTransitionError

    def _end_unfinished(
        self, user_id: int, public_id: str, client_msg_id: str, to_status: str, partial: str, *, attempt: int | None = None,
        structured_result: Mapping[str, Any] | None = None,
    ) -> MemoryLog | None:
        save = self.get_owned(user_id, public_id)
        if save is None:
            return None
        turn = self._turn_by_client_id(save.id, client_msg_id)
        if turn is None:
            return None
        values: dict[str, Any] = {"turn_number": None, "llm_raw_response": partial}
        if structured_result is not None:
            values["structured_result"] = dict(structured_result)
        self._finish_turn(turn, to_status, attempt=attempt, **values)
        return turn
