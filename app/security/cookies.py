"""Cookie 的共同規則：命名、登入 cookie 的 Flask 設定（見 AGENTS.md 第 10 節）。

- 登入 cookie：簽章、HttpOnly、SameSite=Lax、閒置 30 天（每次使用就續期，不設絕對上限，見 `session.py`）；
  staging 與 production 另設 Secure 並使用 `__Host-` 前綴（要求 Secure、Path=/、無 Domain，三者皆符合）。
- 內容只含使用者內部編號與 `token_version`，不使用 JWT；CSRF 與 OAuth 交易各用獨立的 cookie，
  不放進登入 cookie。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from flask import Response
from itsdangerous import BadSignature, TimestampSigner, URLSafeTimedSerializer

from app.config import AppConfig

HOST_PREFIX = "__Host-"
SESSION_LIFETIME = timedelta(days=30)   # 閒置期限；每次使用就續期（SlidingSessionInterface）


def cookie_name(config: AppConfig, base: str) -> str:
    """staging 與 production 的 cookie 一律加 `__Host-` 前綴，其餘環境維持原名。"""
    return f"{HOST_PREFIX}{base}" if config.is_production_like else base


def flask_session_settings(config: AppConfig) -> dict[str, Any]:
    """登入 cookie 的 Flask 設定，以及簽章金鑰輪替用的舊金鑰。"""
    return {
        "SESSION_COOKIE_NAME": cookie_name(config, "session"),
        "SESSION_COOKIE_SECURE": config.is_production_like,
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_PATH": "/",
        "SESSION_COOKIE_DOMAIN": None,  # 不設 Domain：cookie 只屬於目前主機（__Host- 的要求）
        "PERMANENT_SESSION_LIFETIME": SESSION_LIFETIME,
        # 閒置 30 天、每次使用就續期（SlidingSessionInterface 決定何時重發）；Flask 用它當讀取時的簽章 max_age
        "SECRET_KEY_FALLBACKS": [secret.reveal() for secret in config.secret_key_fallbacks],
    }


class SignedCookies:
    """簽章的短效 cookie（CSRF 權杖、OAuth 交易）的讀寫工具。

    - 以 `SECRET_KEY` 簽章，並以 `SECRET_KEY_FALLBACKS` 驗證輪替期間的舊簽章（最後一把金鑰負責簽發）；
    - 不同用途用不同的 salt，一種 cookie 的內容無法拿去冒充另一種；
    - 到期時間以注入的時鐘判斷，測試不必真的等待；
    - 內容只是簽章、不是加密：不要放不能讓使用者本人看到的東西。
    """

    def __init__(self, config: AppConfig, clock: Callable[[], datetime]) -> None:
        self._config = config
        self._clock = clock
        keys = [secret.reveal() for secret in config.secret_key_fallbacks] + [config.secret_key.reveal()]
        self._keys = keys

        class _ClockSigner(TimestampSigner):
            def get_timestamp(self) -> int:
                return int(clock().timestamp())

            def timestamp_to_datetime(self, ts: int) -> datetime:
                return datetime.fromtimestamp(ts, tz=timezone.utc)

        self._signer_class = _ClockSigner

    def _serializer(self, salt: str) -> URLSafeTimedSerializer:
        serializer = URLSafeTimedSerializer(self._keys, salt=salt)
        serializer.signer = self._signer_class  # type: ignore[assignment]
        return serializer

    def dumps(self, salt: str, payload: Any) -> str:
        return self._serializer(salt).dumps(payload)

    def loads(self, salt: str, value: str | None, *, max_age_seconds: int) -> Any | None:
        """驗證簽章與年齡；缺少、被竄改、用途不符或已過期都回傳 None。"""
        if not value:
            return None
        try:
            return self._serializer(salt).loads(value, max_age=max_age_seconds)
        except BadSignature:  # 含簽章過期（SignatureExpired 是它的子類別）
            return None
        except Exception:  # noqa: BLE001 - 內容格式不合（例如被截斷）一律當作無效
            return None

    def set(self, response: Response, name: str, value: str, *, max_age_seconds: int | None = None) -> None:
        """寫入 cookie：HttpOnly、SameSite=Lax、Path=/、不設 Domain；staging 與 production 另加 Secure。"""
        response.set_cookie(
            cookie_name(self._config, name),
            value,
            max_age=max_age_seconds,
            httponly=True,
            samesite="Lax",
            secure=self._config.is_production_like,
            path="/",
            domain=None,
        )

    def delete(self, response: Response, name: str) -> None:
        response.delete_cookie(
            cookie_name(self._config, name),
            path="/",
            domain=None,
            secure=self._config.is_production_like,
            httponly=True,
            samesite="Lax",
        )
