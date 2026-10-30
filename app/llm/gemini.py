"""Gemini 轉接器（見 llm-provider 規格）。

只用標準函式庫 `urllib` 呼叫 REST（與 Google 登入一致，不引入 SDK）。所有 Gemini 專屬的東西都只在這個檔案：
請求格式、金鑰標頭、結構描述的轉換、安全過濾與狀態碼的對應。業務程式碼只看得到 `app.llm.types` 的中立型別。

- 金鑰放在 `x-goog-api-key` 標頭，**不在網址**（網址常被代理與日誌記下）。
- 失敗只回報分類與延遲，**不保留供應商的回應本文**：本文可能含請求內容的回聲，也不該進日誌或帳本。
- 安全設定使用供應商預設值：不自建關鍵字過濾（AGENTS.md 第 10 節）。
- 結構描述只送 Gemini 支援的鍵；長度與格式一律由呼叫端驗證，不假設供應商保證它們。
- 傳輸層可注入（`HttpTransport`，與其他轉接器共用，見 `transport.py`），測試不必連網。
"""
from __future__ import annotations

import time
from typing import Any, Callable

from app.llm.transport import HttpTransport, UrllibTransport  # noqa: F401  (UrllibTransport 供既有的匯入路徑使用)
from app.llm.types import (
    BLOCKED_INPUT, BLOCKED_OUTPUT, FINISH_LENGTH, FINISH_OTHER, FINISH_STOP, TRANSIENT_NETWORK, TRANSIENT_RATE_LIMITED,
    TRANSIENT_SERVER_ERROR, TRANSIENT_TIMEOUT, LLMBlockedError, LLMConfigError, LLMRequest, LLMResponse, LLMTransientError,
)

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
KEY_HEADER = "x-goog-api-key"

# Gemini 結構描述支援的鍵；其餘（例如 maxLength、additionalProperties）一律丟掉
_SCHEMA_KEYS = ("enum", "required", "nullable", "minItems", "maxItems", "description", "minimum", "maximum")
# 輸出被擋下的結束原因（有內容也不能用）
_BLOCKED_FINISH = {"SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY"}


def to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """中立的 JSON Schema 子集 → Gemini 的結構描述（型別名稱大寫，只保留支援的鍵）。"""
    converted: dict[str, Any] = {}
    schema_type = schema.get("type")
    if isinstance(schema_type, list):                       # ["string", "null"] → STRING + nullable
        non_null = [t for t in schema_type if t != "null"]
        converted["nullable"] = "null" in schema_type
        schema_type = non_null[0] if non_null else "string"
    if schema_type:
        converted["type"] = str(schema_type).upper()
    for key in _SCHEMA_KEYS:
        if key in schema:
            converted[key] = schema[key]
    if "properties" in schema:
        converted["properties"] = {name: to_gemini_schema(sub) for name, sub in schema["properties"].items()}
    if "items" in schema:
        converted["items"] = to_gemini_schema(schema["items"])
    return converted


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: HttpTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not api_key:
            raise ValueError("Gemini API 金鑰不可為空")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._transport = transport or UrllibTransport()
        self._monotonic = monotonic

    def __repr__(self) -> str:                              # 絕不顯示金鑰
        return "GeminiProvider(api_key='***')"

    # ---- 請求 ---------------------------------------------------------------------

    def build_body(self, request: LLMRequest) -> dict[str, Any]:
        config: dict[str, Any] = {"maxOutputTokens": request.max_output_tokens}
        if request.response_schema is not None:
            config["responseMimeType"] = "application/json"
            config["responseSchema"] = to_gemini_schema(request.response_schema)
        if request.thinking_budget is not None:
            config["thinkingConfig"] = {"thinkingBudget": request.thinking_budget}
        return {
            "system_instruction": {"parts": [{"text": request.system_instruction}]},
            "contents": [{"role": c.role, "parts": [{"text": part} for part in c.parts]} for c in request.contents],
            "generationConfig": config,
        }

    def generate(self, request: LLMRequest) -> LLMResponse:
        url = f"{self._base_url}/models/{request.model}:generateContent"
        headers = {"Content-Type": "application/json; charset=utf-8", KEY_HEADER: self._api_key}
        started = self._monotonic()

        def elapsed_ms() -> int:
            return int((self._monotonic() - started) * 1000)

        try:
            status, payload = self._transport.post_json(url, headers, self.build_body(request), request.timeout_seconds)
        except TimeoutError:
            raise LLMTransientError(TRANSIENT_TIMEOUT, latency_ms=elapsed_ms()) from None
        except OSError:
            raise LLMTransientError(TRANSIENT_NETWORK, latency_ms=elapsed_ms()) from None

        latency = elapsed_ms()
        if status == 200:
            return self._parse(payload, request.model, latency)
        if status == 429:
            raise LLMTransientError(TRANSIENT_RATE_LIMITED, latency_ms=latency, http_status=status)
        if status == 408:
            raise LLMTransientError(TRANSIENT_TIMEOUT, latency_ms=latency, http_status=status)
        if status >= 500:
            raise LLMTransientError(TRANSIENT_SERVER_ERROR, latency_ms=latency, http_status=status)
        if 400 <= status < 500:                             # 400 請求不合法、401 與 403 無權限、404 模型不存在……：需要人工處理
            raise LLMConfigError(http_status=status, latency_ms=latency)
        raise LLMTransientError(TRANSIENT_SERVER_ERROR, latency_ms=latency, http_status=status)

    # ---- 回應 ---------------------------------------------------------------------

    def _parse(self, payload: Any, model: str, latency_ms: int) -> LLMResponse:
        data = payload if isinstance(payload, dict) else {}
        feedback = data.get("promptFeedback") or {}
        candidates = data.get("candidates") or []
        if feedback.get("blockReason") and not candidates:
            raise LLMBlockedError(BLOCKED_INPUT, latency_ms=latency_ms)

        candidate = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
        finish = str(candidate.get("finishReason") or "")
        if finish in _BLOCKED_FINISH:
            raise LLMBlockedError(BLOCKED_OUTPUT, latency_ms=latency_ms)

        parts = (candidate.get("content") or {}).get("parts") or []
        # 供應商可能回傳「思考摘要」片段（thought 為真）：它不是答案，不能混進輸出
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought"))

        usage = data.get("usageMetadata") or {}
        return LLMResponse(
            text=text,
            model=model,
            finish_reason={"STOP": FINISH_STOP, "MAX_TOKENS": FINISH_LENGTH}.get(finish, FINISH_OTHER),
            latency_ms=latency_ms,
            prompt_tokens=usage.get("promptTokenCount"),
            completion_tokens=usage.get("candidatesTokenCount"),
            thinking_tokens=usage.get("thoughtsTokenCount"),
        )
