"""存活與就緒檢查端點：不需登入、回應最小內容、禁止快取。

- `/healthz`：只表示行程存活，不碰資料庫（資料庫停止時仍回 200，避免平台誤殺服務）。
- `/readyz`：實際確認資料庫可用；不可用回 503。
兩者的回應都不含版本、設定、連線字串或錯誤細節。
"""
from flask import Blueprint, Response, jsonify

from app.services.registry import get_health_service

health_bp = Blueprint("health", __name__)


def _json(body: dict[str, str], status: int) -> Response:
    response = jsonify(body)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


@health_bp.get("/healthz")
def healthz() -> Response:
    return _json({"status": "ok"}, 200)


@health_bp.get("/readyz")
def readyz() -> Response:
    if get_health_service().is_database_ready():
        return _json({"status": "ok"}, 200)
    return _json({"status": "unavailable"}, 503)
