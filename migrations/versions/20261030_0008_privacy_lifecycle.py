"""個資生命週期（privacy-lifecycle）：自助刪除帳號的兩個欄位與期限清除用的索引。

只新增（expand）：`users.deletion_requested_at`、`users.status_before_deletion` 兩個可為空的欄位，以及
`chat_conversations.updated_at` 的索引；不動任何既有資料、不加約束。舊版程式不認識這兩個欄位也能運作，
所以先升級資料庫再部署新版程式是安全的。
回復（downgrade）會刪除這兩個欄位與其中的資料（待刪除帳號的申請時間），只在確定沒有待刪除帳號時使用，
否則請改用備份還原。

這個檔案不匯入應用程式的程式碼：migration 描述的是「當時」的結構，不應隨應用程式修改而改變。
日期時間欄位的型別需與 app.models.base.UTCDateTime 在資料庫中的型別一致（SQL Server 為 DATETIME2(6)）。

Revision ID: 0008_privacy_lifecycle
Revises: 0007_admin_audit_logs
Create Date: 2026-10-30
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mssql

# revision identifiers, used by Alembic.
revision = "0008_privacy_lifecycle"
down_revision = "0007_admin_audit_logs"
branch_labels = None
depends_on = None


def _utc_datetime() -> sa.types.TypeEngine:
    """UTC 時間：SQL Server 使用 DATETIME2(6) 以保留微秒精度，其餘為一般 DateTime。"""
    return sa.DateTime().with_variant(mssql.DATETIME2(precision=6), "mssql")


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("deletion_requested_at", _utc_datetime(), nullable=True))
        batch.add_column(sa.Column("status_before_deletion", sa.Unicode(length=20), nullable=True))
    op.create_index("ix_chat_conversations_updated_at", "chat_conversations", ["updated_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_chat_conversations_updated_at", table_name="chat_conversations")
    # recreate="never"：SQLite 直接用 ALTER TABLE DROP COLUMN。預設的「重建資料表」會先刪除 users，
    # 而 user_identities、auth_tokens 與對話以串聯刪除參照它，外鍵啟用時會連帶清掉這些資料（同 0003、0004）。
    with op.batch_alter_table("users", recreate="never") as batch:
        batch.drop_column("status_before_deletion")
        batch.drop_column("deletion_requested_at")
