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
from dataclasses import dataclass, replace
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
REQUIRED_SECRETS = ("SECRET_KEY", "DATABASE_URL", "GEMINI_API_KEY")

# ---- 模型呼叫（C-3）：模型名稱與逾時是設定值，不寫死在服務程式裡（AGENTS.md 第 5 節）----
DEFAULT_PRIMARY_MODEL = "gemini-3.8-flash"
DEFAULT_FALLBACK_MODEL = "gemini-3.5-flash-lite"
DEFAULT_CALL_TIMEOUT_SECONDS = 20.0       # 單次模型呼叫；逾時即切換備援
DEFAULT_REQUEST_BUDGET_SECONDS = 30.0     # 單次請求總預算（含重試與切換備援）
MAX_REQUEST_BUDGET_SECONDS = 55.0         # 預算必須小於它：gunicorn worker 逾時是 60 秒，要讓降級回覆先送達
DEFAULT_BREAKER_SECONDS = 60.0            # 主要模型逾時或伺服器錯誤後，暫時跳過它的時間
DEFAULT_MAX_OUTPUT_TOKENS = 4096          # 含供應商計入的思考 token
MIN_OUTPUT_TOKENS, MAX_OUTPUT_TOKENS = 256, 32768
_MODEL_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
# OpenAI 相容端點的模型識別碼含供應商與變體（例如 vendor/model:free），所以另允許斜線與冒號。
# 長度上限與 Gemini 相同（64），因為帳本 `llm_call_logs.model` 欄位只有 64 字元：設定接受的名稱必須寫得進帳本，
# 否則 SQL Server 會截斷或出錯，斷路（以模型名稱查帳本）也會悄悄失效。
_OPENAI_MODEL_NAME = re.compile(r"[A-Za-z0-9._:/-]{1,64}")
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")

# ---- 全站每日用量上限（C-4a）：預設值依 gemini-3.5-flash-lite 免費層（15 RPM、25 萬 TPM、每日 500 次請求）----
# 呼叫次數取 RPD 的 90%（留給進行中的請求）；token 取一般對話約 330 輪。換模型或供應商時須重設這兩個變數。
DEFAULT_DAILY_TOKEN_LIMIT = 1_000_000
DEFAULT_DAILY_CALL_LIMIT = 450
MAX_DAILY_TOKEN_LIMIT = 1_000_000_000
MAX_DAILY_CALL_LIMIT = 10_000_000

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
class AlertSettings:
    """營運告警（ops-alerting）：收件人。沒有設定收件人時（development 與 testing 可以）告警檢查只評估、不寄信。"""

    recipients: tuple[str, ...] = ()


@dataclass(frozen=True)
class GoogleSettings:
    """Google 登入（OAuth 用戶端）設定；識別碼與密鑰兩者都設定時才存在。"""

    client_id: str
    client_secret: Secret


@dataclass(frozen=True)
class LlmSettings:
    """模型呼叫設定。沒有金鑰時 `api_key` 為空：客服功能不可用（503），但應用程式仍可啟動（staging 與 production 例外）。"""

    api_key: Secret | None = None
    primary_model: str = DEFAULT_PRIMARY_MODEL
    fallback_model: str = DEFAULT_FALLBACK_MODEL
    summary_model: str = DEFAULT_PRIMARY_MODEL    # 未指定時與主要模型相同
    call_timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS
    request_budget_seconds: float = DEFAULT_REQUEST_BUDGET_SECONDS
    breaker_seconds: float = DEFAULT_BREAKER_SECONDS
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    thinking_budget: int | None = None            # 不設定 = 使用供應商的預設
    base_url: str | None = None                   # Gemini 端點覆寫（GEMINI_BASE_URL，僅非正式環境）；None = 轉接器的預設端點

    @property
    def enabled(self) -> bool:
        return self.api_key is not None

    @property
    def frontend_timeout_seconds(self) -> float:
        """前端請求逾時：後端總預算加 5 秒，使伺服器自己的降級回覆先於前端的逾時送達。"""
        return self.request_budget_seconds + 5


