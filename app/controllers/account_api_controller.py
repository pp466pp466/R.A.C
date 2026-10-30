"""Web API Controller：`/api/account/*`，一律回傳 JSON（見 AGENTS.md 第 3 節與 account-deletion、personal-data-export 規格）。

Controller 只讀取請求、呼叫 Service、處理登入狀態；確認本人、狀態轉換與匯出內容都在 Service。
所有寫入的請求都經過全站的 CSRF 與 Origin 檢查；回應一律禁止快取（`/api/account/` 在 `cache_headers.py` 的前綴清單內）。

- `POST /api/account/delete`：申請刪除（有密碼者帶 `password`，只靠 Google 者帶 `confirm_text`）。成功後所有裝置的登入狀態失效，
  這台裝置也一併清除。
- `POST /api/account/export`：匯出自己的帳號資料與全部對話（本文 `{"format": "json" | "csv"}`），回應是附件下載、禁止快取。
  用 POST 是為了受 CSRF 與來源檢查保護，也不會被預取連結觸發。受限登入狀態（等待刪除）不能匯出：請在申請刪除之前匯出。
- `POST /api/account/cancel-deletion`：寬限期內撤銷。**這是少數允許「受限登入狀態」的端點之一**（見 `require_login`）：
  帳號正在等待刪除的人登入後只能做這件事，目前的登入狀態在撤銷後直接變成完整的。
"""
from flask import Blueprint, jsonify, send_file

from app.controllers.request_helpers import read_json_object
from app.security.session import logout_user, require_login
from app.services.current_user import iso_utc
from app.services.registry import get_account_deletion_service, get_auth_service, get_personal_export_service

account_api_bp = Blueprint("account_api", __name__, url_prefix="/api/account")

DELETE_MESSAGE = "已收到刪除申請，帳號已停用。若要保留，請在 7 天內重新登入並到帳號設定撤銷刪除。"
CANCEL_MESSAGE = "已撤銷刪除，您的帳號已恢復。"


@account_api_bp.post("/delete")
def delete_account():
    user = require_login()
    body = read_json_object()
    scheduled_at = get_account_deletion_service().request(
        user, password=body.get("password"), confirm_text=body.get("confirm_text")
    )
    logout_user()          # token_version 已加 1，其他裝置也已失效；這台的 cookie 一併清除
    return jsonify(ok=True, scheduled_at=iso_utc(scheduled_at), message=DELETE_MESSAGE)


@account_api_bp.post("/export")
def export_data():
    user = require_login()
    body = read_json_object()
    result = get_personal_export_service().build(user, body.get("format"))
    response = send_file(
        result.handle, mimetype=result.mimetype, as_attachment=True, download_name=result.filename, max_age=0
    )
    response.call_on_close(result.handle.close)          # 暫存檔在回應送完之後關閉
    response.headers["Cache-Control"] = "no-store"
    return response


@account_api_bp.post("/cancel-deletion")
def cancel_deletion():
    user = require_login(allow_pending_deletion=True)
    updated = get_account_deletion_service().cancel(user)
    return jsonify(ok=True, user=get_auth_service().profile(updated), message=CANCEL_MESSAGE)
