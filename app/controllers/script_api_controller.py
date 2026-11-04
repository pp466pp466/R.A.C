"""Web API Controller：`/api/scripts/*`（玩家創作劇本），一律回傳 JSON（見 script-authoring 規格）。

Controller 只讀取請求、呼叫 Service、回應；帳號與年齡閘門、擁有者條件（他人的劇本一律 404）、固定規格驗證、規則層與額度都在 Service。
CSRF 在 `before_request` 統一檢查；業務錯誤由 controllers/errors.py 統一轉成 JSON（審查拒絕的回應只有類別列舉）。
所有回應禁止快取（`/api/scripts/` 在 `cache_headers.py` 的前綴清單內）。受限登入狀態（鎖定、等待刪除、待確認年齡）由 `require_login` 預設拒絕。

- `GET /api/scripts`：自己的全部劇本與版本摘要。 `POST /api/scripts`：建立劇本與第 1 版草稿，本文 `{"content": {...}}`。
- `GET /api/scripts/example`：內建首發劇本的標準化內容（給編輯器帶入欄位範例）。 `GET /api/scripts/limits`：自己的額度與暫停狀態。
- `GET /api/scripts/versions/<id>`：自己的一個版本與完整內容。 `PUT /api/scripts/versions/<id>`：儲存草稿，本文 `{"content": {...}, "base_hash": "…"}`。
- `DELETE /api/scripts/versions/<id>`：刪除草稿。 `POST /api/scripts/versions/<id>/withdraw`：撤回待審。
- `POST /api/scripts/versions/<id>/submit`：送審（草稿 -> 待審），依序檢查暫停、每日次數、規則層與模型層；自動拒絕回 422 與類別，
  模型層不可用回 503（不計次）。審查呼叫最長約 30 秒，前端的逾時要略大於它。
- `POST /api/scripts/<script_id>/versions`：由最新的非進行中版本建立新草稿。
  `POST /api/scripts/<script_id>/author-name/refresh`：重新取用目前的暱稱作為筆名（發佈前）。
"""
from flask import Blueprint, jsonify

from app.controllers.request_helpers import read_json_object
from app.security.session import require_login
from app.services.registry import get_script_authoring_service, get_script_submission_service

script_api_bp = Blueprint("script_api", __name__, url_prefix="/api/scripts")


@script_api_bp.get("")
def list_scripts():
    user = require_login()
    return jsonify(ok=True, scripts=get_script_authoring_service().list_mine(user))


@script_api_bp.post("")
def create_script():
    user = require_login()
    body = read_json_object()
    return jsonify(ok=True, version=get_script_authoring_service().create(user, body.get("content"))), 201


@script_api_bp.get("/example")
def example():
    user = require_login()
    return jsonify(ok=True, **get_script_authoring_service().example(user))


@script_api_bp.get("/limits")
def limits():
    user = require_login()
    return jsonify(ok=True, limits=get_script_authoring_service().limits(user))


@script_api_bp.get("/versions/<version_id>")
def get_version(version_id: str):
    user = require_login()
    return jsonify(ok=True, version=get_script_authoring_service().get_version(user, version_id))


@script_api_bp.put("/versions/<version_id>")
def update_draft(version_id: str):
    user = require_login()
    body = read_json_object()
    version = get_script_authoring_service().update_draft(user, version_id, body.get("content"), body.get("base_hash"))
    return jsonify(ok=True, version=version)


@script_api_bp.delete("/versions/<version_id>")
def delete_draft(version_id: str):
    user = require_login()
    get_script_authoring_service().delete_draft(user, version_id)
    return jsonify(ok=True)


@script_api_bp.post("/versions/<version_id>/withdraw")
def withdraw(version_id: str):
    user = require_login()
    return jsonify(ok=True, version=get_script_authoring_service().withdraw(user, version_id))


@script_api_bp.post("/versions/<version_id>/submit")
def submit(version_id: str):
    user = require_login()
    return jsonify(ok=True, version=get_script_submission_service().submit(user, version_id))


@script_api_bp.post("/<script_id>/versions")
def new_version(script_id: str):
    user = require_login()
    return jsonify(ok=True, version=get_script_authoring_service().new_version(user, script_id)), 201


@script_api_bp.post("/<script_id>/author-name/refresh")
def refresh_author_name(script_id: str):
    user = require_login()
    return jsonify(ok=True, script=get_script_authoring_service().refresh_author_name(user, script_id))
