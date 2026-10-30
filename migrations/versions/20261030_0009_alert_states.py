"""營運告警（ops-alerting）：新增告警狀態資料表 `alert_states`。

只新增一張資料表（expand），不動任何既有的資料表或資料：舊版程式不認識它也不受影響，所以先升級資料庫再部署新版程式是安全的。
這張表沒有外鍵、不含個資（只有告警代碼、狀態與數字），至多 5 列。
回復（downgrade）會刪除這張表與其中的狀態，之後的第一次檢查會重新建立（已成立的告警會被當成新的進入而再通知一次）。

這個檔案不匯入應用程式的程式碼：migration 描述的是「當時」的結構，不應隨應用程式修改而改變。
日期時間欄位的型別需與 app.models.base.UTCDateTime 在資料庫中的型別一致（SQL Server 為 DATETIME2(6)）。

Revision ID: 0009_alert_states
Revises: 0008_privacy_lifecycle
Create Date: 2026-10-30
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mssql

# revision identifiers, used by Alembic.
revision = "0009_alert_states"
down_revision = "0008_privacy_lifecycle"
branch_labels = None
depends_on = None


def _utc_datetime() -> sa.types.TypeEngine:
    """UTC 時間：SQL Server 使用 DATETIME2(6) 以保留微秒精度，其餘為一般 DateTime。"""
    return sa.DateTime().with_variant(mssql.DATETIME2(precision=6), "mssql")


def upgrade() -> None:
    op.create_table(
        "alert_states",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("alert_key", sa.Unicode(length=40), nullable=False),
        sa.Column("state", sa.Unicode(length=10), nullable=False),
        sa.Column("severity", sa.Unicode(length=10), nullable=True),
        sa.Column("changed_at", _utc_datetime(), nullable=False),
        sa.Column("last_notified_at", _utc_datetime(), nullable=True),
        sa.Column("last_value", sa.Float(), nullable=True),
        sa.Column("updated_at", _utc_datetime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_alert_states"),
        sa.UniqueConstraint("alert_key", name="uq_alert_states_alert_key"),
    )


def downgrade() -> None:
    op.drop_table("alert_states")
