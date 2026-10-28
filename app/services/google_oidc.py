"""Google 登入（OpenID Connect 授權碼流程）的用戶端。見 google-sign-in 規格。

為什麼自己組裝而不用 Authlib 的 Flask 整合：它把 state、nonce 存進 Flask session，與「登入 cookie 只含 uid 與 tv」
衝突（AGENTS.md 第 10 節）。這裡只用標準函式庫 `urllib`（5 秒逾時的 HTTP）加 PyJWT（簽章驗證）。

防護：
- PKCE（S256）、`state`、`nonce`：產生與綁定在瀏覽器上的部分由 `GoogleSignInService` 與 Controller 負責，這裡負責驗證。
- ID token：只接受 RS256；金鑰依 `kid` 從 Google 的 JWKS 取得；驗證簽章、發行者（兩種寫法）、受眾（本系統的用戶端識別碼）、
  過期（容許 60 秒時鐘誤差）、`nonce`；並要求有穩定的使用者識別（`sub`）、`email`，且 `email_verified` 為真。
- 呼叫 Google 的等待上限 5 秒；網路問題與 Google 端錯誤一律歸為「暫時無法使用」，被 Google 拒絕或驗證不通過歸為「被拒絕」。
- JWKS 的記憶體快取只是效能最佳化（公開資料、遺失可重建），不保存任何使用者或流程狀態。
- 用戶端密鑰只出現在送往 Google 的請求中，不會出現在例外訊息或日誌。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Protocol

import jwt
from jwt.exceptions import PyJWTError

from app.config import GoogleSettings

AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")
SCOPE = "openid email profile"
HTTP_TIMEOUT_SECONDS = 5.0
CLOCK_LEEWAY_SECONDS = 60
JWKS_CACHE_SECONDS = 3600
MAX_SUBJECT_LENGTH = 255


class GoogleAuthError(Exception):
    """Google 登入失敗。`reason` 為 `unavailable`（網路或 Google 端問題）或 `token`（被拒絕、驗證不通過）。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class GoogleProfile:
    subject: str
    email: str
    email_verified: bool
    name: str | None


class GoogleIdentityProvider(Protocol):
    def build_authorization_url(self, *, state: str, nonce: str, code_challenge: str) -> str: ...

    def exchange_code(self, *, code: str, code_verifier: str, expected_nonce: str) -> GoogleProfile: ...


class HttpTransport(Protocol):
    """HTTP 傳輸層（測試時以假的取代）。回傳 (狀態碼, 解析後的 JSON)；網路層失敗拋出 OSError。"""

    def post_form(self, url: str, data: dict[str, str], timeout: float) -> tuple[int, Any]: ...

    def get_json(self, url: str, timeout: float) -> tuple[int, Any]: ...


# ---- PKCE ---------------------------------------------------------------------

def new_code_verifier() -> str:
    """RFC 7636：43 至 128 個字元；token_urlsafe(64) 產生 86 個字元。"""
    return secrets.token_urlsafe(64)


def code_challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_state_or_nonce() -> str:
    return secrets.token_urlsafe(32)


# ---- 傳輸層 -------------------------------------------------------------------

class UrllibTransport:
    def _open(self, request: urllib.request.Request, timeout: float) -> tuple[int, Any]:
        context = ssl.create_default_context()
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                return response.status, _parse_json(response.read())
        except urllib.error.HTTPError as error:        # Google 回了錯誤狀態碼：仍是「有回應」，交給呼叫端判斷
            try:
                return error.code, _parse_json(error.read())
            finally:
                error.close()
        except urllib.error.URLError as error:
            raise OSError("無法連線到 Google") from error

    def post_form(self, url: str, data: dict[str, str], timeout: float) -> tuple[int, Any]:
        body = urllib.parse.urlencode(data).encode("ascii")
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        return self._open(request, timeout)

    def get_json(self, url: str, timeout: float) -> tuple[int, Any]:
        return self._open(urllib.request.Request(url, method="GET"), timeout)


def _parse_json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


# ---- 用戶端 -------------------------------------------------------------------

