"""帳號鎖定的計次（見 account-lockout 規格與 google-first-login 的 design D3、D4）。

每次「身分被拒」（已辨識帳號但被拒絕的 Google 登入、補填年齡時的不一致）或「訊息被限流」記一筆，
由 `AccountLockService` 在 15 分鐘的滑動視窗內計數；達門檻即鎖定帳號。

- 只存帳號編號、類別與時間：**不含** email、生日、訊息或任何請求內容。
- 外鍵 `ON DELETE CASCADE`：帳號被抹除時計次一併消失（`tests/architecture/test_user_foreign_keys.py` 會檢查）。
- 視窗只有 15 分鐘，所以由期限清除在 7 天後刪除（data-retention 規格）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, Unicode
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime, utcnow

STRIKE_IDENTITY_REJECTED = "identity_rejected"
STRIKE_RATE_LIMITED = "rate_limited"
STRIKE_KINDS = (STRIKE_IDENTITY_REJECTED, STRIKE_RATE_LIMITED)


class LockStrike(Base):
    __tablename__ = "lock_strikes"
    __table_args__ = (Index("ix_lock_strikes_user_id_created_at", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Unicode(20))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