@dataclass(frozen=True)
class OpenAICompatibleSettings:
    """RPG 敘事用途的 OpenAI 相容端點設定（見 openai-compatible-provider）。

    `llm` 只用到金鑰與主要、備援模型；逾時、預算與輸出上限沿用客服的 `LLM_*`（一個地方調整），思考預算是 Gemini 專屬所以為空。
    `base_url` 為 None 表示使用轉接器的預設網址（供應商網域只出現在 `app/llm/`，不寫在設定裡）。
    """

    llm: LlmSettings
    base_url: str | None = None
    zero_retention: bool = False            # 要求只路由到不保存提示的端點（付費模型才有；免費端點通常沒有）
    supports_json_schema: bool = False      # 該模型支援結構描述輸出參數時才開啟；預設以系統指令附結構描述


@dataclass(frozen=True)
class ContentSettings:
    """內容分級的環境設定（AGENTS.md 第 7 節）。等級 3 預設關閉，只能以環境設定開啟。"""

    level3_enabled: bool = False
    level3_models: tuple[str, ...] = ()     # 取得書面許可所涵蓋的模型識別碼；開啟時 RPG 用途的模型必須都在裡面


@dataclass(frozen=True)
class UsageSettings:
    """全站每日用量上限（以 Asia/Taipei 的日界計）；每位使用者依等級的限制是程式常數，見 `quota_policy`。"""

    daily_token_limit: int = DEFAULT_DAILY_TOKEN_LIMIT
    daily_call_limit: int = DEFAULT_DAILY_CALL_LIMIT


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
    # ---- 客服與模型呼叫（C-3）----
    llm: LlmSettings = LlmSettings()
    # ---- 用量限制（C-4a）----
    usage: UsageSettings = UsageSettings()
    # ---- RPG 用途的 OpenAI 相容端點與內容分級（openai-compatible-provider）----
    rpg_llm: OpenAICompatibleSettings | None = None
    content: ContentSettings = ContentSettings()
    # ---- 營運告警（ops-alerting）----
    alerts: AlertSettings = AlertSettings()

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


MAX_ALERT_RECIPIENTS = 5


def _resolve_alerts(env: str, values: Mapping[str, str]) -> AlertSettings:
    """告警收件人 `ALERT_EMAIL_TO`：逗號分隔、去除空白與重複（不分大小寫）、至多 5 個、每個都是單純的電子郵件地址。

    staging 與 production 必須設定（缺漏時訊息只列變數名稱）；development 與 testing 可以省略。錯誤訊息不回顯設定的值。
    """
    parts = [part.strip() for part in _get(values, "ALERT_EMAIL_TO").split(",") if part.strip()]
    if not parts:
        if env in PRODUCTION_LIKE_ENVS:
            raise ConfigError(f"{env} 環境缺少必要的環境變數：ALERT_EMAIL_TO。請由平台機密功能注入。")
        return AlertSettings()
    recipients: list[str] = []
    for address in parts:
        if not _PLAIN_ADDRESS.fullmatch(address) or "\x00" in address:
            raise ConfigError("ALERT_EMAIL_TO 的每個地址都必須是單純的電子郵件地址（例如 ops@example.com），多個以逗號分隔。")
        if address.lower() not in {existing.lower() for existing in recipients}:
            recipients.append(address)
    if len(recipients) > MAX_ALERT_RECIPIENTS:
        raise ConfigError(f"ALERT_EMAIL_TO 最多只能設定 {MAX_ALERT_RECIPIENTS} 個地址。")
    return AlertSettings(recipients=tuple(recipients))


def _resolve_mail(env: str, values: Mapping[str, str], *, reason: str = "信箱驗證已開啟") -> MailSettings:
    """寄信設定；呼叫端只在信箱驗證開啟、或設定了告警收件人時才需要它。"""
    required = ("MAIL_SMTP_HOST", "MAIL_SMTP_PORT", "MAIL_FROM")
    missing = [name for name in required if not _get(values, name)]
    if missing:
        raise ConfigError(f"{reason}，但缺少寄信設定：" + "、".join(missing) + "。")

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


def _model_name(values: Mapping[str, str], name: str, default: str) -> str:
    raw = _get(values, name)
    if not raw:
        return default
    if not _MODEL_NAME.fullmatch(raw):
        raise ConfigError(f"{name} 不是合法的模型名稱（只允許英文字母、數字、點、底線與連字號，最長 64 字）。")
    return raw


def _seconds(values: Mapping[str, str], name: str, default: float) -> float:
    raw = _get(values, name)
    if not raw:
        return default
    try:
        number = float(raw)
    except ValueError:
        number = 0.0
    if not 0 < number < float("inf"):        # 0、負數、nan（比較為假）與 inf 都不合法
        raise ConfigError(f"{name} 必須是大於 0 的數字（秒）。")
    return number


