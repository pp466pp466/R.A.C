"""database-access 規格：資料表只能透過版本化 migration 變更。"""
import re

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, Table, func, inspect, select
from sqlalchemy.orm import Session

import app.config as config_module
from app.config import PROJECT_ROOT
from app.db.dialect import create_engine_for
from app.models import AuthToken, Base, User, UserIdentity
from app.models.base import utcnow
from tests.conftest import require_sqlserver_test_url

MEMBER_TABLES = ("users", "user_identities", "auth_tokens")
AGE_COLUMNS = ("age_confirmed_at", "age_policy_version")
BIRTHDATE_COLUMNS = ("birth_date", "age_range", "is_adult_at_confirmation")
TOMBSTONE_TABLE = "deletion_tombstones"
CHAT_TABLES = ("chat_conversations", "chat_messages", "llm_call_logs")
ADMIN_TABLES = ("admin_audit_logs",)
ALERT_TABLES = ("alert_states",)
LOCK_TABLES = ("lock_strikes", "lock_appeals")


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
    assert set(inspect(engine).get_table_names()) == {"alembic_version", *MEMBER_TABLES, TOMBSTONE_TABLE, *CHAT_TABLES, *ADMIN_TABLES, *ALERT_TABLES, *LOCK_TABLES}

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


def test_0003_adds_only_two_columns_to_users_and_follows_0002(tmp_path):
    """age-confirmation：只新增欄位（expand），不新增、不刪除任何資料表。"""
    url = f"sqlite+pysqlite:///{tmp_path / 'age.sqlite3'}"
    cfg = alembic_config(url)
    engine = create_engine_for(url)
    assert ScriptDirectory.from_config(cfg).get_revision("0003_users_age_confirmation").down_revision == "0002_users_and_auth_tokens"

    command.upgrade(cfg, "0002_users_and_auth_tokens")
    before = {c["name"] for c in inspect(engine).get_columns("users")}
    assert not set(AGE_COLUMNS) & before

    command.upgrade(cfg, "0003_users_age_confirmation")
    after = {c["name"] for c in inspect(engine).get_columns("users")}
    assert after - before == set(AGE_COLUMNS) and before <= after
    assert set(inspect(engine).get_table_names()) == {"alembic_version", *MEMBER_TABLES}

    command.downgrade(cfg, "0002_users_and_auth_tokens")
    assert {c["name"] for c in inspect(engine).get_columns("users")} == before
    engine.dispose()


def test_0004_adds_three_columns_and_one_table_and_follows_0003(tmp_path):
    """age-verification-birthdate：只新增三個可為空的欄位與一張新資料表（expand）。"""
    url = f"sqlite+pysqlite:///{tmp_path / 'birthdate.sqlite3'}"
    cfg = alembic_config(url)
    engine = create_engine_for(url)
    revision = ScriptDirectory.from_config(cfg).get_revision("0004_users_birthdate")
    assert revision.down_revision == "0003_users_age_confirmation"

    command.upgrade(cfg, "0003_users_age_confirmation")
    before_columns = {c["name"] for c in inspect(engine).get_columns("users")}
    before_tables = set(inspect(engine).get_table_names())
    assert not set(BIRTHDATE_COLUMNS) & before_columns and TOMBSTONE_TABLE not in before_tables

    command.upgrade(cfg, "0004_users_birthdate")
    inspector = inspect(engine)
    after_columns = {c["name"]: c for c in inspector.get_columns("users")}
    assert set(after_columns) - before_columns == set(BIRTHDATE_COLUMNS) and before_columns <= set(after_columns)
    assert all(after_columns[name]["nullable"] is True for name in BIRTHDATE_COLUMNS)
    assert set(inspector.get_table_names()) - before_tables == {TOMBSTONE_TABLE}
    tombstone = {c["name"]: c for c in inspector.get_columns(TOMBSTONE_TABLE)}
    assert set(tombstone) == {"id", "user_id", "deleted_at", "reason"}
    assert inspector.get_foreign_keys(TOMBSTONE_TABLE) == []             # 被指向的帳號已不存在，不設外鍵

    command.downgrade(cfg, "0003_users_age_confirmation")
    assert {c["name"] for c in inspect(engine).get_columns("users")} == before_columns
    assert set(inspect(engine).get_table_names()) == before_tables
    engine.dispose()


