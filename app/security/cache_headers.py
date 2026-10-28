"""認證相關回應禁止被快取（見 session-authentication 規格「認證回應不可被快取」）。

- `/api/auth/` 底下的所有回應（含錯誤）：內容是使用者本人的資料或一次性的結果；
- 所有 HTML 頁面：每一頁都內嵌 CSRF 權杖，帳號頁與確認頁更含有使用者資料或一次性流程，
  被瀏覽器或中介快取後可能顯示過期的權杖，或在登出後仍看得到內容。
靜態資源（CSS、JavaScript、字型）不受影響，仍可正常快取。
"""
from __future__ import annotations

from flask import Flask, Response, request

AUTH_API_PREFIX = "/api/auth/"


def init_no_store(app: Flask) -> None:
    @app.after_request
    def _no_store(response: Response) -> Response:
        if "Cache-Control" in response.headers:
            return response  # 端點已明確決定快取行為（例如健康檢查的 no-store、favicon 的長期快取），不覆蓋
        if response.mimetype == "text/html" or request.path.startswith(AUTH_API_PREFIX):
            response.headers["Cache-Control"] = "no-store"
        return response
