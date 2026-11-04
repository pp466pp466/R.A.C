"""Web API Controller：`/api/auth/*`，一律回傳 JSON（見 AGENTS.md 第 3 節）。

Controller 只做三件事：讀取請求、呼叫 Service、把結果交給 HTTP 安全輔助（登入 cookie）。
權限與業務判斷都在 Service；CSRF 在 `before_request` 統一檢查；業務錯誤由 controllers/errors.py 統一轉成 JSON。

只讀取白名單內的欄位：請求裡夾帶的 `role`、`member_tier`、`status` 等欄位一律被忽略，
因此無法透過任何 API 建立管理員或提升角色。玩家只用 Google 登入：沒有註冊、密碼登入、信箱驗證與玩家的忘記密碼端點。
"""
from flask import Blueprint, abort, jsonify

from app.controllers.request_helpers import read_json_object
from app.runtime import get_identity_provider
from app.security.oauth_cookie import store_transaction
from app.security.redirects import safe_next
from app.security.admin_step import clear_admin_step, read_admin_step
from app.security.redirects import landing_for
from app.security.session import current_user, login_user, logout_user, renew_login_session, require_login
from app.services.errors import Unauthenticated, UnderageAccountErased
from app.services.registry import (
    admin_password_reset_available, get_auth_service, get_email_flow_service, get_google_sign_in_service,
)

auth_api_bp = Blueprint("auth_api", __name__, url_prefix="/api/auth")


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
    # 三種受限登入狀態都要靠它得知自己的狀態（預定刪除的時間、該去哪一頁）
    user = require_login(allow_pending_deletion=True, allow_age_pending=True, allow_locked=True)
    return jsonify(user=get_auth_service().profile(user))


@auth_api_bp.post("/admin-password")
def admin_password():
    """管理員通過 Google 之後的密碼驗證：成功才建立登入 session（並清除中間狀態）。

    沒有中間狀態（沒通過 Google、已過 10 分鐘、被竄改）一律 401，要求重新以 Google 登入；密碼不對的回應與一般登入失敗相同。
    """
    step = read_admin_step()
    if step is None:
        raise Unauthenticated()
    body = read_json_object()
    user = get_auth_service().verify_admin_password(step.user_id, step.token_version, body.get("password"))
    login_user(user)
    response = jsonify(ok=True, next=landing_for(user, step.next_url))
    clear_admin_step(response)
    return response


# 管理員忘記密碼的回應：與結果無關（寄了、被頻率限制、對象已變動都一樣）
ADMIN_FORGOT_MESSAGE = "已寄出重設密碼的信件到您的信箱，請於 1 小時內點擊信中的連結。"


@auth_api_bp.post("/admin-password/forgot")
def admin_password_forgot():
    """管理員在密碼驗證步驟按「忘記密碼」：寄重設信到該管理員的信箱。

    只接受持有有效中間狀態的請求（對象由它決定，不收請求內容）；匿名請求，以及沒有寄信設定時，一律 404。
    """
    step = read_admin_step()
    if step is None or not admin_password_reset_available():
        abort(404)
    get_email_flow_service().request_admin_password_reset(step.user_id, step.token_version)
    return jsonify(ok=True, message=ADMIN_FORGOT_MESSAGE)


@auth_api_bp.post("/verify-age")
def verify_age():
    """已登入的使用者補填生日與年齡區間。`token_version` 沒有變，所以不需要換發登入 cookie。"""
    user = require_login(allow_age_pending=True)             # 待確認年齡的玩家唯一能做的事（登出本來就不需要登入）
    body = read_json_object()
    service = get_auth_service()
    try:
        updated = service.verify_age(
            user, birth_date=body.get("birth_date"), age_range=body.get("age_range"), nickname=body.get("nickname")
        )
    except UnderageAccountErased:
        logout_user()          # 帳號已抹除：目前裝置的登入狀態一併清除（錯誤回應照常由錯誤處理器產生）
        raise
    return jsonify(user=service.profile(updated))


# ---- 管理員的重設密碼（沒有寄信設定時一律 404）--------------------------------------

@auth_api_bp.post("/reset-password")
def reset_password():
    if not admin_password_reset_available():
        abort(404)
    body = read_json_object()
    get_email_flow_service().reset_password(body.get("token"), body.get("new_password"))
    logout_user()          # 這台裝置的舊登入狀態一併清除（token_version 已加 1，其他裝置也已失效）
    response = jsonify(ok=True, message="密碼已重設，請使用新密碼登入。")
    clear_admin_step(response)          # 管理員的中間狀態同樣作廢（token_version 加 1 之後它本來就對不上）
    return response


# ---- Google 登入（未啟用時整組不存在，一律 404）---------------------------------

@auth_api_bp.post("/google/start")
def google_start():
    """發起 Google 登入：回傳授權網址，並把 state、nonce、PKCE 驗證碼綁在這個瀏覽器的簽章 cookie 上。"""
    if get_identity_provider() is None:
        abort(404)
    body = read_json_object()
    # 只收條款旗標；請求若仍帶著生日與年齡區間，一律忽略（年齡確認在登入之後才填）
    start = get_google_sign_in_service().begin(accept_terms=body.get("accept_terms"))
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