def test_upgrading_0004_keeps_existing_accounts_and_marks_them_as_not_verified(tmp_path):
    """升級前的帳號（含勾選版時代、只有 age_confirmed_at 的帳號）保留，三個新欄位為空 = 尚未完成年齡確認。"""
    url = f"sqlite+pysqlite:///{tmp_path / 'keep.sqlite3'}"
    cfg = alembic_config(url)
    engine = create_engine_for(url)
    command.upgrade(cfg, "0003_users_age_confirmation")
    with engine.begin() as conn:
        users = Table("users", MetaData(), autoload_with=conn)
        conn.execute(users.insert().values(
            public_id="00000000-0000-4000-8000-000000000001", email="old@example.com", email_canonical="old@example.com",
            display_name="舊帳號", role="user", member_tier="free", status="active", failed_login_count=0, token_version=1,
            created_at=utcnow().replace(tzinfo=None), updated_at=utcnow().replace(tzinfo=None),
            age_confirmed_at=utcnow().replace(tzinfo=None), age_policy_version="2026-10-28-self-declared",
        ))

    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        users = Table("users", MetaData(), autoload_with=conn)
        row = conn.execute(select(users)).mappings().one()
    assert row["email"] == "old@example.com" and row["age_confirmed_at"] is not None
    assert row["birth_date"] is None and row["age_range"] is None and row["is_adult_at_confirmation"] is None
    engine.dispose()


def test_0005_adds_only_the_three_chat_tables_and_follows_0004(tmp_path):
    """cs-chat-core：只新增三張資料表（expand），不動既有資料表的任何欄位。"""
    url = f"sqlite+pysqlite:///{tmp_path / 'chat.sqlite3'}"
    cfg = alembic_config(url)
    engine = create_engine_for(url)
    revision = ScriptDirectory.from_config(cfg).get_revision("0005_chat_core")
    assert revision.down_revision == "0004_users_birthdate" and len(revision.revision) <= 32     # alembic_version 是 VARCHAR(32)

    command.upgrade(cfg, "0004_users_birthdate")
    before = {t: {c["name"] for c in inspect(engine).get_columns(t)} for t in (*MEMBER_TABLES, TOMBSTONE_TABLE)}
    before_tables = set(inspect(engine).get_table_names())
    assert not set(CHAT_TABLES) & before_tables

    command.upgrade(cfg, "0005_chat_core")
    assert set(inspect(engine).get_table_names()) - before_tables == set(CHAT_TABLES)
    assert {t: {c["name"] for c in inspect(engine).get_columns(t)} for t in before} == before

    command.downgrade(cfg, "0004_users_birthdate")
    assert set(inspect(engine).get_table_names()) == before_tables
    engine.dispose()


def test_0006_adds_only_two_indexes_and_keeps_every_row(tmp_path):
    """usage-limits：只新增索引（expand），不動任何資料表的欄位與資料；回復只移除這兩個索引。"""
    url = f"sqlite+pysqlite:///{tmp_path / 'usage.sqlite3'}"
    cfg = alembic_config(url)
    engine = create_engine_for(url)
    revision = ScriptDirectory.from_config(cfg).get_revision("0006_usage_indexes")
    assert revision.down_revision == "0005_chat_core" and len(revision.revision) <= 32

    command.upgrade(cfg, "0005_chat_core")
    inspector = inspect(engine)
    before_tables = set(inspector.get_table_names())
    before_columns = {t: {c["name"] for c in inspector.get_columns(t)} for t in CHAT_TABLES}
    before_indexes = {t: {i["name"] for i in inspector.get_indexes(t)} for t in ("llm_call_logs", "chat_messages")}
    assert "ix_llm_call_logs_created_at" not in before_indexes["llm_call_logs"]

    command.upgrade(cfg, "0006_usage_indexes")
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == before_tables
    assert {t: {c["name"] for c in inspector.get_columns(t)} for t in CHAT_TABLES} == before_columns
    after = {t: {i["name"] for i in inspector.get_indexes(t)} for t in ("llm_call_logs", "chat_messages")}
    assert after["llm_call_logs"] - before_indexes["llm_call_logs"] == {"ix_llm_call_logs_created_at"}
    assert after["chat_messages"] - before_indexes["chat_messages"] == {"ix_chat_messages_created_at"}
    assert [i["column_names"] for i in inspector.get_indexes("llm_call_logs") if i["name"] == "ix_llm_call_logs_created_at"] == [["created_at"]]

    command.downgrade(cfg, "0005_chat_core")
    inspector = inspect(engine)
    assert {t: {i["name"] for i in inspector.get_indexes(t)} for t in ("llm_call_logs", "chat_messages")} == before_indexes
    engine.dispose()


