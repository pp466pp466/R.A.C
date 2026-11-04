"""玩家創作劇本（rpg-script-authoring）：暱稱、審查結果、送審紀錄與營運限制。

expand 階段，只新增、不刪除任何既有欄位或資料表，新增的欄位都可為空，所以舊版程式不認識它們也不受影響：
- `users` 新增三個可為空的欄位：`nickname`、`nickname_code`（公開暱稱與穩定短代號，既有帳號可為空，第一次創作前才須設定）、
  `publish_suspended_at`（連續被自動拒絕而暫停發佈的時間，建索引供後台列表）。
- `script_versions` 新增四個可為空的審查結果欄位：`review_hash`、`review_verdict`、`review_categories_json`、`reviewed_at`。
  它們是審查的中繼資料，不是內容；`review_hash` 與目前的 `content_hash` 相同才算數。
- 新增 `script_submissions`（送審紀錄：只有使用者、結果、類別列舉與時間；外鍵 `ON DELETE CASCADE`，刻意不連到版本）與
  `runtime_limits`（營運限制，每個鍵一列，`key` 唯一）。
- `llm_call_logs.kind` 由 `Unicode(10)` 加寬為 `Unicode(20)`：新的呼叫類別 `script_review` 有 13 個字，SQL Server 會以「字串會被截斷」
  拒絕寫入（SQLite 不檢查長度，所以只在 SQL Server 才會發現）；RPG 規劃的 `rpg_notebook` 也超過 10 個字。加寬對舊版程式無害（它們只寫入
  `chat`、`summary`）。`kind` 不在任何索引內，所以 SQL Server 的 `ALTER COLUMN` 可行；沒有資料表參照 `llm_call_logs`，所以 SQLite 的重建
  資料表不會連帶影響其他資料。
- 寫入七個初始限制列（劇本數 free 3／plus 10／vip 30、每日送審 3／5／10、連續被拒門檻 3）。以 `op.bulk_insert` 表達，不是原生 SQL；
  重複升級（升級、降級、再升級）不會產生重複的鍵，因為降級會移除整張表。程式在缺值時另有保底預設
  （`app/services/runtime_limits.py`，測試會比對兩者一致）。

部署順序：備份 → migration → 部署新版程式（見營運文件）。

回復（downgrade）會移除新的資料表與欄位（暱稱、審查結果、送審紀錄與限制一併消失）。`recreate="never"`：SQLite 直接用 ALTER TABLE
DROP；預設的「重建資料表」會先刪除 users，而其他資料表以串聯刪除參照它，外鍵啟用時會連帶清掉資料（同 0010、0012）。
只在還沒有任何真實資料時使用，有資料時以備份還原。

這個檔案不匯入應用程式的程式碼；日期時間欄位的型別需與 app.models.base.UTCDateTime 在資料庫中的型別一致
（SQL Server 為 DATETIME2(6)）。

Revision ID: 0013_rpg_script_authoring
Revises: 0012_rpg_data_and_script
Create Date: 2026-11-04
"""
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mssql

# revision identifiers, used by Alembic.
revision = "0013_rpg_script_authoring"
down_revision = "0012_rpg_data_and_script"
branch_labels = None
depends_on = None

# 初始限制（與 app/services/runtime_limits.py 的保底預設一致，由測試比對）
INITIAL_LIMITS = (
    ("script_limit.free", 3),
    ("script_limit.plus", 10),
    ("script_limit.vip", 30),
    ("script_submit_daily.free", 3),
    ("script_submit_daily.plus", 5),
    ("script_submit_daily.vip", 10),
    ("script_reject_streak", 3),
)

_llm_call_logs = sa.table("llm_call_logs", sa.column("kind", sa.Unicode(20)))

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
    with op.batch_alter_table("llm_call_logs") as batch:
        batch.alter_column("kind", existing_type=sa.Unicode(length=10), type_=sa.Unicode(length=20), existing_nullable=False)

    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("nickname", sa.Unicode(length=20), nullable=True))
        batch.add_column(sa.Column("nickname_code", sa.Unicode(length=8), nullable=True))
        batch.add_column(sa.Column("publish_suspended_at", _utc_datetime(), nullable=True))
    op.create_index("ix_users_publish_suspended_at", "users", ["publish_suspended_at"], unique=False)

    with op.batch_alter_table("script_versions") as batch:
        batch.add_column(sa.Column("review_hash", sa.Unicode(length=64), nullable=True))
        batch.add_column(sa.Column("review_verdict", sa.Unicode(length=8), nullable=True))
        batch.add_column(sa.Column("review_categories_json", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("reviewed_at", _utc_datetime(), nullable=True))

    op.create_table(
        "script_submissions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.Unicode(length=16), nullable=False),
        sa.Column("categories_json", sa.JSON(), nullable=True),
        sa.Column("created_at", _utc_datetime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_script_submissions"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_script_submissions_user_id_users", ondelete="CASCADE"),
    )
    op.create_index("ix_script_submissions_user_id_created_at", "script_submissions", ["user_id", "created_at"], unique=False)

    op.create_table(
        "runtime_limits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.Unicode(length=60), nullable=False),
        sa.Column("int_value", sa.Integer(), nullable=False),
        sa.Column("updated_at", _utc_datetime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_runtime_limits"),
        sa.UniqueConstraint("key", name="uq_runtime_limits_key"),
    )
    now = datetime.now(timezone.utc).replace(tzinfo=None)         # 資料庫存 UTC 的無時區時間
    op.bulk_insert(_runtime_limits, [{"key": key, "int_value": value, "updated_at": now} for key, value in INITIAL_LIMITS])


def downgrade() -> None:
    op.drop_table("runtime_limits")
    op.drop_index("ix_script_submissions_user_id_created_at", table_name="script_submissions")
    op.drop_table("script_submissions")
    with op.batch_alter_table("script_versions", recreate="never") as batch:
        batch.drop_column("reviewed_at")
        batch.drop_column("review_categories_json")
        batch.drop_column("review_verdict")
        batch.drop_column("review_hash")
    op.drop_index("ix_users_publish_suspended_at", table_name="users")
    with op.batch_alter_table("users", recreate="never") as batch:
        batch.drop_column("publish_suspended_at")
        batch.drop_column("nickname_code")
        batch.drop_column("nickname")
    # 窄回 10 個字之前，先刪掉放不下的呼叫紀錄（降級本來就會讓這次 change 的資料消失）
    op.get_bind().execute(sa.delete(_llm_call_logs).where(_llm_call_logs.c.kind.not_in(("chat", "summary"))))
    with op.batch_alter_table("llm_call_logs") as batch:
        batch.alter_column("kind", existing_type=sa.Unicode(length=20), type_=sa.Unicode(length=10), existing_nullable=False)
