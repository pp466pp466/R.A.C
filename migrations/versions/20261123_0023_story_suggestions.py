"""視覺小說式遊戲畫面（rpg-story-view）：存檔保存最近一次產生的行動建議。

expand 階段，只新增、不刪除任何既有欄位或資料表；新欄位可為空，舊版程式不認識它也照常運作：
- `sessions.suggestions_json`（可為空，JSON）：`{"seq": <最後完成回合的 seq，第 0 幕為 0>, "revision": <該回合的 revision，第 0 幕為 0>,
  "items": [<建議文字>…]}`。錨點（seq、revision）等於目前的錨點才有效；新回合完成或修訂成功都會使它自動過期，所以回合引擎與修訂流程
  不需要清除它。**不回填**：既有存檔維持空值（視為沒有建議）。無預設的非空值；不建索引（沒有任何查詢依賴它，也不得依 JSON 內容篩選）。
- 內容由模型依玩家的角色卡與近期劇情產生，視為個資：與存檔同期限、隨存檔與帳號刪除（同一資料列，不需要另外的抹除步驟）、納入個人資料匯出。

這個 change 沒有改寫任何既有資料。

部署順序：備份 → migration → 部署新版程式。舊版程式在新結構上可運作（不認識新欄位）；新版程式不要求在舊結構上運作。

回復（downgrade）會移除這個欄位（已保存的建議一併消失，玩家再點圖示即可重新產生，沒有其他影響），只在需要時使用；
`recreate="never"`：SQLite 直接用 ALTER TABLE DROP；預設的「重建資料表」會先刪除 sessions，而回合、記事簿等資料表以串聯刪除參照它，
外鍵啟用時會連帶清掉資料（同 0010、0012、0013、0014、0016、0017、0018、0019、0020、0021、0022）。

這個檔案不匯入應用程式的程式碼。

Revision ID: 0023_story_suggestions
Revises: 0022_script_reports
Create Date: 2026-11-23
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0023_story_suggestions"
down_revision = "0022_script_reports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("sessions") as batch:
        batch.add_column(sa.Column("suggestions_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("sessions", recreate="never") as batch:
        batch.drop_column("suggestions_json")
