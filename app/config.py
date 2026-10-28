"""各環境設定、機密遮罩與啟動驗證（見 AGENTS.md 第 9、10 節）。

原則：
- 一律明確指定 `APP_ENV`，未設定或不合法即拒絕啟動，不預設為任何環境。
- 只有 development 會讀取專案根目錄的 `.env`，且行程環境變數優先。
- staging 與 production 缺少必要機密、使用 sa 帳號、信任自簽憑證或開啟除錯，一律啟動失敗。
- 機密一律包在 `Secret` 內，任何字串表示都只顯示遮罩。
- 錯誤訊息只描述問題與變數名稱，絕不包含機密值。
"""
from __future__ import annotations

import logging
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from dotenv import dotenv_values
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

logger = logging.getLogger(__name__)

VALID_ENVS = ("development", "testing", "staging", "production")
PRODUCTION_LIKE_ENVS = ("staging", "production")
# 只在這些環境註冊開發用路由（例如元件示範頁）；staging 與 production 一律不提供
DEV_TOOL_ENVS = ("development", "testing")

# 專案根目錄（app/ 的上一層）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# staging 與 production 的必要機密；後續 change 會擴充此清單
REQUIRED_SECRETS = ("SECRET_KEY", "DATABASE_URL")

TESTING_DATABASE_URL = "sqlite+pysqlite:///:memory:"
TESTING_SECRET_KEY = "testing-only-not-a-secret"

# staging 與 production 的 SECRET_KEY 最小長度（登入 cookie、CSRF 與 OAuth 交易 cookie 都靠它簽章）
MIN_SECRET_KEY_LENGTH = 32

MAIL_SECURITY_MODES = ("starttls", "ssl", "none")

_TRUTHY = {"1", "true", "yes", "on"}
_VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_PLAIN_ADDRESS = re.compile(r"^[^\s@<>]+@[^\s@<>]+$")


class ConfigError(RuntimeError):
    """設定不合法；訊息只描述問題與變數名稱，絕不包含機密值。"""


class Secret:
    """機密值包裝：repr 與 str 一律遮罩，必須明確呼叫 reveal() 才能取得實際值。"""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:
        return "Secret('***')"

    def __str__(self) -> str:
        return "***"

    def __eq__(self, other: object) -> bool:  # 避免被意外拿去比對或當作字典鍵洩漏
        return isinstance(other, Secret) and self._value == other._value

    def __hash__(self) -> int:
        return hash(("Secret", self._value))


@dataclass(frozen=True)
class MailSettings:
    """寄信（SMTP）設定；只在信箱驗證開啟時存在。"""

    host: str
    port: int
    from_address: str
    security: str  # starttls、ssl、none（none 僅限 development 與 testing）
    username: str | None = None
    password: Secret | None = None


@dataclass(frozen=True)
class GoogleSettings:
    """Google 登入（OAuth 用戶端）設定；識別碼與密鑰兩者都設定時才存在。"""

    client_id: str
    client_secret: Secret


@dataclass(frozen=True)
class AppConfig:
    """已驗證的應用程式設定。"""

    env: str
    debug: bool
    secret_key: Secret
    database_url: Secret
    log_level: str
    trust_proxy: bool
    # ---- 會員驗證（C-2）；有預設值，讓只關心前面欄位的呼叫端不受影響 ----
    email_verification_required: bool = False
    base_url: str | None = None
    mail: MailSettings | None = None
    google: GoogleSettings | None = None
    secret_key_fallbacks: tuple[Secret, ...] = ()

    @property
    def is_production_like(self) -> bool:
        return self.env in PRODUCTION_LIKE_ENVS

    @property
    def google_enabled(self) -> bool:
        return self.google is not None

    @property
    def database_url_masked(self) -> str:
        """隱藏密碼後的連線字串，可安全寫入日誌。"""
        return make_url(self.database_url.reveal()).render_as_string(hide_password=True)


def _is_truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in _TRUTHY


