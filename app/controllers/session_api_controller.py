"""Web API Controller：`/api/sessions/*`（玩家主角與開存檔），一律回傳 JSON（見 rpg-protagonist、protagonist-suggestion 規格）。

Controller 只讀取請求、呼叫 Service、回應；帳號與年齡閘門、可見性與內容模式（他人的草稿一律 404）、主角與錨定事實的驗證、
審查層與每日次數都在 Service。CSRF 在 `before_request` 統一檢查；業務錯誤由 controllers/errors.py 統一轉成 JSON
（審查拒絕的回應只有類別列舉）。所有回應禁止快取（`/api/sessions/` 在 `cache_headers.py` 的前綴清單內）。
受限登入狀態（鎖定、等待刪除、待確認年齡）由 `require_login` 預設拒絕。

- `GET /api/sessions/setup/<version_id>`：序章與創角畫面需要的資料（背景故事、可聯繫的人物／地點／里程碑、欄位上限、今日剩餘的生成次數）。
- `POST /api/sessions/protagonist/suggest`：AI 生成主角的預覽，本文 `{"version_id": "…", "intro": "選填的自我介紹"}`。**無狀態**：不寫存檔，
  自我介紹不保存；失敗（503 `suggestion_unavailable`）時前端改走自創。
- `POST /api/sessions`：開存檔，本文 `{"version_id": "…", "protagonist": {…角色卡…}, "anchor": {kind, ref, text} 或 null, "skip_anchor": true 或省略}`。
  主角驗證與兩層審查通過才寫入；模型層審查最壞約 30 秒（劇本尚未審查過時再加一次），**前端逾時要設為 65 秒**並於離開頁面時中止。
"""
from flask import Blueprint, jsonify

from app.controllers.request_helpers import read_json_object
from app.security.session import require_login
from app.services.registry import get_protagonist_service, get_session_service
from app.services.session_service import session_view

session_api_bp = Blueprint("session_api", __name__, url_prefix="/api/sessions")


@session_api_bp.get("/setup/<version_id>")
def setup(version_id: str):
    user = require_login()
    return jsonify(ok=True, **get_protagonist_service().setup(user, version_id))


@session_api_bp.post("/protagonist/suggest")
def suggest():
    user = require_login()
    body = read_json_object()
    return jsonify(ok=True, **get_protagonist_service().suggest(user, body.get("version_id"), body.get("intro")))


@session_api_bp.post("")
def start_session():
    user = require_login()
    body = read_json_object()
    save = get_session_service().start_session(
        user, body.get("version_id"), protagonist=body.get("protagonist"), anchor=body.get("anchor"), skip_anchor=body.get("skip_anchor"),
    )
    return jsonify(ok=True, session=session_view(save)), 201
