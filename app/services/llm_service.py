"""結構化模型呼叫的編排（見 llm-structured-calls 規格與 design D3、D4）。

`LlmService.run_structured` 是所有需要 JSON 輸出的模型呼叫的唯一入口（客服對話、對話摘要，日後的 RPG）。流程：

    預算 30 秒；第 1 次呼叫用主要模型（近 60 秒主要模型曾逾時或伺服器錯誤則直接用備援）
      成功且驗證通過 ─────────────────────► 成功（對話呼叫計入額度）
      成功但驗證失敗 ─► 第 2 次：同一模型 ─► 通過則成功，否則降級
      暫時性錯誤 ─────► 第 2 次：備援模型（逾時 = 剩餘預算；第 1 次本來就是備援則降級）
      被安全過濾阻擋／設定錯誤 ────────────► 降級（不重試、不切換）

- 呼叫總數至多 2 次（AGENTS.md 第 5 節）；暫時性錯誤的切換算作那次重試。
- 預算以單調時鐘計，不受系統時間調整影響；牆上時鐘 `clock` 只用來標記帳本時間與斷路窗口。
- 斷路只看資料庫帳本，不存放任何行程內狀態，所以多個 worker 看到同一份真相，重啟也不會遺失。
- 每次呼叫一筆帳本，**不含對話全文**；唯一例外是驗證失敗的模型原文（截斷至 2000 字，存於 `error_detail`）。
- **呼叫模型時不持有資料庫交易**：每次呼叫前呼叫 `release`（提交目前的交易），因此最長 30 秒的等待不佔用連線與鎖。
  失敗的呼叫紀錄立刻提交（它們是已發生的事實，也讓其他 worker 的斷路盡快生效）；**最後一筆**只寫入、不提交，
  交給呼叫端與回覆訊息在同一個交易提交（成功的對話呼叫的 `counted` 因此不會在訊息沒存下時就被算成一輪）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Sequence

from app.config import LlmSettings
from app.llm.types import (
    BLOCKED_INPUT, FINISH_PROMPT_BLOCKED, FINISH_SAFETY, LLMBlockedError, LLMConfigError, LLMContent, LLMProvider,
    LLMRequest, LLMResponse, LLMTransientError, LLMValidationError,
)
from app.models.llm_call_log import (
    BREAKER_STATUSES, KIND_CHAT, STATUS_BLOCKED, STATUS_CONFIG_ERROR, STATUS_OK, STATUS_VALIDATION_FAILED,
)
from app.observability.llm_events import log_breaker_opened, log_call
from app.repositories.llm_call_log_repository import LlmCallLogRepository
from app.services.llm_output import SemanticRule, parse_llm_json

# 剩餘預算少於這個秒數就不再發出呼叫：來不及完成一次有意義的呼叫，只會讓使用者多等
MIN_CALL_SECONDS = 2.0
MAX_CALLS = 2

# 降級的原因（呼叫端依此決定降級文案的語氣與是否計入安全過濾的次數）
DEGRADED_VALIDATION = "validation"
DEGRADED_TRANSIENT = "transient"
DEGRADED_BLOCKED = "blocked"
DEGRADED_CONFIG = "config"
DEGRADED_BUDGET = "budget"


def _category(name: str, detail: object | None) -> str:
    """失敗的帳本細節：只有分類與狀態碼（或被擋的階段），絕不含供應商的回應本文。"""
    return name if detail is None else f"{name} {detail}"


@dataclass(frozen=True)
class StructuredOutcome:
    """一次 `run_structured` 的結果。成功時 `value` 是通過驗證的解析結果；降級時 `value` 為 None 並有 `degraded` 原因。"""

    value: Any | None
    degraded: str | None
    model: str | None                 # 最後一次呼叫實際使用的模型（沒有發出任何呼叫時為 None）
    attempts: int                     # 實際發出的呼叫次數
    blocked_phase: str | None = None  # 因被阻擋而降級時：`input`（使用者的輸入被擋）或 `output`（模型的輸出被擋）

    @property
    def ok(self) -> bool:
        return self.degraded is None


class LlmService:
    def __init__(
        self,
        *,
        provider: LLMProvider,
        ledger: LlmCallLogRepository,
        settings: LlmSettings,
        clock: Callable[[], datetime],
        release: Callable[[], None],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._provider = provider
        self._ledger = ledger
        self._settings = settings
        self._clock = clock
        self._release = release
        self._monotonic = monotonic

    def run_structured(
        self,
        *,
        kind: str,
        user_id: int | None,
        conversation_id: int | None,
        system_instruction: str,
        contents: Sequence[LLMContent],
        schema: dict[str, Any],
        rules: Sequence[SemanticRule] = (),
        model: str | None = None,
    ) -> StructuredOutcome:
        """依流程呼叫模型並驗證輸出。`model` 指定這個用途的「首選模型」（預設為主要模型；摘要傳入摘要模型）。"""
        started = self._monotonic()
        settings = self._settings
        fallback = settings.fallback_model
        current = model or settings.primary_model
        if current != fallback and self._breaker_open(current):
            current = fallback
        chat = kind == KIND_CHAT
        used: str | None = None
        attempts = 0

        while attempts < MAX_CALLS:
            remaining = settings.request_budget_seconds - (self._monotonic() - started)
            if remaining < MIN_CALL_SECONDS:
                return StructuredOutcome(None, DEGRADED_BUDGET, used, attempts)
            # 備援模型使用剩餘的全部預算；其餘模型受單次呼叫逾時限制
            timeout = remaining if current == fallback else min(settings.call_timeout_seconds, remaining)
            request = LLMRequest(
                model=current, system_instruction=system_instruction, contents=tuple(contents), response_schema=schema,
                timeout_seconds=timeout, max_output_tokens=settings.max_output_tokens, thinking_budget=settings.thinking_budget,
            )
            attempts += 1
            used = current
            more_allowed = attempts < MAX_CALLS
            self._release()                                   # 呼叫期間不持有交易
            try:
                response = self._provider.generate(request)
            except LLMTransientError as error:
                switch = more_allowed and current != fallback
                self._record(
                    user_id, conversation_id, kind, current, attempts, error.kind, error.latency_ms, final=not switch,
                    error_detail=_category(error.kind, error.http_status), error_type=type(error).__name__,
                    provider_status=error.http_status,
                )
                if current != fallback and error.kind in BREAKER_STATUSES:
                    # 斷路在這次失敗之後生效：就在這裡記一筆（窗口內被跳過的請求走不到這裡，所以自然不會重複）
                    log_breaker_opened(model=current, outcome=error.kind)
                if not switch:
                    return StructuredOutcome(None, DEGRADED_TRANSIENT, used, attempts)
                current = fallback
                continue
            except LLMBlockedError as error:
                finish = FINISH_PROMPT_BLOCKED if error.phase == BLOCKED_INPUT else FINISH_SAFETY
                self._record(
                    user_id, conversation_id, kind, current, attempts, STATUS_BLOCKED, error.latency_ms, final=True,
                    finish_reason=finish, error_detail=_category(STATUS_BLOCKED, error.phase),
                    error_type=type(error).__name__,
                )
                return StructuredOutcome(None, DEGRADED_BLOCKED, used, attempts, blocked_phase=error.phase)
            except LLMConfigError as error:
                self._record(
                    user_id, conversation_id, kind, current, attempts, STATUS_CONFIG_ERROR, error.latency_ms, final=True,
                    error_detail=_category(STATUS_CONFIG_ERROR, error.http_status), error_type=type(error).__name__,
                    provider_status=error.http_status,
                )
                return StructuredOutcome(None, DEGRADED_CONFIG, used, attempts)

            try:
                value = parse_llm_json(response.text, schema, rules)
            except LLMValidationError as error:
                self._record(
                    user_id, conversation_id, kind, current, attempts, STATUS_VALIDATION_FAILED, response.latency_ms,
                    final=not more_allowed, response=response, error_detail=response.text,
                    error_type=type(error).__name__, reason=error.reason,
                )
                if not more_allowed:
                    return StructuredOutcome(None, DEGRADED_VALIDATION, used, attempts)
                continue                                      # 同一模型再試一次
            self._record(
                user_id, conversation_id, kind, current, attempts, STATUS_OK, response.latency_ms, final=True,
                response=response, counted=chat,
            )
            return StructuredOutcome(value, None, used, attempts)

        return StructuredOutcome(None, DEGRADED_BUDGET, used, attempts)       # 不可達：迴圈內每個分支都已回傳

    # ---- 內部 -------------------------------------------------------------------------

    def _breaker_open(self, model: str) -> bool:
        since = self._clock() - timedelta(seconds=self._settings.breaker_seconds)
        return self._ledger.model_unhealthy_since(model, since)

    def _record(
        self,
        user_id: int | None,
        conversation_id: int | None,
        kind: str,
        model: str,
        attempt: int,
        status: str,
        latency_ms: int,
        *,
        final: bool,
        response: LLMResponse | None = None,
        finish_reason: str | None = None,
        counted: bool = False,
        error_detail: str | None = None,
        error_type: str | None = None,
        reason: str | None = None,
        provider_status: int | None = None,
    ) -> None:
        log_call(
            user_id=user_id, conversation_id=conversation_id, kind=kind, provider=self._provider.name, model=model,
            attempt=attempt, outcome=status, latency_ms=latency_ms,
            prompt_tokens=response.prompt_tokens if response else None,
            completion_tokens=response.completion_tokens if response else None,
            thinking_tokens=response.thinking_tokens if response else None,
            finish_reason=response.finish_reason if response else finish_reason,
            error_type=error_type, reason=reason, provider_status=provider_status,
        )
        self._ledger.add(
            user_id=user_id, conversation_id=conversation_id, kind=kind, provider=self._provider.name, model=model,
            attempt=attempt, status=status, counted=counted, latency_ms=latency_ms, created_at=self._clock(),
            prompt_tokens=response.prompt_tokens if response else None,
            completion_tokens=response.completion_tokens if response else None,
            thinking_tokens=response.thinking_tokens if response else None,
            finish_reason=response.finish_reason if response else finish_reason,
            error_detail=error_detail,
        )
        if not final:
            self._release()          # 後面還有呼叫：這筆紀錄是已發生的事實，立刻提交，讓其他 worker 的斷路盡快生效
