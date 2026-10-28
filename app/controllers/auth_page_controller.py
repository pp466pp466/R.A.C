"""會員相關的 MVC Page Controller：只渲染頁面外殼與初始狀態，回應是 HTML 或重新導向（見 AGENTS.md 第 3 節）。

頁面：/login、/register、/forgot-password、/reset-password、/verify-email、/account、/terms、/privacy，
以及 Google 登入的回呼 /auth/google/callback（它是從 Google 回來的瀏覽器導覽，結果是重新導向，不是 JSON）。
所有資料操作都在 /api/auth/* 完成；頁面只帶「初始狀態」：以 JSON 區塊（tojson 已跳脫）傳給前端，不以字串拼進腳本。
`next` 的安全檢查在伺服器端完成，前端只拿到已驗證的值。
"""
from flask import Blueprint, Response, abort, redirect, render_template, request

from app.runtime import get_config, get_identity_provider
from app.security.oauth_cookie import clear_transaction, read_transaction
from app.security.redirects import DEFAULT_AFTER_LOGIN, safe_next
from app.security.session import current_user, login_required_page, login_user
from app.services.google_sign_in_service import (
    ERROR_AGE, ERROR_DISABLED, ERROR_REJECTED, ERROR_STATE, ERROR_TERMS, ERROR_UNAVAILABLE, GoogleLoginRefused,
)
from app.services.legal import CURRENT_TERMS_VERSION
from app.services.registry import get_auth_service, get_google_sign_in_service

auth_pages_bp = Blueprint("auth_pages", __name__)

# /login?error=<代碼> 的說明文字：代碼是固定列舉，文字都在這裡，外部提供的任何文字都不會進入頁面
LOGIN_ERROR_MESSAGES = {
    ERROR_STATE: "Google 登入的驗證已逾時或不完整，請再試一次。",
    ERROR_UNAVAILABLE: "Google 登入暫時無法使用，請稍後再試，或改用信箱與密碼登入。",
    ERROR_REJECTED: "Google 登入沒有完成或被拒絕，請再試一次。",
    ERROR_TERMS: "第一次使用 Google 註冊時，請先勾選同意服務條款與隱私權政策。",
    ERROR_AGE: "請先確認您已年滿 18 歲。",
    ERROR_DISABLED: "此帳號已停用，無法登入。",
}


def _state(**extra) -> dict:
    """所有會員頁面共同的初始狀態。"""
    return {
        "googleEnabled": get_identity_provider() is not None,
        "verificationRequired": get_config().email_verification_required,
        "termsVersion": CURRENT_TERMS_VERSION,
        **extra,
    }


def _require_email_flows() -> None:
    if not get_config().email_verification_required:
        abort(404)


# ---- 登入與註冊 -------------------------------------------------------------

@auth_pages_bp.get("/login")
def login_page():
    if current_user() is not None:
        return redirect("/account")
    notice = LOGIN_ERROR_MESSAGES.get(request.args.get("error", ""))
    return render_template("auth/login.html", initial_state=_state(next=safe_next(request.args.get("next")), notice=notice))


@auth_pages_bp.get("/register")
def register_page():
    if current_user() is not None:
        return redirect("/account")
    return render_template("auth/register.html", initial_state=_state(next=safe_next(request.args.get("next"))))


# ---- 信箱驗證與重設密碼（信箱驗證關閉時整組 404）-----------------------------------
# 這幾頁的 GET 只顯示確認頁：權杖在網址片段（#token=…），伺服器看不到，因此頁面本身不含權杖。

@auth_pages_bp.get("/forgot-password")
def forgot_password_page():
    _require_email_flows()
    return render_template("auth/forgot_password.html", initial_state=_state())


@auth_pages_bp.get("/reset-password")
def reset_password_page():
    _require_email_flows()
    return render_template("auth/reset_password.html", initial_state=_state())


@auth_pages_bp.get("/verify-email")
def verify_email_page():
    _require_email_flows()
    return render_template("auth/verify_email.html", initial_state=_state())


# ---- 帳號設定 -----------------------------------------------------------------

@auth_pages_bp.get("/account")
@login_required_page
def account_page():
    user = current_user()
    return render_template("auth/account.html", initial_state=_state(user=get_auth_service().profile(user)))


# ---- 服務條款與隱私權政策（不需登入）---------------------------------------------

@auth_pages_bp.get("/terms")
def terms_page():
    return render_template("legal/terms.html", terms_version=CURRENT_TERMS_VERSION)


@auth_pages_bp.get("/privacy")
def privacy_page():
    return render_template("legal/privacy.html", terms_version=CURRENT_TERMS_VERSION)


# ---- Google 登入回呼 ----------------------------------------------------------

@auth_pages_bp.get("/auth/google/callback")
def google_callback() -> Response:
    if get_identity_provider() is None:
        abort(404)
    transaction = read_transaction()
    try:
        result = get_google_sign_in_service().complete(
            code=request.args.get("code"),
            state=request.args.get("state"),
            provider_error=request.args.get("error"),
            transaction=transaction,
        )
    except GoogleLoginRefused as refused:
        # 錯誤代碼是固定列舉；Google 回傳的任何文字都不會進入網址或頁面
        response = redirect(f"/login?error={refused.code}")
        clear_transaction(response)
        return response
    login_user(result.user)
    destination = safe_next((transaction or {}).get("x"), DEFAULT_AFTER_LOGIN)
    response = redirect(destination)
    clear_transaction(response)
    return response