def _resolve_gemini_base_url(env: str, values: Mapping[str, str]) -> str | None:
    """Gemini 端點覆寫，供隔離的壓測環境指向假模型伺服器。金鑰與提示詞都會被送到這個位址，所以：
    production 設定了就啟動失敗；其餘環境必須是 https（development 與 testing 另允許回送位址的 http）；
    不得含帳號、密碼、查詢字串與片段（路徑可以，預設端點本身就帶 /v1beta）。錯誤訊息只指出變數名稱，不回顯網址。"""
    raw = _get(values, "GEMINI_BASE_URL")
    if not raw:
        return None
    if env == "production":
        raise ConfigError("production 環境不得設定 GEMINI_BASE_URL：金鑰與提示詞會被送往該位址，只有非正式環境的壓測可以覆寫。")
    message = "GEMINI_BASE_URL 必須是不含帳號、密碼、查詢與片段的絕對網址，且使用 https（development 與 testing 的回送位址可用 http）。"
    try:
        parts = urlsplit(raw)
        _ = parts.port
    except ValueError as exc:
        raise ConfigError(message) from exc
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.query
        or parts.fragment
        or parts.username is not None
        or parts.password is not None
    ):
        raise ConfigError(message)
    if parts.scheme == "http" and not (env in DEV_TOOL_ENVS and parts.hostname.lower() in _LOOPBACK_HOSTS):
        raise ConfigError(message)
    return raw.rstrip("/")


def _resolve_trust_proxy_headers(env: str, values: Mapping[str, str]) -> bool:
    """開發環境經 HTTPS 通道（例如 ngrok）使用時，選擇信任轉送的協定與主機標頭。只有 development 可以設定：
    staging 與 production 本來就信任入口層，不需要第二個開關；testing 不該被通道影響。其餘環境設定了（不論值）即啟動失敗。
    錯誤訊息只指出變數名稱，不回顯值。"""
    raw = _get(values, "TRUST_PROXY_HEADERS")
    if not raw:
        return False
    if env != "development":
        raise ConfigError(f"{env} 環境不得設定 TRUST_PROXY_HEADERS：這個開關只給 development 經 HTTPS 通道使用。")
    return _bool_setting(values, "TRUST_PROXY_HEADERS")


