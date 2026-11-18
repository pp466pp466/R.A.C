"""全站只限滿 18 歲（adult-only-signup）：註冊時年滿 18 歲聲明的時間與版本。

expand 階段，只新增、不刪除任何既有欄位或資料表；兩個新欄位皆可為空，舊版程式不認識它們也照常運作：
- `users.adult_declared_at`（可為空）：新使用者在登入頁勾選「我確認已滿 18 歲」的時間（UTC）。**不回填**：管理員帳號與升級前的帳號
  沒有這個動作，維持空值，任何閘門都不讀它（年齡一律由 `birth_date` 於當下計算；聲明只是稽核紀錄）。
- `users.adult_declaration_version`（可為空，`Unicode(40)`）：聲明當時的年齡政策版本（`AgePolicy.version`），由伺服器決定。
- 不建索引：沒有任何查詢依賴它們。值域由 Service 驗證，不加 CHECK 約束（CHECK 在 SQLite 的 batch migration 與 SQL Server 之間有方言差異）。

部署順序：備份 → migration → 部署新版程式（見營運文件）。舊版程式在新結構上可運作；新版程式不要求在舊結構上運作。
`is_adult_at_confirmation` 欄位保留但不再寫入（日後的 contract 遷移再移除）。

回復（downgrade）會移除這兩個欄位（玩家的聲明紀錄一併消失），只在還沒有任何真實資料時使用，有資料時以備份還原。
`recreate="never"`：SQLite 直接用 ALTER TABLE DROP；預設的「重建資料表」會先刪除 users，而許多資料表以串聯刪除參照它，
外鍵啟用時會連帶清掉資料（同 0010、0012、0013、0014、0016、0017、0018）。

這個檔案不匯入應用程式的程式碼；日期時間欄位的型別需與 app.models.base.UTCDateTime 在資料庫中的型別一致
（SQL Server 為 DATETIME2(6)）。

Revision ID: 0019_adult_declaration
Revises: 0018_rpg_turn_revision
Create Date: 2026-11-17
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mssql

# revision identifiers, used by Alembic.
revision = "0019_adult_declaration"
down_revision = "0018_rpg_turn_revision"
branch_labels = None
depends_on = None


def _utc_datetime() -> sa.types.TypeEngine:
    """UTC 時間：SQL Server 使用 DATETIME2(6) 以保留微秒精度，其餘為一般 DateTime。"""
    return sa.DateTime().with_variant(mssql.DATETIME2(precision=6), "mssql")


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("adult_declared_at", _utc_datetime(), nullable=True))
        batch.add_column(sa.Column("adult_declaration_version", sa.Unicode(length=40), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users", recreate="never") as batch:
        batch.drop_column("adult_declaration_version")
        batch.drop_column("adult_declared_at")
