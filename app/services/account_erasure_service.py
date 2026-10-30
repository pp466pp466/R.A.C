"""抹除一個人的資料：全站唯一的出口（見 account-deletion、account-retention 規格與 privacy-lifecycle 的 design D1）。

閒置 12 個月的帳號清除與使用者自助刪除（寬限期滿）都走這裡；日後 RPG 的存檔與角色資料也應以串聯刪除掛在使用者底下，
新增資料表時由 `tests/architecture/test_account_erasure_coverage.py` 要求登記，不必每個功能各做一套刪除。

每個帳號在**一個交易**內依序做：
1. 把用量帳本中該使用者的對話編號清為空值。帳本的 `conversation_id` 沒有外鍵（SQL Server 錯誤 1785），不先清掉的話，
   SQLite 重用整數編號時會錯指別人的新對話。必須在刪除使用者**之前**：使用者一刪，帳本的 `user_id` 就變成空值，找不到要清的列。
2. 條件式刪除使用者（條件在刪除當下再確認一次）。對話、訊息、第三方身分、驗證權杖靠資料庫的 `ON DELETE CASCADE`，
   帳本與稽核的使用者欄位靠 `SET NULL`（保留紀錄但匿名化）。
3. 寫一筆只含內部編號、時間與原因的刪除紀錄。
4. 提交。

第 2 步沒有刪到任何列（使用者剛好登入、或剛撤銷刪除）時**回復整個交易**，把第 1 步也復原：使用者還活著，
不能讓他的帳本失去對話連結。事件在提交之後才記錄，且只含內部編號，沒有任何個資。
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from app.models.deletion_tombstone import REASON_INACTIVE_12_MONTHS, REASON_USER_REQUESTED
from app.observability import security_events
from app.repositories.deletion_tombstone_repository import DeletionTombstoneRepository
from app.repositories.user_repository import UserRepository


class AccountErasureService:
    def __init__(
        self,
        *,
        users: UserRepository,
        tombstones: DeletionTombstoneRepository,
        commit: Callable[[], None],
        rollback: Callable[[], None] = lambda: None,
    ) -> None:
        self._users = users
        self._tombstones = tombstones
        self._commit = commit
        self._rollback = rollback

    def erase(
        self,
        user_id: int,
        *,
        delete: Callable[[int], bool],
        reason: str,
        now: datetime,
        on_erased: Callable[[int], None] = lambda _user_id: None,
        write_tombstone: bool = True,
    ) -> bool:
        """抹除一個帳號；`delete` 是條件式刪除，回傳是否真的刪到。回傳是否抹除成功（條件不成立則不留任何痕跡）。

        `write_tombstone=False` 只用於備份還原後的重新抹除：該編號已有不早於這次的刪除紀錄，不必再寫一筆（見 deletion-replay 規格）。
        """
        try:
            self._users.clear_ledger_conversation_refs(user_id)
            if not delete(user_id):
                self._rollback()
                return False
            if write_tombstone:
                self._tombstones.record(user_id=user_id, reason=reason, deleted_at=now)
            self._commit()
        except Exception:
            self._rollback()
            raise
        on_erased(user_id)
        return True

    def erase_inactive(self, user_id: int, *, cutoff: datetime, now: datetime) -> bool:
        """連續 12 個月沒有登入的一般帳號（`cutoff` 之前最後登入）。"""
        return self.erase(
            user_id,
            delete=lambda uid: self._users.delete_inactive(uid, cutoff),
            reason=REASON_INACTIVE_12_MONTHS,
            now=now,
            on_erased=lambda uid: security_events.log_event(security_events.ACCOUNT_INACTIVE_PURGED, user_id=uid),
        )

    def erase_pending_due(
        self, user_id: int, *, cutoff: datetime, now: datetime, on_erased: Callable[[int], None] = lambda _user_id: None
    ) -> bool:
        """使用者申請刪除、寬限期已滿的帳號（申請時間不晚於 `cutoff`）。"""
        return self.erase(
            user_id,
            delete=lambda uid: self._users.delete_if_pending_due(uid, cutoff),
            reason=REASON_USER_REQUESTED,
            now=now,
            on_erased=on_erased,
        )