def test_0007_adds_only_the_audit_table_with_one_foreign_key_and_keeps_every_row(tmp_path):
    """admin-console：只新增 `admin_audit_logs`（expand）；只有 `admin_user_id` 有外鍵（SET NULL），目標只存對外識別碼。"""
    url = f"sqlite+pysqlite:///{tmp_path / 'audit.sqlite3'}"
    cfg = alembic_config(url)
    engine = create_engine_for(url)
    revision = ScriptDirectory.from_config(cfg).get_revision("0007_admin_audit_logs")
    assert revision.down_revision == "0006_usage_indexes" and len(revision.revision) <= 32

    command.upgrade(cfg, "0006_usage_indexes")
    before_tables = set(inspect(engine).get_table_names())
    before_columns = {t: {c["name"] for c in inspect(engine).get_columns(t)} for t in (*MEMBER_TABLES, *CHAT_TABLES)}
    assert not set(ADMIN_TABLES) & before_tables

    command.upgrade(cfg, "0007_admin_audit_logs")
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) - before_tables == set(ADMIN_TABLES)
    assert {t: {c["name"] for c in inspector.get_columns(t)} for t in before_columns} == before_columns
    columns = {c["name"]: c for c in inspector.get_columns("admin_audit_logs")}
    assert set(columns) == {
        "id", "admin_user_id", "action", "target_user_public_id", "target_conversation_public_id", "reason", "detail_json", "created_at",
    }
    assert columns["admin_user_id"]["nullable"] and columns["target_user_public_id"]["nullable"] and not columns["action"]["nullable"]
    keys = inspector.get_foreign_keys("admin_audit_logs")
    assert [(k["constrained_columns"], k["referred_table"], (k.get("options") or {}).get("ondelete", "").upper()) for k in keys] == [
        (["admin_user_id"], "users", "SET NULL")
    ]
    assert {i["name"] for i in inspector.get_indexes("admin_audit_logs")} == {
        "ix_admin_audit_logs_created_at", "ix_admin_audit_logs_admin_user_id_created_at",
    }

    command.downgrade(cfg, "0006_usage_indexes")
    assert set(inspect(engine).get_table_names()) == before_tables
    engine.dispose()


