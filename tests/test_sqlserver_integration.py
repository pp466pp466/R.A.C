"""SQL Server 整合測試（標記 sqlserver）：只在設定了 TEST_SQLSERVER_URL 且指向 *_test 資料庫時執行。

涵蓋 database-access 規格中只有真實 SQL Server 才能驗證的情境：
連線、不執行 SQLite 專屬設定、外鍵與唯一約束、閒置連線被資料庫端關閉後仍可成功、migration 升級。
（中文與 JSON 往返、時間精度等雙軌共用情境，見 test_models_base.py 的 sqlserver 參數）
"""
import pytest
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import ForeignKey, String, event, inspect, literal, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.config import PROJECT_ROOT
from app.db.dialect import _sqlite_on_connect
from tests.conftest import require_sqlserver_test_url

pytestmark = pytest.mark.sqlserver


class _Base(DeclarativeBase):
    pass


class SParent(_Base):
    __tablename__ = "t_sparent"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True)


class SChild(_Base):
    __tablename__ = "t_schild"
    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int] = mapped_column(ForeignKey("t_sparent.id"))


@pytest.fixture()
def tables(sqlserver_engine):
    _Base.metadata.drop_all(sqlserver_engine)
    _Base.metadata.create_all(sqlserver_engine)
    yield
    _Base.metadata.drop_all(sqlserver_engine)


def test_connects_and_runs_an_orm_expression(sqlserver_engine):
    assert sqlserver_engine.dialect.name == "mssql"
    with Session(sqlserver_engine) as session:
        assert session.scalar(select(literal(1))) == 1


def test_no_sqlite_specific_initialisation_is_registered(sqlserver_engine):
    assert not event.contains(sqlserver_engine, "connect", _sqlite_on_connect)


def test_foreign_key_violation_is_rejected(sqlserver_engine, tables):
    with Session(sqlserver_engine) as session:
        session.add(SChild(id=1, parent_id=999))
        with pytest.raises(IntegrityError):
            session.commit()


def test_unique_constraint_is_enforced(sqlserver_engine, tables):
    with Session(sqlserver_engine) as session:
        session.add(SParent(id=1, code="A"))
        session.commit()
        session.add(SParent(id=2, code="A"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_connection_closed_by_the_server_is_replaced_transparently(sqlserver_engine):
    """規格：資料庫端斷開閒置連線後，下一個請求仍然成功（pool_pre_ping）。"""
    with sqlserver_engine.connect() as victim:
        spid = victim.exec_driver_sql("SELECT @@SPID").scalar()  # 測試輔助，tests/ 不在原生 SQL 掃描範圍
    # 此時該連線已歸還連線池；從另一條連線在資料庫端把它殺掉
    # 必須用「另一個引擎」的連線去殺：同一個連線池會把剛歸還的連線再借出來，變成殺自己。
    # KILL 也不能在使用者交易內執行，所以用自動提交。
    from app.db.dialect import create_engine_for

    killer_engine = create_engine_for(require_sqlserver_test_url())
    try:
        with killer_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as killer:
            killer.exec_driver_sql(f"KILL {int(spid)}")
    finally:
        killer_engine.dispose()
    with Session(sqlserver_engine) as session:
        assert session.scalar(select(literal(1))) == 1


def _app_for(url: str):
    from app import create_app
    from app.config import load_config

    return create_app(load_config(environ={"APP_ENV": "testing", "DATABASE_URL": url}))


def test_readyz_is_200_against_real_sql_server():
    app = _app_for(require_sqlserver_test_url())
    client = app.test_client()
    assert client.get("/readyz").status_code == 200
    assert client.get("/healthz").status_code == 200
    app.extensions["db_engine"].dispose()


def test_readyz_is_503_without_leaking_when_sql_server_is_unreachable(monkeypatch):
    import time

    from sqlalchemy.engine import make_url

    import app.db.dialect as dialect

    monkeypatch.setattr(dialect, "MSSQL_LOGIN_TIMEOUT_SECONDS", 2)  # 縮短登入逾時，讓測試不必等滿預設值
    # 連到一個沒有服務的連接埠
    dead_url = make_url(require_sqlserver_test_url()).set(host="localhost", port=1).render_as_string(hide_password=False)
    app = _app_for(dead_url)
    client = app.test_client()
    health = client.get("/healthz")
    started = time.perf_counter()
    ready = client.get("/readyz")
    assert time.perf_counter() - started < 8, "登入逾時沒有生效，請求被卡住"
    assert health.status_code == 200
    assert ready.status_code == 503
    body = ready.get_data(as_text=True)
    assert body.strip() == '{"status":"unavailable"}'
    for forbidden in ("localhost", "ODBC", "sa:", "project_test", "Login", "pyodbc"):
        assert forbidden not in body
    assert app.extensions["db_engine"].pool.checkedout() == 0
    app.extensions["db_engine"].dispose()


def test_migration_upgrade_and_downgrade_on_sql_server():
    url = require_sqlserver_test_url()
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = url
    head = ScriptDirectory.from_config(cfg).get_current_head()

    command.upgrade(cfg, "head")
    from app.db.dialect import create_engine_for

    engine = create_engine_for(url)
    try:
        with engine.connect() as conn:
            assert MigrationContext.configure(conn).get_current_revision() == head
        assert "alembic_version" in inspect(engine).get_table_names()
        command.downgrade(cfg, "base")
        with engine.connect() as conn:
            assert MigrationContext.configure(conn).get_current_revision() is None
    finally:
        engine.dispose()
