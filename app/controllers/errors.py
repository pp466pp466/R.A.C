"""統一錯誤處理：繁體中文訊息，不含堆疊追蹤、檔案路徑、SQL 或機密。

- `/api/` 底下的錯誤一律回 JSON：{"error": {"code": ..., "message": ...}}
- 其他路徑回簡潔的繁體中文錯誤頁
"""
from __future__ import annotations

import logging

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from app.observability.errors import describe_exception
from app.security.errors import HttpSecurityError
from app.services.errors import (
    Conflict, Forbidden, NotFound, ServiceError, TooManyRequests, Unauthenticated, ValidationError,
)

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
    415: ("unsupported_media_type", "請以 JSON 格式送出內容。"),
    429: ("too_many_requests", "操作太頻繁，請稍後再試。"),
    500: ("internal_error", "系統發生錯誤，請稍後再試。"),
    503: ("service_unavailable", "服務暫時無法使用，請稍後再試。"),
}
_DEFAULT = ("error", "發生錯誤，請稍後再試。")

# Service 的業務錯誤 → HTTP 狀態碼（Service 不知道 HTTP，對應在這裡統一處理）
_SERVICE_ERROR_STATUS: tuple[tuple[type[ServiceError], int], ...] = (
    (ValidationError, 400),
    (Unauthenticated, 401),
    (Forbidden, 403),
    (NotFound, 404),
    (Conflict, 409),
    (TooManyRequests, 429),
)


def _wants_json() -> bool:
    return request.path == "/api" or request.path.startswith("/api/")


def respond_with_error(status: int, code: str, message: str, fields: dict[str, str] | None = None):
    """依路徑回覆 JSON（`/api/` 底下）或錯誤頁。其他模組（例如 CSRF 檢查）也用它，確保錯誤格式一致。"""
    if _wants_json():
        body: dict[str, object] = {"code": code, "message": message}
        if fields:
            body["fields"] = fields
        response = jsonify(error=body)
        response.status_code = status
        return response
    return render_template("error.html", status=status, message=message), status


def _respond(status: int):
    code, message = _ERRORS.get(status, _DEFAULT)
    return respond_with_error(status, code, message)


def _status_for(exc: ServiceError) -> int:
    for error_type, status in _SERVICE_ERROR_STATUS:
        if isinstance(exc, error_type):
            return status
    return 400


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(HTTPException)
    def _http_error(exc: HTTPException):
        return _respond(exc.code or 500)

    @app.errorhandler(HttpSecurityError)
    def _security_error(exc: HttpSecurityError):
        return respond_with_error(exc.status, exc.code, exc.message)

    @app.errorhandler(ServiceError)
    def _service_error(exc: ServiceError):
        fields = exc.fields if isinstance(exc, ValidationError) else None
        return respond_with_error(_status_for(exc), exc.code, exc.message, fields)

    @app.errorhandler(Exception)
    def _unexpected_error(exc: Exception):
        # 只記錄類型與位置；訊息可能含機密，絕不寫入日誌或回應
        logger.error("未預期的例外", extra=describe_exception(exc))
        return _respond(500)
