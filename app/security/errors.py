"""HTTP 安全輔助拋出的錯誤；由 controllers/errors.py 統一轉成 JSON（API）或錯誤頁。"""
from __future__ import annotations


class HttpSecurityError(Exception):
    """被 HTTP 層的安全檢查擋下（例如 CSRF）。`status`、`code`、`message` 直接用於回應。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
