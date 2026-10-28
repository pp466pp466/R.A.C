"""database-access 規格：資料表只能透過版本化 migration 變更。"""
import re

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, Table, inspect

import app.config as config_module
from app.config import PROJECT_ROOT
from app.db.dialect import create_engine_for
from app.models import Base
from tests.conftest import require_sqlserver_test_url

MEMBER_TABLES = ("users", "user_identities", "auth_tokens")


def alembic_config(url: str) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = url
    return cfg


def current_revision(engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def test_upgrade_then_downgrade_on_a_fresh_file_database(tmp_path):
    url = f"sqlite+pysqlite:///{tmp_path / 'migrate.sqlite3'}"
    cfg = alembic_config(url)
    head = ScriptDirectory.from_config(cfg).get_current_head()

    command.upgrade(cfg, "head")
    engine = create_engine_for(url)
    assert current_revision(engine) == head
    assert set(inspect(engine).get_table_names()) == {"alembic_version", *MEMBER_TABLES}

    command.downgrade(cfg, "base")
    assert current_revision(engine) is None
    assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    engine.dispose()


def test_baseline_creates_no_business_table_and_0002_adds_only_the_member_tables(tmp_path):
    url = f"sqlite+pysqlite:///{tmp_path / 'steps.sqlite3'}"
    cfg = alembic_config(url)
    engine = create_engine_for(url)

    command.upgrade(cfg, "0001_baseline")
    assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    command.upgrade(cfg, "0002_users_and_auth_tokens")
    assert set(inspect(engine).get_table_names()) == {"alembic_version", *MEMBER_TABLES}
    command.downgrade(cfg, "0001_baseline")
    assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    engine.dispose()


def test_migration_scripts_do_not_import_application_code():
    """migration 描述的是「當時」的結構，不應隨應用程式的修改而改變，因此不匯入 app。"""
    for path in (PROJECT_ROOT / "migrations" / "versions").glob("*.py"):
        text = path.read_text(encoding="utf-8-sig")
        assert not re.search(r"^\s*(from|import)\s+app(\.|\s)", text, re.M), f"{path.name} 匯入了應用程式程式碼"


# ---- 升級後的結構必須與模型一致（SQLite 與 SQL Server）--------------------------

@pytest.fixture(params=["sqlite-file", pytest.param("sqlserver", marks=pytest.mark.sqlserver)])
def migrated_database(request, tmp_path):
    if request.param == "sqlite-file":
        url = f"sqlite+pysqlite:///{tmp_path / 'compare.sqlite3'}"
    else:
        url = require_sqlserver_test_url()
    cfg = alembic_config(url)
    command.upgrade(cfg, "head")
    engine = create_engine_for(url)
    yield engine
    command.downgrade(cfg, "base")
    with engine.begin() as conn:
        Table("alembic_version", MetaData()).drop(conn, checkfirst=True)
    engine.dispose()


def test_migrated_schema_has_no_difference_from_the_models(migrated_database):
    with migrated_database.connect() as conn:
        context = MigrationContext.configure(conn, opts={"compare_type": True, "compare_server_default": False})
        differences = compare_metadata(context, Base.metadata)
    assert differences == [], f"migration 與模型不一致：{differences}"


def test_the_schema_comparison_really_reports_differences(migrated_database):
    """自我驗證：與「少了三張表的模型」比較時必須回報差異，否則上面的『沒有差異』沒有意義。"""
    with migrated_database.connect() as conn:
        context = MigrationContext.configure(conn, opts={"compare_type": True})
        differences = compare_metadata(context, MetaData())
    removed = {d[1].name for d in differences if d[0] == "remove_table"}
    assert set(MEMBER_TABLES) <= removed


def test_migrated_tables_enforce_the_documented_constraints(migrated_database):
    inspector = inspect(migrated_database)
    for table in MEMBER_TABLES:
        assert table in inspector.get_table_names()
    foreign_keys = {t: inspector.get_foreign_keys(t) for t in ("user_identities", "auth_tokens")}
    for table, keys in foreign_keys.items():
        assert len(keys) == 1, table
        assert keys[0]["referred_table"] == "users"
        assert (keys[0].get("options") or {}).get("ondelete", "").upper() == "CASCADE", table
    index_names = {i["name"] for i in inspector.get_indexes("auth_tokens")}
    assert "ix_auth_tokens_user_id_purpose_created_at" in index_names


def test_there_is_exactly_one_migration_head():
    cfg = alembic_config("sqlite+pysqlite:///:memory:")
    assert len(ScriptDirectory.from_config(cfg).get_heads()) == 1


def test_env_enables_batch_mode_for_sqlite_alter_support():
    env_text = (PROJECT_ROOT / "migrations" / "env.py").read_text(encoding="utf-8")
    assert "render_as_batch=True" in env_text


def test_create_all_is_not_used_outside_tests():
    offenders = []
    for folder in ("app", "migrations"):
        for path in (PROJECT_ROOT / folder).rglob("*.py"):
            if "create_all" in path.read_text(encoding="utf-8-sig"):
                offenders.append(str(path.relative_to(PROJECT_ROOT)))
    assert offenders == [], f"正式程式碼不得使用 create_all()：{offenders}"


def test_alembic_ini_is_ascii_only_because_alembic_reads_it_with_the_locale_encoding():
    raw = (PROJECT_ROOT / "alembic.ini").read_bytes()
    assert raw.isascii(), "alembic.ini 必須是純 ASCII：Alembic 以系統地區編碼（cp950）讀取，中文會造成解析失敗"


def test_migration_template_is_ascii_only():
    raw = (PROJECT_ROOT / "migrations" / "script.py.mako").read_bytes()
    assert raw.isascii()


def test_alembic_ini_does_not_hold_a_connection_string():
    ini_text = (PROJECT_ROOT / "alembic.ini").read_text(encoding="utf-8")
    active = [ln for ln in ini_text.splitlines() if ln.strip().startswith("sqlalchemy.url")]
    assert active == []


# ---- bootstrap_env_for_tools：只給命令列工具用的便利，不影響應用程式的嚴格規則 ----

@pytest.mark.parametrize(
    ("declared", "expected"),
    [("development", "development"), ("testing", "testing"), ("production", None), ("staging", None)],
)
def test_tools_bootstrap_only_adopts_development_or_testing(tmp_path, monkeypatch, declared, expected):
    (tmp_path / ".env").write_text(f"APP_ENV={declared}\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("APP_ENV", raising=False)
    config_module.bootstrap_env_for_tools()
    import os

    assert os.environ.get("APP_ENV") == expected


def test_tools_bootstrap_never_overrides_the_process_environment(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("APP_ENV=development\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("APP_ENV", "production")
    config_module.bootstrap_env_for_tools()
    import os

    assert os.environ["APP_ENV"] == "production"
