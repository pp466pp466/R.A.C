"""管理員二階段登入的中間狀態 cookie（見 admin-two-step-login 規格與 google-first-login 的 design D7）。

管理員每次登入都先通過 Google，再通過密碼，兩者都通過才建立登入 session。通過 Google 之後、輸入密碼之前，
瀏覽器持有的就是這個簽章的短效 cookie：

- 內容只有使用者編號 `uid`、`token_version`（`tv`）與登入後要去的位置 `x`（發起時已驗證過）；用途專屬的 salt、
  10 分鐘有效、HttpOnly、`SameSite=Lax`，密碼驗證成功或失敗的帳號異動（`token_version` 加 1）都會使它失效。
- **不是登入狀態**：`current_user()` 完全不認得它，所以所有需要登入的端點對只持有它的請求都是 401。
  只有 `POST /api/auth/admin-password`、`POST /api/auth/admin-password/forgot` 與 `/login/password` 讀取它。
"""
from __future__ import annotations

from dataclasses import dataclass

from flask import Response, current_app, request

from app.runtime import get_config
from app.security.cookies import SignedCookies, cookie_name

ADMIN_STEP_COOKIE = "admin_step"
ADMIN_STEP_SALT = "admin.login.step.v1"
ADMIN_STEP_TTL_SECONDS = 600


@dataclass(frozen=True)
class AdminStep:
    user_id: int
    token_version: int
    next_url: str


def _cookies() -> SignedCookies:
    return current_app.extensions["signed_cookies"]


def store_admin_step(response: Response, *, user_id: int, token_version: int, next_url: str) -> None:
    payload = {"uid": user_id, "tv": token_version, "x": next_url}
    _cookies().set(response, ADMIN_STEP_COOKIE, _cookies().dumps(ADMIN_STEP_SALT, payload), max_age_seconds=ADMIN_STEP_TTL_SECONDS)


def read_admin_step() -> AdminStep | None:
    """目前瀏覽器的中間狀態；沒有、被竄改、用途不符、形狀不對或超過 10 分鐘都回傳 None。"""
    value = request.cookies.get(cookie_name(get_config(), ADMIN_STEP_COOKIE))
    payload = _cookies().loads(ADMIN_STEP_SALT, value, max_age_seconds=ADMIN_STEP_TTL_SECONDS)
    if not isinstance(payload, dict):
        return None
    uid, tv, nxt = payload.get("uid"), payload.get("tv"), payload.get("x")
    if type(uid) is not int or type(tv) is not int or not isinstance(nxt, str):   # bool 是 int 的子類別，不能當作編號
        return None
    return AdminStep(user_id=uid, token_version=tv, next_url=nxt)


def clear_admin_step(response: Response) -> None:
    _cookies().delete(response, ADMIN_STEP_COOKIE)
