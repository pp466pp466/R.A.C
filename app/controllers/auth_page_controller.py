"""會員相關的 MVC Page Controller：只渲染頁面外殼與初始狀態，回應是 HTML 或重新導向（見 AGENTS.md 第 3 節）。

頁面：/login、/login/password、/age、/appeal、/forgot-password、/reset-password、/verify-email、/account、/terms、/privacy（/register 只作導向），
以及 Google 登入的回呼 /auth/google/callback（它是從 Google 回來的瀏覽器導覽，結果是重新導向，不是 JSON）。
所有資料操作都在 /api/auth/* 完成；頁面只帶「初始狀態」：以 JSON 區塊（tojson 已跳脫）傳給前端，不以字串拼進腳本。
`next` 的安全檢查在伺服器端完成，前端只拿到已驗證的值。
"""
from flask import Blueprint, Response, abort, redirect, render_template, request

from app.observability import security_events
from app.runtime import get_age_policy, get_clock, get_identity_provider
from app.security.admin_step import read_admin_step, store_admin_step
from app.security.oauth_cookie import clear_transaction, read_transaction
from app.security.redirects import landing_for, safe_next
from app.security.session import current_user, login_required_page, login_user
from app.services.age_policy import EARLIEST_BIRTH_DATE, taipei_today
from app.services.appeal_service import APPEAL_HOURLY_LIMIT, MAX_APPEAL_CHARS
from app.services.current_user import RESTRICTION_AGE_PENDING, RESTRICTION_LOCKED
from app.services.google_sign_in_service import (
    ERROR_DISABLED, ERROR_REJECTED, ERROR_STATE, ERROR_TERMS, ERROR_UNAVAILABLE, GoogleLoginRefused,
)
from app.services.account_deletion_service import CONFIRM_TEXT
from app.services.legal import CURRENT_TERMS_VERSION
from app.services.registry import admin_password_reset_available, get_auth_service, get_google_sign_in_service

auth_pages_bp = Blueprint("auth_pages", __name__)

# /login?error=<代碼> 的說明文字：代碼是固定列舉，文字都在這裡，外部提供的任何文字都不會進入頁面
LOGIN_ERROR_MESSAGES = {
    ERROR_STATE: "Google 登入的驗證已逾時或不完整，請再試一次。",
    ERROR_UNAVAILABLE: "Google 登入暫時無法使用，請稍後再試。",
    ERROR_REJECTED: "Google 登入沒有完成或被拒絕，請再試一次。",
    ERROR_TERMS: "第一次使用 Google 註冊時，請先勾選同意服務條款與隱私權政策。",
    ERROR_DISABLED: "此帳號已停用，無法登入。",
}


# /login?notice=<代碼> 的說明文字（不是錯誤）：代碼是固定列舉，文字都在這裡
LOGIN_INFO_MESSAGES = {
    "deletion_requested": "我們已收到刪除申請，帳號已停用。若要保留，請在 7 天內重新登入並到帳號設定撤銷刪除。",
}


def _age_rules() -> dict:
    """給前端的年齡規則：區間選項（代碼與標籤）、最低註冊年齡、成年門檻與生日欄位的範圍。

    全部來自 `AgePolicy` 與時鐘：頁面與腳本不內嵌任何年齡數字。生日欄位的 `max` 是今天（Asia/Taipei），
    只是方便輸入；伺服器端一律重新驗證。
    """
    policy = get_age_policy()
    return {
        "ranges": policy.range_choices(),
        "minRegisterAge": policy.min_register_age,
        "adultAge": policy.adult_age,
        "birthDateMin": EARLIEST_BIRTH_DATE.isoformat(),
        "birthDateMax": taipei_today(get_clock()()).isoformat(),
    }


def _state(**extra) -> dict:
    """所有會員頁面共同的初始狀態。"""
    return {
        "googleEnabled": get_identity_provider() is not None,
        "termsVersion": CURRENT_TERMS_VERSION,
        "ageRules": _age_rules(),
        **extra,
    }


def _legal_context() -> dict:
    """條款與隱私權政策範本的共同變數：版本與年齡數字（範本內不得寫死年齡）。"""
    policy = get_age_policy()
    return {
        "terms_version": CURRENT_TERMS_VERSION,
        "min_age": policy.min_register_age,
        "adult_age": policy.adult_age,
    }


def _require_reset_available() -> None:
    """重設密碼確認頁：只服務管理員，需要寄信設定（寄信器與對外網址），缺少時 404。"""
    if not admin_password_reset_available():
        abort(404)


# ---- 登入與註冊 -------------------------------------------------------------

@auth_pages_bp.get("/login")
def login_page():
    user = current_user()
    if user is not None:
        return redirect(landing_for(user))
    notice = LOGIN_ERROR_MESSAGES.get(request.args.get("error", ""))
    info = LOGIN_INFO_MESSAGES.get(request.args.get("notice", ""))
    return render_template(
        "auth/login.html", initial_state=_state(next=safe_next(request.args.get("next")), notice=notice, info=info)
    )


