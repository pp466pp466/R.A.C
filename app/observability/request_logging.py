"""請求識別碼與請求日誌。

- 上游提供的 `X-Request-ID` 格式合法才沿用，否則重新產生；識別碼一律回傳於回應標頭。
- 請求日誌只記錄白名單欄位：路徑（不含查詢字串）、方法、狀態碼、延遲。
  路徑優先記錄「路由規則」（例如 /verify/<token>），避免把路徑中的權杖寫進日誌。
"""
from __future__ import annotations

import logging
import re
import time
import uuid

from flask import Flask, Response, g, request

logger = logging.getLogger("app.request")

REQUEST_ID_HEADER = "X-Request-ID"
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def _resolve_request_id(incoming: str | None) -> str:
    if incoming and REQUEST_ID_PATTERN.fullmatch(incoming):
        return incoming
    return uuid.uuid4().hex


def _loggable_path() -> str:
    rule = request.url_rule
    if rule is not None:
        return rule.rule
    return request.path[:200]  # 未比對到路由（例如 404）：只取路徑，不含查詢字串


def init_request_logging(app: Flask) -> None:
    @app.before_request
    def _begin_request() -> None:
        g.request_id = _resolve_request_id(request.headers.get(REQUEST_ID_HEADER))
        g.request_started = time.perf_counter()

    @app.after_request
    def _finish_request(response: Response) -> Response:
        request_id = g.get("request_id")
        if request_id:
            response.headers[REQUEST_ID_HEADER] = request_id
        started = g.get("request_started")
        logger.info(
            "請求完成",
            extra={
                "path": _loggable_path(),
                "method": request.method,
                "status": response.status_code,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1) if started else None,
            },
        )
        return response
