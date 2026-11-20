"""Web API Controller：`/api/admin/*`（管理員後台），一律回傳 JSON（見 AGENTS.md 第 3、10 節與 admin-console 規格）。

Controller 只做三件事：確認登入、**先確認是管理員**、讀取請求、呼叫 `AdminService`、回應。權限判斷、稽核與重新輸入密碼
全在 Service；CSRF 在 `before_request` 統一檢查；業務錯誤由 controllers/errors.py 統一轉成 JSON。

順序刻意是「登入 → `authorize` → 解析請求本文與查詢參數」：非管理員對後台的任何請求（包含格式錯誤的本文）都得到與
「不存在」相同的 404，而不是 400 或 415 這種會透露端點存在的回應。回應只含明確組出來的資料，沒有內部編號、密碼雜湊與
`token_version`，也沒有任何個別使用者的年齡資料。
"""
from flask import Blueprint, jsonify, request

from app.controllers.request_helpers import int_arg, read_json_object
from app.security.session import require_login
from app.services.admin_service import DEFAULT_PAGE_SIZE, DEFAULT_SCRIPT_STATUS, AdminService
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


@admin_api_bp.post("/users/<public_id>/wallet/adjust")
def adjust_wallet(public_id: str):
    """調整玩家的遊戲幣餘額（高風險：帶管理員的密碼；稽核 `wallet.adjust`）。回傳調整後的使用者檢視（含兩個餘額）。"""
    admin, service = _admin()
    body = read_json_object()
    return jsonify(user=service.adjust_wallet(
        admin, public_id, bucket=body.get("bucket"), delta=body.get("delta"), reason=body.get("reason"), password=body.get("password"),
    ))


# ---- 帳號鎖定與申訴 ---------------------------------------------------------------------------

@admin_api_bp.get("/locked")
def locked_accounts():
    admin, service = _admin()
    limit, offset = _page()
    return jsonify(accounts=service.locked_accounts(admin, limit=limit, offset=offset))


@admin_api_bp.get("/appeals")
def appeals():
    """被鎖定帳號送出的申訴（解除之後不再出現）。這是管理員唯一能讀到的使用者撰寫內容之外的另一類，不含任何對話。"""
    admin, service = _admin()
    limit, offset = _page()
    return jsonify(appeals=service.appeals(admin, limit=limit, offset=offset))


@admin_api_bp.post("/users/<public_id>/unlock")
def unlock_account(public_id: str):
    admin, service = _admin()
    body = read_json_object()
    return jsonify(user=service.unlock_account(admin, public_id, password=body.get("password")))


# ---- 暫停發佈（rpg-script-authoring）-------------------------------------------------------------

@admin_api_bp.get("/publish-suspended")
def publish_suspended():
    """連續被自動拒絕而被暫停發佈的帳號（只有對外識別碼、顯示名稱與暫停時間）。"""
    admin, service = _admin()
    limit, offset = _page()
    return jsonify(accounts=service.publish_suspended_accounts(admin, limit=limit, offset=offset))


@admin_api_bp.post("/users/<public_id>/lift-publish-suspension")
def lift_publish_suspension(public_id: str):
    admin, service = _admin()
    body = read_json_object()
    return jsonify(user=service.lift_publish_suspension(admin, public_id, password=body.get("password")))


# ---- 劇本審核、下架與恢復（rpg-script-publishing）-----------------------------------------------------
# 先 `_admin()`（非管理員一律 404，含格式錯誤的本文）才解析本文與查詢參數。核可不讀本文（不需要密碼與其他欄位）；
# 退回與下架帶固定列舉的 `category` 與選填的 `note`；下架與恢復另帶管理員自己的密碼（高風險）。回應不含內部編號。

@admin_api_bp.get("/scripts")
def script_queue():
    """審核清單：`status`（`pending_review` 預設、`published`、`removed`）、`limit`、`offset`。沒有劇本內文與備註全文。"""
    admin, service = _admin()
    limit, offset = _page()
    status = request.args.get("status", DEFAULT_SCRIPT_STATUS)
    return jsonify(versions=service.script_queue(admin, status=status, limit=limit, offset=offset))


@admin_api_bp.get("/scripts/versions/<public_id>")
def read_script(public_id: str):
    """送審過的版本的全文與審核備註；先寫 `script.read` 稽核再回傳（草稿與不存在一律 404）。"""
    admin, service = _admin()
    return jsonify(service.read_script(admin, public_id))


@admin_api_bp.post("/scripts/versions/<public_id>/approve")
def approve_script(public_id: str):
    admin, service = _admin()
    return jsonify(service.approve_script(admin, public_id))


@admin_api_bp.post("/scripts/versions/<public_id>/reject")
def reject_script(public_id: str):
    admin, service = _admin()
    body = read_json_object()
    return jsonify(service.reject_script(admin, public_id, category=body.get("category"), note=body.get("note")))


@admin_api_bp.post("/scripts/versions/<public_id>/remove")
def remove_script(public_id: str):
    admin, service = _admin()
    body = read_json_object()
    return jsonify(service.remove_script(
        admin, public_id, category=body.get("category"), note=body.get("note"), password=body.get("password"),
    ))


@admin_api_bp.post("/scripts/versions/<public_id>/restore")
def restore_script(public_id: str):
    admin, service = _admin()
    body = read_json_object()
    return jsonify(service.restore_script(admin, public_id, password=body.get("password")))


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
