"""結構化 JSON 日誌（見 AGENTS.md 第 11 節與 service-observability 規格）。

設計原則：以「白名單」決定能寫什麼，而不是試圖濾掉壞東西——
- 只輸出固定的基本欄位，加上白名單內的 extra 欄位；其餘 extra 一律丟棄。
- 例外只輸出類型與位置，絕不輸出例外訊息或堆疊（訊息可能夾帶機密）。
- 訊息文字再做一層遮罩（密碼、權杖、連線字串中的帳密），作為縱深防禦。
絕不記錄：密碼、Cookie、CSRF 與驗證權杖、授權標頭、完整 API 金鑰、email、使用者訊息全文、模型回覆。
"""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any

from flask import g, has_request_context

from app.observability.errors import describe_exception

# 允許出現在日誌中的 extra 欄位（白名單）
ALLOWED_FIELDS = (
    "request_id",
    "path",
    "method",
    "status",
    "latency_ms",
    "user_id",
    "conversation_id",
    "intent",
    "model",
    "attempt",
    "prompt_tokens",
    "completion_tokens",
    "finish_reason",
    "error_type",
    "error_location",
    "event",  # 認證與安全事件名稱（固定列舉，見 security_events.py）、模型呼叫事件（見 llm_events.py）
    # 模型呼叫（llm_events.py）：全是分類與數字，函式沒有接收任何文字內容的參數
    "kind",
    "provider",
    "outcome",
    "thinking_tokens",
    "reason",
    "provider_status",
    # 用量事件（usage_events.py）：只有整數，沒有任何文字內容
    "used",
    "limit",
    # 期限清除（lifecycle_events.py）與營運告警（alert_events.py）：筆數或數值（整數）
    "count",
    # 營運告警的嚴重度（固定列舉：warning、critical）
    "severity",
)

MAX_TEXT_LENGTH = 300

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # 連線字串中的帳密：scheme://user:password@host
    (re.compile(r"(://[^/\s:@]+):[^@\s]+@"), r"\1:***@"),
    # password=xxx、token: xxx、api_key=xxx、authorization: Basic xxx（授權方案連同憑證一起遮掉）
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|authorization|cookie)\b"
            r"\s*[=:]\s*(?:(?:basic|bearer|digest)\s+)?\S+"
        ),
        r"\1=***",
    ),
    # Bearer 權杖
    (re.compile(r"(?i)\bbearer\s+\S+"), "Bearer ***"),
)


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def _clean(value: Any) -> Any:
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return redact(str(value))[:MAX_TEXT_LENGTH]


class JsonFormatter(logging.Formatter):
    """每筆日誌輸出為單行 JSON，欄位限於白名單。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage())[:MAX_TEXT_LENGTH],
        }
        for name in ALLOWED_FIELDS:
            value = getattr(record, name, None)
            if value is not None:
                payload[name] = _clean(value)
        # logger.exception() 等帶有例外資訊的紀錄：只取類型與位置
        if record.exc_info and record.exc_info[1] is not None and "error_type" not in payload:
            payload.update(describe_exception(record.exc_info[1]))
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


class RequestContextFilter(logging.Filter):
    """在請求期間，自動為每筆日誌補上請求識別碼與（若已登入的）內部使用者編號。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if has_request_context():
            if getattr(record, "request_id", None) is None:
                record.request_id = g.get("request_id")
            if getattr(record, "user_id", None) is None:
                record.user_id = g.get("user_id")
        return True


_HANDLER_MARK = "_app_json_handler"


def configure_logging(level: str = "INFO") -> None:
    """設定根記錄器輸出 JSON 到標準輸出；可重複呼叫（不會產生重複的處理器）。"""
    root = logging.getLogger()
    root.setLevel(level)
    if not any(getattr(h, _HANDLER_MARK, False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        handler.addFilter(RequestContextFilter())
        setattr(handler, _HANDLER_MARK, True)
        root.addHandler(handler)
    # 開發伺服器會把「含查詢字串的完整網址」寫進日誌，改由我們自己的請求日誌取代
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
