"""期限清除專用的資料存取（見 data-retention 規格與 privacy-lifecycle 的 design D10）。

只有 `DataRetentionService` 使用（`tests/architecture/test_retention_boundaries.py` 守住）。每一類資料三個方法：
- `find_<類別>(cutoff, limit)`：找出已過期的主鍵（由小到大，最多 `limit` 個）。只回編號，不載入任何內容；
- `count_<類別>(cutoff)`：預覽用，只計數；
- `purge_<類別>(ids)`：處理這一批（刪除，或失敗原文為清成空值），回傳受影響的筆數。
全部只用 ORM 表達式；批次上限由呼叫端控制（500），避開 SQL Server 約 2100 個參數的限制。
這個類別不提交也不回復交易，由 Service 決定每一批的提交點。

**刪除對話之前，先把用量帳本中指向它們的編號清為空值**（`llm_call_logs.conversation_id` 沒有外鍵，見該模型的說明）。
稽核紀錄沒有其他修改或刪除的途徑（`AdminAuditRepository` 刻意只有新增與讀取），期限屆滿的清除只在這裡。
"""
from __future__ import annotations

from datetime import datetime
from typing import Sequence

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.models.admin_audit_log import AdminAuditLog
from app.models.chat import ChatConversation
from app.models.llm_call_log import LlmCallLog
from app.models.user import AuthToken


class RetentionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    # ---- 共用 --------------------------------------------------------------------

    def _ids(self, statement, limit: int) -> list[int]:
        return list(self._session.scalars(statement.limit(limit)))

    def _count(self, statement) -> int:
        return self._session.execute(statement).scalar_one()

    def _run(self, statement) -> int:
        return self._session.execute(statement.execution_options(synchronize_session=False)).rowcount

    # ---- 對話與訊息：最後活動早於 cutoff -----------------------------------------------

    def find_conversations(self, cutoff: datetime, limit: int) -> list[int]:
        return self._ids(
            select(ChatConversation.id).where(ChatConversation.updated_at < cutoff).order_by(ChatConversation.id), limit
        )

    def count_conversations(self, cutoff: datetime) -> int:
        return self._count(select(func.count(ChatConversation.id)).where(ChatConversation.updated_at < cutoff))

    def purge_conversations(self, ids: Sequence[int]) -> int:
        """先把帳本的對話編號清為空值，再刪除對話（訊息靠資料庫的 `ON DELETE CASCADE` 一併移除）。"""
        if not ids:
            return 0
        self._session.flush()
        self._run(update(LlmCallLog).where(LlmCallLog.conversation_id.in_(ids)).values(conversation_id=None))
        return self._run(delete(ChatConversation).where(ChatConversation.id.in_(ids)))

    # ---- 用量帳本：建立早於 cutoff --------------------------------------------------------

    def find_ledger(self, cutoff: datetime, limit: int) -> list[int]:
        return self._ids(select(LlmCallLog.id).where(LlmCallLog.created_at < cutoff).order_by(LlmCallLog.id), limit)

    def count_ledger(self, cutoff: datetime) -> int:
        return self._count(select(func.count(LlmCallLog.id)).where(LlmCallLog.created_at < cutoff))

    def purge_ledger(self, ids: Sequence[int]) -> int:
        if not ids:
            return 0
        return self._run(delete(LlmCallLog).where(LlmCallLog.id.in_(ids)))

    # ---- 失敗的模型原文：建立早於 cutoff，清為空值，帳本那一筆保留 ----------------------------------

    def find_error_detail(self, cutoff: datetime, limit: int) -> list[int]:
        return self._ids(
            select(LlmCallLog.id)
            .where(LlmCallLog.error_detail.is_not(None), LlmCallLog.created_at < cutoff)
            .order_by(LlmCallLog.id),
            limit,
        )

    def count_error_detail(self, cutoff: datetime) -> int:
        return self._count(
            select(func.count(LlmCallLog.id)).where(LlmCallLog.error_detail.is_not(None), LlmCallLog.created_at < cutoff)
        )

    def purge_error_detail(self, ids: Sequence[int]) -> int:
        if not ids:
            return 0
        return self._run(update(LlmCallLog).where(LlmCallLog.id.in_(ids)).values(error_detail=None))

    # ---- 驗證權杖：到期早於 cutoff（不論是否已使用）------------------------------------------------

    def find_auth_tokens(self, cutoff: datetime, limit: int) -> list[int]:
        return self._ids(select(AuthToken.id).where(AuthToken.expires_at < cutoff).order_by(AuthToken.id), limit)

    def count_auth_tokens(self, cutoff: datetime) -> int:
        return self._count(select(func.count(AuthToken.id)).where(AuthToken.expires_at < cutoff))

    def purge_auth_tokens(self, ids: Sequence[int]) -> int:
        if not ids:
            return 0
        return self._run(delete(AuthToken).where(AuthToken.id.in_(ids)))

    # ---- 管理稽核紀錄：建立早於 cutoff -------------------------------------------------------------

    def find_admin_audit(self, cutoff: datetime, limit: int) -> list[int]:
        return self._ids(select(AdminAuditLog.id).where(AdminAuditLog.created_at < cutoff).order_by(AdminAuditLog.id), limit)

    def count_admin_audit(self, cutoff: datetime) -> int:
        return self._count(select(func.count(AdminAuditLog.id)).where(AdminAuditLog.created_at < cutoff))

    def purge_admin_audit(self, ids: Sequence[int]) -> int:
        if not ids:
            return 0
        return self._run(delete(AdminAuditLog).where(AdminAuditLog.id.in_(ids)))
