"""供應商中立的模型呼叫型別（見 llm-provider 規格）。

業務邏輯（`LlmService`、客服）只認得這裡的型別：要求、回應，以及四種中立的失敗類別。
各供應商的轉接器負責把自己的請求格式、狀態碼與安全過濾結果對應成這些型別，所以更換供應商只需要新的轉接器與新的環境變數。

錯誤物件只帶「分類與延遲」，**不帶供應商的回應本文**：本文可能含有請求內容的回聲，也不該被寫進日誌或帳本。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Protocol

# 正規化後的結束原因（帳本的 finish_reason 欄位只會出現這些值）
FINISH_STOP = "stop"
FINISH_LENGTH = "length"                  # 因輸出 token 上限被截斷：JSON 通常不完整
FINISH_SAFETY = "safety"                  # 輸出被安全過濾擋下
FINISH_PROMPT_BLOCKED = "prompt_blocked"  # 輸入被擋下，沒有產生任何內容
FINISH_OTHER = "other"

# 暫時性錯誤的分類（帳本的 status 以同名字串記錄）
TRANSIENT_RATE_LIMITED = "rate_limited"
TRANSIENT_SERVER_ERROR = "server_error"
TRANSIENT_TIMEOUT = "timeout"
TRANSIENT_NETWORK = "network"

BLOCKED_INPUT = "input"
BLOCKED_OUTPUT = "output"


@dataclass(frozen=True)
class LLMContent:
    """一個內容區塊：`role` 為 `user` 或 `model`，`parts` 是依序的文字片段。"""

    role: str
    parts: tuple[str, ...]


@dataclass(frozen=True)
class LLMRequest:
    """一次非串流的結構化輸出呼叫。`response_schema` 是中立的 JSON Schema 子集，由轉接器轉成供應商的格式。"""

    model: str
    system_instruction: str
    contents: tuple[LLMContent, ...]
    response_schema: dict[str, Any] | None
    timeout_seconds: float
    max_output_tokens: int
    thinking_budget: int | None = None


@dataclass(frozen=True)
class LLMResponse:
    """成功取得的結果。`model` 是**實際使用**的模型；思考 token 供應商沒有提供時為 None（不是 0）。"""

    text: str
    model: str
    finish_reason: str
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    thinking_tokens: int | None = None


class LLMProvider(Protocol):
    """模型供應商的轉接器介面。只有一個方法，失敗一律以下列錯誤類別表達。"""

    name: str

    def generate(self, request: LLMRequest) -> LLMResponse: ...


# ---- 串流（見 llm-streaming 規格）-------------------------------------------------------------------


@dataclass(frozen=True)
class StreamText:
    """一段正文片段（非空）。推理片段、空片段不是 `StreamText`，只會變成 `StreamTick`。"""

    text: str


@dataclass(frozen=True)
class StreamTick:
    """喚醒外層檢查期限與送出心跳。上游超過約 1 秒沒有位元組，或收到的行不含正文（保活註解、推理片段、空片段）時產生。"""


@dataclass(frozen=True)
class StreamEnd:
    """串流正常結束。用量供應商沒有提供時為 None（不是 0）；`model` 是呼叫端要求的模型名稱。"""

    finish_reason: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    thinking_tokens: int | None = None


StreamEvent = StreamText | StreamTick | StreamEnd


class StreamHandle(Protocol):
    """進行中的串流：可迭代取得 `StreamEvent`，也可以隨時 `close()`（客戶端斷線或審查命中時）。

    失敗以中立的錯誤類別在迭代中拋出，可發生在第一個片段之前（串流開始前的 HTTP 錯誤）或之後。
    `close()` 之後不得再讀取上游，且可重複呼叫。
    """

    def __iter__(self) -> Iterator[StreamEvent]: ...

    def close(self) -> None: ...


class StreamingLLMProvider(LLMProvider, Protocol):
    """能串流的轉接器：`generate` 之外多一個 `stream`。呼叫 `stream` 本身不阻塞，連線與錯誤在迭代時才出現。"""

    def stream(self, request: LLMRequest) -> StreamHandle: ...


class LLMError(Exception):
    """所有模型呼叫失敗的基底。`latency_ms` 是這次呼叫實際花的時間（記入帳本）。"""

    def __init__(self, message: str, *, latency_ms: int = 0) -> None:
        super().__init__(message)
        self.latency_ms = latency_ms


class LLMTransientError(LLMError):
    """暫時性錯誤：速率限制、伺服器錯誤、逾時、連線失敗。呼叫端可以改用備援模型。"""

    def __init__(self, kind: str, *, latency_ms: int = 0, http_status: int | None = None) -> None:
        super().__init__(f"模型暫時性錯誤：{kind}", latency_ms=latency_ms)
        self.kind = kind
        self.http_status = http_status


class LLMBlockedError(LLMError):
    """被供應商的內容安全過濾擋下。`phase` 為 `input`（輸入被擋）或 `output`（輸出被擋）。不重試、不切換備援。"""

    def __init__(self, phase: str, *, latency_ms: int = 0) -> None:
        super().__init__(f"內容被安全過濾阻擋：{phase}", latency_ms=latency_ms)
        self.phase = phase


class LLMConfigError(LLMError):
    """設定錯誤：請求不合法（400）或金鑰無權限（403）。切換備援也不會好，需要人工處理。"""

    def __init__(self, *, http_status: int | None = None, latency_ms: int = 0) -> None:
        super().__init__("模型供應商回應設定錯誤", latency_ms=latency_ms)
        self.http_status = http_status


class LLMValidationError(Exception):
    """模型輸出無法使用：不是 JSON、結構不符，或語意驗證不過。

    `reason` 只是分類（例如 `not_json`、`schema`、`semantic:reply_text_empty`），**不含任何模型文字**。
    刻意不用 frozen dataclass：框架在例外上設定 `__traceback__`、`__context__` 時，凍結的例外會再拋出錯誤。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"模型輸出驗證失敗：{reason}")
        self.reason = reason
