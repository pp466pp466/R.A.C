"""故事偏好與劇本讀者頻道（story-preference）：使用者的偏好欄位與劇本版本的 audience 欄位。

expand 階段，只新增、不刪除任何既有欄位或資料表；新欄位的形狀都讓舊版程式不認識也照常運作：
- `users.story_preference`（NOT NULL、`server_default` 'all'）：玩家的故事偏好（all 全讀者、male 男頻、female 女頻），只影響劇本清單的
  排序。既有的帳號全部取得 'all'（資料庫補預設值），舊版程式的 INSERT 不含此欄時也取得 'all'。
- `script_versions.audience`（可為空）：版本的讀者頻道，屬版本內容。**不回填**：既有的版本維持空值，視同全讀者（已發佈的版本不可變，
  空值與 'all' 行為相同）。值域由 Service 驗證，不加 CHECK 約束（CHECK 在 SQLite 的 batch migration 與 SQL Server 之間有方言差異）。
- 不建索引：目錄量體小，排序鍵是運算式，篩選仍由既有的 (status, content_tier) 索引處理。

部署順序：備份 → migration → 部署新版程式（見營運文件）。舊版程式在新結構上可運作；新版程式不要求在舊結構上運作。

回復（downgrade）會移除這兩個欄位（玩家的偏好與版本的頻道一併消失），只在還沒有任何真實資料時使用，有資料時以備份還原。
`recreate="never"`：SQLite 直接用 ALTER TABLE DROP；預設的「重建資料表」會先刪除 users／script_versions，而許多資料表以串聯刪除參照它們，
外鍵啟用時會連帶清掉資料（同 0010、0012、0013、0014、0016）。`story_preference` 有預設值約束，SQL Server 需先移除才能刪欄位（`mssql_drop_default`）。

這個檔案不匯入應用程式的程式碼。

Revision ID: 0017_story_preference
Revises: 0016_rpg_turn_engine
Create Date: 2026-11-10
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0017_story_preference"
down_revision = "0016_rpg_turn_engine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("story_preference", sa.Unicode(length=10), nullable=False, server_default="all"))
    with op.batch_alter_table("script_versions") as batch:
        batch.add_column(sa.Column("audience", sa.Unicode(length=10), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("script_versions", recreate="never") as batch:
        batch.drop_column("audience")
    with op.batch_alter_table("users", recreate="never") as batch:
        batch.drop_column("story_preference", mssql_drop_default=True)
