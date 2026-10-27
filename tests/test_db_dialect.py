"""database-access 規格：方言檢查與連線初始化。"""
import pytest
from sqlalchemy import ForeignKey, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.db.dialect import SQLITE_BUSY_TIMEOUT_MS, _sqlite_on_connect, create_engine_for

MSSQL_URL = "mssql+pyodbc://u:p@db.example.com/project?driver=ODBC+Driver+18+for+SQL+Server"


class _Base(DeclarativeBase):
    pass


class Parent(_Base):
    __tablename__ = "parent"
    id: Mapped[int] = mapped_column(primary_key=True)


class Child(_Base):
    __tablename__ = "child"
    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int] = mapped_column(ForeignKey("parent.id"))


def _pragma(engine, name: str):
    with engine.connect() as conn:
        # 測試輔助：直接向驅動查詢 PRAGMA（tests/ 不在原生 SQL 掃描範圍）
        return conn.exec_driver_sql(f"PRAGMA {name}").scalar()


def test_memory_sqlite_connection_enables_foreign_keys_and_busy_timeout():
    engine = create_engine_for("sqlite+pysqlite:///:memory:")
    assert _pragma(engine, "foreign_keys") == 1
    assert _pragma(engine, "busy_timeout") == SQLITE_BUSY_TIMEOUT_MS
    assert SQLITE_BUSY_TIMEOUT_MS > 0


def test_file_sqlite_uses_write_ahead_logging(tmp_path):
    engine = create_engine_for(f"sqlite+pysqlite:///{tmp_path / 'wal.sqlite3'}")
    assert _pragma(engine, "journal_mode").lower() == "wal"
    assert _pragma(engine, "foreign_keys") == 1
    engine.dispose()


def test_memory_sqlite_shares_one_database_across_connections():
    engine = create_engine_for("sqlite+pysqlite:///:memory:")
    _Base.metadata.create_all(engine)
    with Session(engine) as s1:
        s1.add(Parent(id=1))
        s1.commit()
    with Session(engine) as s2:
        assert s2.scalar(select(Parent.id)) == 1


def test_foreign_key_violation_is_rejected_not_silently_accepted():
    engine = create_engine_for("sqlite+pysqlite:///:memory:")
    _Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Child(id=1, parent_id=999))  # 父資料不存在
        with pytest.raises(IntegrityError):
            session.commit()


def test_sqlserver_engine_registers_no_sqlite_connect_event():
    engine = create_engine_for(MSSQL_URL)  # 建立引擎不會連線
    assert engine.dialect.name == "mssql"
    assert not event.contains(engine, "connect", _sqlite_on_connect)


def test_sqlserver_pool_checks_and_recycles_connections():
    engine = create_engine_for(MSSQL_URL)
    assert engine.pool._pre_ping is True
    assert 0 < engine.pool._recycle <= 3600


def test_unsupported_backend_is_rejected_without_leaking_url():
    with pytest.raises(ValueError) as exc:
        create_engine_for("postgresql://user:secret-pw@localhost/db")
    assert "secret-pw" not in str(exc.value)
