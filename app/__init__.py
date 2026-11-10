"""應用程式工廠（見 AGENTS.md 第 3 節）。

`create_app()` 依設定建立彼此隔離的應用程式實例：每個實例有自己的設定與資料庫引擎，
測試可同時建立多個互不影響的實例。啟動時不會建立資料表，也不會執行 migration。
"""
from __future__ import annotations

from typing import Any

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from app.cli import register_cli
from app.config import DEV_TOOL_ENVS, AppConfig, load_config
from app.controllers.account_api_controller import account_api_bp
from app.controllers.script_api_controller import script_api_bp
from app.controllers.script_page_controller import script_pages_bp
from app.controllers.game_api_controller import game_api_bp
from app.controllers.session_api_controller import session_api_bp
from app.controllers.wallet_api_controller import wallet_api_bp
from app.controllers.admin_api_controller import admin_api_bp
from app.controllers.admin_page_controller import admin_pages_bp
from app.controllers.api_controller import api_bp
from app.controllers.appeal_api_controller import appeal_api_bp
from app.controllers.auth_api_controller import auth_api_bp
from app.controllers.auth_page_controller import auth_pages_bp
from app.controllers.cs_api_controller import cs_api_bp
from app.controllers.cs_page_controller import cs_pages_bp
from app.controllers.errors import register_error_handlers
from app.controllers.health_controller import health_bp
from app.controllers.page_controller import page_bp
from app.controllers.site_controller import site_bp
from app.db.dialect import create_engine_for
from app.db.session import init_session
from app.llm.gemini import GeminiProvider
from app.llm.openai_compatible import OpenAICompatibleProvider
from app.models.base import utcnow
from app.observability.json_logging import configure_logging
from app.observability.request_logging import init_request_logging
from app.runtime import Clock
from app.security.cookies import SignedCookies, flask_session_settings
from app.security.cache_headers import init_no_store
from app.security.security_headers import init_security_headers
from app.security.csrf import init_csrf
from app.security.session import init_login_session
from app.services.age_policy import DEFAULT_AGE_POLICY, AgePolicy
from app.services.google_oidc import GoogleOidcClient
from app.services.mail_sender import SmtpMailSender

# 請求本文上限（API 一律是小型 JSON；超過回 413）
MAX_REQUEST_BYTES = 64 * 1024


def create_app(
    config: AppConfig | None = None,
    *,
    mail_sender: Any | None = None,
    identity_provider: Any | None = None,
    clock: Clock | None = None,
    age_policy: AgePolicy | None = None,
    llm_provider: Any | None = None,
    rpg_llm_provider: Any | None = None,
) -> Flask:
    """建立應用程式實例。

    後六個關鍵字參數只供注入（測試用假寄信器、假的 Google 供應商、假時鐘、不同的年齡規則、假的模型供應商，
    以及 RPG 用途的假模型供應商）；正式執行時省略即可，由設定建立真實的實作（年齡規則用 `DEFAULT_AGE_POLICY`）。
    """
    cfg = config or load_config()
    configure_logging(cfg.log_level)

    app = Flask(__name__)
    app.config.update(SECRET_KEY=cfg.secret_key.reveal(), DEBUG=cfg.debug, MAX_CONTENT_LENGTH=MAX_REQUEST_BYTES)
    app.config.update(flask_session_settings(cfg))
    app.json.ensure_ascii = False  # Flask 3 起用此屬性；回應中的繁體中文不轉成 \uXXXX
    app.extensions["app_config"] = cfg
    app.extensions["clock"] = clock or utcnow
    app.extensions["age_policy"] = age_policy or DEFAULT_AGE_POLICY
    # 寄信器：測試可注入假的；否則在設定了寄信參數（告警收件人或任何 MAIL_*）時以設定建立真實的 SMTP 寄信器
    app.extensions["mail_sender"] = mail_sender or (SmtpMailSender(cfg.mail) if cfg.mail else None)
    # Google 身分供應商：測試可注入假的；否則在設定了 Google 用戶端時以設定建立真實的 OIDC 用戶端
    app.extensions["identity_provider"] = identity_provider or (
        GoogleOidcClient(cfg.google, redirect_uri=f"{cfg.base_url}/auth/google/callback") if cfg.google else None
    )
    # 模型供應商：測試可注入假的；否則在設定了 GEMINI_API_KEY 時建立 Gemini 轉接器；兩者皆無則為空（需要模型的功能回 503）
    # GEMINI_BASE_URL（僅非正式環境）讓隔離的壓測環境把轉接器指向假模型伺服器；未設定時用轉接器的預設端點
    app.extensions["llm_provider"] = llm_provider or (
        GeminiProvider(cfg.llm.api_key.reveal(), **({"base_url": cfg.llm.base_url} if cfg.llm.base_url else {}))
        if cfg.llm.enabled
        else None
    )
    # RPG 敘事用途的供應商（OpenAI 相容端點）：與客服、摘要的 Gemini 互相獨立；沒有設定且沒有注入時為空（只有該用途回 503）
    app.extensions["rpg_llm_provider"] = rpg_llm_provider or (
        OpenAICompatibleProvider(
            cfg.rpg_llm.llm.api_key.reveal(),
            **({"base_url": cfg.rpg_llm.base_url} if cfg.rpg_llm.base_url else {}),
            supports_json_schema=cfg.rpg_llm.supports_json_schema,
            zero_retention=cfg.rpg_llm.zero_retention,
            reasoning=cfg.rpg_llm.reasoning,
        )
        if cfg.rpg_llm
        else None
    )
    app.extensions["signed_cookies"] = SignedCookies(cfg, app.extensions["clock"])
    init_login_session(app)

    if cfg.trust_proxy:
        # staging 與 production：信任入口層（HTTPS 終止端）轉送的協定與主機標頭；
        # development 只有明確設定 TRUST_PROXY_HEADERS（經 HTTPS 通道使用）時才信任
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # type: ignore[method-assign]

    # 請求日誌必須先於 session 註冊：after_request 以相反順序執行，這樣它最後執行，
    # 記錄到的才是最終狀態碼（例如提交失敗後變成的 500）
    init_request_logging(app)
    init_session(app, create_engine_for(cfg.database_url.reveal()))
    init_csrf(app)  # 在 init_session 之後：被拒絕的請求仍會走完 after_request，日誌才記得到最終狀態碼
    init_no_store(app)
    # HSTS 只在 staging 與 production 送出；development 即使經通道也不送（瀏覽器會把 HSTS 記在通道網域，牽連之後的本機開發）
    init_security_headers(app, hsts=cfg.is_production_like)

    app.register_blueprint(page_bp)
    app.register_blueprint(auth_pages_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(auth_api_bp)
    app.register_blueprint(account_api_bp)
    app.register_blueprint(script_api_bp)
    app.register_blueprint(script_pages_bp)
    app.register_blueprint(session_api_bp)
    app.register_blueprint(game_api_bp)
    app.register_blueprint(wallet_api_bp)
    app.register_blueprint(appeal_api_bp)
    app.register_blueprint(cs_api_bp)
    app.register_blueprint(cs_pages_bp)
    app.register_blueprint(admin_api_bp)
    app.register_blueprint(admin_pages_bp)
    app.register_blueprint(health_bp)
    app.register_blueprint(site_bp)
    if cfg.env in DEV_TOOL_ENVS:
        from app.controllers.dev_controller import dev_bp  # 延後匯入：其他環境根本不會載入開發用程式碼

        app.register_blueprint(dev_bp)
    register_error_handlers(app)
    register_cli(app)
    return app
