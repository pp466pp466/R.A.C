"""遊戲幣錢包（game-wallet）：錢包、帳本與遊戲幣的五個設定。

expand 階段，只新增、不刪除任何既有欄位或資料表，舊版程式不認識新增的東西也不受影響：
- `wallets`：每位玩家至多一個（`user_id` 唯一、`ON DELETE CASCADE`），免費與付費兩個餘額（`CHECK` 不小於 0）與 `version`。
- `wallet_entries`：只增不改的帳本（`user_id` `ON DELETE CASCADE`）。`idempotency_key` 全站唯一，`(user_id, seq)` 唯一。
  關聯的存檔以對外識別碼（UUID）存放，**沒有指向 `sessions` 的外鍵**（SQL Server 不允許 `users` 到帳本有兩條串聯路徑，
  也避免整數編號重用而錯指別人的存檔）。
- `runtime_limits` 補五個鍵：`coin.turn_cost.standard` 30、`coin.signup_grant` 200、`coin.checkin_grant` 200、`coin.free_cap` 1000、
  `coin.hold_timeout_seconds` 180。以 `op.bulk_insert` 表達，不是原生 SQL；**只插入資料庫還沒有的鍵**（先查再插），所以部署者
  先前手動調整過的值不會被蓋掉，重複升級也不會產生重複的鍵。程式在缺值時另有保底預設（`app/services/runtime_limits.py`，
  測試會比對兩者一致）。

沒有資料回填：既有且已完成年齡確認的玩家，在第一次存取錢包時由程式補發首次註冊贈幣（冪等鍵相同，不會重複）。

部署順序：備份 → migration → 部署新版程式（見營運文件）。

回復（downgrade）會刪除兩張表（錢包資料一併消失）與五個設定鍵，只在還沒有任何真實資料時使用，有資料時以備份還原。

這個檔案不匯入應用程式的程式碼；日期時間欄位的型別需與 app.models.base.UTCDateTime 在資料庫中的型別一致
（SQL Server 為 DATETIME2(6)）。

Revision ID: 0015_game_wallet
Revises: 0014_rpg_character_creation
Create Date: 2026-11-05
"""
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mssql

# revision identifiers, used by Alembic.
revision = "0015_game_wallet"
down_revision = "0014_rpg_character_creation"
branch_labels = None
depends_on = None

# 這次新增的設定（與 app/services/runtime_limits.py 的保底預設一致，由測試比對）
INITIAL_LIMITS = (
    ("coin.turn_cost.standard", 30),
    ("coin.signup_grant", 200),
    ("coin.checkin_grant", 200),
    ("coin.free_cap", 1000),
    ("coin.hold_timeout_seconds", 180),
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
        "wallets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("free_balance", sa.Integer(), nullable=False),
        sa.Column("paid_balance", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", _utc_datetime(), nullable=False),
        sa.Column("updated_at", _utc_datetime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_wallets"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_wallets_user_id_users", ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", name="uq_wallets_user_id"),
        sa.CheckConstraint(
            sa.and_(sa.column("free_balance") >= 0, sa.column("paid_balance") >= 0), name="balances_non_negative"
        ),
    )
    op.create_table(
        "wallet_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Unicode(length=20), nullable=False),
        sa.Column("idempotency_key", sa.Unicode(length=200), nullable=False),
        sa.Column("free_delta", sa.Integer(), nullable=False),
        sa.Column("paid_delta", sa.Integer(), nullable=False),
        sa.Column("free_after", sa.Integer(), nullable=False),
        sa.Column("paid_after", sa.Integer(), nullable=False),
        sa.Column("session_public_id", sa.Unicode(length=36), nullable=True),
        sa.Column("ref_key", sa.Unicode(length=200), nullable=True),
        sa.Column("resolution", sa.Unicode(length=10), nullable=True),
        sa.Column("created_at", _utc_datetime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_wallet_entries"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_wallet_entries_user_id_users", ondelete="CASCADE"),
        sa.UniqueConstraint("idempotency_key", name="uq_wallet_entries_idempotency_key"),
        sa.UniqueConstraint("user_id", "seq", name="uq_wallet_entries_user_seq"),
    )
    op.create_index("ix_wallet_entries_user_id_created_at", "wallet_entries", ["user_id", "created_at"], unique=False)

    bind = op.get_bind()
    present = set(bind.execute(sa.select(_runtime_limits.c.key)).scalars())
    now = datetime.now(timezone.utc).replace(tzinfo=None)         # 資料庫存 UTC 的無時區時間
    missing = [{"key": key, "int_value": value, "updated_at": now} for key, value in INITIAL_LIMITS if key not in present]
    if missing:
        op.bulk_insert(_runtime_limits, missing)


def downgrade() -> None:
    op.get_bind().execute(sa.delete(_runtime_limits).where(_runtime_limits.c.key.in_([key for key, _ in INITIAL_LIMITS])))
    op.drop_index("ix_wallet_entries_user_id_created_at", table_name="wallet_entries")
    op.drop_table("wallet_entries")
    op.drop_table("wallets")