def test_0008_adds_two_nullable_user_columns_and_one_index_and_keeps_every_row(tmp_path):
    """privacy-lifecycle：只新增（expand）兩個可為空的欄位與一個索引，不動任何資料；回復只移除它們。"""
    url = f"sqlite+pysqlite:///{tmp_path / 'privacy.sqlite3'}"
    cfg = alembic_config(url)
    engine = create_engine_for(url)
    revision = ScriptDirectory.from_config(cfg).get_revision("0008_privacy_lifecycle")
    assert revision.down_revision == "0007_admin_audit_logs" and len(revision.revision) <= 32

    command.upgrade(cfg, "0007_admin_audit_logs")
    with engine.begin() as conn:
        users = Table("users", MetaData(), autoload_with=conn)
        conn.execute(users.insert().values(
            public_id="00000000-0000-4000-8000-000000000008", email="old@example.com", email_canonical="old@example.com",
            display_name="舊帳號", role="user", member_tier="free", status="active", failed_login_count=0, token_version=1,
            created_at=utcnow().replace(tzinfo=None), updated_at=utcnow().replace(tzinfo=None),
        ))
    inspector = inspect(engine)
    before_tables = set(inspector.get_table_names())
    before_user_columns = {c["name"] for c in inspector.get_columns("users")}
    before_indexes = {i["name"] for i in inspector.get_indexes("chat_conversations")}
    assert "ix_chat_conversations_updated_at" not in before_indexes

    command.upgrade(cfg, "0008_privacy_lifecycle")
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == before_tables
    columns = {c["name"]: c for c in inspector.get_columns("users")}
    assert set(columns) - before_user_columns == {"deletion_requested_at", "status_before_deletion"}
    assert columns["deletion_requested_at"]["nullable"] and columns["status_before_deletion"]["nullable"]
    assert {i["name"] for i in inspector.get_indexes("chat_conversations")} - before_indexes == {"ix_chat_conversations_updated_at"}
    assert [i["column_names"] for i in inspector.get_indexes("chat_conversations") if i["name"] == "ix_chat_conversations_updated_at"] == [["updated_at"]]
    with engine.connect() as conn:
        row = conn.execute(select(Table("users", MetaData(), autoload_with=conn))).mappings().one()
    assert (row["display_name"], row["status"]) == ("舊帳號", "active")
    assert row["deletion_requested_at"] is None and row["status_before_deletion"] is None

    command.downgrade(cfg, "0007_admin_audit_logs")
    inspector = inspect(engine)
    assert {c["name"] for c in inspector.get_columns("users")} == before_user_columns
    assert {i["name"] for i in inspector.get_indexes("chat_conversations")} == before_indexes
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(Table("users", MetaData(), autoload_with=conn))).scalar_one() == 1
    engine.dispose()


def test_0009_adds_only_the_alert_state_table_without_foreign_keys_and_keeps_every_row(tmp_path):
    """ops-alerting：只新增 `alert_states`（expand）；沒有外鍵、`alert_key` 唯一；回復只移除這張表。"""
    url = f"sqlite+pysqlite:///{tmp_path / 'alerts.sqlite3'}"
    cfg = alembic_config(url)
    engine = create_engine_for(url)
    revision = ScriptDirectory.from_config(cfg).get_revision("0009_alert_states")
    assert revision.down_revision == "0008_privacy_lifecycle" and len(revision.revision) <= 32

    command.upgrade(cfg, "0008_privacy_lifecycle")
    with engine.begin() as conn:
        users = Table("users", MetaData(), autoload_with=conn)
        conn.execute(users.insert().values(
            public_id="00000000-0000-4000-8000-000000000009", email="old@example.com", email_canonical="old@example.com",
            display_name="舊帳號", role="user", member_tier="free", status="active", failed_login_count=0, token_version=1,
            created_at=utcnow().replace(tzinfo=None), updated_at=utcnow().replace(tzinfo=None),
        ))
    before_tables = set(inspect(engine).get_table_names())
    before_columns = {t: {c["name"] for c in inspect(engine).get_columns(t)} for t in (*MEMBER_TABLES, *CHAT_TABLES, *ADMIN_TABLES)}
    assert not set(ALERT_TABLES) & before_tables

    command.upgrade(cfg, "0009_alert_states")
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) - before_tables == set(ALERT_TABLES)
    assert {t: {c["name"] for c in inspector.get_columns(t)} for t in before_columns} == before_columns
    columns = {c["name"]: c for c in inspector.get_columns("alert_states")}
    assert set(columns) == {"id", "alert_key", "state", "severity", "changed_at", "last_notified_at", "last_value", "updated_at"}
    assert columns["severity"]["nullable"] and columns["last_notified_at"]["nullable"] and columns["last_value"]["nullable"]
    assert not columns["alert_key"]["nullable"] and not columns["state"]["nullable"]
    assert inspector.get_foreign_keys("alert_states") == []
    assert [sorted(u["column_names"]) for u in inspector.get_unique_constraints("alert_states")] == [["alert_key"]]
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(Table("users", MetaData(), autoload_with=conn))).scalar_one() == 1

    command.downgrade(cfg, "0008_privacy_lifecycle")
    assert set(inspect(engine).get_table_names()) == before_tables
    engine.dispose()


