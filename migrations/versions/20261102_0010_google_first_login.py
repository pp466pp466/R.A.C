"""Google 登入改版（google-first-login）：年齡確認期限、帳號鎖定與申訴。

expand 階段，只新增、不動既有資料：
- `users` 新增三個可為空的欄位：`age_deadline_at`（年齡確認期限，既有帳號為空值 → 不會被逾時清除）、
  `locked_at`、`lock_reason`（玩家帳號鎖定）；`age_deadline_at` 與 `locked_at` 各有一個索引（每小時的逾時清除、告警計數用）。
- 新增 `lock_strikes`（鎖定計次，只有帳號編號、類別與時間）與 `lock_appeals`（申訴，含 `public_id`）；
  兩張表都只有一個指向 `users` 的外鍵，`ON DELETE CASCADE`。
舊版程式不認識這些欄位與資料表也不受影響，所以先升級資料庫再部署新版程式是安全的；新版程式需要這些欄位，
所以部署順序固定是「備份 → migration → 部署」（見營運文件）。

回復（downgrade）會移除這些欄位與兩張表（鎖定狀態與申訴一併消失）。`recreate="never"`：SQLite 直接用 ALTER TABLE DROP COLUMN，
預設的「重建資料表」會先刪除 users，而其他資料表以串聯刪除參照它，外鍵啟用時會連帶清掉資料（同 0003、0004、0008）。

這個檔案不匯入應用程式的程式碼；日期時間欄位的型別需與 app.models.base.UTCDateTime 在資料庫中的型別一致（SQL Server 為 DATETIME2(6)）。

Revision ID: 0010_google_first_login
Revises: 0009_alert_states
Create Date: 2026-11-02
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mssql

# revision identifiers, used by Alembic.
revision = "0010_google_first_login"
down_revision = "0009_alert_states"
branch_labels = None
depends_on = None


def _utc_datetime() -> sa.types.TypeEngine:
    """UTC 時間：SQL Server 使用 DATETIME2(6) 以保留微秒精度，其餘為一般 DateTime。"""
    return sa.DateTime().with_variant(mssql.DATETIME2(precision=6), "mssql")


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("age_deadline_at", _utc_datetime(), nullable=True))
        batch.add_column(sa.Column("locked_at", _utc_datetime(), nullable=True))
        batch.add_column(sa.Column("lock_reason", sa.Unicode(length=20), nullable=True))
    op.create_index("ix_users_age_deadline_at", "users", ["age_deadline_at"], unique=False)
    op.create_index("ix_users_locked_at", "users", ["locked_at"], unique=False)

    op.create_table(
        "lock_strikes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Unicode(length=20), nullable=False),
        sa.Column("created_at", _utc_datetime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_lock_strikes"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_lock_strikes_user_id_users", ondelete="CASCADE"),
    )
    op.create_index("ix_lock_strikes_user_id_created_at", "lock_strikes", ["user_id", "created_at"], unique=False)

    op.create_table(
        "lock_appeals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("content", sa.UnicodeText(), nullable=False),
        sa.Column("created_at", _utc_datetime(), nullable=False),
        sa.Column("handled_at", _utc_datetime(), nullable=True),
        sa.Column("public_id", sa.Unicode(length=36), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_lock_appeals"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_lock_appeals_user_id_users", ondelete="CASCADE"),
        sa.UniqueConstraint("public_id", name="uq_lock_appeals_public_id"),
    )
    op.create_index("ix_lock_appeals_user_id_created_at", "lock_appeals", ["user_id", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_lock_appeals_user_id_created_at", table_name="lock_appeals")
    op.drop_table("lock_appeals")
    op.drop_index("ix_lock_strikes_user_id_created_at", table_name="lock_strikes")
    op.drop_table("lock_strikes")
    op.drop_index("ix_users_locked_at", table_name="users")
    op.drop_index("ix_users_age_deadline_at", table_name="users")
    with op.batch_alter_table("users", recreate="never") as batch:
        batch.drop_column("lock_reason")
        batch.drop_column("locked_at")
        batch.drop_column("age_deadline_at")
