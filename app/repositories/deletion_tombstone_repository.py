"""帳號刪除紀錄的資料存取（見 account-retention 規格）。

只存內部使用者編號、刪除時間與原因；**不接受也不保存任何個資**。這張表不屬於任何使用者（被指向的帳號已經不存在），
所以沒有依使用者查詢的方法；`deleted_user_ids` 供備份還原後依紀錄重新執行刪除（C-4）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from app.models.deletion_tombstone import DeletionTombstone
from app.repositories.base import BaseRepository


class DeletionTombstoneRepository(BaseRepository[DeletionTombstone]):
    model = DeletionTombstone

    def record(self, *, user_id: int, reason: str, deleted_at: datetime) -> DeletionTombstone:
        return self.add(DeletionTombstone(user_id=user_id, reason=reason, deleted_at=deleted_at))

    def deleted_user_ids(self) -> set[int]:
        """所有已被刪除的使用者編號。"""
        return set(self._session.scalars(select(DeletionTombstone.user_id)))
