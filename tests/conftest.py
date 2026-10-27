"""全域測試設定與共用 fixture。"""
import os
from pathlib import Path

import pytest
from dotenv import dotenv_values
from sqlalchemy.engine import make_url

# 測試一律在 testing 環境執行；config 在 testing 環境不會讀取開發用的 .env
os.environ["APP_ENV"] = "testing"

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
