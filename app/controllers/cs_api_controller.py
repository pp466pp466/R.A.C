"""Web API Controller：`/api/cs/*`（客服對話），一律回傳 JSON（見 AGENTS.md 第 3 節與 cs-conversations 規格）。

Controller 只做三件事：讀取請求、呼叫 Service、回應。閘門、擁有者條件與冪等都在 Service；
CSRF 在 `before_request` 統一檢查；業務錯誤由 controllers/errors.py 統一轉成 JSON。
回應只含對話的對外欄位與訊息文字（含建議動作）；意圖、實體、信心、摘要與狀態從不離開伺服器。
"""
from flask import Blueprint, jsonify, request

from app.controllers.request_helpers import read_json_object
from app.security.session import require_login
from app.services.errors import ValidationError
from app.services.registry import get_chat_service, get_usage_service

cs_api_bp = Blueprint("cs_api", __name__, url_prefix="/api/cs")


def _int_arg(name: str, default: int) -> int:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValidationError(fields={name: "必須是整數。"}) from None


@cs_api_bp.get("/usage")
def usage():
    """呼叫者自己的用量（今日輪數、每分鐘上限、對話數、暫停截止時間）。只需登入，不過閘門：被停權或尚未完成年齡確認的
    使用者仍能看到頁面上的說明。不含全站用量、token 數或其他使用者的資料。"""
    user = require_login()
    return jsonify(usage=get_usage_service().snapshot(user).to_dict())


@cs_api_bp.post("/conversations")
def create_conversation():
    user = require_login()
    body = read_json_object()
    view, created = get_chat_service().create_conversation(user, body.get("client_id"))
    return jsonify(conversation=view.to_dict()), 201 if created else 200


@cs_api_bp.get("/conversations")
def list_conversations():
    user = require_login()
    views = get_chat_service().list_conversations(user, limit=_int_arg("limit", 50), offset=_int_arg("offset", 0))
    return jsonify(conversations=[view.to_dict() for view in views])


@cs_api_bp.patch("/conversations/<public_id>")
def rename_conversation(public_id: str):
    user = require_login()
    body = read_json_object()
    view = get_chat_service().rename_conversation(user, public_id, body.get("title"), body.get("state_version"))
    return jsonify(conversation=view.to_dict())


@cs_api_bp.delete("/conversations/<public_id>")
def delete_conversation(public_id: str):
    user = require_login()
    get_chat_service().delete_conversation(user, public_id)
    return jsonify(ok=True)


@cs_api_bp.post("/conversations/<public_id>/messages")
def send_message(public_id: str):
    user = require_login()
    body = read_json_object()
    result = get_chat_service().send_message(user, public_id, body.get("client_msg_id"), body.get("content"))
    return jsonify(
        user_message=result.user_message.to_dict(),
        assistant_message=result.assistant_message.to_dict(),
        conversation=result.conversation.to_dict(),
    )


@cs_api_bp.get("/conversations/<public_id>/messages")
def list_messages(public_id: str):
    user = require_login()
    messages, has_more = get_chat_service().list_messages(
        user, public_id, since_seq=_int_arg("since_seq", 0), limit=_int_arg("limit", 200)
    )
    return jsonify(messages=[message.to_dict() for message in messages], has_more=has_more)
