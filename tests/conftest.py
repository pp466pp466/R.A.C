"""全域測試設定與共用 fixture。"""
import io
import json
import logging
import os
from pathlib import Path

import pytest
from dotenv import dotenv_values
from sqlalchemy.engine import make_url

# 測試一律在 testing 環境執行；config 在 testing 環境不會讀取開發用的 .env
os.environ["APP_ENV"] = "testing"

# 所有 app.test_client() 預設使用「緩衝回應並自動帶 Origin 與 CSRF 權杖」的 client（見 tests/support/client.py）。
# 專門測試 CSRF 與 cookie 行為的測試請改用 raw_client()。
from flask import Flask  # noqa: E402

from tests.support.client import BufferedClient  # noqa: E402

Flask.test_client_class = BufferedClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def sqlserver_test_url() -> str | None:
    """整合測試用的 SQL Server 連線字串；只讀取 TEST_SQLSERVER_URL，絕不使用開發用的 DATABASE_URL。"""
    url = os.environ.get("TEST_SQLSERVER_URL")
    if not url:
        env_file = PROJECT_ROOT / ".env"
        if env_file.is_file():
            url = dotenv_values(env_file, encoding="utf-8-sig").get("TEST_SQLSERVER_URL")
    return url or None


def require_sqlserver_test_url() -> str:
    """取得 SQL Server 測試連線字串；未設定則略過，指向非 *_test 資料庫則直接失敗。"""
    url = sqlserver_test_url()
    if not url:
        pytest.skip("未設定 TEST_SQLSERVER_URL，略過 SQL Server 整合測試")
    database = make_url(url).database or ""
    if not database.lower().endswith("_test"):
        # 防止誤把測試資料表建在開發用的資料庫：直接失敗，不是略過
        pytest.fail("TEST_SQLSERVER_URL 必須指向名稱以 _test 結尾的資料庫（例如 project_test）")
    return url


@pytest.fixture(params=["sqlite-file", pytest.param("sqlserver", marks=pytest.mark.sqlserver)])
def concurrent_engine(request, tmp_path):
    """並行測試用：檔案型 SQLite 與真實 SQL Server（記憶體 SQLite 只有一條共用連線，無法測真正的並行）。
    已建立會員資料表；測試結束後刪除。"""
    from app.db.dialect import create_engine_for
    from app.models import Base

    if request.param == "sqlite-file":
        engine = create_engine_for(f"sqlite+pysqlite:///{tmp_path / 'concurrent.sqlite3'}")
    else:
        engine = create_engine_for(require_sqlserver_test_url())
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def member_session(db_engine):
    """已建立會員資料表的 Session（SQLite 與 SQL Server 各跑一次）；測試結束後刪除資料表。"""
    from sqlalchemy.orm import Session

    from app.models import Base

    Base.metadata.drop_all(db_engine)
    Base.metadata.create_all(db_engine)
    with Session(db_engine) as session:
        yield session
    Base.metadata.drop_all(db_engine)


@pytest.fixture()
def generous_free_limits(monkeypatch):
    """讓 free 等級幾乎沒有用量限制。

    測對話流程的檔案常在同一個（假）時間點連發很多則訊息，會撞到每分鐘 6 則的限制；那不是它們要測的事。
    用量限制本身在 `test_usage_service.py`（單元）與 `test_chat_usage_limits.py`（整合）逐項測試，那兩個檔案不使用它。
    用法：測試檔加上 `pytestmark = pytest.mark.usefixtures("generous_free_limits")`。
    """
    from dataclasses import replace

    from app.services.quota_policy import TIER_LIMITS

    monkeypatch.setitem(TIER_LIMITS, "free", replace(TIER_LIMITS["free"], daily_turns=10_000, per_minute=10_000, conversations=10_000))


@pytest.fixture()
def log_lines():
    """把 JSON 日誌導到記憶體，回傳讀取已解析日誌行的函式（`read.raw()` 取得原始文字，供掃描敏感資料用）。"""
    from app.observability.json_logging import JsonFormatter, RequestContextFilter

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestContextFilter())
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    def read() -> list[dict]:
        return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]

    read.raw = stream.getvalue  # type: ignore[attr-defined]
    yield read
    root.removeHandler(handler)
    root.setLevel(previous_level)


@pytest.fixture()
def sqlserver_engine():
    """只在 SQL Server（project_test）上執行的測試使用。"""
    from app.db.dialect import create_engine_for

    engine = create_engine_for(require_sqlserver_test_url())
    yield engine
    engine.dispose()


@pytest.fixture(
    params=["sqlite", pytest.param("sqlserver", marks=pytest.mark.sqlserver)],
)
def db_engine(request):
    """同一組測試分別在記憶體 SQLite 與真實 SQL Server（project_test）上執行，驗證雙軌一致。"""
    from app.db.dialect import create_engine_for

    if request.param == "sqlite":
        engine = create_engine_for("sqlite+pysqlite:///:memory:")
    else:
        engine = create_engine_for(require_sqlserver_test_url())
    yield engine
    engine.dispose()


@pytest.fixture()
def wallet_kit(member_session):
    """錢包 Service 與它周邊的東西（真的 Repository 與設定表，假的只有時鐘）；SQLite 與 SQL Server 各跑一次。"""
    from tests.support.wallet import WalletKit

    return WalletKit(member_session)
