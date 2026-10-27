"""統一錯誤處理：繁體中文訊息，不含堆疊追蹤、檔案路徑、SQL 或機密。

- `/api/` 底下的錯誤一律回 JSON：{"error": {"code": ..., "message": ...}}
- 其他路徑回簡潔的繁體中文錯誤頁
"""
from __future__ import annotations

import logging

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from app.observability.errors import describe_exception

logger = logging.getLogger(__name__)

# 狀態碼 → (機器可讀代碼, 繁體中文訊息)
_ERRORS: dict[int, tuple[str, str]] = {
    400: ("bad_request", "請求內容不正確。"),
    401: ("unauthorized", "請先登入。"),
    403: ("forbidden", "沒有權限執行這個操作。"),
    404: ("not_found", "找不到您要的內容。"),
    405: ("method_not_allowed", "不支援這個操作。"),
    409: ("conflict", "目前無法處理，請稍後再試。"),
    413: ("payload_too_large", "送出的內容太大。"),
    429: ("too_many_requests", "操作太頻繁，請稍後再試。"),
    500: ("internal_error", "系統發生錯誤，請稍後再試。"),
    503: ("service_unavailable", "服務暫時無法使用，請稍後再試。"),
}
_DEFAULT = ("error", "發生錯誤，請稍後再試。")


def _wants_json() -> bool:
    return request.path == "/api" or request.path.startswith("/api/")


def _respond(status: int):
    code, message = _ERRORS.get(status, _DEFAULT)
    if _wants_json():
        response = jsonify(error={"code": code, "message": message})
        response.status_code = status
        return response
    return render_template("error.html", status=status, message=message), status


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(HTTPException)
    def _http_error(exc: HTTPException):
        return _respond(exc.code or 500)

    @app.errorhandler(Exception)
    def _unexpected_error(exc: Exception):
        # 只記錄類型與位置；訊息可能含機密，絕不寫入日誌或回應
        logger.error("未預期的例外", extra=describe_exception(exc))
        return _respond(500)
