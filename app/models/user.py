"""會員相關資料表：使用者、第三方身分、重設密碼權杖（見 AGENTS.md 第 4、10 節）。

設計要點：
- email 與 `email_canonical` 各自唯一；兩者都由 Service 先正規化再寫入（SQLite 區分大小寫、SQL Server 不區分）。
- 值域（角色、等級、狀態、權杖用途）由本檔的常數與 Service 驗證，不加資料庫 CHECK 約束
  （CHECK 在 SQLite 的 batch migration 與 SQL Server 之間有方言差異）。
- 這幾張表的欄位都不可為空或本身不要求唯一，因此不需要 `unique_when_not_null`。
- 年齡確認的五個欄位（`age_confirmed_at`、`age_policy_version`、`birth_date`、`age_range`、
  `is_adult_at_confirmation`）可為空；只有「依 `age_range` 分組計數」用到它們，不依生日查詢，因此不建索引。
- 對外只用 `public_id`（UUID）；`user_identities`、`auth_tokens` 不對外暴露，不設 `public_id`。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import Boolean, Date, ForeignKey, Index, Integer, Unicode, UniqueConstraint
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

STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"
STATUS_DELETION_PENDING = "deletion_pending"
USER_STATUSES = (STATUS_ACTIVE, STATUS_SUSPENDED, STATUS_DELETION_PENDING)

PROVIDER_GOOGLE = "google"
IDENTITY_PROVIDERS = (PROVIDER_GOOGLE,)

# 年齡確認的期限：首次 Google 登入建立玩家帳號後，最多保留這麼久（AGENTS.md 第 10、11 節）
AGE_CONFIRMATION_DEADLINE = timedelta(hours=24)

# 申請刪除後的寬限期：期間內可登入並撤銷，期滿由排程指令抹除（AGENTS.md 第 11 節）
DELETION_GRACE_PERIOD = timedelta(days=7)

TOKEN_RESET_PASSWORD = "reset_password"       # 管理員的重設密碼；信箱驗證權杖已退役（遷移前遺留的只等著過期被清除）
TOKEN_PURPOSES = (TOKEN_RESET_PASSWORD,)

# 內容模式（content-tier-modes 規格、AGENTS.md 第 7 節），由保守到寬鬆。順序與取較保守者的邏輯在 `app/services/content_tier.py`；
# 常數放在這裡是因為資料模型不得匯入 Service，而存檔、劇本與使用者偏好的欄位預設值都要用到它們。
CONTENT_TIER_SAFE = "safe"
CONTENT_TIER_STANDARD = "standard"
CONTENT_TIER_ADULT = "adult"
CONTENT_TIERS = (CONTENT_TIER_SAFE, CONTENT_TIER_STANDARD, CONTENT_TIER_ADULT)

# 成年確認的方式（目前只有自行聲明：勾選「我確定已滿 18 歲」與同意安全規範，不蒐集證件）
ADULT_CONFIRM_SELF_DECLARED = "self_declared"


class User(Base, PublicIdMixin, TimestampMixin):
    """會員。`status` 沒有預設值：由 Service 明確指定（玩家與管理員建立時都是 `active`）。"""

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
    # 年齡確認（age-confirmation 規格）：自行聲明的生日加年齡區間，經伺服器對照通過後才寫入，不收證件。
    # 「已完成」只看 `birth_date` 與 `age_range` 是否存在；管理員指令建立的帳號、勾選版時代（只有
    # `age_confirmed_at`）的帳號都視為尚未完成。是否成年一律由 `birth_date` 於當下計算，
    # `is_adult_at_confirmation` 只是確認當時的紀錄，**不參與任何判斷**。政策版本由伺服器決定。
    # 這幾欄是個資：不進 API 回應、日誌與模型；帳號連續 12 個月未登入即連同刪除。
    age_confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    age_policy_version: Mapped[str | None] = mapped_column(Unicode(40), nullable=True)
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    age_range: Mapped[str | None] = mapped_column(Unicode(20), nullable=True)
    is_adult_at_confirmation: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # 自助刪除（account-deletion 規格）：申請時間與申請前的狀態（只會是 active，撤銷時還原）。
    # 兩欄平時都是空值，不進索引：待刪除的帳號極少，掃描即可。
    deletion_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    status_before_deletion: Mapped[str | None] = mapped_column(Unicode(20), nullable=True)
    # Google 登入改版（google-first-login）：
    # - `age_deadline_at`：首次 Google 登入建立的玩家帳號，年齡確認的期限（建立時間加 24 小時）；補填成功時清空。
    #   空值（既有帳號、管理員）不會被逾時清除，但沒有生日的玩家帳號一律是「待確認年齡」。
    # - `locked_at`、`lock_reason`：玩家帳號被自動鎖定（只有管理員能解除）。與密碼鎖定的 `locked_until` 分開：
    #   那一個 15 分鐘後自動解除，只適用於管理員的密碼驗證。
    age_deadline_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    locked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    lock_reason: Mapped[str | None] = mapped_column(Unicode(20), nullable=True)
    # 內容模式（rpg-data-and-script）：
    # - `content_tier_preference`：玩家選擇的模式。年齡確認成功時依當下的年齡設定（成年 `standard`、未成年 `safe`），
    #   之後**不因年齡增加而自動改變**（滿 18 歲要自行切換）。伺服器端預設與資料庫預設都是最保守的 `safe`，
    #   所以舊版程式的 INSERT 在新結構上仍然有效且保守。
    # - `adult_confirmed_at`、`adult_rules_version`、`adult_confirm_method`：切換為成人模式時的成年確認紀錄
    #   （確認時間、當時的安全規範版本、方式）；只保存「目前這一次」，規範版本改變後視為失效、須重新確認。
    content_tier_preference: Mapped[str] = mapped_column(Unicode(10), default=CONTENT_TIER_SAFE, server_default=CONTENT_TIER_SAFE)
    adult_confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    adult_rules_version: Mapped[str | None] = mapped_column(Unicode(40), nullable=True)
    adult_confirm_method: Mapped[str | None] = mapped_column(Unicode(20), nullable=True)


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
    """管理員重設密碼的權杖（遷移前遺留的信箱驗證權杖只等著過期被清除）。只存 SHA-256 雜湊，原值只出現在寄出的信件中。"""

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
