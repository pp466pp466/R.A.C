"""模型呼叫用量帳本的資料存取（見 llm-structured-calls 規格）。

帳本不屬於「某位使用者能讀的資料」：API 不提供讀取它的路徑（管理員與額度彙總由 C-4 以專用方法提供），
所以這裡的方法不像 `ChatRepository` 那樣要求擁有者參數。這個類別只負責寫入與「主要模型近期是否異常」的斷路查詢。
`error_detail` 在寫入時一律截斷至 2000 字（AGENTS.md 第 5 節），不依賴呼叫端記得截斷。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.llm_call_log import BREAKER_STATUSES, LlmCallLog

ERROR_DETAIL_LIMIT = 2000


class LlmCallLogRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self,
        *,
        user_id: int | None,
        conversation_id: int | None,
        kind: str,
        provider: str,
        model: str,
        attempt: int,
        status: str,
        counted: bool,
        latency_ms: int,
        created_at: datetime,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        thinking_tokens: int | None = None,
        finish_reason: str | None = None,
        error_detail: str | None = None,
    ) -> LlmCallLog:
        row = LlmCallLog(
            user_id=user_id, conversation_id=conversation_id, kind=kind, provider=provider, model=model, attempt=attempt,
            status=status, counted=counted, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            thinking_tokens=thinking_tokens, latency_ms=latency_ms, finish_reason=finish_reason,
            error_detail=None if error_detail is None else error_detail[:ERROR_DETAIL_LIMIT], created_at=created_at,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def model_unhealthy_since(self, model: str, since: datetime) -> bool:
        """這個模型自 `since` 起是否有逾時或伺服器錯誤的紀錄（斷路用）。

        速率限制不算：它回得很快、沒有人在等。查詢只看資料庫，所以多個 worker 看到同一份真相，也不怕重啟。
        """
        found = self._session.scalar(
            select(LlmCallLog.id)
            .where(LlmCallLog.model == model, LlmCallLog.status.in_(BREAKER_STATUSES), LlmCallLog.created_at >= since)
            .limit(1)
        )
        return found is not None
