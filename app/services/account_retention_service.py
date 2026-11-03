"""閒置帳號與逾時未完成年齡確認帳號的保存期限與清除（見 AGENTS.md 第 11 節與 account-retention 規格）。

連續 12 個月（以 365 天計）沒有成功登入的一般帳號，直接刪除（不經 7 天寬限期），連同其生日、年齡區間與年齡確認紀錄。
管理員不納入。每個被刪除的帳號留下一筆只含內部編號、時間與原因的刪除紀錄，供備份還原後重新執行刪除。

- 判斷在刪除當下再確認一次（條件式 DELETE）：清除期間剛好登入的帳號不會被刪。
- 實際的抹除交給 `AccountErasureService`（與使用者自助刪除共用同一個出口）：先把用量帳本的對話編號清為空值，
  再刪除使用者，與「刪除紀錄」在同一個交易提交；提交後才記錄事件（事件只含內部編號，沒有任何個資）。
- 預覽模式只計數，不刪除、不寫紀錄、不記事件。
- 逾時未完成年齡確認（`purge_unconfirmed`）：玩家首次以 Google 登入建立的帳號有 24 小時的補填期限（`age_deadline_at`），
  期限到了仍沒有生日就直接刪除，連同其 Google 綁定與信箱；管理員、已完成年齡確認的帳號、沒有期限記錄的舊帳號都不納入。
  由排程每小時執行；與閒置清除共用同一個抹除出口與批次邏輯。
- 對話等資料以資料庫的串聯刪除一併移除
  （`tests/architecture/test_user_foreign_keys.py` 與 `test_account_erasure_coverage.py` 會在有資料表沒做到時失敗）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from app.repositories.deletion_tombstone_repository import DeletionTombstoneRepository
from app.repositories.user_repository import UserRepository
from app.services.account_erasure_service import AccountErasureService

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
        rollback: Callable[[], None] = lambda: None,
        period: timedelta = INACTIVITY_PERIOD,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._users = users
        self._erasure = AccountErasureService(users=users, tombstones=tombstones, commit=commit, rollback=rollback)
        self._clock = clock
        self._period = period
        self._batch_size = batch_size

    def purge_inactive(self, *, dry_run: bool = False) -> PurgeResult:
        now = self._clock()
        cutoff = now - self._period
        return self._purge(
            dry_run=dry_run,
            count=lambda: self._users.count_inactive(cutoff),
            find=lambda: self._users.find_inactive_ids(cutoff, limit=self._batch_size),
            erase=lambda user_id: self._erasure.erase_inactive(user_id, cutoff=cutoff, now=now),   # 剛好登入的不刪
        )

    def purge_unconfirmed(self, *, dry_run: bool = False) -> PurgeResult:
        now = self._clock()
        return self._purge(
            dry_run=dry_run,
            count=lambda: self._users.count_overdue_unconfirmed(now),
            find=lambda: self._users.find_overdue_unconfirmed_ids(now, limit=self._batch_size),
            erase=lambda user_id: self._erasure.erase_unconfirmed(user_id, now=now),               # 剛好完成的不刪
        )

    def _purge(
        self,
        *,
        dry_run: bool,
        count: Callable[[], int],
        find: Callable[[], list[int]],
        erase: Callable[[int], bool],
    ) -> PurgeResult:
        if dry_run:
            return PurgeResult(matched=count(), deleted=0, dry_run=True)

        matched = deleted = 0
        while True:
            ids = find()
            if not ids:
                break
            matched += len(ids)
            deleted_in_batch = 0
            for user_id in ids:
                if not erase(user_id):
                    continue                                       # 判斷之後條件不成立了（或已被別人刪掉）：不刪
                deleted_in_batch += 1
            deleted += deleted_in_batch
            if deleted_in_batch == 0:                              # 這一批一個都沒刪成：不再重試，避免無窮迴圈
                break
        return PurgeResult(matched=matched, deleted=deleted, dry_run=False)
