"""劇本檢舉與匿名保留我已發佈的劇本（rpg-script-reports）：檢舉表、保留偏好欄位與每日檢舉上限設定。

expand 階段，只新增、不刪除任何既有欄位或資料表；新欄位有預設值，舊版程式不認識它們也照常運作：
- 新表 `script_reports`：玩家對「已上架的劇本版本」的檢舉（預設原因的列舉陣列加選填的自寫文字，文字是玩家文字、視為個資）。
  - `version_id`：指向 `script_versions.id`，**刻意沒有 `ondelete`（無動作）**。使用者刪除有兩條路徑會到達這張表：
    `users → script_reports`（檢舉人）與 `users → scripts → script_versions → script_reports`（被檢舉版本的作者）；
    SQL Server 不允許多重串聯路徑（錯誤 1785），與 `sessions.source_version_id` 同一個做法：由抹除流程在刪除版本之前明確清除。
  - `reporter_id`：指向 `users.id`，`ON DELETE CASCADE`（檢舉人刪帳號時其檢舉一併刪除；漏處理時偏向刪除）。
  - `reasons_json`：預設原因的列舉陣列，只用來顯示；排序與聚合只讀 `red_line`（不得依 JSON 內容篩選或排序）。
  - `red_line`：是否含任一紅線理由，建立時由 Service 算出、之後不變。**不加 `server_default`**：檢舉只由新版程式寫入，沒有舊版寫入者。
  - `closed_at`、`closed_result`：結案時間與結果（`removed`／`dismissed`／`superseded`），可為空（未結案）；保存期限為結案後 12 個月。
  - `UNIQUE (reporter_id, version_id)`：同一人對同一版本只能檢舉一次；索引 `(version_id, closed_at)`（佇列與自動結案）、
    `(reporter_id, created_at)`（每日上限）、`(closed_at)`（期限清除）。
  值域（理由、結果）由 Service 驗證，不加 CHECK 約束（CHECK 在 SQLite 的 batch migration 與 SQL Server 之間有方言差異）。
- `users.keep_published_scripts`（NOT NULL、`server_default` 假值）：帳號偏好「匿名保留我已發佈的劇本」，預設關閉；既有帳號視為關閉。
- `runtime_limits` 補一個鍵 `script_report_daily`（5）：每人每個台灣日的檢舉上限。以 `op.bulk_insert` 表達，不是原生 SQL；
  **只插入資料庫還沒有的鍵**，所以部署者先前調整過的值不會被蓋掉，重複升級也不會產生重複的鍵。

部署順序：備份 → migration → 部署新版程式。不需要 flask seed-scripts。舊版程式在新結構上可運作；新版程式不要求在舊結構上運作。
**回復到舊版程式之前須確認 `script_reports` 為空**：舊版的抹除流程不認識這張表，刪除含檢舉的作者會被無動作外鍵擋下。

回復（downgrade）會移除新表、`users.keep_published_scripts` 與設定鍵（檢舉資料與玩家的保留偏好一併消失），只在還沒有任何真實資料時使用，
有資料時以備份還原。`recreate="never"`：SQLite 直接用 ALTER TABLE DROP；預設的「重建資料表」會先刪除 users，而它被許多資料表以串聯刪除參照，
外鍵啟用時會連帶清掉資料（同 0010、0012、0013、0014、0016、0017、0018、0021）。`keep_published_scripts` 有預設值約束，
SQL Server 需先移除才能刪欄位（`mssql_drop_default`）。

這個檔案不匯入應用程式的程式碼；日期時間欄位的型別需與 app.models.base.UTCDateTime 在資料庫中的型別一致
（SQL Server 為 DATETIME2(6)）。

Revision ID: 0022_script_reports
Revises: 0021_script_publishing
Create Date: 2026-11-21
"""
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mssql

# revision identifiers, used by Alembic.
revision = "0022_script_reports"
down_revision = "0021_script_publishing"
branch_labels = None
depends_on = None

# 這次新增的設定（與 app/services/runtime_limits.py 的保底預設一致，由測試比對）
INITIAL_LIMITS = (
    ("script_report_daily", 5),
)

_runtime_limits = sa.table(
    "runtime_limits",
    sa.column("key", sa.Unicode(60)),
    sa.column("int_value", sa.Integer()),
    sa.column("updated_at", sa.DateTime()),
)


def _utc_datetime() -> sa.types.TypeEngine:
    """UTC 時間：SQL Server 使用 DATETIME2(6) 以保留微秒精度，其餘為一般 DateTime。"""
    return sa.DateTime().with_variant(mssql.DATETIME2(precision=6), "mssql")


def upgrade() -> None:
    op.create_table(
        "script_reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.Column("reporter_id", sa.Integer(), nullable=False),
        sa.Column("reasons_json", sa.JSON(), nullable=False),
        sa.Column("red_line", sa.Boolean(), nullable=False),
        sa.Column("note", sa.UnicodeText(), nullable=True),
        sa.Column("created_at", _utc_datetime(), nullable=False),
        sa.Column("updated_at", _utc_datetime(), nullable=False),
        sa.Column("closed_at", _utc_datetime(), nullable=True),
        sa.Column("closed_result", sa.Unicode(length=12), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_script_reports"),
        # 版本外鍵刻意沒有 ondelete（無動作）：避免 SQL Server 的多重串聯路徑（錯誤 1785），由抹除流程明確清除
        sa.ForeignKeyConstraint(["version_id"], ["script_versions.id"], name="fk_script_reports_version_id_script_versions"),
        sa.ForeignKeyConstraint(["reporter_id"], ["users.id"], name="fk_script_reports_reporter_id_users", ondelete="CASCADE"),
        sa.UniqueConstraint("reporter_id", "version_id", name="uq_script_reports_reporter_id"),
    )
    op.create_index("ix_script_reports_version_id_closed_at", "script_reports", ["version_id", "closed_at"])
    op.create_index("ix_script_reports_reporter_id_created_at", "script_reports", ["reporter_id", "created_at"])
    op.create_index("ix_script_reports_closed_at", "script_reports", ["closed_at"])

    with op.batch_alter_table("users") as batch:
        # 預設值用字串 "0"，不用 sa.false()：運算式預設值會讓 Alembic 在 SQLite 以「重建資料表」處理，而重建 users 會串聯清掉劇本與存檔
        batch.add_column(sa.Column("keep_published_scripts", sa.Boolean(), nullable=False, server_default="0"))

    bind = op.get_bind()
    present = set(bind.execute(sa.select(_runtime_limits.c.key)).scalars())
    now = datetime.now(timezone.utc).replace(tzinfo=None)         # 資料庫存 UTC 的無時區時間
    missing = [{"key": key, "int_value": value, "updated_at": now} for key, value in INITIAL_LIMITS if key not in present]
    if missing:
        op.bulk_insert(_runtime_limits, missing)


def downgrade() -> None:
    op.get_bind().execute(sa.delete(_runtime_limits).where(_runtime_limits.c.key.in_([key for key, _ in INITIAL_LIMITS])))
    with op.batch_alter_table("users", recreate="never") as batch:
        batch.drop_column("keep_published_scripts", mssql_drop_default=True)
    op.drop_index("ix_script_reports_closed_at", table_name="script_reports")
    op.drop_index("ix_script_reports_reporter_id_created_at", table_name="script_reports")
    op.drop_index("ix_script_reports_version_id_closed_at", table_name="script_reports")
    op.drop_table("script_reports")
