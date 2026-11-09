"""測試用的假模型供應商（見 llm-provider 規格「以假的供應商執行業務邏輯」）。

`FakeLLMProvider` 實作與真實轉接器相同的介面：依腳本依序回覆或拋出中立的錯誤，記錄每次收到的請求；
並用一個可注入的「假的單調時鐘」模擬耗時——每個步驟有自己的耗時，**超過請求的逾時就變成逾時錯誤**，
所以重試、備援與預算的測試完全不必真的等待，行為卻與真實的逾時一致。

用法：
    clock = FakeMonotonic()
    provider = FakeLLMProvider(clock)
    provider.script(Step(reply_json), Step(LLMTransientError("rate_limited"), seconds=0.3))
    service = LlmService(..., provider=provider, monotonic=clock)
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from app.llm.types import (
    FINISH_STOP, LLMError, LLMRequest, LLMResponse, LLMTransientError, StreamEnd, StreamText, StreamTick,
    TRANSIENT_NETWORK, TRANSIENT_TIMEOUT,
)


class FakeMonotonic:
    """可手動推進的單調時鐘（秒）；呼叫它得到目前的時間。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class Step:
    """腳本中的一步：`result` 是回覆文字（字串）、要拋出的例外，或接收請求並回傳前兩者的函式；`seconds` 是這一步花的時間。"""

    result: Any
    seconds: float = 1.0
    prompt_tokens: int | None = 100
    completion_tokens: int | None = 50
    thinking_tokens: int | None = None
    finish_reason: str = FINISH_STOP


@dataclass
class Chunk:
    """串流腳本中的一個正文片段：時鐘先前進 `seconds` 再產生。"""

    text: str
    seconds: float = 0.2


@dataclass
class Idle:
    """串流腳本中的一次閒置 tick（上游沉默、保活註解或推理片段）：時鐘前進 `seconds` 後產生 `StreamTick`。"""

    seconds: float = 1.0


@dataclass
class Fail:
    """串流腳本中的失敗：時鐘前進後拋出 `error`（可以是第一個片段之前，也可以是中途）。"""

    error: Exception
    seconds: float = 0.2


@dataclass
class Finish:
    """串流腳本中的正常結束事件；用量為 None 表示供應商沒有提供。"""

    finish_reason: str = FINISH_STOP
    prompt_tokens: int | None = 100
    completion_tokens: int | None = 50
    thinking_tokens: int | None = None
    seconds: float = 0.1


StreamItem = Chunk | Idle | Fail | Finish


class FakeStreamHandle:
    """依腳本產生串流事件；每一項先推進假時鐘。`close()` 之後不再產生任何事件。腳本用完還沒有 `Finish` 視為連線中途斷掉。"""

    def __init__(self, clock: FakeMonotonic, items: list[StreamItem], model: str) -> None:
        self._clock = clock
        self._items = list(items)
        self._model = model
        self.closed = False
        self.served = 0                                  # 已經交給呼叫端的事件數

    def __iter__(self):
        return self._events()

    def close(self) -> None:
        self.closed = True

    def _events(self):
        for item in self._items:
            if self.closed:
                return
            self._clock.advance(item.seconds)
            if isinstance(item, Chunk):
                self.served += 1
                yield StreamText(item.text)
            elif isinstance(item, Idle):
                self.served += 1
                yield StreamTick()
            elif isinstance(item, Fail):
                if isinstance(item.error, LLMError):
                    item.error.latency_ms = int(item.seconds * 1000)
                raise item.error
            else:
                self.served += 1
                yield StreamEnd(
                    finish_reason=item.finish_reason, model=self._model, prompt_tokens=item.prompt_tokens,
                    completion_tokens=item.completion_tokens, thinking_tokens=item.thinking_tokens,
                )
                return
        if not self.closed:
            raise LLMTransientError(TRANSIENT_NETWORK)


class FakeLLMProvider:
    name = "fake"

    def __init__(self, clock: FakeMonotonic | None = None, *, default: Step | None = None) -> None:
        self.clock = clock or FakeMonotonic()
        self.calls: list[LLMRequest] = []
        self.hooks: list[Callable[[LLMRequest], None]] = []       # 每次呼叫前執行（例如斷言「此刻沒有開啟的資料庫交易」）
        self._steps: list[Step] = []
        self._default = default
        self._streams: list[list[StreamItem]] = []
        self.handles: list[FakeStreamHandle] = []                 # 每次 `stream()` 回傳的串流（檢查 `closed` 與 `served`）

    def script(self, *steps: Step | str | Exception) -> "FakeLLMProvider":
        for step in steps:
            self._steps.append(step if isinstance(step, Step) else Step(step))
        return self

    @property
    def remaining(self) -> int:
        return len(self._steps)

    def script_stream(self, *items: StreamItem) -> "FakeLLMProvider":
        """為下一次 `stream()` 排一個腳本（依 `Chunk`、`Idle`、`Fail`、`Finish` 順序）。每次 `stream()` 取走一個腳本。"""
        self._streams.append(list(items))
        return self

    @property
    def remaining_streams(self) -> int:
        return len(self._streams)

    def stream(self, request: LLMRequest) -> FakeStreamHandle:
        self.calls.append(request)
        for hook in self.hooks:
            hook(request)
        if not self._streams:
            raise AssertionError("假供應商的串流腳本已用完，但程式又開了一次串流（呼叫次數多於預期）")
        handle = FakeStreamHandle(self.clock, self._streams.pop(0), request.model)
        self.handles.append(handle)
        return handle

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        for hook in self.hooks:
            hook(request)
        if self._steps:
            step = self._steps.pop(0)
        elif self._default is not None:
            step = self._default
        else:
            raise AssertionError("假供應商的腳本已用完，但程式又呼叫了一次（呼叫次數多於預期）")

        result = step.result(request) if callable(step.result) and not isinstance(step.result, Exception) else step.result
        if step.seconds > request.timeout_seconds:                    # 真實轉接器的逾時行為：等到逾時就放棄
            self.clock.advance(request.timeout_seconds)
            raise LLMTransientError(TRANSIENT_TIMEOUT, latency_ms=int(request.timeout_seconds * 1000))
        self.clock.advance(step.seconds)
        latency = int(step.seconds * 1000)
        if isinstance(result, LLMError):
            result.latency_ms = latency
            raise result
        if isinstance(result, Exception):
            raise result
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        return LLMResponse(
            text=text, model=request.model, finish_reason=step.finish_reason, latency_ms=latency,
            prompt_tokens=step.prompt_tokens, completion_tokens=step.completion_tokens, thinking_tokens=step.thinking_tokens,
        )
