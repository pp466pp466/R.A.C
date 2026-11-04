"""模型呼叫的用量帳本（見 llm-structured-calls 規格）。

每一次對模型供應商的呼叫一筆，同時是額度計算、成本稽核與失敗紀錄的來源。**不保存對話全文**；
唯一的例外是 `error_detail`：輸出驗證失敗時的模型原文，截斷至 2000 字、保存 14 天、僅管理員可讀（AGENTS.md 第 5 節）。

- `user_id`：`ON DELETE SET NULL`，使用者被刪除時紀錄保留但匿名化。
- `conversation_id`：**刻意不設外鍵**。SQL Server 不允許經由多條路徑的串聯動作：
  `users → chat_conversations`（CASCADE）加上 `users → llm_call_logs`（SET NULL）加上
  `chat_conversations → llm_call_logs`（SET NULL），會讓 `users` 到 `llm_call_logs` 有兩條動作路徑，建表被拒絕（錯誤 1785）。
  帳本本來就要比對話活得久（額度），所以只存內部編號。對話被刪除時由 Repository 在同一交易把編號清為空值；
  帳號刪除（C-4）的流程須先做同樣的清除，再刪使用者，否則 SQLite 重用編號時會錯指別人的新對話。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, Unicode, UnicodeText
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime, utcnow

KIND_CHAT = "chat"
KIND_SUMMARY = "summary"
KIND_SCRIPT_REVIEW = "script_review"       # 劇本審查的模型層（RPG 用途的模型；script-review 規格）
CALL_KINDS = (KIND_CHAT, KIND_SUMMARY, KIND_SCRIPT_REVIEW)

STATUS_OK = "ok"
STATUS_VALIDATION_FAILED = "validation_failed"
STATUS_RATE_LIMITED = "rate_limited"
STATUS_SERVER_ERROR = "server_error"
STATUS_TIMEOUT = "timeout"
STATUS_NETWORK = "network"
STATUS_BLOCKED = "blocked"
STATUS_CONFIG_ERROR = "config_error"
CALL_STATUSES = (
    STATUS_OK, STATUS_VALIDATION_FAILED, STATUS_RATE_LIMITED, STATUS_SERVER_ERROR,
    STATUS_TIMEOUT, STATUS_NETWORK, STATUS_BLOCKED, STATUS_CONFIG_ERROR,
)
# 這兩種結果代表「主要模型目前不健康」，會讓斷路暫時跳過它（速率限制回得快，沒有人在等，所以不算）
BREAKER_STATUSES = (STATUS_TIMEOUT, STATUS_SERVER_ERROR)


class LlmCallLog(Base):
    __tablename__ = "llm_call_logs"
    __table_args__ = (
        Index("ix_llm_call_logs_user_id_created_at", "user_id", "created_at"),      # 每位使用者的用量與額度
        Index("ix_llm_call_logs_model_status_created_at", "model", "status", "created_at"),   # 斷路查詢
        Index("ix_llm_call_logs_created_at", "created_at"),                                    # 全站每日用量的彙總（C-4a）
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    conversation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(Unicode(20))      # 0013 由 10 加寬為 20：`script_review` 有 13 個字（SQL Server 會截斷而報錯，SQLite 不會）
    provider: Mapped[str] = mapped_column(Unicode(20))
    model: Mapped[str] = mapped_column(Unicode(64))
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Unicode(20))
    counted: Mapped[bool] = mapped_column(Boolean, default=False)          # 這則訊息得到正常的 AI 回覆時，最後成功的對話呼叫為真
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thinking_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    finish_reason: Mapped[str | None] = mapped_column(Unicode(20), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(UnicodeText, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
