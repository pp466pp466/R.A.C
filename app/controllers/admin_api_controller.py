"""Web API Controller：`/api/admin/*`（管理員後台），一律回傳 JSON（見 AGENTS.md 第 3、10 節與 admin-console 規格）。

Controller 只做三件事：確認登入、**先確認是管理員**、讀取請求、呼叫 `AdminService`、回應。權限判斷、稽核與重新輸入密碼
全在 Service；CSRF 在 `before_request` 統一檢查；業務錯誤由 controllers/errors.py 統一轉成 JSON。

順序刻意是「登入 → `authorize` → 解析請求本文與查詢參數」：非管理員對後台的任何請求（包含格式錯誤的本文）都得到與
「不存在」相同的 404，而不是 400 或 415 這種會透露端點存在的回應。回應只含明確組出來的資料，沒有內部編號、密碼雜湊與
`token_version`，也沒有任何個別使用者的年齡資料。
"""
from flask import Blueprint, jsonify

from app.controllers.request_helpers import int_arg, read_json_object
from app.security.session import require_login
from app.services.admin_service import DEFAULT_PAGE_SIZE, AdminService
from app.services.current_user import CurrentUser
from app.services.registry import get_admin_service

admin_api_bp = Blueprint("admin_api", __name__, url_prefix="/api/admin")


def _admin() -> tuple[CurrentUser, AdminService]:
    user = require_login()
    service = get_admin_service()
    service.authorize(user)
    return user, service


def _page() -> tuple[int, int]:
    return int_arg("limit", DEFAULT_PAGE_SIZE), int_arg("offset", 0)


# ---- 總覽與稽核 -------------------------------------------------------------------------------

@admin_api_bp.get("/overview")
def overview():
    admin, service = _admin()
    return jsonify(overview=service.overview(admin))


@admin_api_bp.get("/audit")
def audit():
    admin, service = _admin()
    limit, offset = _page()
    return jsonify(entries=service.audit_log(admin, limit=limit, offset=offset))


# ---- 使用者 -----------------------------------------------------------------------------------

@admin_api_bp.post("/users/lookup")
def lookup_user():
    """以信箱精確查詢（信箱在請求本文，不在網址）。次要入口：客服反應時使用。"""
    admin, service = _admin()
    body = read_json_object()
    return jsonify(user=service.lookup_user(admin, body.get("email")))


@admin_api_bp.get("/users/<public_id>")
def get_user(public_id: str):
    admin, service = _admin()
    return jsonify(user=service.get_user(admin, public_id))


@admin_api_bp.post("/users/<public_id>/suspend")
def suspend_user(public_id: str):
    admin, service = _admin()
    body = read_json_object()
    return jsonify(user=service.suspend_user(admin, public_id, reason=body.get("reason"), password=body.get("password")))


@admin_api_bp.post("/users/<public_id>/unsuspend")
def unsuspend_user(public_id: str):
    admin, service = _admin()
    body = read_json_object()
    return jsonify(user=service.unsuspend_user(admin, public_id, password=body.get("password")))


@admin_api_bp.post("/users/<public_id>/tier")
def change_tier(public_id: str):
    admin, service = _admin()
    body = read_json_object()
    return jsonify(user=service.change_tier(admin, public_id, tier=body.get("tier"), password=body.get("password")))


# ---- 對話與模型失敗的原文 ---------------------------------------------------------------------------

@admin_api_bp.get("/feedback")
def feedback():
    admin, service = _admin()
    limit, offset = _page()
    return jsonify(conversations=service.feedback_conversations(admin, limit=limit, offset=offset))


@admin_api_bp.get("/flagged")
def flagged():
    admin, service = _admin()
    limit, offset = _page()
    return jsonify(conversations=service.flagged_conversations(admin, limit=limit, offset=offset))


@admin_api_bp.get("/conversations/<public_id>")
def read_conversation(public_id: str):
    admin, service = _admin()
    return jsonify(service.read_conversation(admin, public_id))


@admin_api_bp.post("/conversations/<public_id>/delete")
def delete_conversation(public_id: str):
    admin, service = _admin()
    body = read_json_object()
    service.delete_conversation(admin, public_id, reason=body.get("reason"), password=body.get("password"))
    return jsonify(ok=True)


@admin_api_bp.get("/failures")
def failures():
    admin, service = _admin()
    return jsonify(failures=service.failures(admin))
