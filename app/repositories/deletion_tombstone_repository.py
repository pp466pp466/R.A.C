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

    def records(self) -> list[tuple[int, datetime]]:
        """所有刪除紀錄的（使用者編號, 刪除時間），由編號小到大；備份還原後重新抹除用（deletion-replay 規格）。只有編號與時間。"""
        rows = self._session.execute(select(DeletionTombstone.user_id, DeletionTombstone.deleted_at).order_by(DeletionTombstone.user_id, DeletionTombstone.deleted_at))
        return [(user_id, deleted_at) for user_id, deleted_at in rows]

    def has_record_not_before(self, user_id: int, deleted_at: datetime) -> bool:
        """這個編號是否已有一筆「刪除時間不早於 `deleted_at`」的紀錄；有的話重新抹除時不必再寫一筆。"""
        return self._session.execute(
            select(DeletionTombstone.id).where(DeletionTombstone.user_id == user_id, DeletionTombstone.deleted_at >= deleted_at).limit(1)
        ).first() is not None
