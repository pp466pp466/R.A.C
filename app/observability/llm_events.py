"""模型呼叫的結構化日誌（見 service-observability 規格與 AGENTS.md 第 11 節）。

每次呼叫一筆，欄位限於白名單內的「分類與數字」：用途、供應商、模型名稱、嘗試次數、結果、token、延遲、結束原因、
錯誤類型與供應商的狀態碼。**絕不含**使用者訊息、助理回覆、摘要、模型原始輸出與金鑰——這裡的函式根本沒有接收這些的參數，
所以不是「記得不要放」，而是「放不進來」。驗證失敗的原文只存在用量帳本的 `error_detail`（保存 14 天、僅管理員可讀）。

層級：成功與被安全過濾阻擋為資訊；驗證失敗與暫時性錯誤為警告；供應商回應設定錯誤（請求不合法、無權限）為錯誤，
因為需要人工處理。另有 `llm.breaker_opened`：主要模型因逾時或伺服器錯誤而將被暫時跳過時，在記錄該次失敗的當下記一筆警告
（窗口內被跳過的請求不再記錄，所以不需要任何額外的狀態來去重）。
"""
from __future__ import annotations

import logging

from app.models.llm_call_log import STATUS_BLOCKED, STATUS_CONFIG_ERROR, STATUS_OK

logger = logging.getLogger("app.llm")

EVENT_CALL = "llm.call"
EVENT_BREAKER_OPENED = "llm.breaker_opened"
EVENT_REPLY = "cs.reply"


def _level_for(outcome: str) -> int:
    if outcome == STATUS_CONFIG_ERROR:
        return logging.ERROR
    if outcome in (STATUS_OK, STATUS_BLOCKED):
        return logging.INFO
    return logging.WARNING


def log_call(
    *,
    user_id: int | None,
    conversation_id: int | None,
    kind: str,
    provider: str,
    model: str,
    attempt: int,
    outcome: str,
    latency_ms: int,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    thinking_tokens: int | None = None,
    finish_reason: str | None = None,
    error_type: str | None = None,
    reason: str | None = None,
    provider_status: int | None = None,
) -> None:
    """記錄一次模型呼叫。`reason` 只用於驗證失敗，是解析器產生的分類（不含模型文字）。"""
    logger.log(
        _level_for(outcome),
        "模型呼叫",
        extra={
            "event": EVENT_CALL, "user_id": user_id, "conversation_id": conversation_id, "kind": kind, "provider": provider,
            "model": model, "attempt": attempt, "outcome": outcome, "latency_ms": latency_ms,
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "thinking_tokens": thinking_tokens,
            "finish_reason": finish_reason, "error_type": error_type, "reason": reason, "provider_status": provider_status,
        },
    )


def log_reply(*, user_id: int | None, conversation_id: int | None, intent: str | None, outcome: str) -> None:
    """一則客服回覆完成：意圖（固定列舉）與結果（`ok` 或 `fallback`）。沒有任何對話文字。"""
    logger.info(
        "客服回覆",
        extra={"event": EVENT_REPLY, "user_id": user_id, "conversation_id": conversation_id, "intent": intent, "outcome": outcome},
    )


def log_breaker_opened(*, model: str, outcome: str) -> None:
    """主要模型逾時或伺服器錯誤：接下來的窗口內它會被暫時跳過。只含模型名稱與分類。"""
    logger.warning("主要模型異常，暫時改用備援模型", extra={"event": EVENT_BREAKER_OPENED, "model": model, "outcome": outcome})
