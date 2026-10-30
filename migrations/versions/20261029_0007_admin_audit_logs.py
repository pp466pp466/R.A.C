"""管理員後台（admin-console）：新增管理員操作的稽核資料表 `admin_audit_logs`。

只新增一張資料表與兩個索引（expand），不動任何既有的資料表或資料：舊版程式不認識它也不受影響，所以先升級資料庫
再部署新版程式是安全的。
外鍵：只有 `admin_user_id` 指向 `users.id`（`ON DELETE SET NULL`，管理員被刪除後紀錄保留）。目標使用者與對話只存對外識別碼、
不設外鍵——同一張表兩個指向 `users` 且帶動作的外鍵，SQL Server 會以錯誤 1785 拒絕建表，而且紀錄本來就該比被操作的資料活得久。
回復（downgrade）會刪除這張表與其中全部的稽核紀錄，已有真實紀錄之後不可使用，請改用備份還原。

這個檔案不匯入應用程式的程式碼：migration 描述的是「當時」的結構，不應隨應用程式修改而改變。
日期時間欄位的型別需與 app.models.base.UTCDateTime 在資料庫中的型別一致（SQL Server 為 DATETIME2(6)）。

Revision ID: 0007_admin_audit_logs
Revises: 0006_usage_indexes
Create Date: 2026-10-29
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mssql

# revision identifiers, used by Alembic.
revision = "0007_admin_audit_logs"
down_revision = "0006_usage_indexes"
branch_labels = None
depends_on = None


def _utc_datetime() -> sa.types.TypeEngine:
    """UTC 時間：SQL Server 使用 DATETIME2(6) 以保留微秒精度，其餘為一般 DateTime。"""
    return sa.DateTime().with_variant(mssql.DATETIME2(precision=6), "mssql")


def upgrade() -> None:
    op.create_table(
        "admin_audit_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("admin_user_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.Unicode(length=40), nullable=False),
        sa.Column("target_user_public_id", sa.Unicode(length=36), nullable=True),
        sa.Column("target_conversation_public_id", sa.Unicode(length=36), nullable=True),
        sa.Column("reason", sa.Unicode(length=200), nullable=True),
        sa.Column("detail_json", sa.JSON(), nullable=False),
        sa.Column("created_at", _utc_datetime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_admin_audit_logs"),
        sa.ForeignKeyConstraint(
            ["admin_user_id"], ["users.id"], name="fk_admin_audit_logs_admin_user_id_users", ondelete="SET NULL"
        ),
    )
    op.create_index("ix_admin_audit_logs_created_at", "admin_audit_logs", ["created_at"], unique=False)
    op.create_index(
        "ix_admin_audit_logs_admin_user_id_created_at", "admin_audit_logs", ["admin_user_id", "created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_table("admin_audit_logs")
