"""客服核心：對話（chat_conversations）、訊息（chat_messages）與模型呼叫用量帳本（llm_call_logs）。

只新增三張資料表（expand）：舊版程式不認識它們也不受影響，所以先升級資料庫再部署新版程式是安全的。
外鍵：`chat_conversations.user_id` 與 `chat_messages.conversation_id` 為 CASCADE（刪除對話或使用者時內容一併消失）；
`llm_call_logs.user_id` 為 SET NULL（帳本保留但匿名化）；`llm_call_logs.conversation_id` **刻意不設外鍵**——SQL Server
不允許經由多條路徑的串聯動作（users 到 llm_call_logs 會有兩條），細節見 app/models/llm_call_log.py。
回復（downgrade）會刪除這三張表與其中全部的對話，已有真實對話之後不可使用，請改用備份還原。

這個檔案不匯入應用程式的程式碼：migration 描述的是「當時」的結構，不應隨應用程式修改而改變。
日期時間欄位的型別需與 app.models.base.UTCDateTime 在資料庫中的型別一致（SQL Server 為 DATETIME2(6)）。

Revision ID: 0005_chat_core
Revises: 0004_users_birthdate
Create Date: 2026-10-29
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mssql

# revision identifiers, used by Alembic.
revision = "0005_chat_core"
down_revision = "0004_users_birthdate"
branch_labels = None
depends_on = None


def _utc_datetime() -> sa.types.TypeEngine:
    """UTC 時間：SQL Server 使用 DATETIME2(6) 以保留微秒精度，其餘為一般 DateTime。"""
    return sa.DateTime().with_variant(mssql.DATETIME2(precision=6), "mssql")


def upgrade() -> None:
    op.create_table(
        "chat_conversations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.Unicode(length=36), nullable=False),
        sa.Column("title", sa.Unicode(length=100), nullable=False),
        sa.Column("summary", sa.UnicodeText(), nullable=True),
        sa.Column("summary_upto_seq", sa.Integer(), nullable=False),
        sa.Column("state_json", sa.JSON(), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("processing_since", _utc_datetime(), nullable=True),
        sa.Column("public_id", sa.Unicode(length=36), nullable=False),
        sa.Column("created_at", _utc_datetime(), nullable=False),
        sa.Column("updated_at", _utc_datetime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_chat_conversations"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_chat_conversations_user_id_users", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("public_id", name="uq_chat_conversations_public_id"),
        sa.UniqueConstraint("user_id", "client_id", name="uq_chat_conversations_user_client"),
    )
    op.create_index(
        "ix_chat_conversations_user_id_updated_at", "chat_conversations", ["user_id", "updated_at"], unique=False
    )

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("client_msg_id", sa.Unicode(length=36), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.Unicode(length=10), nullable=False),
        sa.Column("content", sa.UnicodeText(), nullable=False),
        sa.Column("intent", sa.Unicode(length=50), nullable=True),
        sa.Column("entities_json", sa.JSON(), nullable=True),
        sa.Column("suggested_actions_json", sa.JSON(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("msg_status", sa.Unicode(length=10), nullable=False),
        sa.Column("created_at", _utc_datetime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_chat_messages"),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["chat_conversations.id"],
            name="fk_chat_messages_conversation_id_chat_conversations", ondelete="CASCADE",
        ),
        sa.UniqueConstraint("conversation_id", "seq", name="uq_chat_messages_conversation_seq"),
        sa.UniqueConstraint("conversation_id", "client_msg_id", name="uq_chat_messages_conversation_client_msg"),
    )
    op.create_index("ix_chat_messages_intent", "chat_messages", ["intent"], unique=False)

    op.create_table(
        "llm_call_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.Unicode(length=10), nullable=False),
        sa.Column("provider", sa.Unicode(length=20), nullable=False),
        sa.Column("model", sa.Unicode(length=64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.Unicode(length=20), nullable=False),
        sa.Column("counted", sa.Boolean(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("thinking_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("finish_reason", sa.Unicode(length=20), nullable=True),
        sa.Column("error_detail", sa.UnicodeText(), nullable=True),
        sa.Column("created_at", _utc_datetime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_llm_call_logs"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_llm_call_logs_user_id_users", ondelete="SET NULL"),
    )
    op.create_index("ix_llm_call_logs_user_id_created_at", "llm_call_logs", ["user_id", "created_at"], unique=False)
    op.create_index(
        "ix_llm_call_logs_model_status_created_at", "llm_call_logs", ["model", "status", "created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_table("llm_call_logs")
    op.drop_table("chat_messages")
    op.drop_table("chat_conversations")
