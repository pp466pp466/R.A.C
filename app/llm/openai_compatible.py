"""OpenAI 相容端點的轉接器（見 llm-openai-compatible 規格）。

目前接 OpenRouter 的開放模型，也可以指向任何實作 `/chat/completions` 的服務（含本機模型）。所有這類供應商專屬的東西都只在
這個檔案：請求格式、驗證標頭、狀態碼與結束原因的對應。業務程式碼只看得到 `app.llm.types` 的中立型別。

- 只用標準函式庫（共用 `transport.py`），非串流；金鑰放 `Authorization: Bearer` 標頭，**不在網址**。
- 失敗只回報分類、狀態碼與延遲，**不保留供應商的回應本文**（它可能含請求內容的回聲）。
- 結構化輸出預設不送供應商的結構描述參數（許多開放模型不支援，會被拒絕），改在系統指令末端附上結構描述與
  「只輸出 JSON」的要求；明確設定該模型支援時才送參數。輸出一律由呼叫端驗證，轉接器不假設它符合。
- 零保存偏好：要求時每個請求都帶，沒有符合的端點就由供應商拒絕，**不會改成不帶偏好重送**（否則使用者以為資料不被保存，
  實際上被靜默降級）。
- 免費與付費模型沒有任何程式路徑上的差別：模型名稱只是請求裡的一個字串。
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from app.llm.transport import HttpTransport, UrllibTransport
from app.llm.types import (
    BLOCKED_OUTPUT, FINISH_LENGTH, FINISH_OTHER, FINISH_STOP, TRANSIENT_NETWORK, TRANSIENT_RATE_LIMITED,
    TRANSIENT_SERVER_ERROR, TRANSIENT_TIMEOUT, LLMBlockedError, LLMConfigError, LLMRequest, LLMResponse, LLMTransientError,
)

PROVIDER_NAME = "openai-compatible"            # 帳本的 provider 欄位只有 20 個字元
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
AUTH_HEADER = "Authorization"

# 沒有結構描述參數可用時，附在系統指令末端的要求（內容是給模型的提示詞，一律繁體中文）
_JSON_ONLY_INSTRUCTION = "請只輸出一個符合下列 JSON Schema 的 JSON 物件，不要加上任何說明文字，也不要使用 Markdown 程式碼區塊。JSON Schema："


class OpenAICompatibleProvider:
    name = PROVIDER_NAME

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        supports_json_schema: bool = False,
        zero_retention: bool = False,
        transport: HttpTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not api_key:
            raise ValueError("模型金鑰不可為空")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._supports_json_schema = supports_json_schema
        self._zero_retention = zero_retention
        self._transport = transport or UrllibTransport()
        self._monotonic = monotonic

    def __repr__(self) -> str:                              # 絕不顯示金鑰
        return "OpenAICompatibleProvider(api_key='***')"

    # ---- 請求 ---------------------------------------------------------------------

    def build_body(self, request: LLMRequest) -> dict[str, Any]:
        system = request.system_instruction
        body: dict[str, Any] = {"model": request.model, "stream": False, "max_tokens": request.max_output_tokens}
        if request.response_schema is not None:
            if self._supports_json_schema:
                body["response_format"] = {"type": "json_schema", "json_schema": {"name": "response", "schema": request.response_schema}}
            else:
                system = f"{system}\n\n{_JSON_ONLY_INSTRUCTION}\n{json.dumps(request.response_schema, ensure_ascii=False)}"
        body["messages"] = [{"role": "system", "content": system}] + [
            {"role": "assistant" if content.role == "model" else "user", "content": "\n\n".join(content.parts)}
            for content in request.contents
        ]
        if self._zero_retention:
            body["provider"] = {"zdr": True, "data_collection": "deny"}
        return body

    def generate(self, request: LLMRequest) -> LLMResponse:
        url = f"{self._base_url}/chat/completions"
        headers = {"Content-Type": "application/json; charset=utf-8", AUTH_HEADER: f"Bearer {self._api_key}"}
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
        raise _error_for_status(status, latency)

    # ---- 回應 ---------------------------------------------------------------------

    def _parse(self, payload: Any, model: str, latency_ms: int) -> LLMResponse:
        data = payload if isinstance(payload, dict) else {}
        choices = data.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
        if choice is None:
            # 沒有結果：可能是 200 狀態碼夾帶錯誤物件。依錯誤代碼歸類；無法判斷就當作暫時性（呼叫端可以切換備援）
            error = data.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            raise _error_for_status(code if isinstance(code, int) else 0, latency_ms)

        finish = str(choice.get("finish_reason") or "")
        if finish == "content_filter":
            raise LLMBlockedError(BLOCKED_OUTPUT, latency_ms=latency_ms)
        if finish == "error":
            raise LLMTransientError(TRANSIENT_SERVER_ERROR, latency_ms=latency_ms)

        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        details = usage.get("completion_tokens_details")
        return LLMResponse(
            text=content if isinstance(content, str) else "",
            model=model,
            finish_reason={"stop": FINISH_STOP, "length": FINISH_LENGTH}.get(finish, FINISH_OTHER),
            latency_ms=latency_ms,
            prompt_tokens=_int_or_none(usage.get("prompt_tokens")),
            completion_tokens=_int_or_none(usage.get("completion_tokens")),
            thinking_tokens=_int_or_none(details.get("reasoning_tokens")) if isinstance(details, dict) else None,
        )


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _error_for_status(status: int, latency_ms: int) -> Exception:
    """HTTP 狀態碼（或錯誤物件的代碼）→ 中立的錯誤。只帶分類、狀態碼與延遲，不帶回應本文。"""
    if status == 429:
        return LLMTransientError(TRANSIENT_RATE_LIMITED, latency_ms=latency_ms, http_status=status)
    if status == 408:
        return LLMTransientError(TRANSIENT_TIMEOUT, latency_ms=latency_ms, http_status=status)
    if status >= 500:
        return LLMTransientError(TRANSIENT_SERVER_ERROR, latency_ms=latency_ms, http_status=status)
    if 400 <= status < 500:     # 400 請求不合法、401、402（餘額不足）、403、404（模型下架或沒有可用端點）：需要人工處理
        return LLMConfigError(http_status=status, latency_ms=latency_ms)
    return LLMTransientError(TRANSIENT_SERVER_ERROR, latency_ms=latency_ms, http_status=status or None)
