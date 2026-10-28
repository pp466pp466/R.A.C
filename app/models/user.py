"""會員相關資料表：使用者、第三方身分、電子郵件權杖（見 AGENTS.md 第 4、10 節）。

設計要點：
- email 與 `email_canonical` 各自唯一；兩者都由 Service 先正規化再寫入（SQLite 區分大小寫、SQL Server 不區分）。
- 值域（角色、等級、狀態、權杖用途）由本檔的常數與 Service 驗證，不加資料庫 CHECK 約束
  （CHECK 在 SQLite 的 batch migration 與 SQL Server 之間有方言差異）。
- 這幾張表的欄位都不可為空或本身不要求唯一，因此不需要 `unique_when_not_null`。
- 年齡確認的兩個欄位（`age_confirmed_at`、`age_policy_version`）可為空且從不依值查詢，因此不建索引。
- 對外只用 `public_id`（UUID）；`user_identities`、`auth_tokens` 不對外暴露，不設 `public_id`。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, Unicode, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PublicIdMixin, TimestampMixin, UTCDateTime, utcnow

# ---- 值域 ---------------------------------------------------------------------

ROLE_USER = "user"
ROLE_ADMIN = "admin"
ROLES = (ROLE_USER, ROLE_ADMIN)

TIER_FREE = "free"
TIER_PLUS = "plus"
TIER_VIP = "vip"
MEMBER_TIERS = (TIER_FREE, TIER_PLUS, TIER_VIP)

STATUS_PENDING_VERIFICATION = "pending_verification"
STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"
STATUS_DELETION_PENDING = "deletion_pending"
USER_STATUSES = (STATUS_PENDING_VERIFICATION, STATUS_ACTIVE, STATUS_SUSPENDED, STATUS_DELETION_PENDING)

PROVIDER_GOOGLE = "google"
IDENTITY_PROVIDERS = (PROVIDER_GOOGLE,)

TOKEN_VERIFY_EMAIL = "verify_email"
TOKEN_RESET_PASSWORD = "reset_password"
TOKEN_PURPOSES = (TOKEN_VERIFY_EMAIL, TOKEN_RESET_PASSWORD)


class User(Base, PublicIdMixin, TimestampMixin):
    """會員。`status` 沒有預設值：由 Service 依信箱驗證設定明確指定。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(Unicode(255), unique=True)
    email_canonical: Mapped[str] = mapped_column(Unicode(255), unique=True)
    display_name: Mapped[str] = mapped_column(Unicode(50))
    password_hash: Mapped[str | None] = mapped_column(Unicode(255), nullable=True)  # 純 Google 帳號沒有密碼
    email_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    role: Mapped[str] = mapped_column(Unicode(10), default=ROLE_USER)
    member_tier: Mapped[str] = mapped_column(Unicode(10), default=TIER_FREE)
    status: Mapped[str] = mapped_column(Unicode(20))
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    token_version: Mapped[int] = mapped_column(Integer, default=1)
    # 註冊與 Google 建立帳號時由 Service 記錄；管理員指令建立的帳號沒有「同意」這個動作，故可為空
    terms_version: Mapped[str | None] = mapped_column(Unicode(40), nullable=True)
    terms_accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # 年齡確認（age-confirmation 規格）：只記「是否確認、何時、依據哪一版政策」，不收生日、年齡或證件。
    # 空值 = 尚未確認（升級前的帳號與管理員指令建立的帳號都是空值）；政策版本由伺服器決定，日後改用更嚴格的
    # 確認方式時提高版本，即可區分舊的自行聲明紀錄。
    age_confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    age_policy_version: Mapped[str | None] = mapped_column(Unicode(40), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class UserIdentity(Base):
    """第三方登入身分。一個帳號最多一個 Google 身分，一個 Google 身分只屬於一個帳號。"""

    __tablename__ = "user_identities"
    __table_args__ = (
        UniqueConstraint("provider", "provider_subject", name="uq_user_identities_provider_subject"),
        UniqueConstraint("user_id", "provider", name="uq_user_identities_user_provider"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(Unicode(20))
    provider_subject: Mapped[str] = mapped_column(Unicode(255))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AuthToken(Base):
    """信箱驗證與重設密碼的權杖。只存 SHA-256 雜湊，原值只出現在寄出的信件中。"""

    __tablename__ = "auth_tokens"
    __table_args__ = (
        # 供「同一使用者同用途的最近寄信時間與筆數」查詢（寄信頻率限制）
        Index("ix_auth_tokens_user_id_purpose_created_at", "user_id", "purpose", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    purpose: Mapped[str] = mapped_column(Unicode(20))
    token_hash: Mapped[str] = mapped_column(Unicode(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
