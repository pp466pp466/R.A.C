"""帳號鎖定計次的資料存取（見 account-lockout 規格與 google-first-login 的 design D4）。

屬於「某一位使用者」的紀錄，所以讀寫方法的第一個參數都是 `user_id`；跨使用者的期限清除（7 天）在 `RetentionRepository`，與其他保存期限放在一起。
只用 ORM，不提交也不回復交易（由請求統一提交；需要在 4xx 時保留的呼叫端自行呼叫 `keep_changes_on_error`）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Sequence

from sqlalchemy import delete, func, select

from app.models.lock_strike import LockStrike
from app.repositories.base import BaseRepository


class LockStrikeRepository(BaseRepository[LockStrike]):
    model = LockStrike

    def record(self, user_id: int, kind: str, now: datetime) -> LockStrike:
        return self.add(LockStrike(user_id=user_id, kind=kind, created_at=now))

    def count_since(self, user_id: int, kinds: Sequence[str], since: datetime) -> int:
        """視窗內（`created_at` 晚於 `since`）指定類別的計次數。"""
        return self._session.execute(
            select(func.count(LockStrike.id)).where(
                LockStrike.user_id == user_id, LockStrike.kind.in_(tuple(kinds)), LockStrike.created_at > since
            )
        ).scalar_one()

    def clear_for_user(self, user_id: int) -> int:
        """解除鎖定時清除該帳號的計次（單一 DELETE），回傳筆數。"""
        self._session.flush()
        result = self._session.execute(
            delete(LockStrike).where(LockStrike.user_id == user_id).execution_options(synchronize_session=False)
        )
        return result.rowcount
