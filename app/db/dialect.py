"""引擎工廠與方言檢查（見 AGENTS.md 第 4 節）。

這是全專案「唯一」允許出現原生 SQL 字串的檔案，且僅限 SQLite 的連線初始化。
業務邏輯與 Repository 一律使用 ORM，由 tests/architecture/test_no_raw_sql.py 守住。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import StaticPool

SQLITE_BUSY_TIMEOUT_MS = 5000

# SQL Server 連線池：pool_size 不小於 gunicorn 每個 worker 的執行緒數（初始為 8，見 design D8）
MSSQL_POOL_SIZE = 10
MSSQL_MAX_OVERFLOW = 5
MSSQL_POOL_RECYCLE_SECONDS = 1800
# 建立連線（含登入）的等待上限；須小於客服請求的 30 秒總預算，資料庫不可達時才不會卡住請求
MSSQL_LOGIN_TIMEOUT_SECONDS = 10

SUPPORTED_BACKENDS = ("sqlite", "mssql")


def create_engine_for(database_url: str, *, echo: bool = False) -> Engine:
    """依連線字串建立引擎，並套用該方言專屬的初始化。"""
    url = make_url(database_url)
    backend = url.get_backend_name()
    if backend not in SUPPORTED_BACKENDS:
        raise ValueError("不支援的資料庫類型；僅支援：" + "、".join(SUPPORTED_BACKENDS))

    kwargs: dict[str, Any] = {"echo": echo}
    if backend == "sqlite":
        # 允許跨執行緒使用同一條連線（Flask 的請求可能在不同執行緒）
        kwargs["connect_args"] = {"check_same_thread": False}
        if url.database in (None, "", ":memory:"):
            # 記憶體資料庫：所有連線共用同一個資料庫，否則每條新連線都是空的
            kwargs["poolclass"] = StaticPool
    else:  # mssql
        kwargs.update(
            connect_args={"timeout": MSSQL_LOGIN_TIMEOUT_SECONDS},
            pool_pre_ping=True,  # 取用前確認連線仍可用
            pool_recycle=MSSQL_POOL_RECYCLE_SECONDS,  # 回收閒置過久的連線
            pool_size=MSSQL_POOL_SIZE,
            max_overflow=MSSQL_MAX_OVERFLOW,
        )

    engine = create_engine(url, **kwargs)
    apply_dialect_hooks(engine)
    return engine


def apply_dialect_hooks(engine: Engine) -> None:
    """方言檢查：只有 SQLite 才註冊連線初始化事件；SQL Server 不註冊任何事件。"""
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _sqlite_on_connect)


def _sqlite_on_connect(dbapi_connection: Any, connection_record: Any) -> None:
    """SQLite 每條新連線：預寫日誌、強制外鍵、忙碌等待。業務程式碼不得自行呼叫。"""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    finally:
        cursor.close()
