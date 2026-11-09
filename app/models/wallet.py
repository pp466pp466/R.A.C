"""遊戲幣的錢包與帳本（見 game-wallet 規格與 design D1、D2）。

- `wallets`：每位玩家至多一個，**免費餘額與付費餘額兩個整數**（兩者任何時候都不小於 0，除了應用程式的條件式更新，資料庫的
  `CHECK` 是第二道防線）與 `version`（每次餘額異動加 1，所有異動都以它做比對並重試，不使用資料庫專屬的鎖）。
- `wallet_entries`：只增不改的帳本。每次餘額異動同一個交易寫一筆；`seq` 是「這位使用者的第幾筆」（等於寫入當下錢包的 `version`），
  記錄頁用它分頁，所以回應不必洩漏全站遞增的內部主鍵。`idempotency_key` 全站唯一：同一個鍵的異動只生效一次。
  **唯一的例外**：預扣列的 `resolution` 從空值被設定一次為 `settled` 或 `refunded`（條件式更新，結算與退回因此互斥）。
- 帳本只放固定列舉與整數，**不放任何玩家輸入的文字**。關聯的存檔以對外識別碼（UUID）存放，不存內部編號：帳本要比存檔活得久，
  SQLite 會重用整數編號而錯指別人的存檔（用量帳本的 `conversation_id` 就為此多了一整套清除流程）。
- 兩張表的 `user_id` 都是 `ON DELETE CASCADE`：錢包與帳本是個資，隨帳號刪除，不匿名化保留。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Unicode, UniqueConstraint, and_, column
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime, utcnow

# 帳本的種類（固定列舉）
ENTRY_SIGNUP_GRANT = "signup_grant"
ENTRY_DAILY_CHECKIN = "daily_checkin"
ENTRY_TURN_HOLD = "turn_hold"
ENTRY_TURN_REFUND = "turn_refund"
ENTRY_ADMIN_ADJUST = "admin_adjust"
ENTRY_KINDS = (ENTRY_SIGNUP_GRANT, ENTRY_DAILY_CHECKIN, ENTRY_TURN_HOLD, ENTRY_TURN_REFUND, ENTRY_ADMIN_ADJUST)

# 預扣列的結算結果（只能從空值設定一次）
RESOLUTION_SETTLED = "settled"
RESOLUTION_REFUNDED = "refunded"
RESOLUTIONS = (RESOLUTION_SETTLED, RESOLUTION_REFUNDED)

# 餘額的兩個桶
BUCKET_FREE = "free"
BUCKET_PAID = "paid"
BUCKETS = (BUCKET_FREE, BUCKET_PAID)

IDEMPOTENCY_KEY_MAX = 200


class Wallet(Base):
    __tablename__ = "wallets"
    __table_args__ = (
        # ORM 表達式，不是原生 SQL 字串；兩個資料庫都支援 CHECK
        CheckConstraint(and_(column("free_balance") >= 0, column("paid_balance") >= 0), name="balances_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    free_balance: Mapped[int] = mapped_column(Integer, default=0)
    paid_balance: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class WalletEntry(Base):
    __tablename__ = "wallet_entries"
    __table_args__ = (
        UniqueConstraint("user_id", "seq", name="uq_wallet_entries_user_seq"),
        Index("ix_wallet_entries_user_id_created_at", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)                                   # 這位使用者的第幾筆（= 寫入當下錢包的 version）
    kind: Mapped[str] = mapped_column(Unicode(20))
    idempotency_key: Mapped[str] = mapped_column(Unicode(IDEMPOTENCY_KEY_MAX), unique=True)
    free_delta: Mapped[int] = mapped_column(Integer, default=0)
    paid_delta: Mapped[int] = mapped_column(Integer, default=0)
    free_after: Mapped[int] = mapped_column(Integer)
    paid_after: Mapped[int] = mapped_column(Integer)
    session_public_id: Mapped[str | None] = mapped_column(Unicode(36), nullable=True)   # 關聯的存檔（對外識別碼）
    ref_key: Mapped[str | None] = mapped_column(Unicode(IDEMPOTENCY_KEY_MAX), nullable=True)   # 退回列指向它所退回的預扣
    resolution: Mapped[str | None] = mapped_column(Unicode(10), nullable=True)           # 只有預扣列使用
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