def test_0010_adds_three_nullable_user_columns_two_indexes_and_two_tables_and_keeps_every_row(tmp_path):
    """google-first-login：users 新增三個可為空的欄位與兩個索引，新增 lock_strikes 與 lock_appeals（各一個 CASCADE 外鍵）；回復只移除新增的部分。"""
    url = f"sqlite+pysqlite:///{tmp_path / 'lock.sqlite3'}"
    cfg = alembic_config(url)
    engine = create_engine_for(url)
    revision = ScriptDirectory.from_config(cfg).get_revision("0010_google_first_login")
    assert revision.down_revision == "0009_alert_states" and len(revision.revision) <= 32

    command.upgrade(cfg, "0009_alert_states")
    with engine.begin() as conn:
        users = Table("users", MetaData(), autoload_with=conn)
        conn.execute(users.insert().values(
            public_id="00000000-0000-4000-8000-000000000010", email="old@example.com", email_canonical="old@example.com",
            display_name="舊帳號", role="user", member_tier="free", status="active", failed_login_count=0, token_version=1,
            created_at=utcnow().replace(tzinfo=None), updated_at=utcnow().replace(tzinfo=None),
        ))
    before_tables = set(inspect(engine).get_table_names())
    before_user_columns = {c["name"] for c in inspect(engine).get_columns("users")}

    command.upgrade(cfg, "0010_google_first_login")
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) - before_tables == set(LOCK_TABLES)
    new_columns = {c["name"]: c for c in inspector.get_columns("users")}
    assert set(new_columns) - before_user_columns == {"age_deadline_at", "locked_at", "lock_reason"}
    assert all(new_columns[name]["nullable"] for name in ("age_deadline_at", "locked_at", "lock_reason"))
    index_names = {i["name"] for i in inspector.get_indexes("users")}
    assert {"ix_users_age_deadline_at", "ix_users_locked_at"} <= index_names
    for table in LOCK_TABLES:
        foreign_keys = inspector.get_foreign_keys(table)
        assert len(foreign_keys) == 1 and foreign_keys[0]["referred_table"] == "users"
        assert foreign_keys[0]["options"].get("ondelete") == "CASCADE"
    assert [sorted(u["column_names"]) for u in inspector.get_unique_constraints("lock_appeals")] == [["public_id"]]
    with engine.connect() as conn:
        row = conn.execute(select(Table("users", MetaData(), autoload_with=conn))).mappings().one()
        assert row["email"] == "old@example.com" and row["age_deadline_at"] is None and row["locked_at"] is None

    command.downgrade(cfg, "0009_alert_states")
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == before_tables
    assert {c["name"] for c in inspector.get_columns("users")} == before_user_columns
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
    assert {*MEMBER_TABLES, TOMBSTONE_TABLE, *CHAT_TABLES, *ADMIN_TABLES, *ALERT_TABLES, *LOCK_TABLES} <= removed


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


def test_age_columns_are_nullable_after_upgrade(migrated_database):
    """空值 = 尚未確認；升級前已存在的帳號不回填，所以這兩欄必須可為空。"""
    columns = {c["name"]: c for c in inspect(migrated_database).get_columns("users")}
    for name in AGE_COLUMNS:
        assert name in columns and columns[name]["nullable"] is True


def test_downgrading_0003_removes_only_the_age_columns_and_keeps_every_row(migrated_database):
    """SQLite 預設以「重建資料表」刪欄位，會連帶清掉以串聯刪除參照 users 的資料；0003 必須避免。"""
    engine = migrated_database
    with Session(engine) as session:
        user = User(
            email="a@example.com", email_canonical="a@example.com", display_name="甲", status="active",
            age_confirmed_at=utcnow(), age_policy_version="v",
        )
        session.add(user)
        session.flush()
        session.add(UserIdentity(user_id=user.id, provider="google", provider_subject="s1"))
        session.add(AuthToken(user_id=user.id, purpose="verify_email", token_hash="h" * 64, expires_at=utcnow()))
        session.commit()

    command.downgrade(alembic_config(engine.url.render_as_string(hide_password=False)), "0002_users_and_auth_tokens")

    assert not set(AGE_COLUMNS) & {c["name"] for c in inspect(engine).get_columns("users")}
    with engine.connect() as conn:
        for table in MEMBER_TABLES:
            reflected = Table(table, MetaData(), autoload_with=engine)
            assert conn.execute(select(func.count()).select_from(reflected)).scalar_one() == 1, table


