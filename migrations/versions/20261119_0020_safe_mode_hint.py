"""安全模式開關（safe-mode-toggle）：遊戲內提示每個存檔只出現一次的記錄。

expand 階段，只新增、不刪除任何既有欄位或資料表；新欄位可為空，舊版程式不認識它也照常運作：
- `sessions.safe_hint_at`（可為空）：這個存檔已向玩家提示過「可以嘗試關閉安全模式」的時間（UTC）。以條件式更新
  （`safe_hint_at` 為空才寫入，更新列數為 1 才送出提示）保證多 worker、換裝置與重送都不會重複提示。**不回填**：既有存檔維持空值，
  之後第一次符合條件的回合才會提示一次。
- 不建索引：沒有任何查詢依賴它。內容不是玩家文字，只有時間戳記。

這個 change 沒有改寫任何既有資料：舊值 `standard`（原一般模式）於讀取時一律讀作 `adult`（`parse_tier`），玩家偏好、劇本版本與存檔
都不需要資料改寫（劇本版本「已發佈不可變」，改寫它們與規範相衝）。`sessions.content_tier` 欄位保留並仍於開存檔時寫入，但新版程式
不讀它（存檔的生效模式一律現算）；移除它屬日後另立的 contract change。

部署順序：備份 → migration → 部署新版程式 → flask seed-scripts（見營運文件）。舊版程式在新結構上可運作；新版程式不要求在舊結構上運作。

回復（downgrade）會移除這個欄位（提示記錄一併消失，之後可能再提示一次，沒有其他影響），只在需要時使用；
`recreate="never"`：SQLite 直接用 ALTER TABLE DROP；預設的「重建資料表」會先刪除 sessions，而回合、記事簿等資料表以串聯刪除參照它，
外鍵啟用時會連帶清掉資料（同 0010、0012、0013、0014、0016、0017、0018、0019）。

這個檔案不匯入應用程式的程式碼；日期時間欄位的型別需與 app.models.base.UTCDateTime 在資料庫中的型別一致
（SQL Server 為 DATETIME2(6)）。

Revision ID: 0020_safe_mode_hint
Revises: 0019_adult_declaration
Create Date: 2026-11-19
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mssql

# revision identifiers, used by Alembic.
revision = "0020_safe_mode_hint"
down_revision = "0019_adult_declaration"
branch_labels = None
depends_on = None


def _utc_datetime() -> sa.types.TypeEngine:
    """UTC 時間：SQL Server 使用 DATETIME2(6) 以保留微秒精度，其餘為一般 DateTime。"""
    return sa.DateTime().with_variant(mssql.DATETIME2(precision=6), "mssql")


def upgrade() -> None:
    with op.batch_alter_table("sessions") as batch:
        batch.add_column(sa.Column("safe_hint_at", _utc_datetime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("sessions", recreate="never") as batch:
        batch.drop_column("safe_hint_at")