def _resolve_env(environ: Mapping[str, str]) -> str:
    env = (environ.get("APP_ENV") or "").strip()
    if not env:
        raise ConfigError(
            "未設定環境變數 APP_ENV，無法啟動。請明確指定為："
            + "、".join(VALID_ENVS)
            + "（系統不會預設為任何環境）。"
        )
    if env not in VALID_ENVS:
        raise ConfigError(
            "APP_ENV 的值不合法。合法值為：" + "、".join(VALID_ENVS) + "。"
        )
    return env


def _merge_dotenv(
    environ: Mapping[str, str], dotenv_path: os.PathLike[str] | str | None
) -> dict[str, str]:
    """development 專用：以 .env 補足行程環境變數沒有的項目（行程環境變數優先）。"""
    path = Path(dotenv_path) if dotenv_path is not None else PROJECT_ROOT / ".env"
    merged: dict[str, str] = {}
    if path.is_file():
        merged.update({k: v for k, v in dotenv_values(path, encoding="utf-8-sig").items() if v is not None})
    merged.update(environ)
    return merged


def _odbc_connect_params(url: URL) -> dict[str, str]:
    """解析 odbc_connect 原始字串中的 key=value，供檢查帳號與憑證設定。"""
    raw = url.query.get("odbc_connect")
    if not raw:
        return {}
    text = raw if isinstance(raw, str) else raw[0]
    params: dict[str, str] = {}
    for part in text.split(";"):
        if "=" in part:
            key, _, value = part.partition("=")
            params[key.strip().lower()] = value.strip()
    return params


def _url_username(url: URL) -> str:
    username = url.username or _odbc_connect_params(url).get("uid", "")
    return username.strip().lower()


def _url_trusts_server_certificate(url: URL) -> bool:
    for key, value in url.query.items():
        if key.lower() == "trustservercertificate":
            values = value if isinstance(value, tuple) else (value,)
            if any(_is_truthy(v) for v in values):
                return True
    return _is_truthy(_odbc_connect_params(url).get("trustservercertificate"))


def _validate_production_like(env: str, values: Mapping[str, str]) -> URL:
    """staging 與 production 的嚴格驗證；回傳解析後的資料庫 URL。"""
    missing = [name for name in REQUIRED_SECRETS if not (values.get(name) or "").strip()]
    if missing:
        raise ConfigError(
            f"{env} 環境缺少必要的環境變數：" + "、".join(missing) + "。請由平台機密功能注入。"
        )
    if len(values["SECRET_KEY"].strip()) < MIN_SECRET_KEY_LENGTH:
        raise ConfigError(f"{env} 環境的 SECRET_KEY 至少需要 {MIN_SECRET_KEY_LENGTH} 個字元。")
    if _is_truthy(values.get("FLASK_DEBUG")):
        raise ConfigError(f"{env} 環境不得開啟除錯模式（FLASK_DEBUG）。")
    try:
        url = make_url(values["DATABASE_URL"])
    except ArgumentError as exc:
        raise ConfigError("DATABASE_URL 格式不正確。") from exc
    if _url_username(url) == "sa":
        raise ConfigError(f"{env} 環境的資料庫連線不得使用 sa 帳號，請改用最小權限的專用帳號。")
    if _url_trusts_server_certificate(url):
        raise ConfigError(
            f"{env} 環境的資料庫連線不得設定 TrustServerCertificate=yes；此設定只限本機開發使用。"
        )
    return url


def _get(values: Mapping[str, str], name: str) -> str:
    return (values.get(name) or "").strip()


def _resolve_email_verification(env: str, values: Mapping[str, str]) -> bool:
    """信箱驗證開關。未設定時 development、testing 為關閉，staging、production 為開啟；production 不得關閉。"""
    raw = _get(values, "EMAIL_VERIFICATION_REQUIRED").lower()
    if not raw:
        return env in PRODUCTION_LIKE_ENVS
    if raw not in ("true", "false"):
        raise ConfigError("EMAIL_VERIFICATION_REQUIRED 的值不合法。合法值為：true、false。")
    required = raw == "true"
    if env == "production" and not required:
        raise ConfigError("production 環境必須開啟信箱驗證（EMAIL_VERIFICATION_REQUIRED 不得為 false）。")
    return required


