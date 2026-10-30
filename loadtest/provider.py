"""可設定延遲與錯誤率的假模型供應商（load-testing 規格「假模型入口只在開發與測試載入」）。

實作與真實轉接器相同的介面（`generate`），但**真的睡眠**來模擬延遲（測試 worker、執行緒與資料庫的容量需要真實的占用時間），
不呼叫任何外部服務、不消耗額度。錯誤類別對應真實轉接器的暫時性錯誤：速率限制、逾時、伺服器錯誤；
逾時會睡到請求的逾時秒數才失敗（和真實轉接器一樣佔住執行緒）。回覆是合法的客服結構化輸出（繁體中文、不含任何個資）。
"""
from __future__ import annotations

import json
import random
import threading
import time
from collections.abc import Callable, Mapping

from app.llm.types import (
    FINISH_STOP, LLMRequest, LLMResponse, LLMTransientError, TRANSIENT_RATE_LIMITED, TRANSIENT_SERVER_ERROR, TRANSIENT_TIMEOUT,
)

ERROR_KINDS = (TRANSIENT_RATE_LIMITED, TRANSIENT_TIMEOUT, TRANSIENT_SERVER_ERROR)
HTTP_STATUS = {TRANSIENT_RATE_LIMITED: 429, TRANSIENT_SERVER_ERROR: 500}
SUMMARY_MARKER = "累積摘要"            # 摘要呼叫的系統指令含這個詞（見 app/services/summary_service.py）
PROMPT_TOKENS = 1500
COMPLETION_TOKENS = 120

REPLY = {
    "intent": "account_help",
    "confidence": 0.9,
    "entities": {"topic": None, "membership_tier": None, "account_issue": None, "feedback_type": None},
    "reply_text": "這是負載測試的假回覆，沒有任何真實內容。",
    "suggested_actions": [],
}


class LoadTestConfigError(ValueError):
    """假供應商的設定不合法。訊息只含變數名稱與規則，不含值以外的資訊。"""


def parse_error_rates(spec: str | None) -> dict[str, float]:
    """`rate_limited=0.02,timeout=0.01` → {"rate_limited": 0.02, "timeout": 0.01}；各項在 0 到 1 之間，總和不得超過 1。"""
    rates: dict[str, float] = {}
    for item in (spec or "").split(","):
        item = item.strip()
        if not item:
            continue
        kind, _, number = item.partition("=")
        kind = kind.strip()
        if kind not in ERROR_KINDS:
            raise LoadTestConfigError(f"LOADTEST_ERROR_RATES 的錯誤類別必須是 {'、'.join(ERROR_KINDS)}")
        try:
            rate = float(number)
        except ValueError:
            raise LoadTestConfigError("LOADTEST_ERROR_RATES 的比率必須是 0 到 1 之間的數字") from None
        if not 0.0 <= rate <= 1.0:
            raise LoadTestConfigError("LOADTEST_ERROR_RATES 的比率必須是 0 到 1 之間的數字")
        rates[kind] = rate
    if sum(rates.values()) > 1.0:
        raise LoadTestConfigError("LOADTEST_ERROR_RATES 的比率總和不得超過 1")
    return rates


class LoadTestProvider:
    name = "loadtest"

    def __init__(
        self,
        *,
        latency_seconds: float = 1.5,
        jitter_seconds: float = 0.5,
        error_rates: Mapping[str, float] | None = None,
        seed: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if latency_seconds < 0 or jitter_seconds < 0:
            raise LoadTestConfigError("延遲與抖動不得為負數")
        self._latency = latency_seconds
        self._jitter = jitter_seconds
        self._rates = dict(error_rates or {})
        self._random = random.Random(seed)
        self._lock = threading.Lock()
        self._sleep = sleep
        self.calls = 0
        self.errors: dict[str, int] = {kind: 0 for kind in ERROR_KINDS}

    def _draw(self) -> tuple[float, str | None]:
        with self._lock:
            self.calls += 1
            delay = max(0.0, self._latency + self._random.uniform(-self._jitter, self._jitter))
            roll = self._random.random()
            cumulative = 0.0
            for kind in ERROR_KINDS:
                cumulative += self._rates.get(kind, 0.0)
                if roll < cumulative:
                    self.errors[kind] += 1
                    return delay, kind
        return delay, None

    def generate(self, request: LLMRequest) -> LLMResponse:
        delay, kind = self._draw()
        if kind == TRANSIENT_TIMEOUT:
            self._sleep(request.timeout_seconds)      # 和真實轉接器一樣：等到逾時才放棄，佔住這條執行緒
            raise LLMTransientError(TRANSIENT_TIMEOUT, latency_ms=int(request.timeout_seconds * 1000))
        self._sleep(delay)
        if kind is not None:
            raise LLMTransientError(kind, latency_ms=int(delay * 1000), http_status=HTTP_STATUS.get(kind))
        body = {"summary": "負載測試的假摘要。"} if SUMMARY_MARKER in request.system_instruction else REPLY
        return LLMResponse(
            text=json.dumps(body, ensure_ascii=False), model=request.model, finish_reason=FINISH_STOP, latency_ms=int(delay * 1000),
            prompt_tokens=PROMPT_TOKENS, completion_tokens=COMPLETION_TOKENS, thinking_tokens=None,
        )


def provider_from_environment(environ: Mapping[str, str]) -> LoadTestProvider:
    def number(name: str, default: float) -> float:
        raw = environ.get(name)
        if raw in (None, ""):
            return default
        try:
            return float(raw)
        except ValueError:
            raise LoadTestConfigError(f"{name} 必須是數字") from None

    seed = environ.get("LOADTEST_SEED")
    return LoadTestProvider(
        latency_seconds=number("LOADTEST_LATENCY_SECONDS", 1.5),
        jitter_seconds=number("LOADTEST_JITTER_SECONDS", 0.5),
        error_rates=parse_error_rates(environ.get("LOADTEST_ERROR_RATES")),
        seed=int(seed) if seed else None,
    )
