"""用量限制（usage-limits）：為每分鐘訊息數與全站每日用量的彙總新增兩個索引。

只新增索引（expand），不新增欄位或資料表、不改動任何資料：
- `llm_call_logs.created_at`：全站每日 token 與呼叫次數的彙總是「某一天內的全部列」，既有的兩個索引第一欄都不是時間；
- `chat_messages.created_at`：每分鐘訊息數是「最近 60 秒內某位使用者的訊息」，以時間範圍縮小後再連到對話。
舊版程式不認識它們也不受影響（只是多了索引）；新版程式在沒有索引時結果也正確，只是較慢，所以先升級資料庫再部署新版
程式、或反過來，都是安全的。回復（downgrade）只移除這兩個索引，不影響任何資料。

這個檔案不匯入應用程式的程式碼：migration 描述的是「當時」的結構，不應隨應用程式修改而改變。

Revision ID: 0006_usage_indexes
Revises: 0005_chat_core
Create Date: 2026-10-29
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0006_usage_indexes"
down_revision = "0005_chat_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_llm_call_logs_created_at", "llm_call_logs", ["created_at"], unique=False)
    op.create_index("ix_chat_messages_created_at", "chat_messages", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_chat_messages_created_at", table_name="chat_messages")
    op.drop_index("ix_llm_call_logs_created_at", table_name="llm_call_logs")