def test_birthdate_columns_are_nullable_and_the_tombstone_table_exists_after_upgrade(migrated_database):
    """空值 = 尚未完成年齡確認；不回填，所以三個新欄位必須可為空。墓碑資料表不得有外鍵。"""
    inspector = inspect(migrated_database)
    columns = {c["name"]: c for c in inspector.get_columns("users")}
    for name in BIRTHDATE_COLUMNS:
        assert name in columns and columns[name]["nullable"] is True
    assert TOMBSTONE_TABLE in inspector.get_table_names()
    assert inspector.get_foreign_keys(TOMBSTONE_TABLE) == []


def test_downgrading_0004_removes_only_the_new_columns_and_table_and_keeps_every_row(migrated_database):
    """降級同樣不能因為 SQLite 重建資料表而連帶清掉以串聯刪除參照 users 的資料（見 0003 的同一個陷阱）。"""
    from datetime import date

    engine = migrated_database
    with Session(engine) as session:
        user = User(
            email="b@example.com", email_canonical="b@example.com", display_name="乙", status="active",
            birth_date=date(1996, 1, 5), age_range="25_34", is_adult_at_confirmation=True,
            age_confirmed_at=utcnow(), age_policy_version="v",
        )
        session.add(user)
        session.flush()
        session.add(UserIdentity(user_id=user.id, provider="google", provider_subject="s2"))
        session.add(AuthToken(user_id=user.id, purpose="verify_email", token_hash="g" * 64, expires_at=utcnow()))
        session.commit()

    command.downgrade(alembic_config(engine.url.render_as_string(hide_password=False)), "0003_users_age_confirmation")

    columns = {c["name"] for c in inspect(engine).get_columns("users")}
    assert not set(BIRTHDATE_COLUMNS) & columns and set(AGE_COLUMNS) <= columns        # 只退回 0004 加的
    assert TOMBSTONE_TABLE not in inspect(engine).get_table_names()
    with engine.connect() as conn:
        for table in MEMBER_TABLES:
            reflected = Table(table, MetaData(), autoload_with=engine)
            assert conn.execute(select(func.count()).select_from(reflected)).scalar_one() == 1, table


def _fk_actions(inspector, table):
    return {tuple(k["constrained_columns"]): (k["referred_table"], (k.get("options") or {}).get("ondelete", "").upper())
            for k in inspector.get_foreign_keys(table)}


def _unique_sets(inspector, table):
    try:
        constraints = {tuple(u["column_names"]) for u in inspector.get_unique_constraints(table)}
    except NotImplementedError:          # SQL Server 的反射不提供唯一約束；它們會以唯一索引的形式出現在 get_indexes
        constraints = set()
    return constraints | {tuple(i["column_names"]) for i in inspector.get_indexes(table) if i.get("unique")}


