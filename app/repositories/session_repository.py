"""RPG 存檔與回合的資料存取（見 rpg-sessions 規格、AGENTS.md 第 6、7、10、11 節、rpg-data-and-script 的 design D3、D4、D11）。

只用 ORM 表達式，不提交交易（請求結束時統一提交）。

**擁有者隔離**：所有方法的第一個參數是 `user_id`，查詢一律帶 `RpgSession.user_id == user_id`；別人的存檔與不存在的存檔得到相同的結果
（`None`），Service 據此回 404。沒有只憑識別碼取得存檔或回合的方法，管理員也一樣（沒有專用路徑；管理員預設看不到存檔內容）。

**回合**
- `(session_id, client_msg_id)` 與 `(session_id, seq)` 由資料庫唯一約束保證。開始回合先對存檔那一列做條件式 UPDATE
  （同時確認存檔未完結、更新 `last_active_at`）：這個 UPDATE 取得該列的鎖並持有到交易結束，所以同一存檔的並行請求被串行化，
  之後「讀既有回合、`seq` 取目前最大值加 1、寫入」不會互相搶；仍保留撞到唯一約束時重試一次的保險。
- 重送：`complete` 直接回傳既有回合；`interrupted`／`blocked` 重設為 `streaming` 並覆蓋該筆；`streaming` 拋出 `TurnInProgressError`。
- 輪數（`turn_number`）只在回合完成時指派（已完成的回合數加 1），所以中斷與被擋的回合不計輪、不佔輪數。

**已完結的存檔**：`end_session` 之後不得再開始回合或改動世界狀態；沒有任何方法把狀態改回進行中（不可恢復）。
玩家自行封存（`is_archived`）與劇情完結是兩件事，`set_archived` 對已完結的存檔也有效。

存檔與回合的內容（行動、劇情原文、記事簿、世界狀態、劇本複本）是個資：這裡不記錄任何日誌。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator, Mapping

import sqlalchemy as sa
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.models.rpg_session import (
    SESSION_ACTIVE, SESSION_ENDED, TURN_BLOCKED, TURN_COMPLETE, TURN_INTERRUPTED, TURN_STREAMING, MemoryLog, RpgSession,
)
from app.repositories.base import BaseRepository
from app.repositories.errors import (
    InvalidTurnTransitionError, SessionEndedError, StaleSessionVersionError, TurnInProgressError,
)

# `start_turn` 的結果
TURN_CREATED = "created"          # 新增了一個回合
TURN_RESTARTED = "restarted"      # 重送了中斷或被擋的回合：該筆被重設為生成中
TURN_REPLAYED = "replayed"        # 重送了已完成的回合：回傳既有的回合，沒有新增或改動

# `update_world_state` 可以寫入的欄位（對應存檔的欄位名稱）
WORLD_STATE_FIELDS = (
    "current_location", "status_summary", "entities_json", "goals_json", "milestone_progress_json", "elapsed_months",
)


@dataclass(frozen=True)
class TurnStart:
    turn: MemoryLog
    outcome: str


class SessionRepository(BaseRepository[RpgSession]):
    model = RpgSession

    # ---- 存檔 ---------------------------------------------------------------------

    def create(
        self, user_id: int, *, source_version_id: int | None, snapshot: Mapping[str, Any], snapshot_spec_version: int,
        player_name: str, character_age: int, content_tier: str, now: datetime,
    ) -> RpgSession:
        """開存檔：複製劇本版本標準化後的完整內容，並給所有欄位初始值（版本號 1、經過 0 個月、記事簿為空…）。"""
        save = RpgSession(
            user_id=user_id, source_version_id=source_version_id, script_snapshot_json=copy.deepcopy(dict(snapshot)),
            snapshot_spec_version=snapshot_spec_version, player_name=player_name, character_age=character_age,
            content_tier=content_tier, last_active_at=now,
            entities_json={}, goals_json={"active": [], "archived": []}, milestone_progress_json={},
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

    def count_complete_turns(self, user_id: int, public_id: str) -> int | None:
        save = self.get_owned(user_id, public_id)
        if save is None:
            return None
        return self._complete_count(save.id)

    def start_turn(self, user_id: int, public_id: str, *, client_msg_id: str, user_action: str, now: datetime) -> TurnStart | None:
        """開始一個回合（冪等）。不是自己的存檔回傳 `None`；存檔已完結拋出 `SessionEndedError`；
        同一個編號的回合仍在串流中拋出 `TurnInProgressError`。"""
        save = self.get_owned(user_id, public_id)
        if save is None:
            return None
        # 取得存檔那一列的鎖（持有到交易結束）並確認未完結：同一存檔的並行請求從這裡開始被串行化
        if self._update_owned(save, RpgSession.status == SESSION_ACTIVE, last_active_at=now) != 1:
            raise SessionEndedError
        for attempt in (1, 2):
            existing = self._turn_by_client_id(save.id, client_msg_id)
            if existing is not None:
                return self._resend(existing, user_action)
            try:
                with self._session.begin_nested():
                    turn = MemoryLog(
                        session_id=save.id, client_msg_id=client_msg_id, seq=self._next_seq(save.id),
                        turn_status=TURN_STREAMING, user_action=user_action, llm_raw_response="",
                    )
                    self._session.add(turn)
                    self._session.flush()
                return TurnStart(turn, TURN_CREATED)
            except IntegrityError:
                if attempt == 2:
                    raise
        raise AssertionError("不會走到這裡")           # pragma: no cover

    def complete_turn(
        self, user_id: int, public_id: str, client_msg_id: str, *, llm_raw_response: str, structured_result: Mapping[str, Any] | None,
        now: datetime,
    ) -> MemoryLog | None:
        """回合完成：指派輪數（已完成的回合數加 1）。不是自己的回傳 `None`；不是生成中拋出 `InvalidTurnTransitionError`。"""
        save = self.get_owned(user_id, public_id)
        if save is None:
            return None
        self._update_owned(save, last_active_at=now)             # 取得存檔那一列的鎖，輪數的指派因此不會被並行的完成搶走
        turn = self._turn_by_client_id(save.id, client_msg_id)
        if turn is None:
            return None
        self._finish_turn(
            turn, TURN_COMPLETE, turn_number=self._complete_count(save.id) + 1, llm_raw_response=llm_raw_response,
            structured_result=dict(structured_result) if structured_result is not None else None,
        )
        return turn

    def interrupt_turn(self, user_id: int, public_id: str, client_msg_id: str, *, partial_response: str) -> MemoryLog | None:
        """串流中斷：保留已收到的片段，不計輪。"""
        return self._end_unfinished(user_id, public_id, client_msg_id, TURN_INTERRUPTED, partial_response)

    def block_turn(self, user_id: int, public_id: str, client_msg_id: str, *, partial_response: str = "") -> MemoryLog | None:
        """被審查擋下：不計輪，也不進上下文。"""
        return self._end_unfinished(user_id, public_id, client_msg_id, TURN_BLOCKED, partial_response)

    # ---- 匯出（個人資料匯出用）-------------------------------------------------------------

    def export_sessions(self, user_id: int, *, page_size: int = 50) -> Iterator[dict[str, Any]]:
        """這位使用者的全部存檔（含已完結與已封存的），由舊到新逐筆產生（以存檔編號為游標分頁）。回合另以 `export_turns` 分頁讀取。

        含劇本標題（取自存檔自己的複本）、狀態、內容模式、主角姓名與角色年齡、所在地點、狀態摘要、各時間、記事簿全文與世界狀態
        （`world_state`，只給 JSON 匯出使用）。**不含**劇本複本本身（那是來源劇本作者的內容）、來源版本編號、版本號與其他內部編號。
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
                    "notebook_text": save.notebook_text,
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

    def _next_seq(self, session_id: int) -> int:
        return (self._session.scalar(select(func.max(MemoryLog.seq)).where(MemoryLog.session_id == session_id)) or 0) + 1

    def _complete_count(self, session_id: int) -> int:
        return self._session.scalar(
            select(func.count()).select_from(MemoryLog).where(MemoryLog.session_id == session_id, MemoryLog.turn_status == TURN_COMPLETE)
        ) or 0

    def _resend(self, existing: MemoryLog, user_action: str) -> TurnStart:
        if existing.turn_status == TURN_COMPLETE:
            return TurnStart(existing, TURN_REPLAYED)
        if existing.turn_status == TURN_STREAMING:
            raise TurnInProgressError
        # interrupted、blocked：重新生成並覆蓋該筆（序號與編號不變，輪數仍是空的）
        self._session.execute(
            update(MemoryLog)
            .where(MemoryLog.id == existing.id, MemoryLog.turn_status.in_((TURN_INTERRUPTED, TURN_BLOCKED)))
            .values(turn_status=TURN_STREAMING, user_action=user_action, llm_raw_response="", structured_result=None, turn_number=None)
            .execution_options(synchronize_session=False)
        )
        self._session.expire(existing)
        return TurnStart(existing, TURN_RESTARTED)

    def _finish_turn(self, turn: MemoryLog, to_status: str, **values: Any) -> None:
        """回合離開生成中：單一條件式 UPDATE，只有 `streaming` 的回合成立。"""
        result = self._session.execute(
            update(MemoryLog).where(MemoryLog.id == turn.id, MemoryLog.turn_status == TURN_STREAMING)
            .values(turn_status=to_status, **values).execution_options(synchronize_session=False)
        )
        self._session.expire(turn)
        if result.rowcount != 1:
            raise InvalidTurnTransitionError

    def _end_unfinished(self, user_id: int, public_id: str, client_msg_id: str, to_status: str, partial: str) -> MemoryLog | None:
        save = self.get_owned(user_id, public_id)
        if save is None:
            return None
        turn = self._turn_by_client_id(save.id, client_msg_id)
        if turn is None:
            return None
        self._finish_turn(turn, to_status, turn_number=None, llm_raw_response=partial)
        return turn
