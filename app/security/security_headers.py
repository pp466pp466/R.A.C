"""安全標頭（見 security-headers 規格）。

- 每個回應（頁面、API、靜態資源、4xx 與 5xx 錯誤）都帶 `X-Content-Type-Options`、`Referrer-Policy`、`X-Frame-Options`
  與 `Permissions-Policy`；`Strict-Transport-Security` 只在信任入口層的環境（staging 與 production）送出。
- development 與 testing 不送 HSTS：本機是 http，瀏覽器會把 HSTS 記在 localhost，之後的本機開發全部失敗。
- HSTS 只設一年且不含 `includeSubDomains` 與 `preload`：瀏覽器記住後無法由伺服器撤回，網域與子網域尚未確定；日後升級是放寬而非撤回。
- 刻意**不送** `Content-Security-Policy`：前端使用 Vue 全域建置，模板在瀏覽器以 `new Function` 編譯，政策必須允許
  `unsafe-eval`（加上 `style=` 屬性還要 `unsafe-inline`），保護力趨近於零。前端改為預先編譯模板時再重新評估（見營運文件）。
- 端點已明確設定同名標頭時不覆蓋。
"""
from __future__ import annotations

from flask import Flask, Response

HSTS_VALUE = "max-age=31536000"
PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"

BASE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": PERMISSIONS_POLICY,
}


def init_security_headers(app: Flask, *, hsts: bool) -> None:
    headers = dict(BASE_HEADERS)
    if hsts:
        headers["Strict-Transport-Security"] = HSTS_VALUE

    @app.after_request
    def _security_headers(response: Response) -> Response:
        for name, value in headers.items():
            response.headers.setdefault(name, value)
        return response
