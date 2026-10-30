"""登入狀態（見 AGENTS.md 第 10 節與 session-authentication 規格）。

- 登入 cookie 是 Flask 的簽章 session，**內容只有 `uid`（使用者內部編號）與 `tv`（token_version）**，不使用 JWT。
  存活 7 天，自登入時起算、不因使用而延長；到期時間固定寫在 cookie 上，不需要 Flask 的 `_permanent` 旗標。
- `current_user()` 是惰性的：只有端點或 `login_required_*` 呼叫時才讀資料庫，因此 `/healthz` 與靜態檔
  不會因為帶著 cookie 就去碰資料庫。每次辨識都比對 `token_version` 與帳號狀態，這是可以撤銷登入的代價。
- 登入時一律先清空 session 再寫入（不沿用登入前的任何內容），並換發 CSRF 權杖。
"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable, TypeVar
from urllib.parse import quote

from flask import Flask, Response, current_app, g, redirect, request, session
from flask.sessions import SecureCookieSessionInterface, SessionMixin

from app.security.csrf import rotate_csrf_token
from app.services.current_user import CurrentUser
from app.services.errors import AccountDeletionPending, Unauthenticated
from app.services.registry import get_auth_service

SESSION_USER_KEY = "uid"
SESSION_VERSION_KEY = "tv"
LOGIN_PATH = "/login"

_CACHE_KEY = "current_user_resolved"
_UNSET = object()

ViewT = TypeVar("ViewT", bound=Callable[..., Any])


class AbsoluteExpirySessionInterface(SecureCookieSessionInterface):
    """到期時間一律是「寫入 cookie 的當下加 7 天」，不依賴 session 的 permanent 旗標。

    Flask 預設只有 permanent 的 session 才有到期時間，而 permanent 會在 cookie 內多寫一個 `_permanent`；
    規格要求內容只含使用者編號與 token_version，所以改由這裡固定到期時間。
    """

    def get_expiration_time(self, app: Flask, session: SessionMixin) -> datetime | None:
        return datetime.now(timezone.utc) + app.permanent_session_lifetime


def init_login_session(app: Flask) -> None:
    app.session_interface = AbsoluteExpirySessionInterface()


# ---- 讀取 -------------------------------------------------------------------

def _cookie_identity() -> tuple[int, int] | None:
    uid = session.get(SESSION_USER_KEY)
    version = session.get(SESSION_VERSION_KEY)
    # bool 是 int 的子類別，True 不能當作使用者編號
    if type(uid) is not int or type(version) is not int:
        return None
    return uid, version


def current_user() -> CurrentUser | None:
    """目前登入的使用者；未登入、cookie 無效或已被撤銷（token_version 不符、帳號被停用）時為 None。

    同一個請求內只查一次。無效的 cookie 會被清除。
    """
    cached = g.get(_CACHE_KEY, _UNSET)
    if cached is not _UNSET:
        return cached
    user: CurrentUser | None = None
    identity = _cookie_identity()
    if identity is not None:
        user = get_auth_service().resolve_session(*identity)
    if user is None and (session or request.cookies.get(current_app.config["SESSION_COOKIE_NAME"])):
        # 無效、被竄改（Flask 會給空的 session）或已撤銷：清掉，瀏覽器就不會再帶著它。
        # 空的 session 要明確標記為已修改，Flask 才會送出刪除 cookie 的標頭。
        session.clear()
        session.modified = True
    if user is not None:
        g.user_id = user.id      # 供請求日誌使用（內部整數編號）
    setattr(g, _CACHE_KEY, user)
    return user


def require_login(*, allow_pending_deletion: bool = False) -> CurrentUser:
    """已登入就回傳使用者，否則拋出 `Unauthenticated`（API 回 401 JSON）。

    **受限登入狀態預設拒絕**：帳號正在等待刪除（寬限期內）的人可以登入，但只為了撤銷刪除。所以所有透過這裡取得使用者的端點
    對這類使用者一律回 403 `account_deletion_pending`，只有明確傳入 `allow_pending_deletion=True` 的端點例外
    （查詢自己、撤銷刪除）。新增的端點不必記得檢查，漏掉也是安全的一邊；`tests/test_restricted_session.py` 掃描全部路由證明這一點。
    """
    user = current_user()
    if user is None:
        raise Unauthenticated()
    if user.awaiting_deletion and not allow_pending_deletion:
        raise AccountDeletionPending()
    return user


# ---- 寫入 -------------------------------------------------------------------

def login_user(user: CurrentUser) -> None:
    """建立登入狀態：清空登入前的 session 內容、寫入新的 uid 與 tv、換發 CSRF 權杖。"""
    session.clear()
    session[SESSION_USER_KEY] = user.id
    session[SESSION_VERSION_KEY] = user.token_version
    g.user_id = user.id
    setattr(g, _CACHE_KEY, user)
    rotate_csrf_token()


def renew_login_session(user: CurrentUser) -> None:
    """已登入者的 token_version 變了（例如變更密碼）：讓目前裝置以新版本維持登入。

    與 `login_user` 不同，這裡**不**清空 session、也**不**換發 CSRF 權杖：使用者沒有重新登入，
    頁面也沒有重新載入，頁面上的權杖若被換掉，之後的請求都會被擋成 403。
    """
    session[SESSION_USER_KEY] = user.id
    session[SESSION_VERSION_KEY] = user.token_version
    g.user_id = user.id
    setattr(g, _CACHE_KEY, user)


def logout_user() -> None:
    """清除目前裝置的登入狀態並換發 CSRF 權杖；其他裝置不受影響。"""
    session.clear()
    setattr(g, _CACHE_KEY, None)
    g.pop("user_id", None)
    rotate_csrf_token()


# ---- 裝飾器 -----------------------------------------------------------------

def login_required_api(view: ViewT) -> ViewT:
    """API 端點：未登入回 401（JSON）。"""

    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        require_login()
        return view(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


ACCOUNT_PATH = "/account"


def login_required_page(view: ViewT | None = None, *, allow_pending_deletion: bool = False):
    """頁面端點：未登入導向登入頁，並以 `next` 保留原本要去的站內位置（登入後由伺服器再驗證一次）。

    與 `require_login` 相同，**受限登入狀態（等待刪除的帳號）預設被導向 `/account`**，只有 `/account` 自己明確允許。
    可直接當裝飾器用（`@login_required_page`），也可帶參數（`@login_required_page(allow_pending_deletion=True)`）。
    """

    def decorate(inner: ViewT) -> ViewT:
        @wraps(inner)
        def wrapper(*args: Any, **kwargs: Any):
            user = current_user()
            if user is None:
                target = request.path
                if request.query_string:
                    target += "?" + request.query_string.decode("utf-8", "replace")
                response: Response = redirect(f"{LOGIN_PATH}?next={quote(target, safe='')}")
                return response
            if user.awaiting_deletion and not allow_pending_deletion:
                return redirect(ACCOUNT_PATH)
            return inner(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorate if view is None else decorate(view)
