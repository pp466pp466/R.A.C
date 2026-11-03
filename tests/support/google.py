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


# ---- 走完整的 Google 登入流程（發起 → 回呼）-----------------------------------------------------------------

def google_sign_in(app, client, **start_body):
    """以假的供應商走一遍 Google 登入：發起（預設勾選條款）再以正確的 state 完成回呼。回傳回呼的回應（302）。"""
    from app.security.cookies import SignedCookies
    from app.security.oauth_cookie import OAUTH_SALT

    started = client.post("/api/auth/google/start", json={"accept_terms": True, **start_body})
    assert started.status_code == 200, started.get_data(as_text=True)
    cookies = SignedCookies(app.extensions["app_config"], app.extensions["clock"])
    transaction = cookies.loads(OAUTH_SALT, client.get_cookie("oauth_tx").value, max_age_seconds=600)
    return client.get(f"/auth/google/callback?code=code-from-google&state={transaction['s']}")


def admin_sign_in(app, client, password: str, **start_body):
    """管理員的完整登入：先通過 Google，再輸入密碼（兩者都通過才有登入 session）。回傳密碼驗證的回應。"""
    step = google_sign_in(app, client, **start_body)
    assert step.status_code == 302 and step.headers["Location"] == "/login/password", step.headers.get("Location")
    return client.post("/api/auth/admin-password", json={"password": password})
