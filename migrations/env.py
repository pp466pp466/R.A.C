"""Alembic 執行環境。

- 連線取自應用程式設定（DATABASE_URL），或由呼叫端（例如測試）經 `config.attributes["database_url"]` 傳入。
- 啟用 render_as_batch，使 SQLite 也能執行 ALTER 類變更。
- migration 腳本只使用 Alembic 的 op API 與 ORM 模型，不得含方言專屬的原生 SQL（由架構測試強制）。
"""
from logging.config import fileConfig

from alembic import context

from app.config import bootstrap_env_for_tools, load_config
from app.db.dialect import create_engine_for
from app.models import Base  # 匯入 app.models 會一併載入全部模型，metadata 才完整

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    explicit = config.attributes.get("database_url")
    if explicit:
        return explicit
    bootstrap_env_for_tools()
    return load_config().database_url.reveal()


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine_for(_database_url())
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=True,
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
