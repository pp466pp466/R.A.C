"""模型呼叫用量帳本的資料存取（見 llm-structured-calls 規格）。

帳本不屬於「某位使用者能讀的資料」：API 不提供讀取它的路徑（管理員的檢視由 C-4b 以專用方法提供），
所以這裡的方法不像 `ChatRepository` 那樣要求擁有者參數。這個類別負責寫入、「主要模型近期是否異常」的斷路查詢，
以及額度與全站用量的彙總（單一使用者的彙總仍帶 `user_id` 條件；全站彙總沒有）。
`error_detail` 在寫入時一律截斷至 2000 字（AGENTS.md 第 5 節），不依賴呼叫端記得截斷。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select, true
from sqlalchemy.orm import Session

from app.models.llm_call_log import BREAKER_STATUSES, KIND_CHAT, STATUS_OK, STATUS_VALIDATION_FAILED, STATUS_BLOCKED, LlmCallLog

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

    # ---- 用量彙總（只讀；日界由呼叫端以 Asia/Taipei 算好後傳入，起含訖不含）-------------------------

    def count_counted_chat_calls(self, user_id: int, *, start: datetime, end: datetime) -> int:
        """這位使用者在期間內「得到正常 AI 回覆」的輪數：標記為計入額度的對話呼叫（降級與摘要不算）。"""
        return self._session.scalar(
            select(func.count(LlmCallLog.id)).where(
                LlmCallLog.user_id == user_id,
                LlmCallLog.kind == KIND_CHAT,
                LlmCallLog.counted == true(),                  # 不用 `.is_(True)`：SQL Server 會產生 `IS 1` 而被拒絕
                LlmCallLog.created_at >= start,
                LlmCallLog.created_at < end,
            )
        ) or 0

    def blocked_chat_calls(self, user_id: int, *, start: datetime, end: datetime) -> tuple[int, datetime | None]:
        """這位使用者在期間內，對話呼叫被內容安全過濾阻擋的（次數, 最近一次的時間）。摘要呼叫被擋不算。"""
        count, latest = self._session.execute(
            select(func.count(LlmCallLog.id), func.max(LlmCallLog.created_at)).where(
                LlmCallLog.user_id == user_id,
                LlmCallLog.kind == KIND_CHAT,
                LlmCallLog.status == STATUS_BLOCKED,
                LlmCallLog.created_at >= start,
                LlmCallLog.created_at < end,
            )
        ).one()
        if latest is not None and latest.tzinfo is None:       # 聚合結果不經過 UTCDateTime 的轉換，補回 UTC
            latest = latest.replace(tzinfo=timezone.utc)
        return count or 0, latest

    def usage_totals(self, *, start: datetime, end: datetime) -> tuple[int, int]:
        """全站在期間內的（token 總數, 呼叫次數）。

        這是全站的彙總，不屬於任何一位使用者，所以沒有 `user_id` 參數。包含摘要、降級與失敗的嘗試，因為供應商都已
        計入配額；供應商沒有回報的 token 以 0 計，但仍算一次呼叫。
        """
        tokens = (
            func.coalesce(LlmCallLog.prompt_tokens, 0)
            + func.coalesce(LlmCallLog.completion_tokens, 0)
            + func.coalesce(LlmCallLog.thinking_tokens, 0)
        )
        total, calls = self._session.execute(
            select(func.sum(tokens), func.count(LlmCallLog.id)).where(
                LlmCallLog.created_at >= start, LlmCallLog.created_at < end
            )
        ).one()
        return int(total or 0), int(calls or 0)

    # ---- 營運告警的唯讀彙總（ops-alerting；全站，沒有 `user_id`；窗口起含訖不含）-------------------

    def chat_status_counts(self, *, start: datetime, end: datetime) -> dict[str, int]:
        """窗口內對話呼叫（`kind = chat`）依結果狀態的筆數（`GROUP BY status`）。"""
        rows = self._session.execute(
            select(LlmCallLog.status, func.count(LlmCallLog.id))
            .where(LlmCallLog.kind == KIND_CHAT, LlmCallLog.created_at >= start, LlmCallLog.created_at < end)
            .group_by(LlmCallLog.status)
        ).all()
        return {status: int(count) for status, count in rows}

    def status_count(self, status: str, *, start: datetime, end: datetime) -> int:
        """窗口內某個結果狀態的筆數（不分用途：設定錯誤不論是對話或摘要都要知道）。"""
        return self._session.execute(
            select(func.count(LlmCallLog.id)).where(
                LlmCallLog.status == status, LlmCallLog.created_at >= start, LlmCallLog.created_at < end
            )
        ).scalar_one()

    @staticmethod
    def _completed_chat_calls(start: datetime, end: datetime):
        """「完成的對話呼叫」：結果為成功或輸出驗證失敗（供應商有回應、延遲有意義；逾時與錯誤另算在錯誤率）。"""
        return (
            LlmCallLog.kind == KIND_CHAT,
            LlmCallLog.status.in_((STATUS_OK, STATUS_VALIDATION_FAILED)),
            LlmCallLog.created_at >= start,
            LlmCallLog.created_at < end,
        )

    def completed_chat_call_count(self, *, start: datetime, end: datetime) -> int:
        return self._session.execute(
            select(func.count(LlmCallLog.id)).where(*self._completed_chat_calls(start, end))
        ).scalar_one()

    def completed_chat_latency_at(self, offset: int, *, start: datetime, end: datetime) -> int | None:
        """窗口內完成的對話呼叫，依延遲由小到大排序後第 `offset`（由 0 起算）筆的延遲毫秒；只讀一列。

        百分位數不用資料庫的函式（SQLite 沒有）：呼叫端先取筆數，再以最近排名法算出位置（`alert_rules.percentile_offset`）。
        """
        return self._session.scalar(
            select(LlmCallLog.latency_ms)
            .where(*self._completed_chat_calls(start, end))
            .order_by(LlmCallLog.latency_ms.asc(), LlmCallLog.id.asc())
            .offset(offset)
            .limit(1)
        )

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
