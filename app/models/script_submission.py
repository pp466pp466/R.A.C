"""劇本的送審紀錄（見 script-submission 規格與 rpg-script-authoring 的 design D2、D8）。

每次成功送審（`sent`）、被自動拒絕（`auto_rejected`）與管理員解除暫停發佈（`lifted`）記一筆，用來計算每日送審次數與
「連續被自動拒絕」；`lifted` 使連續中斷，所以解除後不會立即再被暫停。

- 只存使用者、結果、類別列舉與時間：**不含**版本識別碼、標題、內容或任何劇本文字（AGENTS.md 第 11 節：被拒絕的送審只保留類別與時間）。
  刻意不連到版本，也就不會多出 `users → script_versions → script_submissions` 的第二條串聯路徑（SQL Server 錯誤 1785）。
- 外鍵 `ON DELETE CASCADE`：帳號被抹除時紀錄一併消失（`tests/architecture/test_user_foreign_keys.py` 會檢查）。
- 保存 12 個月（data-retention 規格）；它只被「近期」的判斷使用，清除舊紀錄不影響暫停旗標與當日次數。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, JSON, Unicode
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime, utcnow

SUBMISSION_SENT = "sent"                      # 成功送審（進入待審）
SUBMISSION_AUTO_REJECTED = "auto_rejected"    # 規則層或模型層明確拒絕
SUBMISSION_LIFTED = "lifted"                  # 管理員解除暫停發佈（使連續被拒中斷）
SUBMISSION_OUTCOMES = (SUBMISSION_SENT, SUBMISSION_AUTO_REJECTED, SUBMISSION_LIFTED)
# 計入「每日送審次數」的結果（解除暫停不是一次送審）
COUNTED_OUTCOMES = (SUBMISSION_SENT, SUBMISSION_AUTO_REJECTED)


class ScriptSubmission(Base):
    __tablename__ = "script_submissions"
    __table_args__ = (Index("ix_script_submissions_user_id_created_at", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    outcome: Mapped[str] = mapped_column(Unicode(16))
    categories_json: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)      # 固定列舉；`sent` 帶有疑慮的類別、`auto_rejected` 帶拒絕的類別
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
