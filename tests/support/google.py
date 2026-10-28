"""Google 登入測試的共用輔助：假的身分供應商（不連網）。"""
from __future__ import annotations

from app.services.google_oidc import GoogleAuthError, GoogleProfile

SUB = "110169484474386276334"


class FakeProvider:
    """代替 `GoogleOidcClient`：記錄收到的呼叫；可設定要回傳的個人資料，或要拋出的錯誤。"""

    def __init__(self) -> None:
        self.profile = GoogleProfile(subject=SUB, email="person@example.com", email_verified=True, name="王小明")
        self.error: GoogleAuthError | None = None
        self.exchanges: list[dict] = []
        self.urls: list[dict] = []

    def build_authorization_url(self, *, state, nonce, code_challenge):
        self.urls.append({"state": state, "nonce": nonce, "code_challenge": code_challenge})
        return f"https://accounts.example/auth?state={state}&nonce={nonce}&code_challenge={code_challenge}"

    def exchange_code(self, *, code, code_verifier, expected_nonce):
        self.exchanges.append({"code": code, "code_verifier": code_verifier, "expected_nonce": expected_nonce})
        if self.error:
            raise self.error
        return self.profile
