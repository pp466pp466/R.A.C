"""database-access 規格：資料表只能透過版本化 migration 變更。"""
import pytest
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

import app.config as config_module
from app.config import PROJECT_ROOT
from app.db.dialect import create_engine_for


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
    # 基線只建立版本機制，不建立任何業務資料表
    assert set(inspect(engine).get_table_names()) == {"alembic_version"}

    command.downgrade(cfg, "base")
    assert current_revision(engine) is None
    engine.dispose()


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
