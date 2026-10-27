"""應用程式工廠（見 AGENTS.md 第 3 節）。

`create_app()` 依設定建立彼此隔離的應用程式實例：每個實例有自己的設定與資料庫引擎，
測試可同時建立多個互不影響的實例。啟動時不會建立資料表，也不會執行 migration。
"""
from __future__ import annotations

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from app.config import DEV_TOOL_ENVS, AppConfig, load_config
from app.controllers.api_controller import api_bp
from app.controllers.errors import register_error_handlers
from app.controllers.health_controller import health_bp
from app.controllers.page_controller import page_bp
from app.controllers.site_controller import site_bp
from app.db.dialect import create_engine_for
from app.db.session import init_session
from app.observability.json_logging import configure_logging
from app.observability.request_logging import init_request_logging


def create_app(config: AppConfig | None = None) -> Flask:
    cfg = config or load_config()
    configure_logging(cfg.log_level)

    app = Flask(__name__)
    app.config.update(SECRET_KEY=cfg.secret_key.reveal(), DEBUG=cfg.debug)
    app.json.ensure_ascii = False  # Flask 3 起用此屬性；回應中的繁體中文不轉成 \uXXXX
    app.extensions["app_config"] = cfg

    if cfg.trust_proxy:
        # 僅 staging 與 production：信任入口層（HTTPS 終止端）轉送的協定與主機標頭
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # type: ignore[method-assign]

    # 請求日誌必須先於 session 註冊：after_request 以相反順序執行，這樣它最後執行，
    # 記錄到的才是最終狀態碼（例如提交失敗後變成的 500）
    init_request_logging(app)
    init_session(app, create_engine_for(cfg.database_url.reveal()))

    app.register_blueprint(page_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(health_bp)
    app.register_blueprint(site_bp)
    if cfg.env in DEV_TOOL_ENVS:
        from app.controllers.dev_controller import dev_bp  # 延後匯入：其他環境根本不會載入開發用程式碼

        app.register_blueprint(dev_bp)
    register_error_handlers(app)
    return app
