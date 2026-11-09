"""OpenAI 相容端點的轉接器（見 llm-openai-compatible 規格）。

目前接 OpenRouter 的開放模型，也可以指向任何實作 `/chat/completions` 的服務（含本機模型）。所有這類供應商專屬的東西都只在
這個檔案：請求格式、驗證標頭、狀態碼與結束原因的對應。業務程式碼只看得到 `app.llm.types` 的中立型別。

- 只用標準函式庫（共用 `transport.py`）；金鑰放 `Authorization: Bearer` 標頭，**不在網址**。`generate` 是非串流，`stream` 是 SSE 串流
  （逐行讀取 `data:` 事件，對應為中立的 `StreamText`／`StreamTick`／`StreamEnd`）。
- 推理由設定控制（`reasoning`，預設關閉）：關閉時每個請求（串流與非串流、主要與備援）都帶 `reasoning: {"enabled": false}`，
  推理型模型才有可接受的首字時間；推理片段（`delta.reasoning` 等）永遠不算正文。
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
from typing import Any, Callable, Iterator

from app.llm.transport import STREAM_EOF, HttpTransport, LineStream, StreamHead, UrllibTransport
from app.llm.types import (
    BLOCKED_OUTPUT, FINISH_LENGTH, FINISH_OTHER, FINISH_STOP, TRANSIENT_NETWORK, TRANSIENT_RATE_LIMITED,
    TRANSIENT_SERVER_ERROR, TRANSIENT_TIMEOUT, LLMBlockedError, LLMConfigError, LLMRequest, LLMResponse, LLMTransientError,
    StreamEnd, StreamEvent, StreamText, StreamTick,
)

PROVIDER_NAME = "openai-compatible"            # 帳本的 provider 欄位只有 20 個字元
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
AUTH_HEADER = "Authorization"
STREAM_TICK_SECONDS = 1.0                      # 上游超過這麼久沒有位元組就產生閒置 tick，讓外層檢查期限、送出心跳

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
        reasoning: bool = False,
        transport: HttpTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not api_key:
            raise ValueError("模型金鑰不可為空")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._supports_json_schema = supports_json_schema
        self._zero_retention = zero_retention
        self._reasoning = reasoning
        self._transport: Any = transport or UrllibTransport()       # 串流另需 `open_stream`（真實傳輸兩者都有）
        self._monotonic = monotonic

    def __repr__(self) -> str:                              # 絕不顯示金鑰
        return "OpenAICompatibleProvider(api_key='***')"

    # ---- 請求 ---------------------------------------------------------------------

    def build_body(self, request: LLMRequest, *, stream: bool = False) -> dict[str, Any]:
        system = request.system_instruction
        body: dict[str, Any] = {"model": request.model, "stream": stream, "max_tokens": request.max_output_tokens}
        if stream:
            body["stream_options"] = {"include_usage": True}          # 最後多一個只含用量的事件
        if not self._reasoning:
            body["reasoning"] = {"enabled": False}                    # 開啟時不帶，由供應商與模型的預設行為決定
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

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json; charset=utf-8", AUTH_HEADER: f"Bearer {self._api_key}"}

    def stream(self, request: LLMRequest) -> "OpenAIStream":
        """開始一次串流呼叫。立即回傳（連線在背景進行），失敗在迭代時以中立的錯誤拋出。

        `request.timeout_seconds` 是連線與每次讀取的逾時（安全網）；首字與總時間的期限由外層以閒置 tick 檢查。
        `response_schema` 不用於串流（劇情是純文字）。
        """
        started = self._monotonic()
        try:
            raw = self._transport.open_stream(
                f"{self._base_url}/chat/completions", {**self._headers(), "Accept": "text/event-stream"},
                self.build_body(request, stream=True), request.timeout_seconds,
            )
        except TimeoutError:
            raise LLMTransientError(TRANSIENT_TIMEOUT) from None
        except OSError:
            raise LLMTransientError(TRANSIENT_NETWORK) from None
        return OpenAIStream(raw, model=request.model, started=started, monotonic=self._monotonic)

    def generate(self, request: LLMRequest) -> LLMResponse:
        url = f"{self._base_url}/chat/completions"
        headers = self._headers()
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
        text = content if isinstance(content, str) else ""
        if finish == "length" and not text:
            # 思考把輸出上限燒光、沒有任何正文：不是成功的空回覆，讓呼叫端可以切換備援模型
            raise LLMTransientError(TRANSIENT_SERVER_ERROR, latency_ms=latency_ms)
        prompt_tokens, completion_tokens, thinking_tokens = _usage_tokens(data.get("usage"))
        return LLMResponse(
            text=text,
            model=model,
            finish_reason=_normalise_finish(finish),
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            thinking_tokens=thinking_tokens,
        )


class OpenAIStream:
    """一次進行中的 SSE 串流（見 `StreamHandle`）。

    只收 `data:` 行；`:` 開頭的註解行（保活）、`event:` 等其他欄位、推理片段與空片段都不是正文，它們與上游的沉默一樣只產生
    `StreamTick`（讓外層有機會檢查期限）。`[DONE]` 或結束原因代表串流完整；兩者都沒有就結束，視為連線中途斷掉。
    錯誤沿用非串流的對應，只帶分類、狀態碼與延遲，不保留供應商的本文。
    """

    def __init__(self, raw: LineStream, *, model: str, started: float, monotonic: Callable[[], float]) -> None:
        self._raw = raw
        self._model = model
        self._started = started
        self._monotonic = monotonic
        self._closed = False
        self._events = self._run()

    def __iter__(self) -> Iterator[StreamEvent]:
        return self._events

    def close(self) -> None:
        """關閉上游；之後不再讀取。可重複呼叫，也可在尚未開始迭代時呼叫。"""
        self._closed = True
        self._raw.close()

    def _elapsed_ms(self) -> int:
        return int((self._monotonic() - self._started) * 1000)

    def _run(self) -> Iterator[StreamEvent]:
        finish: str | None = None
        usage: object = None
        any_text = False
        done = False
        try:
            while True:
                try:
                    item = self._raw.poll(STREAM_TICK_SECONDS)
                except TimeoutError:
                    raise LLMTransientError(TRANSIENT_TIMEOUT, latency_ms=self._elapsed_ms()) from None
                except OSError:
                    raise LLMTransientError(TRANSIENT_NETWORK, latency_ms=self._elapsed_ms()) from None
                if self._closed:
                    return
                if item is None:
                    yield StreamTick()
                    continue
                if isinstance(item, StreamHead):
                    if item.status != 200:
                        raise _error_for_status(item.status, self._elapsed_ms())
                    continue
                if item is STREAM_EOF:
                    break

                line = item.decode("utf-8", errors="replace").strip()   # type: ignore[union-attr]  # 此處只剩 bytes
                if not line:
                    continue                                            # 事件之間的空行
                if not line.startswith("data:"):
                    yield StreamTick()                                  # 保活註解或其他欄位：忽略，但讓外層有機會檢查期限
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    done = True
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    yield StreamTick()
                    continue
                if not isinstance(chunk, dict):
                    yield StreamTick()
                    continue
                error = chunk.get("error")
                if error:
                    code = error.get("code") if isinstance(error, dict) else None
                    raise _error_for_status(code if isinstance(code, int) else 0, self._elapsed_ms())
                if isinstance(chunk.get("usage"), dict):
                    usage = chunk["usage"]
                choices = chunk.get("choices")
                choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
                text = ""
                if choice is not None:
                    if choice.get("finish_reason"):
                        finish = str(choice["finish_reason"])
                    delta = choice.get("delta")
                    content = delta.get("content") if isinstance(delta, dict) else None
                    text = content if isinstance(content, str) else ""
                if text:
                    any_text = True
                    yield StreamText(text)
                else:
                    yield StreamTick()                                  # 推理片段、空片段、只含用量的事件

            if self._closed:
                return
            if not done and finish is None:
                raise LLMTransientError(TRANSIENT_NETWORK, latency_ms=self._elapsed_ms())      # 沒有結尾就斷線
            if finish == "content_filter":
                raise LLMBlockedError(BLOCKED_OUTPUT, latency_ms=self._elapsed_ms())
            if finish == "error" or (finish == "length" and not any_text):
                raise LLMTransientError(TRANSIENT_SERVER_ERROR, latency_ms=self._elapsed_ms())
            prompt_tokens, completion_tokens, thinking_tokens = _usage_tokens(usage)
            yield StreamEnd(
                finish_reason=_normalise_finish(finish or ""), model=self._model, prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens, thinking_tokens=thinking_tokens,
            )
        finally:
            self._raw.close()


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _normalise_finish(finish: str) -> str:
    return {"stop": FINISH_STOP, "length": FINISH_LENGTH}.get(finish, FINISH_OTHER)


def _usage_tokens(usage: object) -> tuple[int | None, int | None, int | None]:
    """（輸入, 輸出, 思考）token；供應商沒有提供或格式不對的是 None，不是 0。"""
    data = usage if isinstance(usage, dict) else {}
    details = data.get("completion_tokens_details")
    return (
        _int_or_none(data.get("prompt_tokens")),
        _int_or_none(data.get("completion_tokens")),
        _int_or_none(details.get("reasoning_tokens")) if isinstance(details, dict) else None,
    )


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
