"""帳號刪除紀錄（見 AGENTS.md 第 11 節與 account-retention 規格）。

只存內部使用者編號、刪除時間與原因（列舉值），**不存 email、名稱、生日或任何個資**。用途：資料庫從備份還原時，
依這份紀錄重新執行刪除，使已刪除的帳號不會因還原而復活。

- `user_id` 刻意**不設外鍵**：被指向的帳號已經不存在，外鍵會讓紀錄無法寫入。
- 編號不在這張表中唯一：同一個編號理論上只會被刪一次，但不以約束強制，避免在還原與重跑時失敗。
- 注意：SQLite 的整數主鍵在刪除最大值後可能重用編號；SQL Server 的 IDENTITY 不會。SQLite 只用於測試與
  本機展示，正式環境的還原不受影響（見 age-verification-birthdate 的 design.md）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Integer, Unicode
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime, utcnow

REASON_INACTIVE_12_MONTHS = "inactive_12_months"
DELETION_REASONS = (REASON_INACTIVE_12_MONTHS,)


class DeletionTombstone(Base):
    __tablename__ = "deletion_tombstones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer)
    deleted_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    reason: Mapped[str] = mapped_column(Unicode(30))
