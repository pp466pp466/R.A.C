"""RPG 回合引擎（rpg-turn-engine）：回合的嘗試序號、第 0 幕的三個欄位、串流首字延遲，與每回合經過月數的上限設定。

expand 階段，只新增、不刪除任何既有欄位或資料表；新欄位皆可為空或有預設值，舊版程式不認識它們也照常運作：
- `memory_logs.attempt`（NOT NULL、`server_default` 1）：嘗試序號，是遊戲幣預扣冪等鍵的一部分。既有的回合視為第 1 次嘗試。
- `sessions.opening_text`／`opening_state`／`opening_started_at`（可為空）：第 0 幕的文字、狀態與搶佔時間。既有的存檔為空，
  程式視為 fallback 且不阻擋回合；不回填。
- `llm_call_logs.first_chunk_ms`（可為空）：串流呼叫從送出到第一個正文片段的毫秒數。
- `runtime_limits` 補一個鍵 `rpg.max_months_per_turn`（24）：單一回合最多推進幾個遊戲內月份。以 `op.bulk_insert` 表達，不是原生 SQL；
  **只插入資料庫還沒有的鍵**，所以部署者先前調整過的值不會被蓋掉，重複升級也不會產生重複的鍵。程式在缺值時另有保底預設
  （`app/services/runtime_limits.py`，測試會比對兩者一致）。
- `llm_call_logs.kind` 不必加寬：新的呼叫類別 `rpg_stream`（10 字）、`rpg_state`（9 字）、`rpg_opening`（11 字）都放得進 0013 加寬後的 20 字。

部署順序：備份 → migration → 部署新版程式（見營運文件）。

回復（downgrade）會移除這些欄位（第 0 幕文字與嘗試序號一併消失）與設定鍵，只在還沒有任何真實資料時使用，有資料時以備份還原。
`recreate="never"`：SQLite 直接用 ALTER TABLE DROP；預設的「重建資料表」會先刪除 sessions／memory_logs，而回合以串聯刪除參照存檔，
外鍵啟用時會連帶清掉資料（同 0010、0012、0013、0014）。`attempt` 有預設值約束，SQL Server 需先移除才能刪欄位（`mssql_drop_default`）。

這個檔案不匯入應用程式的程式碼；日期時間欄位的型別需與 app.models.base.UTCDateTime 在資料庫中的型別一致
（SQL Server 為 DATETIME2(6)）。

Revision ID: 0016_rpg_turn_engine
Revises: 0015_game_wallet
Create Date: 2026-11-09
"""
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mssql

# revision identifiers, used by Alembic.
revision = "0016_rpg_turn_engine"
down_revision = "0015_game_wallet"
branch_labels = None
depends_on = None

# 這次新增的設定（與 app/services/runtime_limits.py 的保底預設一致，由測試比對）
INITIAL_LIMITS = (
    ("rpg.max_months_per_turn", 24),
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
        batch.add_column(sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"))
    with op.batch_alter_table("sessions") as batch:
        batch.add_column(sa.Column("opening_text", sa.UnicodeText(), nullable=True))
        batch.add_column(sa.Column("opening_state", sa.Unicode(length=12), nullable=True))
        batch.add_column(sa.Column("opening_started_at", _utc_datetime(), nullable=True))
    with op.batch_alter_table("llm_call_logs") as batch:
        batch.add_column(sa.Column("first_chunk_ms", sa.Integer(), nullable=True))

    bind = op.get_bind()
    present = set(bind.execute(sa.select(_runtime_limits.c.key)).scalars())
    now = datetime.now(timezone.utc).replace(tzinfo=None)         # 資料庫存 UTC 的無時區時間
    missing = [{"key": key, "int_value": value, "updated_at": now} for key, value in INITIAL_LIMITS if key not in present]
    if missing:
        op.bulk_insert(_runtime_limits, missing)


def downgrade() -> None:
    op.get_bind().execute(sa.delete(_runtime_limits).where(_runtime_limits.c.key.in_([key for key, _ in INITIAL_LIMITS])))
    with op.batch_alter_table("llm_call_logs", recreate="never") as batch:
        batch.drop_column("first_chunk_ms")
    with op.batch_alter_table("sessions", recreate="never") as batch:
        batch.drop_column("opening_started_at")
        batch.drop_column("opening_state")
        batch.drop_column("opening_text")
    with op.batch_alter_table("memory_logs", recreate="never") as batch:
        batch.drop_column("attempt", mssql_drop_default=True)