class GoogleOidcClient:
    def __init__(
        self,
        settings: GoogleSettings,
        *,
        redirect_uri: str,
        transport: HttpTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._redirect_uri = redirect_uri
        self._transport = transport or UrllibTransport()
        self._monotonic = monotonic
        self._jwks: dict[str, Any] = {}
        self._jwks_fetched_at: float | None = None

    # ---- 授權網址 ----
    def build_authorization_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": self._settings.client_id,
                "redirect_uri": self._redirect_uri,
                "scope": SCOPE,
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "prompt": "select_account",
            }
        )
        return f"{AUTHORIZATION_URL}?{query}"

    # ---- 授權碼交換與 ID token 驗證 ----
    def exchange_code(self, *, code: str, code_verifier: str, expected_nonce: str) -> GoogleProfile:
        id_token = self._request_id_token(code, code_verifier)
        claims = self._verify(id_token, expected_nonce)
        email = claims["email"]
        name = claims.get("name")
        return GoogleProfile(
            subject=claims["sub"],
            email=email,
            email_verified=True,
            name=name if isinstance(name, str) and name.strip() else None,
        )

    def _request_id_token(self, code: str, code_verifier: str) -> str:
        form = {
            "code": code,
            "client_id": self._settings.client_id,
            "client_secret": self._settings.client_secret.reveal(),
            "redirect_uri": self._redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
        }
        try:
            status, body = self._transport.post_form(TOKEN_URL, form, HTTP_TIMEOUT_SECONDS)
        except OSError:                                # 含逾時、連線被拒；訊息可能夾帶細節，不往外傳
            raise GoogleAuthError("unavailable") from None
        if status >= 500:
            raise GoogleAuthError("unavailable")
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get("id_token"), str):
            raise GoogleAuthError("token")             # 授權碼無效、已使用、過期，或回應不符預期
        return body["id_token"]

    def _verify(self, id_token: str, expected_nonce: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(id_token)
        except PyJWTError:
            raise GoogleAuthError("token") from None
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise GoogleAuthError("token")             # 只接受 RS256；"none"、HS256 等一律拒絕
        key = self._signing_key(header["kid"])
        try:
            claims = jwt.decode(
                id_token,
                key,
                algorithms=["RS256"],
                audience=self._settings.client_id,
                leeway=CLOCK_LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "iss", "aud", "sub"], "verify_iss": False},
            )
        except PyJWTError:
            raise GoogleAuthError("token") from None
        if claims.get("iss") not in ISSUERS:
            raise GoogleAuthError("token")
        nonce = claims.get("nonce")
        if not isinstance(nonce, str) or not hmac.compare_digest(nonce.encode("utf-8"), expected_nonce.encode("utf-8")):
            raise GoogleAuthError("token")
        subject, email = claims.get("sub"), claims.get("email")
        if not isinstance(subject, str) or not subject or len(subject) > MAX_SUBJECT_LENGTH:
            raise GoogleAuthError("token")
        if not isinstance(email, str) or not email:
            raise GoogleAuthError("token")
        if claims.get("email_verified") not in (True, "true"):
            raise GoogleAuthError("token")             # Google 沒有驗證過這個信箱：不能拿來合併或建立帳號
        return claims

    # ---- Google 的公開金鑰（JWKS）----
    def _signing_key(self, kid: str) -> Any:
        stale = self._jwks_fetched_at is None or self._monotonic() - self._jwks_fetched_at > JWKS_CACHE_SECONDS
        if stale or kid not in self._jwks:
            self._refresh_jwks()                       # 金鑰輪替時，遇到未知的 kid 重新抓一次
        if kid not in self._jwks:
            raise GoogleAuthError("token")
        return self._jwks[kid]

    def _refresh_jwks(self) -> None:
        try:
            status, body = self._transport.get_json(JWKS_URL, HTTP_TIMEOUT_SECONDS)
        except OSError:
            raise GoogleAuthError("unavailable") from None
        if status >= 500 or status != 200 or not isinstance(body, dict) or not isinstance(body.get("keys"), list):
            raise GoogleAuthError("unavailable")
        keys: dict[str, Any] = {}
        for item in body["keys"]:
            try:
                parsed = jwt.PyJWK(item)
            except PyJWTError:
                continue
            if parsed.key_id:
                keys[parsed.key_id] = parsed.key
        self._jwks = keys
        self._jwks_fetched_at = self._monotonic()
