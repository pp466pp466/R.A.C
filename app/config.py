"""各環境設定、機密遮罩與啟動驗證（見 AGENTS.md 第 9、10 節）。

原則：
- 一律明確指定 `APP_ENV`，未設定或不合法即拒絕啟動，不預設為任何環境。
- 只有 development 會讀取專案根目錄的 `.env`，且行程環境變數優先。
- staging 與 production 缺少必要機密、使用 sa 帳號、信任自簽憑證或開啟除錯，一律啟動失敗。
- 機密一律包在 `Secret` 內，任何字串表示都只顯示遮罩。
"""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

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

_TRUTHY = {"1", "true", "yes", "on"}
_VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


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
class AppConfig:
    """已驗證的應用程式設定。"""

    env: str
    debug: bool
    secret_key: Secret
    database_url: Secret
    log_level: str
    trust_proxy: bool

    @property
    def is_production_like(self) -> bool:
        return self.env in PRODUCTION_LIKE_ENVS

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

    return AppConfig(
        env=env,
        debug=debug,
        secret_key=Secret(secret_key),
        database_url=Secret(database_url),
        log_level=log_level,
        trust_proxy=env in PRODUCTION_LIKE_ENVS,
    )
