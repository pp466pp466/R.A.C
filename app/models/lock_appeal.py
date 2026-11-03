"""被鎖定帳號向管理員的申訴（見 lockout-appeals 規格與 google-first-login 的 design D3、D8）。

- `content` 由使用者撰寫，視為個資：保存期限與所屬帳號相同、帳號刪除時一併刪除（外鍵 `ON DELETE CASCADE`）、
  納入個人資料匯出；**絕不**進日誌或稽核紀錄（只記內部編號與字數）。
- 管理員只看得到「目前被鎖定的帳號」的申訴；帳號解除鎖定後 `handled_at` 被填入，申訴不再出現在管理員的檢視中，但仍隨帳號保存。
- 對外只用 `public_id`（UUID）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, UnicodeText
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PublicIdMixin, UTCDateTime, utcnow

MAX_APPEAL_CHARS = 1000


class LockAppeal(Base, PublicIdMixin):
    __tablename__ = "lock_appeals"
    __table_args__ = (Index("ix_lock_appeals_user_id_created_at", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    content: Mapped[str] = mapped_column(UnicodeText)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    handled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
