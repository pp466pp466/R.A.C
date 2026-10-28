"""Google 登入的交易 cookie：把 state、nonce、PKCE 驗證碼綁定在發起流程的那個瀏覽器上。

- 簽章（`SECRET_KEY`）、HttpOnly、`SameSite=Lax`（回呼是從 Google 回來的頂層 GET 導覽，Lax 會帶上它）、
  10 分鐘有效、只供這個流程使用（獨立的 salt）。
- 回呼結束時（成功或失敗）一律刪除，因此同一個回呼網址第二次使用會因為沒有綁定而失敗。
- 內容含 PKCE 驗證碼：只存在於這個 cookie，絕不回傳給前端的 JSON。
"""
from __future__ import annotations

from typing import Any

from flask import Response, current_app, request

from app.runtime import get_config
from app.security.cookies import SignedCookies, cookie_name

OAUTH_COOKIE = "oauth_tx"
OAUTH_SALT = "oauth.google.v1"
OAUTH_TTL_SECONDS = 600


def _cookies() -> SignedCookies:
    return current_app.extensions["signed_cookies"]


def store_transaction(response: Response, transaction: dict[str, Any]) -> None:
    _cookies().set(response, OAUTH_COOKIE, _cookies().dumps(OAUTH_SALT, transaction), max_age_seconds=OAUTH_TTL_SECONDS)


def read_transaction() -> dict[str, Any] | None:
    """目前瀏覽器的交易內容；沒有、被竄改、用途不符或超過 10 分鐘都回傳 None。"""
    value = request.cookies.get(cookie_name(get_config(), OAUTH_COOKIE))
    payload = _cookies().loads(OAUTH_SALT, value, max_age_seconds=OAUTH_TTL_SECONDS)
    return payload if isinstance(payload, dict) else None


def clear_transaction(response: Response) -> None:
    _cookies().delete(response, OAUTH_COOKIE)
