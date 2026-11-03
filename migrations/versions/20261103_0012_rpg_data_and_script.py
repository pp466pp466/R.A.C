"""RPG 的資料基礎（rpg-data-and-script）：劇本、版本、標籤、存檔、回合，以及使用者的內容模式。

expand 階段，只新增、不刪除任何既有欄位或資料表：
- `users` 新增四欄：`content_tier_preference`（`NOT NULL`，伺服器預設 `safe`）與三個可為空的成年確認欄位
  （`adult_confirmed_at`、`adult_rules_version`、`adult_confirm_method`）。伺服器預設讓舊版程式的 INSERT 在新結構上
  仍然有效，且是最保守的值。
- 新增五張資料表：`scripts`、`script_versions`、`script_version_tags`、`sessions`、`memory_logs`。作者與使用者的外鍵一律
  `ON DELETE CASCADE`（失敗時偏向刪除）；`sessions.source_version_id` 是無動作的外鍵（SQL Server 不允許刪除使用者時有多重
  串聯路徑到達 sessions，錯誤 1785），所以由刪除版本與抹除帳號的流程先把參照清為空值。
  `scripts.seed_key`、`script_versions.live_script_id`、`script_versions.open_script_id` 以「只涵蓋非 NULL 列」的部分唯一索引
  （SQLite 與 SQL Server 的 NULL 行為一致）保證：種子鍵唯一、每個劇本至多一個上架版本、至多一個進行中的版本。
- 一次小型的資料回填：既有帳號的內容模式偏好依「年齡確認當時是否成年」設定（`is_adult_at_confirmation` 為真 → `standard`，其餘
  維持 `safe`），所以先前未成年、現在已成年的帳號**不會被自動調高**。用 `sa.table`／`sa.update` 表達，不是原生 SQL。

舊版程式不認識這些欄位與資料表也不受影響，所以先升級資料庫再部署新版程式是安全的；部署順序固定是
「備份 → migration → 部署 → `flask seed-scripts`」（見營運文件）。

回復（downgrade）會移除新的資料表與欄位（劇本、存檔與回合一併消失）。`recreate="never"`：SQLite 直接用 ALTER TABLE DROP；
預設的「重建資料表」會先刪除 users，而其他資料表以串聯刪除參照它，外鍵啟用時會連帶清掉資料（同 0010）。SQL Server 要先拿掉
欄位的預設值約束才能刪欄位（`mssql_drop_default`）。只在還沒有任何真實資料時使用，有資料時以備份還原。

這個檔案不匯入應用程式的程式碼；日期時間欄位的型別需與 app.models.base.UTCDateTime 在資料庫中的型別一致
（SQL Server 為 DATETIME2(6)）。

Revision ID: 0012_rpg_data_and_script
Revises: 0011_remove_player_passwords
Create Date: 2026-11-03
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mssql

# revision identifiers, used by Alembic.
revision = "0012_rpg_data_and_script"
down_revision = "0011_remove_player_passwords"
branch_labels = None
depends_on = None

CONTENT_TIER_SAFE = "safe"
CONTENT_TIER_STANDARD = "standard"

_users = sa.table(
    "users",
    sa.column("is_adult_at_confirmation", sa.Boolean()),
    sa.column("content_tier_preference", sa.Unicode(10)),
)


def _utc_datetime() -> sa.types.TypeEngine:
    """UTC 時間：SQL Server 使用 DATETIME2(6) 以保留微秒精度，其餘為一般 DateTime。"""
    return sa.DateTime().with_variant(mssql.DATETIME2(precision=6), "mssql")


def _not_null(column_name: str):
    """部分唯一索引的條件：只涵蓋該欄位非 NULL 的列（ORM 表達式，不是原生 SQL）。"""
    return sa.column(column_name).is_not(None)


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column("content_tier_preference", sa.Unicode(length=10), nullable=False, server_default=CONTENT_TIER_SAFE)
        )
        batch.add_column(sa.Column("adult_confirmed_at", _utc_datetime(), nullable=True))
        batch.add_column(sa.Column("adult_rules_version", sa.Unicode(length=40), nullable=True))
        batch.add_column(sa.Column("adult_confirm_method", sa.Unicode(length=20), nullable=True))

    # 回填：確認當時已成年的既有帳號為 `standard`；其餘（含舊版勾選時代、此欄為空的帳號）維持保守的 `safe`
    # 布林比較用 `== sa.true()`：SQL Server 沒有原生布林，`IS 1` 不是合法語法，各方言會各自轉成正確的比較
    op.get_bind().execute(
        sa.update(_users).where(_users.c.is_adult_at_confirmation == sa.true()).values(content_tier_preference=CONTENT_TIER_STANDARD)
    )

    op.create_table(
        "scripts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("creator_id", sa.Integer(), nullable=True),
        sa.Column("seed_key", sa.Unicode(length=60), nullable=True),
        sa.Column("author_name", sa.Unicode(length=20), nullable=True),
        sa.Column("author_code", sa.Unicode(length=8), nullable=True),
        sa.Column("author_locked_at", _utc_datetime(), nullable=True),
        sa.Column("public_id", sa.Unicode(length=36), nullable=False),
        sa.Column("created_at", _utc_datetime(), nullable=False),
        sa.Column("updated_at", _utc_datetime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_scripts"),
        sa.ForeignKeyConstraint(["creator_id"], ["users.id"], name="fk_scripts_creator_id_users", ondelete="CASCADE"),
        sa.UniqueConstraint("public_id", name="uq_scripts_public_id"),
    )
    op.create_index("ix_scripts_creator_id", "scripts", ["creator_id"], unique=False)
    op.create_index(
        "uq_scripts_seed_key", "scripts", ["seed_key"], unique=True,
        sqlite_where=_not_null("seed_key"), mssql_where=_not_null("seed_key"),
    )

    op.create_table(
        "script_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("script_id", sa.Integer(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.Unicode(length=16), nullable=False),
        sa.Column("spec_version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.Unicode(length=64), nullable=False),
        sa.Column("total_chars", sa.Integer(), nullable=False),
        sa.Column("title", sa.Unicode(length=30), nullable=False),
        sa.Column("summary", sa.Unicode(length=150), nullable=False),
        sa.Column("world_name", sa.Unicode(length=50), nullable=False),
        sa.Column("content_tier", sa.Unicode(length=10), nullable=False),
        sa.Column("background", sa.UnicodeText(), nullable=False),
        sa.Column("opening", sa.UnicodeText(), nullable=True),
        sa.Column("rules", sa.UnicodeText(), nullable=True),
        sa.Column("ending_mode", sa.Unicode(length=10), nullable=False),
        sa.Column("ending_text", sa.UnicodeText(), nullable=True),
        sa.Column("ending_age", sa.Integer(), nullable=True),
        sa.Column("calendar_name", sa.Unicode(length=12), nullable=True),
        sa.Column("calendar_start_year", sa.Integer(), nullable=True),
        sa.Column("characters_json", sa.JSON(), nullable=False),
        sa.Column("locations_json", sa.JSON(), nullable=False),
        sa.Column("milestones_json", sa.JSON(), nullable=False),
        sa.Column("live_script_id", sa.Integer(), nullable=True),
        sa.Column("open_script_id", sa.Integer(), nullable=True),
        sa.Column("published_at", _utc_datetime(), nullable=True),
        sa.Column("public_id", sa.Unicode(length=36), nullable=False),
        sa.Column("created_at", _utc_datetime(), nullable=False),
        sa.Column("updated_at", _utc_datetime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_script_versions"),
        sa.ForeignKeyConstraint(["script_id"], ["scripts.id"], name="fk_script_versions_script_id_scripts", ondelete="CASCADE"),
        sa.UniqueConstraint("public_id", name="uq_script_versions_public_id"),
        sa.UniqueConstraint("script_id", "version_no", name="uq_script_versions_script_version_no"),
    )
    op.create_index("ix_script_versions_status_content_tier", "script_versions", ["status", "content_tier"], unique=False)
    op.create_index(
        "uq_script_versions_live_script", "script_versions", ["live_script_id"], unique=True,
        sqlite_where=_not_null("live_script_id"), mssql_where=_not_null("live_script_id"),
    )
    op.create_index(
        "uq_script_versions_open_script", "script_versions", ["open_script_id"], unique=True,
        sqlite_where=_not_null("open_script_id"), mssql_where=_not_null("open_script_id"),
    )

    op.create_table(
        "script_version_tags",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.Column("tag", sa.Unicode(length=12), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_script_version_tags"),
        sa.ForeignKeyConstraint(
            ["version_id"], ["script_versions.id"], name="fk_script_version_tags_version_id_script_versions", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("version_id", "tag", name="uq_script_version_tags_version_tag"),
    )
    op.create_index("ix_script_version_tags_tag", "script_version_tags", ["tag"], unique=False)

    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("source_version_id", sa.Integer(), nullable=True),
        sa.Column("script_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("snapshot_spec_version", sa.Integer(), nullable=False),
        sa.Column("player_name", sa.Unicode(length=50), nullable=False),
        sa.Column("character_age", sa.Integer(), nullable=False),
        sa.Column("current_location", sa.Unicode(length=100), nullable=False),
        sa.Column("status_summary", sa.UnicodeText(), nullable=False),
        sa.Column("entities_json", sa.JSON(), nullable=False),
        sa.Column("goals_json", sa.JSON(), nullable=False),
        sa.Column("milestone_progress_json", sa.JSON(), nullable=False),
        sa.Column("elapsed_months", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("notebook_text", sa.UnicodeText(), nullable=False),
        sa.Column("notebook_version", sa.Integer(), nullable=False),
        sa.Column("notebook_upto_turn", sa.Integer(), nullable=False),
        sa.Column("content_tier", sa.Unicode(length=10), nullable=False),
        sa.Column("status", sa.Unicode(length=10), nullable=False),
        sa.Column("ended_at", _utc_datetime(), nullable=True),
        sa.Column("is_archived", sa.Boolean(), nullable=False),
        sa.Column("last_active_at", _utc_datetime(), nullable=False),
        sa.Column("public_id", sa.Unicode(length=36), nullable=False),
        sa.Column("created_at", _utc_datetime(), nullable=False),
        sa.Column("updated_at", _utc_datetime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_sessions_user_id_users", ondelete="CASCADE"),
        # 無動作（不是 SET NULL）：SQL Server 不允許刪除使用者時有多重串聯路徑到達 sessions（錯誤 1785）
        sa.ForeignKeyConstraint(["source_version_id"], ["script_versions.id"], name="fk_sessions_source_version_id_script_versions"),
        sa.UniqueConstraint("public_id", name="uq_sessions_public_id"),
    )
    op.create_index("ix_sessions_user_id_last_active_at", "sessions", ["user_id", "last_active_at"], unique=False)
    op.create_index("ix_sessions_last_active_at", "sessions", ["last_active_at"], unique=False)

    op.create_table(
        "memory_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("client_msg_id", sa.Unicode(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("turn_number", sa.Integer(), nullable=True),
        sa.Column("turn_status", sa.Unicode(length=12), nullable=False),
        sa.Column("user_action", sa.UnicodeText(), nullable=False),
        sa.Column("llm_raw_response", sa.UnicodeText(), nullable=False),
        sa.Column("structured_result", sa.JSON(), nullable=True),
        sa.Column("created_at", _utc_datetime(), nullable=False),
        sa.Column("updated_at", _utc_datetime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_memory_logs"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], name="fk_memory_logs_session_id_sessions", ondelete="CASCADE"),
        sa.UniqueConstraint("session_id", "client_msg_id", name="uq_memory_logs_session_client_msg"),
        sa.UniqueConstraint("session_id", "seq", name="uq_memory_logs_session_seq"),
    )
    op.create_index("ix_memory_logs_session_id_turn_number", "memory_logs", ["session_id", "turn_number"], unique=False)
    op.create_index("ix_memory_logs_turn_status_updated_at", "memory_logs", ["turn_status", "updated_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_memory_logs_turn_status_updated_at", table_name="memory_logs")
    op.drop_index("ix_memory_logs_session_id_turn_number", table_name="memory_logs")
    op.drop_table("memory_logs")
    op.drop_index("ix_sessions_last_active_at", table_name="sessions")
    op.drop_index("ix_sessions_user_id_last_active_at", table_name="sessions")
    op.drop_table("sessions")
    op.drop_index("ix_script_version_tags_tag", table_name="script_version_tags")
    op.drop_table("script_version_tags")
    op.drop_index("uq_script_versions_open_script", table_name="script_versions")
    op.drop_index("uq_script_versions_live_script", table_name="script_versions")
    op.drop_index("ix_script_versions_status_content_tier", table_name="script_versions")
    op.drop_table("script_versions")
    op.drop_index("uq_scripts_seed_key", table_name="scripts")
    op.drop_index("ix_scripts_creator_id", table_name="scripts")
    op.drop_table("scripts")
    with op.batch_alter_table("users", recreate="never") as batch:
        batch.drop_column("adult_confirm_method")
        batch.drop_column("adult_rules_version")
        batch.drop_column("adult_confirmed_at")
        batch.drop_column("content_tier_preference", mssql_drop_default=True)