def _resolve_llm(env: str, values: Mapping[str, str]) -> LlmSettings:
    """模型呼叫設定。金鑰為選用（缺漏的處理由呼叫端依環境決定）；其餘各項有預設值，不合法一律啟動失敗。"""
    key = _get(values, "GEMINI_API_KEY")
    primary = _model_name(values, "GEMINI_MODEL", DEFAULT_PRIMARY_MODEL)
    call_timeout = _seconds(values, "LLM_CALL_TIMEOUT_SECONDS", DEFAULT_CALL_TIMEOUT_SECONDS)
    budget = _seconds(values, "LLM_REQUEST_BUDGET_SECONDS", DEFAULT_REQUEST_BUDGET_SECONDS)
    if budget >= MAX_REQUEST_BUDGET_SECONDS:
        raise ConfigError(f"LLM_REQUEST_BUDGET_SECONDS 必須小於 {MAX_REQUEST_BUDGET_SECONDS:g} 秒（worker 逾時為 60 秒）。")
    if call_timeout > budget:
        raise ConfigError("LLM_CALL_TIMEOUT_SECONDS 不得大於 LLM_REQUEST_BUDGET_SECONDS。")

    raw_tokens = _get(values, "LLM_MAX_OUTPUT_TOKENS")
    try:
        max_tokens = int(raw_tokens) if raw_tokens else DEFAULT_MAX_OUTPUT_TOKENS
    except ValueError:
        max_tokens = 0
    if not MIN_OUTPUT_TOKENS <= max_tokens <= MAX_OUTPUT_TOKENS:
        raise ConfigError(f"LLM_MAX_OUTPUT_TOKENS 必須是 {MIN_OUTPUT_TOKENS} 至 {MAX_OUTPUT_TOKENS} 的整數。")

    raw_thinking = _get(values, "GEMINI_THINKING_BUDGET")
    thinking: int | None = None
    if raw_thinking:
        try:
            thinking = int(raw_thinking)
        except ValueError:
            thinking = -1
        if thinking < 0:
            raise ConfigError("GEMINI_THINKING_BUDGET 必須是 0 或正整數。")

    return LlmSettings(
        api_key=Secret(key) if key else None,
        primary_model=primary,
        fallback_model=_model_name(values, "GEMINI_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL),
        summary_model=_model_name(values, "GEMINI_SUMMARY_MODEL", primary),
        call_timeout_seconds=call_timeout,
        request_budget_seconds=budget,
        breaker_seconds=_seconds(values, "LLM_BREAKER_SECONDS", DEFAULT_BREAKER_SECONDS),
        max_output_tokens=max_tokens,
        thinking_budget=thinking,
        base_url=_resolve_gemini_base_url(env, values),
    )


def _bool_setting(values: Mapping[str, str], name: str) -> bool:
    """布林開關：只接受 true、false（不分大小寫）；沒設定為 false。錯誤訊息只含變數名稱，不含它的值。"""
    raw = _get(values, name).lower()
    if not raw:
        return False
    if raw not in ("true", "false"):
        raise ConfigError(f"{name} 的值不合法。合法值為：true、false。")
    return raw == "true"


def _openai_model_name(values: Mapping[str, str], name: str) -> str | None:
    raw = _get(values, name)
    if not raw:
        return None
    if not _OPENAI_MODEL_NAME.fullmatch(raw):
        raise ConfigError(f"{name} 不是合法的模型名稱（只允許英文字母、數字、點、底線、連字號、斜線與冒號，最長 64 字）。")
    return raw


def _resolve_openai_base_url(env: str, values: Mapping[str, str]) -> str | None:
    """OpenAI 相容端點的基底網址。金鑰會隨它一起送出，所以只允許 https；development 與 testing 另允許回送位址的 http
    （本機模型）。不得含帳號、密碼、查詢字串或片段。錯誤訊息只指出變數名稱，不含網址的內容。"""
    raw = _get(values, "OPENROUTER_BASE_URL")
    if not raw:
        return None
    message = "OPENROUTER_BASE_URL 必須是不含帳號、密碼、查詢與片段的絕對網址，且使用 https（本機模型只限 development 與 testing 的回送位址可用 http）。"
    try:
        parts = urlsplit(raw)
        _ = parts.port
    except ValueError as exc:
        raise ConfigError(message) from exc
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.query
        or parts.fragment
        or parts.username is not None
        or parts.password is not None
    ):
        raise ConfigError(message)
    if parts.scheme == "http" and not (env in DEV_TOOL_ENVS and parts.hostname.lower() in _LOOPBACK_HOSTS):
        raise ConfigError(message)
    return raw.rstrip("/")


def _resolve_rpg_llm(env: str, values: Mapping[str, str], base: LlmSettings) -> OpenAICompatibleSettings | None:
    """RPG 敘事用途的供應商設定。要嘛完整（金鑰與主要模型）要嘛完全沒有：只有一個就啟動失敗。
    備援模型未設定時等於主要模型（既有流程對「主要與備援相同」的處理是暫時性錯誤直接降級）。"""
    key = _get(values, "OPENROUTER_API_KEY")
    primary = _openai_model_name(values, "OPENROUTER_MODEL")
    if not key and primary is None:
        return None
    if not key or primary is None:
        missing = "OPENROUTER_API_KEY" if not key else "OPENROUTER_MODEL"
        raise ConfigError(f"RPG 用途的模型設定不完整：缺少 {missing}（金鑰與主要模型須同時設定或同時省略）。")
    fallback = _openai_model_name(values, "OPENROUTER_FALLBACK_MODEL") or primary
    return OpenAICompatibleSettings(
        llm=replace(base, api_key=Secret(key), primary_model=primary, fallback_model=fallback, summary_model=primary, thinking_budget=None, base_url=None),
        base_url=_resolve_openai_base_url(env, values),
        zero_retention=_bool_setting(values, "OPENROUTER_ZDR"),
        supports_json_schema=_bool_setting(values, "OPENROUTER_JSON_SCHEMA"),
    )