def _resolve_base_url(env: str, values: Mapping[str, str]) -> str | None:
    """對外網址（組信件連結與 OAuth 回呼用，不使用請求的主機標頭）。回傳去除結尾斜線的來源。"""
    raw = _get(values, "BASE_URL")
    if not raw:
        return None
    message = "BASE_URL 必須是不含路徑、查詢與片段的絕對網址（例如 https://example.com）。"
    try:
        parts = urlsplit(raw)
        _ = parts.port  # 連接埠不合法時在此拋出 ValueError
    except ValueError as exc:
        raise ConfigError(message) from exc
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or parts.username is not None
        or parts.password is not None
    ):
        raise ConfigError(message)
    if env in PRODUCTION_LIKE_ENVS and parts.scheme != "https":
        raise ConfigError(f"{env} 環境的 BASE_URL 必須使用 https。")
    return f"{parts.scheme}://{parts.netloc}"


def _resolve_mail(env: str, values: Mapping[str, str]) -> MailSettings:
    """寄信設定；呼叫端只在信箱驗證開啟時才需要它。"""
    required = ("MAIL_SMTP_HOST", "MAIL_SMTP_PORT", "MAIL_FROM")
    missing = [name for name in required if not _get(values, name)]
    if missing:
        raise ConfigError("信箱驗證已開啟，但缺少寄信設定：" + "、".join(missing) + "。")

    try:
        port = int(_get(values, "MAIL_SMTP_PORT"))
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise ConfigError("MAIL_SMTP_PORT 必須是 1 至 65535 的整數。")

    from_address = _get(values, "MAIL_FROM")
    if not _PLAIN_ADDRESS.fullmatch(from_address):
        raise ConfigError("MAIL_FROM 必須是單純的電子郵件地址（例如 service@example.com）。")

    security = _get(values, "MAIL_SMTP_SECURITY").lower() or "starttls"
    if security not in MAIL_SECURITY_MODES:
        raise ConfigError("MAIL_SMTP_SECURITY 的值不合法。合法值為：" + "、".join(MAIL_SECURITY_MODES) + "。")
    if security == "none" and env in PRODUCTION_LIKE_ENVS:
        raise ConfigError(f"{env} 環境的寄信連線不得使用不加密的模式（MAIL_SMTP_SECURITY）。")

    username = _get(values, "MAIL_SMTP_USER")
    password = _get(values, "MAIL_SMTP_PASSWORD")
    if bool(username) != bool(password):
        raise ConfigError("MAIL_SMTP_USER 與 MAIL_SMTP_PASSWORD 必須同時設定或同時省略。")

    return MailSettings(
        host=_get(values, "MAIL_SMTP_HOST"),
        port=port,
        from_address=from_address,
        security=security,
        username=username or None,
        password=Secret(password) if password else None,
    )


def _resolve_google(values: Mapping[str, str]) -> GoogleSettings | None:
    """Google 登入為選用：識別碼與密鑰兩者都設定才啟用，只設定一個則啟動失敗。"""
    client_id = _get(values, "GOOGLE_OAUTH_CLIENT_ID")
    client_secret = _get(values, "GOOGLE_OAUTH_CLIENT_SECRET")
    if client_id and client_secret:
        return GoogleSettings(client_id=client_id, client_secret=Secret(client_secret))
    if client_id or client_secret:
        missing = "GOOGLE_OAUTH_CLIENT_SECRET" if client_id else "GOOGLE_OAUTH_CLIENT_ID"
        raise ConfigError(f"Google 登入設定不完整：缺少 {missing}（用戶端識別碼與密鑰須同時設定）。")
    return None


