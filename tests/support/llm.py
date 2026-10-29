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
    FINISH_STOP, LLMError, LLMRequest, LLMResponse, LLMTransientError, TRANSIENT_TIMEOUT,
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


class FakeLLMProvider:
    name = "fake"

    def __init__(self, clock: FakeMonotonic | None = None, *, default: Step | None = None) -> None:
        self.clock = clock or FakeMonotonic()
        self.calls: list[LLMRequest] = []
        self.hooks: list[Callable[[LLMRequest], None]] = []       # 每次呼叫前執行（例如斷言「此刻沒有開啟的資料庫交易」）
        self._steps: list[Step] = []
        self._default = default

    def script(self, *steps: Step | str | Exception) -> "FakeLLMProvider":
        for step in steps:
            self._steps.append(step if isinstance(step, Step) else Step(step))
        return self

    @property
    def remaining(self) -> int:
        return len(self._steps)

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
