"""Web API Controller：`/api/auth/*`，一律回傳 JSON（見 AGENTS.md 第 3 節）。

Controller 只做三件事：讀取請求、呼叫 Service、把結果交給 HTTP 安全輔助（登入 cookie）。
權限與業務判斷都在 Service；CSRF 在 `before_request` 統一檢查；業務錯誤由 controllers/errors.py 統一轉成 JSON。

只讀取白名單內的欄位：請求裡夾帶的 `role`、`member_tier`、`status` 等欄位一律被忽略，
因此無法透過任何 API 建立管理員或提升角色。
"""
from flask import Blueprint, abort, jsonify

from app.controllers.request_helpers import read_json_object
from app.runtime import get_config, get_identity_provider
from app.security.oauth_cookie import store_transaction
from app.security.redirects import safe_next
from app.security.session import current_user, login_user, logout_user, renew_login_session, require_login
from app.services.registry import get_auth_service, get_email_flow_service, get_google_sign_in_service

auth_api_bp = Blueprint("auth_api", __name__, url_prefix="/api/auth")

# 註冊的回應：不論信箱是否已有帳號都完全相同，不洩漏帳號是否存在
REGISTER_MESSAGE_WITH_VERIFICATION = "若這個信箱可以註冊，我們已寄出驗證信，請於 24 小時內點擊信中的連結完成驗證。"
REGISTER_MESSAGE_WITHOUT_VERIFICATION = "註冊完成，請前往登入。"


@auth_api_bp.post("/register")
def register():
    body = read_json_object()
    get_auth_service().register(
        email=body.get("email"),
        password=body.get("password"),
        display_name=body.get("display_name"),
        accept_terms=body.get("accept_terms"),
        birth_date=body.get("birth_date"),
        age_range=body.get("age_range"),
    )
    verification = get_config().email_verification_required
    return jsonify(
        ok=True,
        verification_required=verification,
        message=REGISTER_MESSAGE_WITH_VERIFICATION if verification else REGISTER_MESSAGE_WITHOUT_VERIFICATION,
    )


@auth_api_bp.post("/login")
def login():
    body = read_json_object()
    service = get_auth_service()
    user = service.login(email=body.get("email"), password=body.get("password"))
    login_user(user)
    return jsonify(user=service.profile(user))


@auth_api_bp.post("/logout")
def logout():
    """登出目前裝置。沒有登入也回成功（重複登出沒有壞處）。"""
    user = current_user()
    if user is not None:
        get_auth_service().record_logout(user)
    logout_user()
    return jsonify(ok=True)


@auth_api_bp.post("/logout-all")
def logout_all():
    user = require_login()
    get_auth_service().logout_all(user)
    logout_user()
    return jsonify(ok=True, message="已登出所有裝置，請重新登入。")


@auth_api_bp.get("/me")
def me():
    user = require_login()
    return jsonify(user=get_auth_service().profile(user))


@auth_api_bp.post("/verify-age")
def verify_age():
    """已登入的使用者補填生日與年齡區間。`token_version` 沒有變，所以不需要換發登入 cookie。"""
    user = require_login()
    body = read_json_object()
    service = get_auth_service()
    updated = service.verify_age(user, birth_date=body.get("birth_date"), age_range=body.get("age_range"))
    return jsonify(user=service.profile(updated))


# ---- 信箱驗證與重設密碼（信箱驗證關閉時整組不存在，一律 404）--------------------------

# 忘記密碼的回應：不論信箱是否存在、是否被頻率限制，都完全相同
FORGOT_PASSWORD_MESSAGE = "若這個信箱已註冊，我們已寄出重設密碼的信件，請於 1 小時內點擊信中的連結。"


def _require_email_flows_enabled() -> None:
    if not get_config().email_verification_required:
        abort(404)


@auth_api_bp.post("/verify-email")
def verify_email():
    _require_email_flows_enabled()
    body = read_json_object()
    get_email_flow_service().verify_email(body.get("token"))
    return jsonify(ok=True, message="信箱驗證完成，請重新登入。")


@auth_api_bp.post("/resend-verification")
def resend_verification():
    _require_email_flows_enabled()
    user = require_login()
    get_email_flow_service().resend_verification(user)
    return jsonify(ok=True, message="驗證信已重新寄出，請檢查您的信箱。")


@auth_api_bp.post("/forgot-password")
def forgot_password():
    _require_email_flows_enabled()
    body = read_json_object()
    get_email_flow_service().request_password_reset(body.get("email"))
    return jsonify(ok=True, message=FORGOT_PASSWORD_MESSAGE)


@auth_api_bp.post("/reset-password")
def reset_password():
    _require_email_flows_enabled()
    body = read_json_object()
    get_email_flow_service().reset_password(body.get("token"), body.get("new_password"))
    logout_user()          # 這台裝置的舊登入狀態一併清除（token_version 已加 1，其他裝置也已失效）
    return jsonify(ok=True, message="密碼已重設，請使用新密碼登入。")


# ---- Google 登入（未啟用時整組不存在，一律 404）---------------------------------

@auth_api_bp.post("/google/start")
def google_start():
    """發起 Google 登入：回傳授權網址，並把 state、nonce、PKCE 驗證碼綁在這個瀏覽器的簽章 cookie 上。"""
    if get_identity_provider() is None:
        abort(404)
    body = read_json_object()
    start = get_google_sign_in_service().begin(
        accept_terms=body.get("accept_terms"), birth_date=body.get("birth_date"), age_range=body.get("age_range")
    )
    transaction = {**start.transaction, "x": safe_next(body.get("next"))}     # 登入後的返回位置：此時就先驗證
    response = jsonify(authorization_url=start.authorization_url)
    store_transaction(response, transaction)
    return response


@auth_api_bp.post("/change-password")
def change_password():
    user = require_login()
    body = read_json_object()
    updated = get_auth_service().change_password(
        user, current_password=body.get("current_password"), new_password=body.get("new_password")
    )
    renew_login_session(updated)   # 目前裝置以新的 token_version 維持登入；其他裝置因版本不符而登出（不換發 CSRF 權杖）
    return jsonify(ok=True, message="密碼已更新，其他裝置已登出。")
