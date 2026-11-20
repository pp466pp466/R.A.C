"""劇本審核、下架與目錄（rpg-script-publishing）：版本的送審時間、下架與決定的中繼資料，以及目錄用的索引。

expand 階段，只新增、不刪除任何既有欄位或資料表；新欄位都可為空，舊版程式不認識它們也照常運作：
- `script_versions.submitted_at`（可為空）：送審時間（UTC），審核佇列由舊到新排序用；撤回時清除。**不回填**：遷移之前已在
  `pending_review` 的版本為空，佇列以 `created_at` 代替。
- `script_versions.removed_by`（可為空，`Unicode(8)`）：下架者，`author` 或 `admin`；決定恢復的權限。
- `script_versions.decision_category`（可為空，`Unicode(24)`）：退回與管理員下架的固定類別列舉（不含任何內容）。
- `script_versions.decision_note`（可為空，`UnicodeText`）：管理員的審核備註，只有管理員看得到；隨所屬版本保存與刪除。
- `script_versions.decided_at`（可為空，UTC 時間）：退回或下架的時間。
- 索引 `ix_script_versions_status_published_at`（`status`、`published_at`）：公開目錄的游標查詢與上架清單只看上架中的版本。
- 版本狀態新增 `removed`（下架）是**值域的新增**，欄位型別 `NVARCHAR(16)` 放得下，不加 CHECK 約束（值域由程式的常數與 Service 驗證）。
  只有新版程式會寫入這個值，所以舊版程式在新結構上可運作，但**回復到舊版程式之前須確認沒有 `removed` 版本**（舊版程式不認識該狀態：
  它不會出現在目錄也不能編輯，舊版的「最新的非進行中版本」看不到它）；有的話先由管理員逐一恢復，或讓作者建立新版本。

這個 change 沒有改寫任何既有資料；五個新欄位都是中繼資料，不進內容雜湊，已發佈的版本「內容不可變」不受影響。

部署順序：備份 → migration → 部署新版程式。不需要 flask seed-scripts（沒有內建劇本內容變動）。
舊版程式在新結構上可運作；新版程式不要求在舊結構上運作。

回復（downgrade）會移除索引與這五個欄位（決定類別與審核備註一併消失，沒有其他影響），只在需要時使用；
`recreate="never"`：SQLite 直接用 ALTER TABLE DROP；預設的「重建資料表」會先刪除 script_versions，而 script_version_tags 以串聯刪除參照它、
sessions.source_version_id 也參照它，外鍵啟用時會連帶清掉資料（同 0010、0012、0013、0014、0016、0017、0018、0019、0020）。

這個檔案不匯入應用程式的程式碼；日期時間欄位的型別需與 app.models.base.UTCDateTime 在資料庫中的型別一致
（SQL Server 為 DATETIME2(6)）。

Revision ID: 0021_script_publishing
Revises: 0020_safe_mode_hint
Create Date: 2026-11-20
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mssql

# revision identifiers, used by Alembic.
revision = "0021_script_publishing"
down_revision = "0020_safe_mode_hint"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_script_versions_status_published_at"


def _utc_datetime() -> sa.types.TypeEngine:
    """UTC 時間：SQL Server 使用 DATETIME2(6) 以保留微秒精度，其餘為一般 DateTime。"""
    return sa.DateTime().with_variant(mssql.DATETIME2(precision=6), "mssql")


def upgrade() -> None:
    with op.batch_alter_table("script_versions") as batch:
        batch.add_column(sa.Column("submitted_at", _utc_datetime(), nullable=True))
        batch.add_column(sa.Column("removed_by", sa.Unicode(8), nullable=True))
        batch.add_column(sa.Column("decision_category", sa.Unicode(24), nullable=True))
        batch.add_column(sa.Column("decision_note", sa.UnicodeText(), nullable=True))
        batch.add_column(sa.Column("decided_at", _utc_datetime(), nullable=True))
        batch.create_index(INDEX_NAME, ["status", "published_at"])


def downgrade() -> None:
    with op.batch_alter_table("script_versions", recreate="never") as batch:
        batch.drop_index(INDEX_NAME)
        batch.drop_column("decided_at")
        batch.drop_column("decision_note")
        batch.drop_column("decision_category")
        batch.drop_column("removed_by")
        batch.drop_column("submitted_at")