@auth_pages_bp.get("/login/password")
def login_password_page():
    """管理員通過 Google 之後的密碼驗證頁。只接受持有有效中間狀態的請求，其餘導向登入頁。"""
    step = read_admin_step()
    if step is None:
        return redirect("/login")
    return render_template(
        "auth/login_password.html",
        initial_state=_state(next=step.next_url, forgotAvailable=admin_password_reset_available()),   # 沒有寄信設定就不顯示按鈕
    )


@auth_pages_bp.get("/register")
def register_page():
    # 玩家只用 Google 登入，登入與註冊合一；舊書籤與舊連結一律導向登入頁
    return redirect("/login")


# ---- 年齡確認（玩家首次以 Google 登入之後）---------------------------------------------

@auth_pages_bp.get("/age")
@login_required_page(allow_age_pending=True)      # 待確認年齡的玩家唯一能開的頁面；其他受限狀態由裝飾器導向各自的頁面
def age_page():
    """生日與年齡區間的頁面外殼。已完成年齡確認的人（含管理員，他們在帳號設定頁補填）開啟時導向預設頁面。

    初始狀態只有年齡規則（區間選項與範圍）與補填成功後要去的位置（`next`，伺服器端先驗證）；沒有任何個人資料。
    """
    user = current_user()
    if user.restriction != RESTRICTION_AGE_PENDING:
        return redirect(landing_for(user))
    return render_template("auth/age.html", initial_state=_state(next=safe_next(request.args.get("next"))))


# ---- 申訴（被鎖定的玩家）---------------------------------------------------------------

@auth_pages_bp.get("/appeal")
@login_required_page(allow_locked=True)           # 被鎖定的玩家唯一能開的頁面；其他受限狀態由裝飾器導向各自的頁面
def appeal_page():
    """申訴頁的外殼。沒有被鎖定的人開啟時導向預設頁面。初始狀態只有字數與頻率上限，沒有申訴內容或個人資料。"""
    user = current_user()
    if user.restriction != RESTRICTION_LOCKED:
        return redirect(landing_for(user))
    return render_template(
        "auth/appeal.html",
        initial_state=_state(appeal={"maxChars": MAX_APPEAL_CHARS, "hourlyLimit": APPEAL_HOURLY_LIMIT}),
    )


# ---- 管理員的重設密碼（沒有寄信設定時 404）------------------------------------------
# GET 只顯示確認頁：權杖在網址片段（#token=…），伺服器看不到，因此頁面本身不含權杖。

@auth_pages_bp.get("/reset-password")
def reset_password_page():
    _require_reset_available()
    return render_template("auth/reset_password.html", initial_state=_state())


# ---- 帳號設定 -----------------------------------------------------------------

@auth_pages_bp.get("/account")
@login_required_page(allow_pending_deletion=True)         # 等待刪除的人在這裡撤銷；頁面只顯示撤銷區塊
def account_page():
    """等待刪除的帳號與管理員看到的是不同的頁面：多餘的區塊（匯出、刪除）不會出現在 HTML 裡，不只是用 v-if 藏起來。"""
    user = current_user()
    scheduled = user.deletion_scheduled_at
    return render_template(
        "auth/account.html",
        initial_state=_state(user=get_auth_service().profile(user), deletion={"confirmText": CONFIRM_TEXT}),
        awaiting_deletion=user.awaiting_deletion,
        # 撤銷期限已過、排程還沒執行：不顯示注定失敗的撤銷按鈕（頁面開著時剛好過期的情形由前端處理 grace_expired）
        grace_over=scheduled is not None and scheduled <= get_clock()(),
        show_delete=user.may_delete_account,
    )


# ---- 服務條款與隱私權政策（不需登入）---------------------------------------------

@auth_pages_bp.get("/terms")
def terms_page():
    return render_template("legal/terms.html", **_legal_context())


@auth_pages_bp.get("/privacy")
def privacy_page():
    return render_template("legal/privacy.html", **_legal_context())


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
    next_url = safe_next((transaction or {}).get("x"))
    if result.user.needs_password_step:
        # 管理員：通過 Google 還不算登入。核發 10 分鐘的簽章中間狀態（不是登入狀態），到密碼驗證頁再輸入一次密碼
        security_events.log_event(security_events.ADMIN_GOOGLE_PASSED, user_id=result.user.id)
        response = redirect("/login/password")
        store_admin_step(response, user_id=result.user.id, token_version=result.user.token_version, next_url=next_url)
        clear_transaction(response)
        return response
    login_user(result.user)
    # 受限登入狀態各去自己的頁面（等待刪除 → 帳號頁、待確認年齡 → 年齡確認頁），否則依 next 或預設頁面
    response = redirect(landing_for(result.user, (transaction or {}).get("x")))
    clear_transaction(response)
    return response
