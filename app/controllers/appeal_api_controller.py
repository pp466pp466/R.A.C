"""Web API Controller：`/api/appeals/*`（被鎖定玩家的申訴），一律回傳 JSON（見 lockout-appeals 規格）。

被鎖定的玩家只持有受限登入狀態，所以這裡的端點明確允許 `allow_locked`；沒有被鎖定的人（含管理員）得到 404，
與不存在相同（Service 判斷）。申訴內容是個資：回應只含呼叫者自己的申訴，日誌與稽核只有字數。
"""
from flask import Blueprint, jsonify

from app.controllers.request_helpers import read_json_object
from app.security.session import require_login
from app.services.registry import get_appeal_service

appeal_api_bp = Blueprint("appeal_api", __name__, url_prefix="/api/appeals")


@appeal_api_bp.post("")
def submit():
    user = require_login(allow_locked=True)
    body = read_json_object()
    view = get_appeal_service().submit(user, body.get("content"))
    return jsonify(appeal=view.to_dict()), 201


@appeal_api_bp.get("/mine")
def mine():
    user = require_login(allow_locked=True)
    return jsonify(appeals=[view.to_dict() for view in get_appeal_service().mine(user)])
