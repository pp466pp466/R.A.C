"""執行寬限期已滿的帳號刪除（排程指令 `execute-account-deletions`；見 account-deletion 規格）。

找出狀態為 `deletion_pending` 且申請已滿 7 天的帳號，逐一交給 `AccountErasureService`（與閒置清除共用的抹除出口）。

- 判斷在刪除當下再確認一次（條件式 DELETE）：剛好在這時撤銷的帳號不會被刪。
- 每個帳號的「清帳本對話參照＋刪除＋刪除紀錄」在同一個交易提交；提交後才記錄事件（只含內部編號）。
- 預覽模式只計數，不刪除、不寫紀錄、不記事件。
- 可重複執行：已刪除的帳號不會再出現。
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from app.models.user import DELETION_GRACE_PERIOD
from app.observability import lifecycle_events
from app.repositories.user_repository import UserRepository
from app.services.account_erasure_service import AccountErasureService
from app.services.account_retention_service import DEFAULT_BATCH_SIZE, PurgeResult


class AccountDeletionExecutor:
    def __init__(
        self,
        *,
        users: UserRepository,
        erasure: AccountErasureService,
        clock: Callable[[], datetime],
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._users = users
        self._erasure = erasure
        self._clock = clock
        self._batch_size = batch_size

    def execute_due(self, *, dry_run: bool = False) -> PurgeResult:
        now = self._clock()
        cutoff = now - DELETION_GRACE_PERIOD
        if dry_run:
            return PurgeResult(matched=self._users.count_pending_due(cutoff), deleted=0, dry_run=True)

        matched = deleted = 0
        while True:
            ids = self._users.find_pending_due_ids(cutoff, limit=self._batch_size)
            if not ids:
                break
            matched += len(ids)
            deleted_in_batch = 0
            for user_id in ids:
                if self._erasure.erase_pending_due(
                    user_id, cutoff=cutoff, now=now, on_erased=lambda uid: lifecycle_events.log_deleted_by_request(user_id=uid)
                ):
                    deleted_in_batch += 1
            deleted += deleted_in_batch
            if deleted_in_batch == 0:                      # 這一批一個都沒刪成（例如全被撤銷）：不再重試，避免無窮迴圈
                break
        return PurgeResult(matched=matched, deleted=deleted, dry_run=False)
