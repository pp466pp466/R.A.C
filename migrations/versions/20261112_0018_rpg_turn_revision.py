"""重新生成與編輯最後一輪（rpg-turn-revision）：回合的修訂欄位、回饋表，與免費重新生成次數的設定。

expand 階段，只新增、不刪除任何既有欄位或資料表；新欄位皆可為空或有預設值，舊版程式不認識它們也照常運作：
- `memory_logs.revision`（NOT NULL、`server_default` 1）：每次成功修訂加 1。既有的回合視為第 1 版。
- `memory_logs.regen_count`（NOT NULL、`server_default` 0）：這份輸入已成功重新生成幾次。既有的回合視為 0 次。
- `memory_logs.revising_started_at`（可為空）：修訂的認領時間；空值＝沒有人在修訂。不回填。
- `memory_logs.state_before_json`（可為空）：回合前的世界快照。**不回填**：遷移之前建立的回合沒有快照，因此不可修訂（只影響開發與驗收資料）。
- 新表 `turn_regenerations`：玩家於重新生成時填的分類與理由（供營運檢視），隨存檔串聯刪除；`(session_id, turn_seq, attempt)` 唯一。
  值域（種類、分類、結果）由 Service 驗證，不加 CHECK 約束（CHECK 在 SQLite 的 batch migration 與 SQL Server 之間有方言差異）。
- `runtime_limits` 補一個鍵 `rpg.free_regenerations`（2）：每個回合前幾次重新生成免費。以 `op.bulk_insert` 表達，不是原生 SQL；
  **只插入資料庫還沒有的鍵**，所以部署者先前調整過的值不會被蓋掉，重複升級也不會產生重複的鍵。程式在缺值時另有保底預設
  （`app/services/runtime_limits.py`，測試會比對兩者一致）。

部署順序：備份 → migration → 部署新版程式（見營運文件）。舊版程式在新結構上可運作；新版程式不要求在舊結構上運作。

回復（downgrade）會移除這些欄位、新表與設定鍵（修訂的快照、版本與玩家填的理由一併消失），只在還沒有任何真實資料時使用，有資料時以備份還原。
`recreate="never"`：SQLite 直接用 ALTER TABLE DROP；預設的「重建資料表」會先刪除 memory_logs，而它以串聯刪除參照存檔、也被其他資料參照，
外鍵啟用時會連帶清掉資料（同 0010、0012、0013、0014、0016、0017）。`revision`、`regen_count` 有預設值約束，SQL Server 需先移除才能刪欄位（`mssql_drop_default`）。

這個檔案不匯入應用程式的程式碼；日期時間欄位的型別需與 app.models.base.UTCDateTime 在資料庫中的型別一致
（SQL Server 為 DATETIME2(6)）。

Revision ID: 0018_rpg_turn_revision
Revises: 0017_story_preference
Create Date: 2026-11-12
"""
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mssql

# revision identifiers, used by Alembic.
revision = "0018_rpg_turn_revision"
down_revision = "0017_story_preference"
branch_labels = None
depends_on = None

# 這次新增的設定（與 app/services/runtime_limits.py 的保底預設一致，由測試比對）
INITIAL_LIMITS = (
    ("rpg.free_regenerations", 2),
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
    with op.batch_alter_table("memory_logs") as batch:
        batch.add_column(sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("regen_count", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("revising_started_at", _utc_datetime(), nullable=True))
        batch.add_column(sa.Column("state_before_json", sa.JSON(), nullable=True))

    op.create_table(
        "turn_regenerations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("turn_seq", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Unicode(length=10), nullable=False),
        sa.Column("category", sa.Unicode(length=30), nullable=True),
        sa.Column("reason", sa.UnicodeText(), nullable=True),
        sa.Column("is_free", sa.Boolean(), nullable=False),
        sa.Column("result", sa.Unicode(length=10), nullable=False),
        sa.Column("created_at", _utc_datetime(), nullable=False),
        sa.Column("updated_at", _utc_datetime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_turn_regenerations"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], name="fk_turn_regenerations_session_id_sessions", ondelete="CASCADE"),
        sa.UniqueConstraint("session_id", "turn_seq", "attempt", name="uq_turn_regenerations_session_turn_attempt"),
    )

    bind = op.get_bind()
    present = set(bind.execute(sa.select(_runtime_limits.c.key)).scalars())
    now = datetime.now(timezone.utc).replace(tzinfo=None)         # 資料庫存 UTC 的無時區時間
    missing = [{"key": key, "int_value": value, "updated_at": now} for key, value in INITIAL_LIMITS if key not in present]
    if missing:
        op.bulk_insert(_runtime_limits, missing)


def downgrade() -> None:
    op.get_bind().execute(sa.delete(_runtime_limits).where(_runtime_limits.c.key.in_([key for key, _ in INITIAL_LIMITS])))
    op.drop_table("turn_regenerations")
    with op.batch_alter_table("memory_logs", recreate="never") as batch:
        batch.drop_column("state_before_json")
        batch.drop_column("revising_started_at")
        batch.drop_column("regen_count", mssql_drop_default=True)
        batch.drop_column("revision", mssql_drop_default=True)
