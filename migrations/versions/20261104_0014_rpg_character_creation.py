"""玩家主角（rpg-character-creation）：存檔的主角資料與主角生成的每日次數限制。

expand 階段，只新增、不刪除任何既有欄位或資料表，新增的欄位可為空，所以舊版程式不認識它也不受影響：
- `sessions` 新增可為空的 `protagonist_json`（JSON）：主角角色卡與錨定事實，形狀為
  `{"character": {...}, "anchor": {kind, ref, text} 或 null}`。既有的 `player_name`、`character_age` 欄位**保留**，之後由開存檔的
  同一個 INSERT 依角色卡寫入（舊程式、匯出與保存期限照常讀這兩欄）；遷移之前建立的存檔此欄為空。
- `runtime_limits` 補三個鍵：`protagonist_suggest_daily.free／plus／vip`（每個台灣日可請 AI 生成主角的次數，初始值 5／15／40）。
  以 `op.bulk_insert` 表達，不是原生 SQL；**只插入資料庫還沒有的鍵**（先查再插），所以資料庫已有這幾個鍵、或部署者先前手動調整過其他
  限制時，升級都不會產生重複的鍵或蓋掉既有的值。程式在缺值時另有保底預設（`app/services/runtime_limits.py`，測試會比對兩者一致）。
- `llm_call_logs.kind` 不必加寬：新的呼叫類別 `protagonist_gen`（15 字）與 `protagonist_review`（18 字）都放得進 0013 加寬後的 20 字。

部署順序：備份 → migration → 部署新版程式（見營運文件）。

回復（downgrade）會移除 `protagonist_json`（主角資料一併消失，姓名與年齡仍在 `player_name`、`character_age`）與三個限制鍵。
`recreate="never"`：SQLite 直接用 ALTER TABLE DROP；預設的「重建資料表」會先刪除 sessions，而回合以串聯刪除參照它，外鍵啟用時會連帶
清掉資料（同 0010、0012、0013）。只在還沒有任何真實資料時使用，有資料時以備份還原。

這個檔案不匯入應用程式的程式碼；日期時間欄位的型別需與 app.models.base.UTCDateTime 在資料庫中的型別一致
（SQL Server 為 DATETIME2(6)）。

Revision ID: 0014_rpg_character_creation
Revises: 0013_rpg_script_authoring
Create Date: 2026-11-04
"""
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0014_rpg_character_creation"
down_revision = "0013_rpg_script_authoring"
branch_labels = None
depends_on = None

# 這次新增的限制（與 app/services/runtime_limits.py 的保底預設一致，由測試比對）
INITIAL_LIMITS = (
    ("protagonist_suggest_daily.free", 5),
    ("protagonist_suggest_daily.plus", 15),
    ("protagonist_suggest_daily.vip", 40),
)

_runtime_limits = sa.table(
    "runtime_limits",
    sa.column("key", sa.Unicode(60)),
    sa.column("int_value", sa.Integer()),
    sa.column("updated_at", sa.DateTime()),
)


def upgrade() -> None:
    with op.batch_alter_table("sessions") as batch:
        batch.add_column(sa.Column("protagonist_json", sa.JSON(), nullable=True))

    bind = op.get_bind()
    present = set(bind.execute(sa.select(_runtime_limits.c.key)).scalars())
    now = datetime.now(timezone.utc).replace(tzinfo=None)         # 資料庫存 UTC 的無時區時間
    missing = [{"key": key, "int_value": value, "updated_at": now} for key, value in INITIAL_LIMITS if key not in present]
    if missing:
        op.bulk_insert(_runtime_limits, missing)


def downgrade() -> None:
    op.get_bind().execute(sa.delete(_runtime_limits).where(_runtime_limits.c.key.in_([key for key, _ in INITIAL_LIMITS])))
    with op.batch_alter_table("sessions", recreate="never") as batch:
        batch.drop_column("protagonist_json")
