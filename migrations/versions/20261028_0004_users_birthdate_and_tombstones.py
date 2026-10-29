"""users 新增生日、年齡區間與確認當時是否成年；新增帳號刪除紀錄（deletion_tombstones）。

只新增可為空的欄位與一張新資料表（expand）：舊版程式不讀取新欄位、也不使用新資料表，所以先升級資料庫再部署新版程式是安全的。
不回填：升級前的帳號（含勾選版 `age-gate` 時代只有 `age_confirmed_at` 的帳號）一律視為尚未完成年齡確認，須在帳號設定頁
補填生日與年齡區間。
回復（downgrade）會刪除這三個欄位與其中的生日、也會刪除刪除紀錄；已有真實資料之後不可使用，請改用備份還原。
`deletion_tombstones.user_id` 刻意不設外鍵（被指向的帳號已經不存在）。

這個檔案不匯入應用程式的程式碼：migration 描述的是「當時」的結構，不應隨應用程式修改而改變。
日期時間欄位的型別需與 app.models.base.UTCDateTime 在資料庫中的型別一致（SQL Server 為 DATETIME2(6)）。

Revision ID: 0004_users_birthdate
Revises: 0003_users_age_confirmation
Create Date: 2026-10-28
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mssql

# revision identifiers, used by Alembic.
revision = "0004_users_birthdate"
down_revision = "0003_users_age_confirmation"
branch_labels = None
depends_on = None


def _utc_datetime() -> sa.types.TypeEngine:
    """UTC 時間：SQL Server 使用 DATETIME2(6) 以保留微秒精度，其餘為一般 DateTime。"""
    return sa.DateTime().with_variant(mssql.DATETIME2(precision=6), "mssql")


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("birth_date", sa.Date(), nullable=True))
        batch.add_column(sa.Column("age_range", sa.Unicode(length=20), nullable=True))
        batch.add_column(sa.Column("is_adult_at_confirmation", sa.Boolean(), nullable=True))

    op.create_table(
        "deletion_tombstones",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("deleted_at", _utc_datetime(), nullable=False),
        sa.Column("reason", sa.Unicode(length=30), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_deletion_tombstones"),
    )


def downgrade() -> None:
    op.drop_table("deletion_tombstones")
    # recreate="never"：SQLite 直接用 ALTER TABLE DROP COLUMN。預設的「重建資料表」會先刪除 users，
    # 而 user_identities 與 auth_tokens 以串聯刪除參照它，外鍵啟用時會連帶清掉這兩張表的資料（同 0003）。
    with op.batch_alter_table("users", recreate="never") as batch:
        batch.drop_column("is_adult_at_confirmation")
        batch.drop_column("age_range")
        batch.drop_column("birth_date")