def _resolve_secret_key_fallbacks(values: Mapping[str, str]) -> tuple[Secret, ...]:
    """輪替期間仍可驗證舊金鑰簽發的內容（以逗號分隔）；新簽發的內容一律使用目前的 SECRET_KEY。"""
    raw = values.get("SECRET_KEY_FALLBACKS") or ""
    return tuple(Secret(part.strip()) for part in raw.split(",") if part.strip())


def bootstrap_env_for_tools() -> None:
    """僅供命令列工具（例如 Alembic）使用：行程環境未指定 APP_ENV 時，讓開發者在 `.env` 明確寫下
    `APP_ENV=development` 或 `testing` 即可，不必每次手動設定。

    應用程式本身（`create_app`、gunicorn）不會呼叫它：環境不明時仍然拒絕啟動。
    `.env` 中若寫的是 staging 或 production，這裡一律不採用。
    """
    if os.environ.get("APP_ENV"):
        return
    env_file = PROJECT_ROOT / ".env"
    if env_file.is_file():
        declared = (dotenv_values(env_file, encoding="utf-8-sig").get("APP_ENV") or "").strip()
        if declared in ("development", "testing"):
            os.environ["APP_ENV"] = declared


def load_config(
    environ: Mapping[str, str] | None = None,
    dotenv_path: os.PathLike[str] | str | None = None,
) -> AppConfig:
    """依 APP_ENV 載入並驗證設定；不合法時拋出 ConfigError。"""
    process_env = os.environ if environ is None else environ
    env = _resolve_env(process_env)

    # 只有 development 會讀取 .env；其餘環境只使用行程環境變數
    values: Mapping[str, str] = (
        _merge_dotenv(process_env, dotenv_path) if env == "development" else process_env
    )

    if env in PRODUCTION_LIKE_ENVS:
        _validate_production_like(env, values)

    log_level = (values.get("LOG_LEVEL") or "INFO").strip().upper()
    if log_level not in _VALID_LOG_LEVELS:
        raise ConfigError("LOG_LEVEL 的值不合法。合法值為：" + "、".join(_VALID_LOG_LEVELS) + "。")

    secret_key = (values.get("SECRET_KEY") or "").strip()
    database_url = (values.get("DATABASE_URL") or "").strip()

    if env == "testing":
        secret_key = secret_key or TESTING_SECRET_KEY
        database_url = database_url or TESTING_DATABASE_URL
    elif env == "development":
        if not database_url:
            raise ConfigError(
                "development 環境缺少 DATABASE_URL。請在專案根目錄的 .env 設定（可參考 .env.example）。"
            )
        if not secret_key:
            secret_key = secrets.token_urlsafe(32)
            logger.warning(
                "SECRET_KEY 未設定，已使用僅限本機開發的臨時金鑰；重新啟動後既有的登入狀態會失效。"
            )

    debug = env in ("development", "testing") and _is_truthy(values.get("FLASK_DEBUG"))

    # ---- 會員驗證相關設定 ----
    email_verification_required = _resolve_email_verification(env, values)
    base_url = _resolve_base_url(env, values)
    google = _resolve_google(values)
    mail = _resolve_mail(env, values) if email_verification_required else None
    if (email_verification_required or google) and not base_url:
        reason = "信箱驗證已開啟" if email_verification_required else "Google 登入已啟用"
        raise ConfigError(f"{reason}，但缺少對外網址 BASE_URL（用於信件連結與 OAuth 回呼）。")

    return AppConfig(
        env=env,
        debug=debug,
        secret_key=Secret(secret_key),
        database_url=Secret(database_url),
        log_level=log_level,
        trust_proxy=env in PRODUCTION_LIKE_ENVS,
        email_verification_required=email_verification_required,
        base_url=base_url,
        mail=mail,
        google=google,
        secret_key_fallbacks=_resolve_secret_key_fallbacks(values),
    )
