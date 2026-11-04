"""營運限制的資料存取（見 runtime-limits 規格）。

限制不屬於任何使用者，所以方法不帶 `user_id`。**讀取一律用欄位查詢**（不載入實體）：實體載入會被 Session 的身分對應表
快取，同一個 Session 內第二次讀就看不到別的行程剛提交的修改；欄位查詢每次都問資料庫。只用 ORM，不提交也不回復交易。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update

from app.models.runtime_limit import RuntimeLimit
from app.repositories.base import BaseRepository


class RuntimeLimitRepository(BaseRepository[RuntimeLimit]):
    model = RuntimeLimit

    def all_values(self) -> dict[str, int]:
        """目前資料庫中全部的鍵與值（每次都查詢）。"""
        rows = self._session.execute(select(RuntimeLimit.key, RuntimeLimit.int_value)).all()
        return {key: value for key, value in rows}

    def get_value(self, key: str) -> int | None:
        """單一鍵的值；沒有這一列回傳 `None`。"""
        return self._session.execute(select(RuntimeLimit.int_value).where(RuntimeLimit.key == key)).scalar_one_or_none()

    def set_value(self, key: str, value: int, now: datetime) -> None:
        """寫入一個鍵（沒有這一列就新增）。驗證鍵與範圍是 Service 的責任。"""
        result = self._session.execute(
            update(RuntimeLimit).where(RuntimeLimit.key == key).values(int_value=value, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 0:
            self.add(RuntimeLimit(key=key, int_value=value, updated_at=now))
