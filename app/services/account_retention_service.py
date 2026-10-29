"""閒置帳號的保存期限與清除（見 AGENTS.md 第 11 節與 account-retention 規格）。

連續 12 個月（以 365 天計）沒有成功登入的一般帳號，直接刪除（不經 7 天寬限期），連同其生日、年齡區間與年齡確認紀錄。
管理員不納入。每個被刪除的帳號留下一筆只含內部編號、時間與原因的刪除紀錄，供備份還原後重新執行刪除。

- 判斷在刪除當下再確認一次（條件式 DELETE）：清除期間剛好登入的帳號不會被刪。
- 每個帳號的「刪除＋刪除紀錄」在同一個交易提交；提交後才記錄事件（事件只含內部編號，沒有任何個資）。
- 預覽模式只計數，不刪除、不寫紀錄、不記事件。
- 這個服務只負責目前存在的資料表；對話等日後新增的資料以資料庫的串聯刪除一併移除
  （`tests/architecture/test_user_foreign_keys.py` 會在有資料表沒做到時失敗）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from app.models.deletion_tombstone import REASON_INACTIVE_12_MONTHS
from app.observability import security_events
from app.repositories.deletion_tombstone_repository import DeletionTombstoneRepository
from app.repositories.user_repository import UserRepository

INACTIVITY_PERIOD = timedelta(days=365)
DEFAULT_BATCH_SIZE = 200


@dataclass(frozen=True)
class PurgeResult:
    matched: int          # 符合條件的帳號數量（預覽模式下就是全部符合者；實際執行時是本次實際處理到的）
    deleted: int          # 實際刪除的數量（預覽模式為 0）
    dry_run: bool


class AccountRetentionService:
    def __init__(
        self,
        *,
        users: UserRepository,
        tombstones: DeletionTombstoneRepository,
        clock: Callable[[], datetime],
        commit: Callable[[], None] = lambda: None,
        period: timedelta = INACTIVITY_PERIOD,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._users = users
        self._tombstones = tombstones
        self._clock = clock
        self._commit = commit
        self._period = period
        self._batch_size = batch_size

    def purge_inactive(self, *, dry_run: bool = False) -> PurgeResult:
        now = self._clock()
        cutoff = now - self._period
        if dry_run:
            return PurgeResult(matched=self._users.count_inactive(cutoff), deleted=0, dry_run=True)

        matched = deleted = 0
        while True:
            ids = self._users.find_inactive_ids(cutoff, limit=self._batch_size)
            if not ids:
                break
            matched += len(ids)
            deleted_in_batch = 0
            for user_id in ids:
                if not self._users.delete_inactive(user_id, cutoff):
                    continue                                       # 判斷之後剛好登入（或已被別人刪掉）：不刪
                self._tombstones.record(user_id=user_id, reason=REASON_INACTIVE_12_MONTHS, deleted_at=now)
                self._commit()
                security_events.log_event(security_events.ACCOUNT_INACTIVE_PURGED, user_id=user_id)
                deleted_in_batch += 1
            deleted += deleted_in_batch
            if deleted_in_batch == 0:                              # 這一批一個都沒刪成：不再重試，避免無窮迴圈
                break
        return PurgeResult(matched=matched, deleted=deleted, dry_run=False)
