"""會員資料表：users、user_identities、auth_tokens。

只新增三張資料表，不改動任何既有結構（expand）：舊版程式不讀這些表，因此先升級資料庫再部署新版程式是安全的。
回復（downgrade）會刪除這三張表與其中的資料；已有正式會員資料之後，不可使用，請改用備份還原。

這個檔案不匯入應用程式的程式碼：migration 描述的是「當時」的結構，不應隨應用程式修改而改變。
日期時間欄位的型別需與 app.models.base.UTCDateTime 在資料庫中的型別一致（SQL Server 為 DATETIME2(6)）。

Revision ID: 0002_users_and_auth_tokens
Revises: 0001_baseline
Create Date: 2026-10-27
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mssql

# revision identifiers, used by Alembic.
revision = "0002_users_and_auth_tokens"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def _utc_datetime() -> sa.types.TypeEngine:
    """UTC 時間：SQL Server 使用 DATETIME2(6) 以保留微秒精度，其餘為一般 DateTime。"""
    return sa.DateTime().with_variant(mssql.DATETIME2(precision=6), "mssql")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Unicode(length=36), nullable=False),
        sa.Column("email", sa.Unicode(length=255), nullable=False),
        sa.Column("email_canonical", sa.Unicode(length=255), nullable=False),
        sa.Column("display_name", sa.Unicode(length=50), nullable=False),
        sa.Column("password_hash", sa.Unicode(length=255), nullable=True),
        sa.Column("email_verified_at", _utc_datetime(), nullable=True),
        sa.Column("role", sa.Unicode(length=10), nullable=False),
        sa.Column("member_tier", sa.Unicode(length=10), nullable=False),
        sa.Column("status", sa.Unicode(length=20), nullable=False),
        sa.Column("failed_login_count", sa.Integer(), nullable=False),
        sa.Column("locked_until", _utc_datetime(), nullable=True),
        sa.Column("token_version", sa.Integer(), nullable=False),
        sa.Column("terms_version", sa.Unicode(length=40), nullable=True),
        sa.Column("terms_accepted_at", _utc_datetime(), nullable=True),
        sa.Column("last_login_at", _utc_datetime(), nullable=True),
        sa.Column("created_at", _utc_datetime(), nullable=False),
        sa.Column("updated_at", _utc_datetime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("public_id", name="uq_users_public_id"),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.UniqueConstraint("email_canonical", name="uq_users_email_canonical"),
    )

    op.create_table(
        "user_identities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.Unicode(length=20), nullable=False),
        sa.Column("provider_subject", sa.Unicode(length=255), nullable=False),
        sa.Column("created_at", _utc_datetime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_user_identities"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_user_identities_user_id_users", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("provider", "provider_subject", name="uq_user_identities_provider_subject"),
        sa.UniqueConstraint("user_id", "provider", name="uq_user_identities_user_provider"),
    )

    op.create_table(
        "auth_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.Unicode(length=20), nullable=False),
        sa.Column("token_hash", sa.Unicode(length=64), nullable=False),
        sa.Column("created_at", _utc_datetime(), nullable=False),
        sa.Column("expires_at", _utc_datetime(), nullable=False),
        sa.Column("used_at", _utc_datetime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_auth_tokens"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_auth_tokens_user_id_users", ondelete="CASCADE"),
        sa.UniqueConstraint("token_hash", name="uq_auth_tokens_token_hash"),
    )
    op.create_index(
        "ix_auth_tokens_user_id_purpose_created_at", "auth_tokens", ["user_id", "purpose", "created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_auth_tokens_user_id_purpose_created_at", table_name="auth_tokens")
    op.drop_table("auth_tokens")
    op.drop_table("user_identities")
    op.drop_table("users")