def _resolve_content(env: str, values: Mapping[str, str], rpg: OpenAICompatibleSettings | None) -> ContentSettings:
    """內容分級的環境設定。等級 3 預設關閉；開啟必須同時滿足（AGENTS.md 第 7 節）：
    - 不得在 staging 或 production（強化的成年驗證完成之前不得對一般會員開啟）；
    - RPG 用途已設定，且 `SEXUAL_CONTENT_LEVEL3_MODELS`（許可涵蓋的模型）非空並涵蓋 RPG 用途的主要與備援模型。
    清單只在開關開啟時才驗證與生效：關閉時不影響任何行為。錯誤訊息只含變數名稱。"""
    enabled = _bool_setting(values, "SEXUAL_CONTENT_LEVEL3_ENABLED")
    models = tuple(part.strip() for part in _get(values, "SEXUAL_CONTENT_LEVEL3_MODELS").split(",") if part.strip())
    if not enabled:
        return ContentSettings(level3_enabled=False, level3_models=models)
    if env in PRODUCTION_LIKE_ENVS:
        raise ConfigError(
            f"{env} 環境不得開啟 SEXUAL_CONTENT_LEVEL3_ENABLED：強化的成年驗證尚未完成，不得對一般會員開啟。"
        )
    if rpg is None:
        raise ConfigError("SEXUAL_CONTENT_LEVEL3_ENABLED 已開啟，但 RPG 用途尚未設定（OPENROUTER_API_KEY 與 OPENROUTER_MODEL）。")
    if not models:
        raise ConfigError("SEXUAL_CONTENT_LEVEL3_ENABLED 已開啟，但 SEXUAL_CONTENT_LEVEL3_MODELS 沒有設定（須列出許可涵蓋的模型）。")
    if any(not _OPENAI_MODEL_NAME.fullmatch(name) for name in models):
        raise ConfigError("SEXUAL_CONTENT_LEVEL3_MODELS 含有不合法的模型名稱。")
    uncovered = [
        variable
        for variable, name in (("OPENROUTER_MODEL", rpg.llm.primary_model), ("OPENROUTER_FALLBACK_MODEL", rpg.llm.fallback_model))
        if name not in models
    ]
    if uncovered:
        raise ConfigError("SEXUAL_CONTENT_LEVEL3_MODELS 沒有涵蓋 RPG 用途使用的模型：" + "、".join(dict.fromkeys(uncovered)) + "。")
    return ContentSettings(level3_enabled=True, level3_models=models)


def _positive_int(values: Mapping[str, str], name: str, default: int, maximum: int) -> int:
    raw = _get(values, name)
    if not raw:
        return default
    try:
        number = int(raw)
    except ValueError:
        number = 0
    if not 1 <= number <= maximum:
        raise ConfigError(f"{name} 必須是 1 至 {maximum} 的整數。")
    return number


def _resolve_usage(values: Mapping[str, str]) -> UsageSettings:
    return UsageSettings(
        daily_token_limit=_positive_int(values, "USAGE_DAILY_TOKEN_LIMIT", DEFAULT_DAILY_TOKEN_LIMIT, MAX_DAILY_TOKEN_LIMIT),
        daily_call_limit=_positive_int(values, "USAGE_DAILY_CALL_LIMIT", DEFAULT_DAILY_CALL_LIMIT, MAX_DAILY_CALL_LIMIT),
    )


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
    alerts = _resolve_alerts(env, values)
    if email_verification_required:
        mail = _resolve_mail(env, values)
    elif alerts.recipients:
        mail = _resolve_mail(env, values, reason="已設定告警收件人（ALERT_EMAIL_TO）")
    else:
        mail = None
    if (email_verification_required or google) and not base_url:
        reason = "信箱驗證已開啟" if email_verification_required else "Google 登入已啟用"
        raise ConfigError(f"{reason}，但缺少對外網址 BASE_URL（用於信件連結與 OAuth 回呼）。")

    llm = _resolve_llm(env, values)
    rpg_llm = _resolve_rpg_llm(env, values, llm)
    content = _resolve_content(env, values, rpg_llm)
    tunnel_trusts_proxy = _resolve_trust_proxy_headers(env, values)

    return AppConfig(
        env=env,
        debug=debug,
        secret_key=Secret(secret_key),
        database_url=Secret(database_url),
        log_level=log_level,
        trust_proxy=env in PRODUCTION_LIKE_ENVS or tunnel_trusts_proxy,
        email_verification_required=email_verification_required,
        base_url=base_url,
        mail=mail,
        google=google,
        secret_key_fallbacks=_resolve_secret_key_fallbacks(values),
        llm=llm,
        usage=_resolve_usage(values),
        rpg_llm=rpg_llm,
        content=content,
        alerts=alerts,
    )
