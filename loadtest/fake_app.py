"""以假模型取代真實供應商的應用程式入口（load-testing 規格「假模型入口只在開發與測試載入」）。

用法（本機，Flask 的 threaded 伺服器；gunicorn 不支援 Windows，容量數字以預備環境為準，見營運文件）：
    set APP_ENV=testing
    set DATABASE_URL=sqlite+pysqlite:///loadtest.db
    set LOADTEST_ERROR_RATES=rate_limited=0.02,timeout=0.01,server_error=0.01
    python -m flask --app loadtest.fake_app run --port 5000

- **APP_ENV 為 staging 或 production 時啟動即失敗**（以非零代碼結束）：這個入口不可能出現在預備或正式環境。
  未設定 APP_ENV 時也拒絕（與整個專案一致，不預設為任何環境）。
- 玩家只用 Google 登入，測試玩家改由 `scripts/test_sessions.py` 在這個環境內建立並簽出 session cookie（見 load-testing 規格）。
- 沒有資料表時（SQLite 檔案等）建立資料表：僅限這兩個本機環境，與「正式環境嚴禁 create_all」不衝突。
- 這個目錄不在容器映像內。
"""
from __future__ import annotations

import os
import sys

FORBIDDEN_ENVS = ("staging", "production")
ALLOWED_ENVS = ("development", "testing")


def guard_environment(environ: dict[str, str]) -> str:
    env = environ.get("APP_ENV", "")
    if env in FORBIDDEN_ENVS or env not in ALLOWED_ENVS:
        raise SystemExit(f"負載測試的假模型入口只能在 {' 或 '.join(ALLOWED_ENVS)} 載入（APP_ENV 目前為「{env or '未設定'}」）：它不可用於預備或正式環境。")
    return env


def build_app(environ: dict[str, str] | None = None):
    environ = os.environ if environ is None else environ
    guard_environment(environ)

    from app import create_app
    from app.config import load_config
    from app.models import Base
    from loadtest.provider import provider_from_environment

    app = create_app(load_config(environ=dict(environ)), llm_provider=provider_from_environment(environ))
    Base.metadata.create_all(app.extensions["db_engine"])
    return app


guard_environment(os.environ)   # 載入模組的當下就檢查：staging 與 production 連匯入都會失敗


def make_app():
    """Flask 的 `--app loadtest.fake_app` 會自動找到這個工廠函式。"""
    return build_app()


if __name__ == "__main__":   # pragma: no cover
    sys.exit("請用 `python -m flask --app loadtest.fake_app run` 啟動。")
