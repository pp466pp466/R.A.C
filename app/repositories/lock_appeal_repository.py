"""申訴的資料存取（見 lockout-appeals 規格與 google-first-login 的 design D8）。

使用者本人的讀寫方法第一個參數都是 `user_id`（只回自己的申訴）。管理員檢視「目前被鎖定的帳號的申訴」是跨使用者的讀取，
放在後台專用的 `AdminRepository`（只被 `AdminService` 使用，並寫入稽核）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update

from app.models.lock_appeal import LockAppeal
from app.repositories.base import BaseRepository


class LockAppealRepository(BaseRepository[LockAppeal]):
    model = LockAppeal

    def create(self, user_id: int, content: str, now: datetime) -> LockAppeal:
        return self.add(LockAppeal(user_id=user_id, content=content, created_at=now))

    def count_since(self, user_id: int, since: datetime) -> int:
        """`since` 之後這位使用者送出的申訴數（每小時上限用）。"""
        return self._session.execute(
            select(func.count(LockAppeal.id)).where(LockAppeal.user_id == user_id, LockAppeal.created_at > since)
        ).scalar_one()

    def has_pending(self, user_id: int) -> bool:
        return self._session.execute(
            select(func.count(LockAppeal.id)).where(LockAppeal.user_id == user_id, LockAppeal.handled_at.is_(None))
        ).scalar_one() > 0

    def latest_for_user(self, user_id: int) -> LockAppeal | None:
        return self._session.scalar(
            select(LockAppeal).where(LockAppeal.user_id == user_id).order_by(LockAppeal.created_at.desc(), LockAppeal.id.desc()).limit(1)
        )

    def list_for_user(self, user_id: int) -> list[LockAppeal]:
        """這位使用者自己的全部申訴（個人資料匯出用），由舊到新。"""
        return list(self._session.scalars(
            select(LockAppeal).where(LockAppeal.user_id == user_id).order_by(LockAppeal.created_at, LockAppeal.id)
        ))

    def mark_pending_handled(self, user_id: int, now: datetime) -> int:
        """解除鎖定時，把這位使用者所有待處理的申訴標為已處理（單一 UPDATE），回傳筆數。"""
        self._session.flush()
        result = self._session.execute(
            update(LockAppeal)
            .where(LockAppeal.user_id == user_id, LockAppeal.handled_at.is_(None))
            .values(handled_at=now)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount
