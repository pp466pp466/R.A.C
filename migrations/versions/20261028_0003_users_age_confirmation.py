"""users 新增年齡確認的兩個欄位。

只新增兩個可為空的欄位（expand）：舊版程式不讀取它們，因此先升級資料庫再部署新版程式是安全的。
不回填：升級前已存在的帳號一律視為尚未確認，須在帳號設定頁補確認（age-confirmation 規格）。
回復（downgrade）會刪除這兩個欄位與其中的資料；已有真實的確認紀錄之後，不可使用，請改用備份還原。

這個檔案不匯入應用程式的程式碼：migration 描述的是「當時」的結構，不應隨應用程式修改而改變。
日期時間欄位的型別需與 app.models.base.UTCDateTime 在資料庫中的型別一致（SQL Server 為 DATETIME2(6)）。

Revision ID: 0003_users_age_confirmation
Revises: 0002_users_and_auth_tokens
Create Date: 2026-10-28
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mssql

# revision identifiers, used by Alembic.
revision = "0003_users_age_confirmation"
down_revision = "0002_users_and_auth_tokens"
branch_labels = None
depends_on = None


def _utc_datetime() -> sa.types.TypeEngine:
    """UTC 時間：SQL Server 使用 DATETIME2(6) 以保留微秒精度，其餘為一般 DateTime。"""
    return sa.DateTime().with_variant(mssql.DATETIME2(precision=6), "mssql")


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("age_confirmed_at", _utc_datetime(), nullable=True))
        batch.add_column(sa.Column("age_policy_version", sa.Unicode(length=40), nullable=True))


def downgrade() -> None:
    # recreate="never"：SQLite 直接用 ALTER TABLE DROP COLUMN（3.35 起支援）。預設的「重建資料表」會先刪除 users，
    # 而 user_identities 與 auth_tokens 以串聯刪除參照它，外鍵啟用時會連帶清掉這兩張表的資料。
    # SQL Server 本來就是直接 ALTER，不受影響。
    with op.batch_alter_table("users", recreate="never") as batch:
        batch.drop_column("age_policy_version")
        batch.drop_column("age_confirmed_at")
