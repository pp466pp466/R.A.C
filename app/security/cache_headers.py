"""認證相關回應禁止被快取（見 session-authentication 規格「認證回應不可被快取」）。

- `/api/auth/`、`/api/cs/`、`/api/admin/`、`/api/account/`、`/api/scripts/` 與 `/api/sessions/` 底下的所有回應（含錯誤）：內容是使用者本人的資料（帳號、對話與訊息）、
  管理員才看得到的他人資料，或一次性的結果；
- 所有 HTML 頁面：每一頁都內嵌 CSRF 權杖，帳號頁與確認頁更含有使用者資料或一次性流程，
  被瀏覽器或中介快取後可能顯示過期的權杖，或在登出後仍看得到內容。
靜態資源（CSS、JavaScript、字型）不受影響，仍可正常快取。
"""
from __future__ import annotations

from flask import Flask, Response, request

AUTH_API_PREFIX = "/api/auth/"
CS_API_PREFIX = "/api/cs/"
ADMIN_API_PREFIX = "/api/admin/"
ACCOUNT_API_PREFIX = "/api/account/"
SCRIPT_API_PREFIX = "/api/scripts"          # 列表與建立沒有尾端斜線，同樣要涵蓋
SESSION_API_PREFIX = "/api/sessions"        # 開存檔（POST /api/sessions）沒有尾端斜線，同樣要涵蓋
NO_STORE_API_PREFIXES = (
    AUTH_API_PREFIX, CS_API_PREFIX, ADMIN_API_PREFIX, ACCOUNT_API_PREFIX, SCRIPT_API_PREFIX, SESSION_API_PREFIX,
)


def init_no_store(app: Flask) -> None:
    @app.after_request
    def _no_store(response: Response) -> Response:
        if "Cache-Control" in response.headers:
            return response  # 端點已明確決定快取行為（例如健康檢查的 no-store、favicon 的長期快取），不覆蓋
        if response.mimetype == "text/html" or request.path.startswith(NO_STORE_API_PREFIXES):
            response.headers["Cache-Control"] = "no-store"
        return response
