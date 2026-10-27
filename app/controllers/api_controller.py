"""Web API Controller：一律回傳 JSON（見 AGENTS.md 第 3 節）。

SSE 串流端點是唯一的非 JSON 例外，屬 RPG 模組，不在本 change 範圍。
C-1 尚無業務 API；後續 change 在此 Blueprint 下新增 `/api/auth/*`、`/api/cs/*` 等端點。
"""
from flask import Blueprint

api_bp = Blueprint("api", __name__, url_prefix="/api")