def test_chat_tables_have_the_documented_foreign_keys_and_unique_constraints(migrated_database):
    """刪除對話與使用者要真的把內容刪掉（CASCADE）；帳本匿名化（SET NULL）；帳本的對話編號刻意沒有外鍵（SQL Server 的多路徑限制）。"""
    inspector = inspect(migrated_database)
    assert _fk_actions(inspector, "chat_conversations") == {("user_id",): ("users", "CASCADE")}
    assert _fk_actions(inspector, "chat_messages") == {("conversation_id",): ("chat_conversations", "CASCADE")}
    assert _fk_actions(inspector, "llm_call_logs") == {("user_id",): ("users", "SET NULL")}
    ledger = {c["name"]: c for c in inspector.get_columns("llm_call_logs")}
    assert ledger["user_id"]["nullable"] is True and ledger["conversation_id"]["nullable"] is True

    assert {("user_id", "client_id"), ("public_id",)} <= _unique_sets(inspector, "chat_conversations")
    assert {("conversation_id", "seq"), ("conversation_id", "client_msg_id")} <= _unique_sets(inspector, "chat_messages")
    indexes = {i["name"] for t in CHAT_TABLES for i in inspector.get_indexes(t)}
    assert {"ix_chat_conversations_user_id_updated_at", "ix_chat_messages_intent",
            "ix_llm_call_logs_user_id_created_at", "ix_llm_call_logs_model_status_created_at"} <= indexes


def test_deleting_a_user_cascades_to_conversations_and_messages_and_anonymises_the_ledger(migrated_database):
    from app.models import ChatConversation, ChatMessage, LlmCallLog

    engine = migrated_database
    with Session(engine) as session:
        user = User(email="c@example.com", email_canonical="c@example.com", display_name="丙", status="active")
        session.add(user)
        session.flush()
        conversation = ChatConversation(user_id=user.id, client_id="c" * 36, title="t")
        session.add(conversation)
        session.flush()
        session.add(ChatMessage(conversation_id=conversation.id, client_msg_id="m" * 36, seq=1, role="user", content="你好"))
        session.add(LlmCallLog(
            user_id=user.id, conversation_id=conversation.id, kind="chat", provider="gemini", model="m", attempt=1, status="ok",
        ))
        session.commit()
        user_id = user.id

        session.delete(session.get(User, user_id))
        session.commit()

        assert session.scalar(select(func.count()).select_from(ChatConversation)) == 0
        assert session.scalar(select(func.count()).select_from(ChatMessage)) == 0
        ledger = session.scalars(select(LlmCallLog)).one()
        assert ledger.user_id is None and ledger.kind == "chat"          # 帳本保留、使用者被匿名化


def test_the_chat_tables_reject_duplicate_sequence_numbers_and_client_ids(migrated_database):
    """「同一則訊息只寫一次」與「一則使用者訊息至多一則助理回覆」由資料庫保證。"""
    from sqlalchemy.exc import IntegrityError

    from app.models import ChatConversation, ChatMessage

    with Session(migrated_database) as session:
        user = User(email="d@example.com", email_canonical="d@example.com", display_name="丁", status="active")
        session.add(user)
        session.flush()
        conversation = ChatConversation(user_id=user.id, client_id="d" * 36, title="t")
        session.add(conversation)
        session.flush()
        session.add(ChatMessage(conversation_id=conversation.id, client_msg_id="a" * 36, seq=1, role="user", content="x"))
        session.commit()
        for duplicate in (
            ChatMessage(conversation_id=conversation.id, client_msg_id="b" * 36, seq=1, role="assistant", content="同序號"),
            ChatMessage(conversation_id=conversation.id, client_msg_id="a" * 36, seq=2, role="assistant", content="同識別碼"),
        ):
            session.add(duplicate)
            with pytest.raises(IntegrityError):
                session.flush()
            session.rollback()
        session.add(ChatConversation(user_id=user.id, client_id="d" * 36, title="重送建立"))
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


def test_downgrading_0005_removes_only_the_chat_tables_and_keeps_every_member_row(migrated_database):
    engine = migrated_database
    with Session(engine) as session:
        user = User(email="e@example.com", email_canonical="e@example.com", display_name="戊", status="active")
        session.add(user)
        session.flush()
        session.add(UserIdentity(user_id=user.id, provider="google", provider_subject="s3"))
        session.commit()

    command.downgrade(alembic_config(engine.url.render_as_string(hide_password=False)), "0004_users_birthdate")

    assert not set(CHAT_TABLES) & set(inspect(engine).get_table_names())
    with engine.connect() as conn:
        for table in ("users", "user_identities"):
            reflected = Table(table, MetaData(), autoload_with=engine)
            assert conn.execute(select(func.count()).select_from(reflected)).scalar_one() == 1, table


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
