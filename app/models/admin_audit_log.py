"""管理員操作的稽核紀錄（見 admin-audit-log 規格與 AGENTS.md 第 10、11 節）。

「誰在何時對誰做了什麼」的唯一依據，所以刻意保守：
- `admin_user_id`：`ON DELETE SET NULL`，管理員帳號被刪除後紀錄保留。**只有這一個欄位有外鍵**：同一張表兩個指向 `users`
  且帶動作的外鍵，SQL Server 會以錯誤 1785 拒絕建表（`llm_call_logs.conversation_id` 遇過同類問題）。
- 目標只存對外識別碼（UUID，不是個資），不設外鍵：使用者或對話被刪除之後，紀錄仍然成立，這正是稽核該有的性質。
- 動作是固定的列舉（`AUDIT_ACTIONS`）；`detail_json` 只放列舉值與整數；`reason` 是管理員填寫的原因，是唯一的自由文字，
  至多 200 字，只在停權、刪除對話與調整遊戲幣時存在。**不記錄** email、顯示名稱、訊息或回覆文字、模型原文、密碼與其雜湊、權杖。
- 不得依 `detail_json` 的內容做篩選查詢（兩種資料庫的 JSON 語法不同）。
- 這個模型沒有任何更新或刪除的流程；保存期限（24 個月）屆滿後的清除由另外的排程指令負責（C-4c）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, Integer, Unicode
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime, utcnow

ACTION_USER_LOOKUP = "user.lookup"
ACTION_USER_SUSPEND = "user.suspend"
ACTION_USER_UNSUSPEND = "user.unsuspend"
ACTION_USER_CHANGE_TIER = "user.change_tier"
ACTION_CONVERSATION_READ = "conversation.read"
ACTION_CONVERSATION_DELETE = "conversation.delete"
ACTION_FAILURES_READ = "failures.read"
ACTION_REAUTH_FAILED = "reauth.failed"
# 帳號鎖定與申訴（google-first-login）：鎖定與申訴提交由系統或玩家觸發，管理員欄位為空
ACTION_ACCOUNT_LOCK = "account.lock"
ACTION_APPEAL_SUBMIT = "appeal.submit"
ACTION_APPEAL_READ = "appeal.read"
ACTION_ACCOUNT_UNLOCK = "account.unlock"
# 劇本創作（rpg-script-authoring）：部署者修改營運限制與連續被自動拒絕而暫停發佈由系統或部署者觸發（管理員欄位為空），
# 解除暫停由管理員觸發
ACTION_LIMIT_SET = "limit.set"
ACTION_PUBLISH_SUSPEND = "publish.suspend"
ACTION_PUBLISH_LIFT = "publish.lift"
# 遊戲幣（game-wallet）：管理員調整玩家的餘額；細節只有餘額種類、增減量與調整後的餘額，理由放在理由欄
ACTION_WALLET_ADJUST = "wallet.adjust"
AUDIT_ACTIONS = (
    ACTION_USER_LOOKUP, ACTION_USER_SUSPEND, ACTION_USER_UNSUSPEND, ACTION_USER_CHANGE_TIER,
    ACTION_CONVERSATION_READ, ACTION_CONVERSATION_DELETE, ACTION_FAILURES_READ, ACTION_REAUTH_FAILED,
    ACTION_ACCOUNT_LOCK, ACTION_APPEAL_SUBMIT, ACTION_APPEAL_READ, ACTION_ACCOUNT_UNLOCK,
    ACTION_LIMIT_SET, ACTION_PUBLISH_SUSPEND, ACTION_PUBLISH_LIFT, ACTION_WALLET_ADJUST,
)

MAX_REASON_CHARS = 200


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_logs"
    __table_args__ = (
        Index("ix_admin_audit_logs_created_at", "created_at"),                          # 由新到舊的檢視與日後的期限清除
        Index("ix_admin_audit_logs_admin_user_id_created_at", "admin_user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admin_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(Unicode(40))
    target_user_public_id: Mapped[str | None] = mapped_column(Unicode(36), nullable=True)
    target_conversation_public_id: Mapped[str | None] = mapped_column(Unicode(36), nullable=True)
    reason: Mapped[str | None] = mapped_column(Unicode(MAX_REASON_CHARS), nullable=True)
    detail_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
