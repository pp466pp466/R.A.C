"""CSRF 防護（見 AGENTS.md 第 10 節與 csrf-protection 規格）。

做法：「簽章的雙重提交 cookie」加來源（Origin）檢查。
- 權杖是伺服器以 `SECRET_KEY` 簽章的隨機值，放在獨立的 cookie（HttpOnly，JavaScript 讀不到），
  同一個值由頁面的 `<meta name="csrf-token">` 提供給前端；非 GET 請求必須在 `X-CSRF-Token` 標頭帶回，
  且與 cookie 完全一致、簽章有效。攻擊者既拿不到頁面上的值，也無法讓瀏覽器帶上他自己編造的、有簽章的 cookie。
- 另外驗證 `Origin`（缺少時用 `Referer`）必須與本站一致；兩者皆缺、為 `null` 或不符一律拒絕。
- **沒有任何端點可以豁免**，包含登入與註冊。權杖只接受標頭，不接受網址參數或表單欄位。
- 為什麼不放進登入 cookie：登入 cookie 內容只含使用者編號與 token_version（AGENTS.md 第 10 節），
  而登入頁在尚未登入時也需要權杖。
- 登入與登出時換發新權杖（`rotate_csrf_token`），登入前取得的權杖隨之失效。
"""
from __future__ import annotations

import hmac
import secrets
from urllib.parse import urlsplit

from flask import Flask, Response, current_app, g, request

from app.observability import security_events
from app.runtime import get_config
from app.security.cookies import SignedCookies, cookie_name
from app.security.errors import HttpSecurityError

CSRF_COOKIE = "csrf"
CSRF_SALT = "csrf.v1"
CSRF_HEADER = "X-CSRF-Token"
CSRF_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
FAILURE_MESSAGE = "頁面已過期或請求來源不正確，請重新整理頁面後再試。"

_PENDING_KEY = "csrf_pending_cookie"


def _cookies() -> SignedCookies:
    return current_app.extensions["signed_cookies"]


def _cookie_value() -> str | None:
    return request.cookies.get(cookie_name(get_config(), CSRF_COOKIE))


def _is_valid(value: str | None) -> bool:
    return _cookies().loads(CSRF_SALT, value, max_age_seconds=CSRF_MAX_AGE_SECONDS) is not None


def rotate_csrf_token() -> str:
    """換發新的權杖（回應會帶上新的 cookie）。登入與登出時呼叫，使舊權杖失效。"""
    token = _cookies().dumps(CSRF_SALT, secrets.token_urlsafe(32))
    setattr(g, _PENDING_KEY, token)
    return token


def get_csrf_token() -> str:
    """目前有效的權杖（給頁面的 `<meta>` 使用）。瀏覽器還沒有有效的權杖時才發放新的。"""
    pending = g.get(_PENDING_KEY)
    if pending:
        return pending
    current = _cookie_value()
    if current and _is_valid(current):
        return current
    return rotate_csrf_token()


def _origin_of(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


def _request_origin() -> str | None:
    origin = request.headers.get("Origin")
    if origin is not None:
        return None if origin == "null" else _origin_of(origin)
    referer = request.headers.get("Referer")
    return _origin_of(referer) if referer else None


def _reject() -> None:
    security_events.log_event(security_events.CSRF_REJECTED)
    raise HttpSecurityError(403, "csrf_failed", FAILURE_MESSAGE)


def check_request() -> None:
    """非 GET 請求的 CSRF 檢查；不通過就拋出 `HttpSecurityError`（403）。"""
    if request.method in SAFE_METHODS:
        return
    expected = _origin_of(request.host_url)   # staging 與 production 經 ProxyFix，是對外的 https 網址
    actual = _request_origin()
    if expected is None or actual is None or actual != expected:
        _reject()
    header = request.headers.get(CSRF_HEADER, "")
    cookie = _cookie_value() or ""
    if not header or not cookie or not hmac.compare_digest(header.encode("utf-8"), cookie.encode("utf-8")):
        _reject()
    if not _is_valid(cookie):
        _reject()


def init_csrf(app: Flask) -> None:
    """掛上檢查與 cookie 發放。必須在 `init_session` 之後呼叫（拒絕時要能被請求日誌記到最終狀態）。"""
    app.before_request(check_request)

    @app.after_request
    def _issue_cookie(response: Response) -> Response:
        pending = g.get(_PENDING_KEY)
        if pending:
            _cookies().set(response, CSRF_COOKIE, pending)
            response.vary.add("Cookie")
        return response

    @app.context_processor
    def _inject() -> dict:
        return {"csrf_token": get_csrf_token}
