"""假模型伺服器（fake-model-server 規格）：讓隔離的壓測環境把真實的 Gemini 轉接器指向它（`GEMINI_BASE_URL`），
整條程式路徑都是真的，只有「模型」是假的，所以壓測不消耗任何額度、不連到任何外部服務。

- 提供 Gemini 相容的 `POST /v1beta/models/<模型>:generateContent`（回覆格式與專案的轉接器解析的一致），以及不需要金鑰的 `GET /healthz`。
- 只接受部署時設定的測試金鑰（`FAKE_LLM_API_KEY`，標頭 `x-goog-api-key`）：不符或缺少回 403 且不處理請求；看起來像真實 Google 金鑰的值拒絕啟動。
- 延遲與錯誤率用與本機假供應商（`loadtest/provider.py`）相同的環境變數與規則：`LOADTEST_LATENCY_SECONDS`、`LOADTEST_JITTER_SECONDS`、
  `LOADTEST_ERROR_RATES`（`rate_limited`、`timeout`、`server_error`，各項 0 到 1、總和不超過 1）、`LOADTEST_SEED`；另有 `LOADTEST_TIMEOUT_SECONDS`
  （逾時類錯誤睡多久才失敗，預設 25 秒，要比轉接器的單次逾時 20 秒長，這樣是客戶端先逾時，伺服器的執行緒仍被佔住直到睡醒）。
  設定不合法就啟動失敗，訊息只指出變數名稱與規則，不回顯值。
- **不記錄任何內容**：沒有請求本文、系統指令、金鑰與回覆；日誌只有請求數與各狀態碼的彙總。
- 這個目錄是獨立的映像（見同目錄的 Dockerfile 與 requirements.txt，只有 Flask 與 gunicorn），**不屬於主映像**；
  為了這一點，本檔只使用標準函式庫與 Flask，不匯入專案的任何程式碼（與 `loadtest/provider.py` 的一致性由測試守住）。

本機執行（金鑰自訂，不要用真實金鑰）：
    set FAKE_LLM_API_KEY=local-fake-key-0001
    python -m flask --app "loadtest.fake_llm.server:create_fake_llm_app" run --port 8081
容器內以 gunicorn 啟動：`server:create_fake_llm_app()`。
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import random
import threading
import time
from collections.abc import Callable, Mapping

from flask import Flask, jsonify, request

ERROR_KINDS = ("rate_limited", "timeout", "server_error")
HTTP_STATUS = {"rate_limited": 429, "server_error": 500, "timeout": 504}
ERROR_STATUS_TEXT = {429: "RESOURCE_EXHAUSTED", 500: "INTERNAL", 504: "DEADLINE_EXCEEDED"}
KEY_HEADER = "x-goog-api-key"
SUMMARY_MARKER = "累積摘要"            # 摘要呼叫的系統指令含這個詞（見 app/services/summary_service.py）
PROMPT_TOKENS = 1500
COMPLETION_TOKENS = 120
DEFAULT_LATENCY_SECONDS = 1.5
DEFAULT_JITTER_SECONDS = 0.5
DEFAULT_TIMEOUT_SECONDS = 25.0
SUMMARY_EVERY = 100                    # 每處理這麼多個請求記錄一次彙總
MAX_BODY_BYTES = 1024 * 1024
MIN_KEY_LENGTH = 8
REAL_KEY_PREFIXES = ("AIza", "sk-")    # 真實的 Google 與 OpenAI 相容供應商金鑰的前綴：測試金鑰不得長得像它們

REPLY = {
    "intent": "account_help",
    "confidence": 0.9,
    "entities": {"topic": None, "membership_tier": None, "account_issue": None, "feedback_type": None},
    "reply_text": "這是負載測試的假回覆，沒有任何真實內容。",
    "suggested_actions": [],
}
SUMMARY_REPLY = {"summary": "負載測試的假摘要。"}

logger = logging.getLogger("fake_llm")


class FakeLlmConfigError(ValueError):
    """設定不合法。訊息只含變數名稱與規則，不含值。"""


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
            raise FakeLlmConfigError(f"LOADTEST_ERROR_RATES 的錯誤類別必須是 {'、'.join(ERROR_KINDS)}")
        try:
            rate = float(number)
        except ValueError:
            raise FakeLlmConfigError("LOADTEST_ERROR_RATES 的比率必須是 0 到 1 之間的數字") from None
        if not 0.0 <= rate <= 1.0:
            raise FakeLlmConfigError("LOADTEST_ERROR_RATES 的比率必須是 0 到 1 之間的數字")
        rates[kind] = rate
    if sum(rates.values()) > 1.0:
        raise FakeLlmConfigError("LOADTEST_ERROR_RATES 的比率總和不得超過 1")
    return rates


def _number(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = (environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise FakeLlmConfigError(f"{name} 必須是數字") from None
    if not 0.0 <= value < float("inf"):
        raise FakeLlmConfigError(f"{name} 必須是不小於 0 的數字")
    return value


def _test_key(environ: Mapping[str, str]) -> str:
    key = (environ.get("FAKE_LLM_API_KEY") or "").strip()
    if not key:
        raise FakeLlmConfigError("缺少 FAKE_LLM_API_KEY：假模型伺服器只接受部署時設定的測試金鑰")
    if len(key) < MIN_KEY_LENGTH or key.startswith(REAL_KEY_PREFIXES):
        raise FakeLlmConfigError(f"FAKE_LLM_API_KEY 必須是自訂的測試金鑰（至少 {MIN_KEY_LENGTH} 字元，且不得是真實供應商的金鑰）")
    return key


class FakeModel:
    """依設定產生延遲與錯誤，並累計各狀態碼的次數（彙總日誌用）。執行緒安全：伺服器是多執行緒的。"""

    def __init__(
        self, *, latency: float, jitter: float, error_rates: Mapping[str, float], timeout_seconds: float,
        seed: int | None, sleep: Callable[[float], None],
    ) -> None:
        self.latency, self.jitter, self.timeout_seconds = latency, jitter, timeout_seconds
        self.sleep = sleep
        self._rates = dict(error_rates)
        self._random = random.Random(seed)
        self._lock = threading.Lock()
        self.draws = 0
        self.requests = 0
        self.statuses: dict[int, int] = {}

    def draw(self) -> tuple[float, str | None]:
        with self._lock:
            self.draws += 1
            delay = max(0.0, self.latency + self._random.uniform(-self.jitter, self.jitter))
            roll = self._random.random()
            cumulative = 0.0
            for kind in ERROR_KINDS:
                cumulative += self._rates.get(kind, 0.0)
                if roll < cumulative:
                    return delay, kind
        return delay, None

    def record(self, status: int) -> None:
        with self._lock:
            self.requests += 1
            self.statuses[status] = self.statuses.get(status, 0) + 1
            due = self.requests % SUMMARY_EVERY == 0
        if due:
            self.log_summary()

    def log_summary(self) -> None:
        """日誌只有請求數與各狀態碼的彙總，沒有任何請求或回覆的內容。"""
        with self._lock:
            line = {"event": "fake_llm.summary", "requests": self.requests, "status": {str(code): n for code, n in sorted(self.statuses.items())}}
        logger.info(json.dumps(line, ensure_ascii=False))


def _system_text(body: object) -> str:
    """請求的系統指令文字（只用來判斷是不是摘要呼叫，不保存、不記錄）。"""
    if not isinstance(body, dict):
        return ""
    parts = (body.get("system_instruction") or body.get("systemInstruction") or {}).get("parts") or []
    return "".join(part.get("text", "") for part in parts if isinstance(part, dict))


def _error(status: int):
    return jsonify({"error": {"code": status, "status": ERROR_STATUS_TEXT[status]}}), status


def create_fake_llm_app(environ: Mapping[str, str] | None = None, *, sleep: Callable[[float], None] = time.sleep) -> Flask:
    """依環境變數建立應用程式；設定不合法就拋出 `FakeLlmConfigError`（啟動失敗）。`sleep` 可注入，測試不必真的等。"""
    environ = os.environ if environ is None else environ
    api_key = _test_key(environ)
    seed_raw = (environ.get("LOADTEST_SEED") or "").strip()
    try:
        seed = int(seed_raw) if seed_raw else None
    except ValueError:
        raise FakeLlmConfigError("LOADTEST_SEED 必須是整數") from None
    model = FakeModel(
        latency=_number(environ, "LOADTEST_LATENCY_SECONDS", DEFAULT_LATENCY_SECONDS),
        jitter=_number(environ, "LOADTEST_JITTER_SECONDS", DEFAULT_JITTER_SECONDS),
        error_rates=parse_error_rates(environ.get("LOADTEST_ERROR_RATES")),
        timeout_seconds=_number(environ, "LOADTEST_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
        seed=seed,
        sleep=sleep,
    )

    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)      # 開發伺服器的逐請求日誌不是彙總，關掉

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES
    app.json.ensure_ascii = False
    app.extensions["fake_llm"] = model

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.post("/v1beta/models/<model_action>")
    def generate_content(model_action: str):
        if not model_action.endswith(":generateContent"):
            return "", 404
        supplied = request.headers.get(KEY_HEADER, "")
        if not hmac.compare_digest(supplied.encode("utf-8"), api_key.encode("utf-8")):
            model.record(403)
            return jsonify({"error": {"code": 403, "status": "PERMISSION_DENIED"}}), 403         # 不處理請求，也不消耗亂數
        summary_call = SUMMARY_MARKER in _system_text(request.get_json(silent=True))
        delay, kind = model.draw()
        if kind == "timeout":
            model.sleep(model.timeout_seconds)       # 和真實供應商一樣佔住連線；客戶端通常已經先逾時離開
            model.record(HTTP_STATUS[kind])
            return _error(HTTP_STATUS[kind])
        model.sleep(delay)
        if kind is not None:
            model.record(HTTP_STATUS[kind])
            return _error(HTTP_STATUS[kind])
        model.record(200)
        return {
            "candidates": [{
                "content": {"role": "model", "parts": [{"text": json.dumps(SUMMARY_REPLY if summary_call else REPLY, ensure_ascii=False)}]},
                "finishReason": "STOP",
            }],
            "usageMetadata": {
                "promptTokenCount": PROMPT_TOKENS, "candidatesTokenCount": COMPLETION_TOKENS, "totalTokenCount": PROMPT_TOKENS + COMPLETION_TOKENS,
            },
        }

    return app
