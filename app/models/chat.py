"""客服對話與訊息（見 cs-conversations 與 cs-conversation-memory 規格、AGENTS.md 第 4、6、10 節）。

- 對外只用 `public_id`（UUID）；內部整數編號不外露，授權也不依賴它的不可猜測性。
- 兩張表都以串聯刪除隨使用者與對話一起消失：使用者刪除對話或帳號被刪除時，內容就是真的刪除，不留軟刪除的殘影。
- `seq` 由後端指派、在同一對話內單調遞增；`(conversation_id, seq)` 與 `(conversation_id, client_msg_id)` 唯一，
  使「同一則訊息只寫一次」與「一則使用者訊息至多一則助理回覆」由資料庫保證，而不是只靠程式邏輯。
- JSON 欄位（`state_json`、`entities_json`、`suggested_actions_json`）由 Repository 序列化；**不得依其內容做篩選查詢**
  （SQLite 與 SQL Server 的 JSON 查詢語法不同）。需要統計的 `intent` 因此獨立成欄位並建索引。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Float, ForeignKey, Index, Integer, Unicode, UnicodeText, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PublicIdMixin, TimestampMixin, UTCDateTime, utcnow

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
MESSAGE_ROLES = (ROLE_USER, ROLE_ASSISTANT)

MSG_STATUS_OK = "ok"
MSG_STATUS_FALLBACK = "fallback"      # 降級回覆：標記後使歷史連貫，但不計入額度、不進入模型的上下文
MESSAGE_STATUSES = (MSG_STATUS_OK, MSG_STATUS_FALLBACK)

DEFAULT_CONVERSATION_TITLE = "新對話"


class ChatConversation(Base, PublicIdMixin, TimestampMixin):
    __tablename__ = "chat_conversations"
    __table_args__ = (
        # 建立對話的冪等：前端為每個新對話產生一個識別碼，逾時重送時回傳同一個對話
        UniqueConstraint("user_id", "client_id", name="uq_chat_conversations_user_client"),
        # 「我的對話」依最後活動時間由新到舊
        Index("ix_chat_conversations_user_id_updated_at", "user_id", "updated_at"),
        # 期限清除（data-retention）：全表依最後活動時間找出 12 個月前的對話，上一個索引以 user_id 開頭，用不上
        Index("ix_chat_conversations_updated_at", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    client_id: Mapped[str] = mapped_column(Unicode(36))
    title: Mapped[str] = mapped_column(Unicode(100), default=DEFAULT_CONVERSATION_TITLE)
    summary: Mapped[str | None] = mapped_column(UnicodeText, nullable=True)       # 累積摘要
    summary_upto_seq: Mapped[int] = mapped_column(Integer, default=0)             # 摘要已涵蓋到哪一則訊息（補償式摘要的標記）
    state_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)        # 客服「當前狀態」，只含列舉值組成的字串
    state_version: Mapped[int] = mapped_column(Integer, default=1)               # 狀態與標題的版本號，寫入時比對
    processing_since: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)  # 同一對話處理中的標記（60 秒過期）


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "seq", name="uq_chat_messages_conversation_seq"),
        UniqueConstraint("conversation_id", "client_msg_id", name="uq_chat_messages_conversation_client_msg"),
        # 每分鐘訊息數限制：「最近 60 秒、某位使用者」的訊息，先以時間範圍縮小再連到對話（C-4a）
        Index("ix_chat_messages_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("chat_conversations.id", ondelete="CASCADE"))
    client_msg_id: Mapped[str] = mapped_column(Unicode(36))       # 使用者訊息：前端的 UUID；助理訊息：由它決定性地推得
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(Unicode(10))
    content: Mapped[str] = mapped_column(UnicodeText)
    intent: Mapped[str | None] = mapped_column(Unicode(50), nullable=True, index=True)   # 只有助理訊息；供統計
    entities_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    suggested_actions_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    msg_status: Mapped[str] = mapped_column(Unicode(10), default=MSG_STATUS_OK)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
