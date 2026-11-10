"""database-access 規格：資料表只能透過版本化 migration 變更。"""
import json
import re

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, Table, func, inspect, select
from sqlalchemy.exc import IntegrityError
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
RPG_TABLES = ("scripts", "script_versions", "script_version_tags", "sessions", "memory_logs")
CONTENT_MODE_COLUMNS = ("content_tier_preference", "adult_confirmed_at", "adult_rules_version", "adult_confirm_method")
AUTHORING_TABLES = ("script_submissions", "runtime_limits")
WALLET_TABLES = ("wallets", "wallet_entries")
AUTHORING_USER_COLUMNS = ("nickname", "nickname_code", "publish_suspended_at")
REVIEW_COLUMNS = ("review_hash", "review_verdict", "review_categories_json", "reviewed_at")
INITIAL_LIMITS = (
    ("script_limit.free", 3), ("script_limit.plus", 10), ("script_limit.vip", 30),
    ("script_submit_daily.free", 3), ("script_submit_daily.plus", 5), ("script_submit_daily.vip", 10),
    ("script_reject_streak", 3),
)
CHARACTER_LIMITS = (
    ("protagonist_suggest_daily.free", 5), ("protagonist_suggest_daily.plus", 15), ("protagonist_suggest_daily.vip", 40),
)
WALLET_LIMITS = (
    ("coin.turn_cost.standard", 30), ("coin.signup_grant", 200), ("coin.checkin_grant", 200), ("coin.free_cap", 1000),
    ("coin.hold_timeout_seconds", 180),
)
RPG_LIMITS = (("rpg.max_months_per_turn", 24),)
HEAD_LIMITS = INITIAL_LIMITS + CHARACTER_LIMITS + WALLET_LIMITS + RPG_LIMITS
WALLET_HEAD_LIMITS = INITIAL_LIMITS + CHARACTER_LIMITS + WALLET_LIMITS       # 0015 當時的十五個
CHARACTER_HEAD_LIMITS = INITIAL_LIMITS + CHARACTER_LIMITS       # 0014 當時的十個


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
    assert set(inspect(engine).get_table_names()) == {"alembic_version", *MEMBER_TABLES, TOMBSTONE_TABLE, *CHAT_TABLES, *ADMIN_TABLES, *ALERT_TABLES, *LOCK_TABLES, *RPG_TABLES, *AUTHORING_TABLES, *WALLET_TABLES}

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


def _insert_user(conn, users, number: int, **values) -> None:
    now = utcnow().replace(tzinfo=None)
    row = dict(
        public_id=f"00000000-0000-4000-8000-{number:012d}", email=f"u{number}@example.com", email_canonical=f"u{number}@example.com",
        display_name=f"帳號{number}", role="user", member_tier="free", status="active", failed_login_count=0, token_version=1,
        created_at=now, updated_at=now,
    )
    row.update(values)
    conn.execute(users.insert().values(**row))


def test_0011_clears_player_passwords_and_retires_pending_verification_but_leaves_admins_and_structure_alone(tmp_path):
    """remove-player-password-auth：只動資料。玩家的密碼雜湊、失敗計數與密碼鎖定被清除；待驗證改為 active；管理員完全不變；可重複執行。"""
    url = f"sqlite+pysqlite:///{tmp_path / 'passwords.sqlite3'}"
    cfg = alembic_config(url)
    engine = create_engine_for(url)
    revision = ScriptDirectory.from_config(cfg).get_revision("0011_remove_player_passwords")
    assert revision.down_revision == "0010_google_first_login" and len(revision.revision) <= 32

    command.upgrade(cfg, "0010_google_first_login")
    locked = utcnow().replace(tzinfo=None)
    with engine.begin() as conn:
        users = Table("users", MetaData(), autoload_with=conn)
        _insert_user(conn, users, 1, password_hash="$argon2id$player", failed_login_count=4, locked_until=locked)
        _insert_user(conn, users, 2, status="pending_verification", password_hash="$argon2id$pending")
        _insert_user(conn, users, 3, status="deletion_pending", status_before_deletion="pending_verification", deletion_requested_at=locked)
        _insert_user(conn, users, 4, status="deletion_pending", status_before_deletion="active", deletion_requested_at=locked)
        _insert_user(conn, users, 5, role="admin", password_hash="$argon2id$admin", failed_login_count=3, locked_until=locked)
        _insert_user(conn, users, 6, status="suspended", password_hash="$argon2id$suspended")
    before_tables = set(inspect(engine).get_table_names())
    before_columns = {c["name"] for c in inspect(engine).get_columns("users")}

    def rows() -> dict[str, dict]:
        with engine.connect() as conn:
            table = Table("users", MetaData(), autoload_with=conn)
            return {r["email"]: dict(r) for r in conn.execute(select(table)).mappings()}

    admin_before = rows()["u5@example.com"]

    command.upgrade(cfg, "0011_remove_player_passwords")
    after = rows()
    players = [after[f"u{n}@example.com"] for n in (1, 2, 3, 4, 6)]
    assert all(p["password_hash"] is None and p["failed_login_count"] == 0 and p["locked_until"] is None for p in players)
    assert after["u2@example.com"]["status"] == "active"
    assert after["u3@example.com"]["status"] == "deletion_pending" and after["u3@example.com"]["status_before_deletion"] == "active"
    assert after["u4@example.com"]["status_before_deletion"] == "active" and after["u4@example.com"]["status"] == "deletion_pending"
    assert after["u6@example.com"]["status"] == "suspended"
    assert after["u5@example.com"] == admin_before                                  # 管理員完全不變
    # 曾持有密碼的玩家（1、2、6）token_version 加 1；沒有密碼的（3、4）不變——它們的登入狀態不是靠密碼取得的
    assert [after[f"u{n}@example.com"]["token_version"] for n in (1, 2, 6)] == [2, 2, 2]
    assert [after[f"u{n}@example.com"]["token_version"] for n in (3, 4)] == [1, 1]
    assert all(after[f"u{n}@example.com"]["email_verified_at"] is None for n in (1, 2, 3))     # 驗證時間保持原樣

    assert set(inspect(engine).get_table_names()) == before_tables                 # 沒有結構變更
    assert {c["name"] for c in inspect(engine).get_columns("users")} == before_columns

    command.downgrade(cfg, "0010_google_first_login")                              # 資料遷移不還原
    assert rows() == after
    command.upgrade(cfg, "0011_remove_player_passwords")                           # 再升級一次，結果相同
    assert rows() == after
    engine.dispose()


def test_0012_adds_the_content_mode_columns_and_five_rpg_tables_backfills_the_preference_and_keeps_every_row(tmp_path):
    """rpg-data-and-script：users 新增四欄（偏好 NOT NULL 並有伺服器預設）、新增五張資料表；既有帳號的偏好依確認當時是否成年回填；
    回復只移除新增的東西；再升級一次回填結果相同。"""
    url = f"sqlite+pysqlite:///{tmp_path / 'rpg.sqlite3'}"
    cfg = alembic_config(url)
    engine = create_engine_for(url)
    revision = ScriptDirectory.from_config(cfg).get_revision("0012_rpg_data_and_script")
    assert revision.down_revision == "0011_remove_player_passwords" and len(revision.revision) <= 32

    command.upgrade(cfg, "0011_remove_player_passwords")
    with engine.begin() as conn:
        users = Table("users", MetaData(), autoload_with=conn)
        _insert_user(conn, users, 1, is_adult_at_confirmation=True, birth_date=None)               # 確認當時已成年
        _insert_user(conn, users, 2, is_adult_at_confirmation=False)                              # 確認當時未成年
        _insert_user(conn, users, 3)                                                              # 勾選版時代：此欄為空
        _insert_user(conn, users, 4, role="admin")                                                # 管理員沒有年齡確認
    before_tables = set(inspect(engine).get_table_names())
    before_columns = {c["name"] for c in inspect(engine).get_columns("users")}

    def preferences() -> dict[str, str]:
        with engine.connect() as conn:
            table = Table("users", MetaData(), autoload_with=conn)
            return {r["email"]: r["content_tier_preference"] for r in conn.execute(select(table)).mappings()}

    command.upgrade(cfg, "0012_rpg_data_and_script")
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) - before_tables == set(RPG_TABLES)
    columns = {c["name"]: c for c in inspector.get_columns("users")}
    assert set(columns) - before_columns == set(CONTENT_MODE_COLUMNS)
    assert not columns["content_tier_preference"]["nullable"] and "safe" in str(columns["content_tier_preference"]["default"])
    assert all(columns[name]["nullable"] for name in CONTENT_MODE_COLUMNS[1:])
    # 滿 18 歲不會自動調高：只有「確認當時已成年」的帳號是 standard，其餘（含舊版此欄為空、管理員）維持最保守的 safe
    assert preferences() == {"u1@example.com": "standard", "u2@example.com": "safe", "u3@example.com": "safe", "u4@example.com": "safe"}

    for table, column, referred, action in (
        ("scripts", "creator_id", "users", "CASCADE"),
        ("script_versions", "script_id", "scripts", "CASCADE"),
        ("script_version_tags", "version_id", "script_versions", "CASCADE"),
        ("sessions", "user_id", "users", "CASCADE"),
        ("memory_logs", "session_id", "sessions", "CASCADE"),
        ("sessions", "source_version_id", "script_versions", None),        # 無動作：SQL Server 不允許多重串聯路徑（錯誤 1785）
    ):
        matches = [fk for fk in inspector.get_foreign_keys(table) if fk["constrained_columns"] == [column]]
        assert len(matches) == 1 and matches[0]["referred_table"] == referred, (table, column)
        assert matches[0]["options"].get("ondelete") == action, (table, column)
    index_names = {i["name"] for t in RPG_TABLES for i in inspector.get_indexes(t)}
    assert {
        "uq_scripts_seed_key", "uq_script_versions_live_script", "uq_script_versions_open_script",
        "ix_script_versions_status_content_tier", "ix_script_version_tags_tag", "ix_sessions_user_id_last_active_at",
        "ix_sessions_last_active_at", "ix_memory_logs_session_id_turn_number", "ix_memory_logs_turn_status_updated_at",
    } <= index_names
    partial = {i["name"] for t in RPG_TABLES for i in inspector.get_indexes(t) if i["unique"]}
    assert {"uq_scripts_seed_key", "uq_script_versions_live_script", "uq_script_versions_open_script"} <= partial
    assert sorted(sorted(u["column_names"]) for u in inspector.get_unique_constraints("memory_logs")) == [["client_msg_id", "session_id"], ["seq", "session_id"]]
    assert sorted(sorted(u["column_names"]) for u in inspector.get_unique_constraints("script_versions") if "version_no" in u["column_names"]) == [["script_id", "version_no"]]

    command.downgrade(cfg, "0011_remove_player_passwords")
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == before_tables
    assert {c["name"] for c in inspector.get_columns("users")} == before_columns
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(Table("users", MetaData(), autoload_with=conn))).scalar_one() == 4

    command.upgrade(cfg, "0012_rpg_data_and_script")                                                # 再升級一次，回填結果相同
    assert preferences() == {"u1@example.com": "standard", "u2@example.com": "safe", "u3@example.com": "safe", "u4@example.com": "safe"}
    engine.dispose()


def test_0013_adds_nullable_columns_two_tables_and_the_seven_initial_limits_and_keeps_every_row(tmp_path):
    """rpg-script-authoring：users 新增三個可為空的欄位與一個索引、script_versions 新增四個可為空的審查欄位、新增兩張表並寫入
    七個初始限制；既有的帳號與劇本一列不少；回復只移除新增的東西；再升級一次初始列不重複。"""
    url = f"sqlite+pysqlite:///{tmp_path / 'authoring.sqlite3'}"
    cfg = alembic_config(url)
    engine = create_engine_for(url)
    revision = ScriptDirectory.from_config(cfg).get_revision("0013_rpg_script_authoring")
    assert revision.down_revision == "0012_rpg_data_and_script" and len(revision.revision) <= 32

    command.upgrade(cfg, "0012_rpg_data_and_script")
    now = utcnow().replace(tzinfo=None)
    with engine.begin() as conn:
        users = Table("users", MetaData(), autoload_with=conn)
        _insert_user(conn, users, 1)
        _insert_user(conn, users, 2, role="admin")
        scripts = Table("scripts", MetaData(), autoload_with=conn)
        conn.execute(scripts.insert().values(
            creator_id=1, public_id="00000000-0000-4000-8000-0000000000a1", created_at=now, updated_at=now,
        ))
    before_tables = set(inspect(engine).get_table_names())
    before_users = {c["name"] for c in inspect(engine).get_columns("users")}
    before_versions = {c["name"] for c in inspect(engine).get_columns("script_versions")}

    command.upgrade(cfg, "0013_rpg_script_authoring")
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) - before_tables == set(AUTHORING_TABLES)
    user_columns = {c["name"]: c for c in inspector.get_columns("users")}
    assert set(user_columns) - before_users == set(AUTHORING_USER_COLUMNS)
    assert all(user_columns[name]["nullable"] for name in AUTHORING_USER_COLUMNS)
    version_columns = {c["name"]: c for c in inspector.get_columns("script_versions")}
    assert set(version_columns) - before_versions == set(REVIEW_COLUMNS)
    assert all(version_columns[name]["nullable"] for name in REVIEW_COLUMNS)
    assert "ix_users_publish_suspended_at" in {i["name"] for i in inspector.get_indexes("users")}
    submission_fk = [fk for fk in inspector.get_foreign_keys("script_submissions") if fk["constrained_columns"] == ["user_id"]]
    assert len(submission_fk) == 1 and submission_fk[0]["referred_table"] == "users"
    assert submission_fk[0]["options"].get("ondelete") == "CASCADE"
    assert [fk["constrained_columns"] for fk in inspector.get_foreign_keys("script_submissions")] == [["user_id"]]   # 刻意不連到版本
    assert "ix_script_submissions_user_id_created_at" in {i["name"] for i in inspector.get_indexes("script_submissions")}
    assert sorted(sorted(u["column_names"]) for u in inspector.get_unique_constraints("runtime_limits")) == [["key"]]
    kind = next(c for c in inspector.get_columns("llm_call_logs") if c["name"] == "kind")
    assert kind["type"].length == 20 and not kind["nullable"]                      # script_review 有 13 個字，SQL Server 放不進 10

    def limits() -> dict[str, int]:
        with engine.connect() as conn:
            table = Table("runtime_limits", MetaData(), autoload_with=conn)
            return {row["key"]: row["int_value"] for row in conn.execute(select(table)).mappings()}

    assert limits() == dict(INITIAL_LIMITS)
    with engine.connect() as conn:                                      # 既有資料一列不少，新欄位為空
        users = Table("users", MetaData(), autoload_with=conn)
        assert conn.execute(select(func.count()).select_from(users)).scalar_one() == 2
        assert conn.execute(select(users.c.nickname, users.c.nickname_code, users.c.publish_suspended_at)).all() == [(None, None, None)] * 2
        assert conn.execute(select(func.count()).select_from(Table("scripts", MetaData(), autoload_with=conn))).scalar_one() == 1

    with engine.begin() as conn:                                        # 降級前有一筆新的呼叫類別：窄回 10 個字之前會先被刪掉
        ledger = Table("llm_call_logs", MetaData(), autoload_with=conn)
        base = dict(provider="fake", model="m", attempt=1, status="ok", counted=False, latency_ms=1, created_at=now)
        conn.execute(ledger.insert().values(kind="script_review", **base))
        conn.execute(ledger.insert().values(kind="chat", **base))

    command.downgrade(cfg, "0012_rpg_data_and_script")
    inspector = inspect(engine)
    assert next(c for c in inspector.get_columns("llm_call_logs") if c["name"] == "kind")["type"].length == 10
    with engine.connect() as conn:
        assert [row[0] for row in conn.execute(select(Table("llm_call_logs", MetaData(), autoload_with=conn).c.kind))] == ["chat"]
    assert set(inspector.get_table_names()) == before_tables
    assert {c["name"] for c in inspector.get_columns("users")} == before_users
    assert {c["name"] for c in inspector.get_columns("script_versions")} == before_versions
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(Table("users", MetaData(), autoload_with=conn))).scalar_one() == 2

    command.upgrade(cfg, "0013_rpg_script_authoring")                   # 再升級一次，初始列不重複
    assert limits() == dict(INITIAL_LIMITS)
    engine.dispose()


def test_0014_adds_a_nullable_protagonist_column_and_only_the_missing_limits_and_keeps_every_row(tmp_path):
    """rpg-character-creation：sessions 新增可為空的 protagonist_json、runtime_limits 補三個生成次數的鍵（只插入缺少的，
    不蓋掉部署者調整過的值）；既有的存檔一列不少、姓名與年齡不變；回復只移除新增的東西；再升級一次不重複。"""
    url = f"sqlite+pysqlite:///{tmp_path / 'character.sqlite3'}"
    cfg = alembic_config(url)
    engine = create_engine_for(url)
    revision = ScriptDirectory.from_config(cfg).get_revision("0014_rpg_character_creation")
    assert revision.down_revision == "0013_rpg_script_authoring" and len(revision.revision) <= 32

    command.upgrade(cfg, "0013_rpg_script_authoring")
    now = utcnow().replace(tzinfo=None)
    with engine.begin() as conn:
        users = Table("users", MetaData(), autoload_with=conn)
        _insert_user(conn, users, 1)
        sessions = Table("sessions", MetaData(), autoload_with=conn)
        conn.execute(sessions.insert().values(
            user_id=1, public_id="00000000-0000-4000-8000-0000000000b1", script_snapshot_json={"title": "舊"}, snapshot_spec_version=1,
            player_name="舊存檔", character_age=30, current_location="", status_summary="", version=1, notebook_text="", notebook_version=1,
            notebook_upto_turn=0, content_tier="safe", status="active", is_archived=False, elapsed_months=0, last_active_at=now,
            entities_json={}, goals_json={"active": [], "archived": []}, milestone_progress_json={},
            created_at=now, updated_at=now,
        ))
        limits = Table("runtime_limits", MetaData(), autoload_with=conn)
        conn.execute(limits.update().where(limits.c.key == "script_limit.free").values(int_value=9))     # 部署者調整過
    before_tables = set(inspect(engine).get_table_names())
    before_columns = {c["name"] for c in inspect(engine).get_columns("sessions")}

    def limits_now() -> dict[str, int]:
        with engine.connect() as conn:
            table = Table("runtime_limits", MetaData(), autoload_with=conn)
            return {row["key"]: row["int_value"] for row in conn.execute(select(table)).mappings()}

    command.upgrade(cfg, "0014_rpg_character_creation")
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == before_tables
    columns = {c["name"]: c for c in inspector.get_columns("sessions")}
    assert set(columns) - before_columns == {"protagonist_json"} and columns["protagonist_json"]["nullable"]
    assert limits_now() == {**dict(CHARACTER_HEAD_LIMITS), "script_limit.free": 9}          # 新增三個，調整過的值沒被蓋掉
    with engine.connect() as conn:
        save = conn.execute(select(Table("sessions", MetaData(), autoload_with=conn))).mappings().one()
        assert (save["player_name"], save["character_age"], save["protagonist_json"]) == ("舊存檔", 30, None)

    command.downgrade(cfg, "0013_rpg_script_authoring")
    assert {c["name"] for c in inspect(engine).get_columns("sessions")} == before_columns
    assert limits_now() == {**dict(INITIAL_LIMITS), "script_limit.free": 9}
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(Table("sessions", MetaData(), autoload_with=conn))).scalar_one() == 1

    command.upgrade(cfg, "0014_rpg_character_creation")                                    # 再升級一次，不重複
    assert limits_now() == {**dict(CHARACTER_HEAD_LIMITS), "script_limit.free": 9}
    engine.dispose()


def test_0014_does_not_duplicate_or_overwrite_a_limit_that_already_exists(tmp_path):
    """資料庫在升級前已經有其中一個新鍵（例如先手動寫入）：保留原值，不產生重複。"""
    url = f"sqlite+pysqlite:///{tmp_path / 'character_existing.sqlite3'}"
    cfg = alembic_config(url)
    engine = create_engine_for(url)
    command.upgrade(cfg, "0013_rpg_script_authoring")
    with engine.begin() as conn:
        limits = Table("runtime_limits", MetaData(), autoload_with=conn)
        conn.execute(limits.insert().values(key="protagonist_suggest_daily.free", int_value=1, updated_at=utcnow().replace(tzinfo=None)))
    command.upgrade(cfg, "0014_rpg_character_creation")
    with engine.connect() as conn:
        table = Table("runtime_limits", MetaData(), autoload_with=conn)
        rows = conn.execute(select(table.c.key, table.c.int_value).where(table.c.key.like("protagonist_suggest_daily.%"))).all()
    assert sorted(rows) == [("protagonist_suggest_daily.free", 1), ("protagonist_suggest_daily.plus", 15), ("protagonist_suggest_daily.vip", 40)]
    engine.dispose()


def test_migrated_authoring_tables_hold_the_initial_limits_and_cascade_with_the_user(migrated_database):
    """SQLite 與 SQL Server 兩軌：升級後有十個初始限制（鍵唯一）；刪除使用者時送審紀錄一併消失。"""
    engine = migrated_database
    metadata = MetaData()
    with engine.begin() as conn:
        limits = Table("runtime_limits", metadata, autoload_with=conn)
        assert {row["key"]: row["int_value"] for row in conn.execute(select(limits)).mappings()} == dict(HEAD_LIMITS)
    with pytest.raises(IntegrityError):                                     # 鍵唯一
        with engine.begin() as conn:
            conn.execute(limits.insert().values(key="script_limit.free", int_value=1, updated_at=utcnow().replace(tzinfo=None)))
    with engine.begin() as conn:
        users = Table("users", metadata, autoload_with=conn)
        submissions = Table("script_submissions", metadata, autoload_with=conn)
        _insert_user(conn, users, 51)
        user_id = conn.execute(select(users.c.id).where(users.c.email == "u51@example.com")).scalar_one()
        conn.execute(submissions.insert().values(user_id=user_id, outcome="auto_rejected", categories_json=["non_consensual"], created_at=utcnow().replace(tzinfo=None)))
        assert conn.execute(select(func.count()).select_from(submissions)).scalar_one() == 1
        conn.execute(users.delete().where(users.c.id == user_id))
        assert conn.execute(select(func.count()).select_from(submissions)).scalar_one() == 0


def test_0015_adds_the_wallet_tables_and_only_the_missing_coin_settings_and_keeps_every_row(tmp_path):
    """game-wallet：新增 wallets 與 wallet_entries（約束與串聯）、runtime_limits 補五個遊戲幣設定（只插入缺少的，不蓋掉部署者
    調整過的值）；既有資料一列不少；回復只移除新增的東西；再升級一次不重複。"""
    url = f"sqlite+pysqlite:///{tmp_path / 'wallet.sqlite3'}"
    cfg = alembic_config(url)
    engine = create_engine_for(url)
    revision = ScriptDirectory.from_config(cfg).get_revision("0015_game_wallet")
    assert revision.down_revision == "0014_rpg_character_creation" and len(revision.revision) <= 32

    command.upgrade(cfg, "0014_rpg_character_creation")
    with engine.begin() as conn:
        _insert_user(conn, Table("users", MetaData(), autoload_with=conn), 1)
        limits = Table("runtime_limits", MetaData(), autoload_with=conn)
        conn.execute(limits.update().where(limits.c.key == "script_limit.free").values(int_value=9))     # 部署者調整過
        conn.execute(limits.insert().values(key="coin.free_cap", int_value=500, updated_at=utcnow().replace(tzinfo=None)))   # 先手動寫入的新鍵
    before_tables = set(inspect(engine).get_table_names())

    def limits_now() -> dict[str, int]:
        with engine.connect() as conn:
            table = Table("runtime_limits", MetaData(), autoload_with=conn)
            return {row["key"]: row["int_value"] for row in conn.execute(select(table)).mappings()}

    command.upgrade(cfg, "0015_game_wallet")
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) - before_tables == set(WALLET_TABLES)
    assert limits_now() == {**dict(WALLET_HEAD_LIMITS), "script_limit.free": 9, "coin.free_cap": 500}      # 新增四個，調整過與先寫入的不被蓋掉

    wallet_columns = {c["name"]: c for c in inspector.get_columns("wallets")}
    assert set(wallet_columns) == {"id", "user_id", "free_balance", "paid_balance", "version", "created_at", "updated_at"}
    assert not any(c["nullable"] for c in wallet_columns.values())
    assert [sorted(u["column_names"]) for u in inspector.get_unique_constraints("wallets")] == [["user_id"]]
    entry_columns = {c["name"]: c for c in inspector.get_columns("wallet_entries")}
    assert {name for name, c in entry_columns.items() if c["nullable"]} == {"session_public_id", "ref_key", "resolution"}
    assert sorted(sorted(u["column_names"]) for u in inspector.get_unique_constraints("wallet_entries")) == [["idempotency_key"], ["seq", "user_id"]]
    assert "ix_wallet_entries_user_id_created_at" in {i["name"] for i in inspector.get_indexes("wallet_entries")}
    for table in WALLET_TABLES:
        (foreign_key,) = inspector.get_foreign_keys(table)
        assert foreign_key["referred_table"] == "users" and foreign_key["options"].get("ondelete") == "CASCADE", table

    command.downgrade(cfg, "0014_rpg_character_creation")
    assert set(inspect(engine).get_table_names()) == before_tables
    assert limits_now() == {**dict(CHARACTER_HEAD_LIMITS), "script_limit.free": 9}                  # 五個遊戲幣鍵（含手動寫入的）一併移除
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(Table("users", MetaData(), autoload_with=conn))).scalar_one() == 1

    command.upgrade(cfg, "0015_game_wallet")                                                        # 再升級一次，不重複
    assert limits_now() == {**dict(WALLET_HEAD_LIMITS), "script_limit.free": 9}
    engine.dispose()


def _insert_old_save_and_turn(conn) -> None:
    """0016 之前的結構上的一個存檔、一個已完成的回合與一筆用量帳本（欄位只含當時存在的）。"""
    now = utcnow().replace(tzinfo=None)
    _insert_user(conn, Table("users", MetaData(), autoload_with=conn), 1)
    user_id = conn.execute(select(Table("users", MetaData(), autoload_with=conn).c.id)).scalar_one()
    sessions = Table("sessions", MetaData(), autoload_with=conn)
    # SQL Server 反射出來的 JSON 欄位是 NVARCHAR，pyodbc 不收 dict；SQLite 的 JSON 型別則自己序列化（傳字串會變成雙重編碼）
    as_json = (lambda value: json.dumps(value, ensure_ascii=False)) if conn.dialect.name == "mssql" else (lambda value: value)
    conn.execute(sessions.insert().values(
        user_id=user_id, public_id="00000000-0000-4000-8000-0000000000c1", script_snapshot_json=as_json({"title": "舊"}), snapshot_spec_version=1,
        player_name="舊存檔", character_age=30, current_location="", status_summary="", version=1, notebook_text="", notebook_version=1,
        notebook_upto_turn=0, content_tier="safe", status="active", is_archived=False, elapsed_months=0, last_active_at=now,
        entities_json=as_json({}), goals_json=as_json({"active": [], "archived": []}), milestone_progress_json=as_json({}),
        created_at=now, updated_at=now,
    ))
    session_id = conn.execute(select(sessions.c.id)).scalar_one()
    conn.execute(Table("memory_logs", MetaData(), autoload_with=conn).insert().values(
        session_id=session_id, client_msg_id="old-1", seq=1, turn_number=1, turn_status="complete", user_action="舊行動",
        llm_raw_response="舊劇情", created_at=now, updated_at=now,
    ))
    conn.execute(Table("llm_call_logs", MetaData(), autoload_with=conn).insert().values(
        user_id=user_id, kind="chat", provider="gemini", model="m", attempt=1, status="ok", counted=True, latency_ms=10, created_at=now,
    ))


def test_0016_adds_the_turn_engine_columns_and_the_month_limit_and_keeps_every_row(tmp_path):
    """rpg-turn-engine：memory_logs.attempt（既有回合為 1）、sessions 的第 0 幕三欄（既有存檔為空）、llm_call_logs.first_chunk_ms
    （可為空），與 runtime_limits 的 rpg.max_months_per_turn（只插入缺少的，不蓋掉部署者調整過的值）；既有資料一列不少；
    回復只移除新增的東西；再升級一次不重複。"""
    url = f"sqlite+pysqlite:///{tmp_path / 'turn_engine.sqlite3'}"
    cfg = alembic_config(url)
    engine = create_engine_for(url)
    revision = ScriptDirectory.from_config(cfg).get_revision("0016_rpg_turn_engine")
    assert revision.down_revision == "0015_game_wallet" and len(revision.revision) <= 32

    command.upgrade(cfg, "0015_game_wallet")
    with engine.begin() as conn:
        _insert_old_save_and_turn(conn)
        limits = Table("runtime_limits", MetaData(), autoload_with=conn)
        conn.execute(limits.update().where(limits.c.key == "script_limit.free").values(int_value=9))     # 部署者調整過
    before_tables = set(inspect(engine).get_table_names())
    before = {table: {c["name"] for c in inspect(engine).get_columns(table)} for table in ("sessions", "memory_logs", "llm_call_logs")}

    def limits_now() -> dict[str, int]:
        with engine.connect() as conn:
            table = Table("runtime_limits", MetaData(), autoload_with=conn)
            return {row["key"]: row["int_value"] for row in conn.execute(select(table)).mappings()}

    command.upgrade(cfg, "0016_rpg_turn_engine")
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == before_tables
    added = {table: {c["name"]: c for c in inspector.get_columns(table) if c["name"] not in before[table]} for table in before}
    assert set(added["sessions"]) == {"opening_text", "opening_state", "opening_started_at"}
    assert all(c["nullable"] for c in added["sessions"].values())
    assert set(added["memory_logs"]) == {"attempt"} and not added["memory_logs"]["attempt"]["nullable"]
    assert set(added["llm_call_logs"]) == {"first_chunk_ms"} and added["llm_call_logs"]["first_chunk_ms"]["nullable"]
    assert limits_now() == {**dict(HEAD_LIMITS), "script_limit.free": 9}                    # 新增一個，調整過的值沒被蓋掉
    with engine.connect() as conn:                                                           # 既有資料不變，新欄位的預設值正確
        save = conn.execute(select(Table("sessions", MetaData(), autoload_with=conn))).mappings().one()
        assert (save["player_name"], save["opening_text"], save["opening_state"], save["opening_started_at"]) == ("舊存檔", None, None, None)
        turn = conn.execute(select(Table("memory_logs", MetaData(), autoload_with=conn))).mappings().one()
        assert (turn["user_action"], turn["llm_raw_response"], turn["turn_number"], turn["attempt"]) == ("舊行動", "舊劇情", 1, 1)
        call = conn.execute(select(Table("llm_call_logs", MetaData(), autoload_with=conn))).mappings().one()
        assert (call["kind"], call["first_chunk_ms"]) == ("chat", None)

    command.downgrade(cfg, "0015_game_wallet")
    after = {table: {c["name"] for c in inspect(engine).get_columns(table)} for table in before}
    assert after == before
    assert limits_now() == {**dict(WALLET_HEAD_LIMITS), "script_limit.free": 9}
    with engine.connect() as conn:                                                           # 回復後既有的存檔與回合仍在，沒有被串聯刪除
        for name in ("users", "sessions", "memory_logs", "llm_call_logs"):
            assert conn.execute(select(func.count()).select_from(Table(name, MetaData(), autoload_with=conn))).scalar_one() == 1, name

    command.upgrade(cfg, "0016_rpg_turn_engine")                                            # 再升級一次，不重複
    assert limits_now() == {**dict(HEAD_LIMITS), "script_limit.free": 9}
    with engine.connect() as conn:
        assert conn.execute(select(Table("memory_logs", MetaData(), autoload_with=conn).c.attempt)).scalar_one() == 1
    engine.dispose()


def test_0016_does_not_overwrite_a_month_limit_that_already_exists(tmp_path):
    """資料庫在升級前已經有新鍵（例如先手動寫入）：保留原值，不產生重複。"""
    url = f"sqlite+pysqlite:///{tmp_path / 'turn_engine_existing.sqlite3'}"
    cfg = alembic_config(url)
    engine = create_engine_for(url)
    command.upgrade(cfg, "0015_game_wallet")
    with engine.begin() as conn:
        limits = Table("runtime_limits", MetaData(), autoload_with=conn)
        conn.execute(limits.insert().values(key="rpg.max_months_per_turn", int_value=6, updated_at=utcnow().replace(tzinfo=None)))
    command.upgrade(cfg, "0016_rpg_turn_engine")
    with engine.connect() as conn:
        table = Table("runtime_limits", MetaData(), autoload_with=conn)
        assert conn.execute(select(table.c.key, table.c.int_value).where(table.c.key.like("rpg.%"))).all() == [("rpg.max_months_per_turn", 6)]
    engine.dispose()


def test_migrated_turn_engine_columns_have_the_right_defaults_on_both_databases(migrated_database):
    """SQLite 與 SQL Server 兩軌：升級後有十六個設定（鍵唯一）；沒有指定嘗試序號的回合為 1，第 0 幕欄位預設為空，首字延遲可為空。"""
    engine = migrated_database
    with engine.begin() as conn:
        limits = Table("runtime_limits", MetaData(), autoload_with=conn)
        assert {row["key"]: row["int_value"] for row in conn.execute(select(limits)).mappings()} == dict(HEAD_LIMITS)
        _insert_old_save_and_turn(conn)
    with engine.connect() as conn:
        turn = conn.execute(select(Table("memory_logs", MetaData(), autoload_with=conn))).mappings().one()
        save = conn.execute(select(Table("sessions", MetaData(), autoload_with=conn))).mappings().one()
        call = conn.execute(select(Table("llm_call_logs", MetaData(), autoload_with=conn))).mappings().one()
    assert turn["attempt"] == 1
    assert (save["opening_text"], save["opening_state"], save["opening_started_at"], call["first_chunk_ms"]) == (None, None, None, None)


def _insert_old_script_version(conn) -> None:
    """0017 之前的結構上的一位使用者、一個劇本與一個已發佈的版本（欄位只含當時存在的）。"""
    now = utcnow().replace(tzinfo=None)
    _insert_user(conn, Table("users", MetaData(), autoload_with=conn), 1)
    user_id = conn.execute(select(Table("users", MetaData(), autoload_with=conn).c.id)).scalar_one()
    scripts = Table("scripts", MetaData(), autoload_with=conn)
    conn.execute(scripts.insert().values(public_id="00000000-0000-4000-8000-0000000000d1", creator_id=user_id, created_at=now, updated_at=now))
    script_id = conn.execute(select(scripts.c.id)).scalar_one()
    as_json = (lambda value: json.dumps(value, ensure_ascii=False)) if conn.dialect.name == "mssql" else (lambda value: value)
    conn.execute(Table("script_versions", MetaData(), autoload_with=conn).insert().values(
        public_id="00000000-0000-4000-8000-0000000000d2", script_id=script_id, version_no=1, status="published", spec_version=1,
        content_hash="a" * 64, total_chars=10, title="舊劇本", summary="簡介", world_name="世界", content_tier="safe", background="背景",
        ending_mode="none", characters_json=as_json([]), locations_json=as_json([]), milestones_json=as_json([]),
        live_script_id=script_id, published_at=now, created_at=now, updated_at=now,
    ))


def test_0017_adds_the_story_preference_columns_and_keeps_every_row(tmp_path):
    """story-preference：users.story_preference（NOT NULL，既有帳號為 all）與 script_versions.audience（可為空，既有版本不回填）；
    既有資料一列不少、內容不變；回復只移除這兩欄；再升級一次不出錯。"""
    url = f"sqlite+pysqlite:///{tmp_path / 'story_preference.sqlite3'}"
    cfg = alembic_config(url)
    engine = create_engine_for(url)
    revision = ScriptDirectory.from_config(cfg).get_revision("0017_story_preference")
    assert revision.down_revision == "0016_rpg_turn_engine" and len(revision.revision) <= 32

    command.upgrade(cfg, "0016_rpg_turn_engine")
    with engine.begin() as conn:
        _insert_old_script_version(conn)
    before = {table: {c["name"] for c in inspect(engine).get_columns(table)} for table in ("users", "script_versions")}
    before_tables = set(inspect(engine).get_table_names())

    command.upgrade(cfg, "0017_story_preference")
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == before_tables
    added = {table: {c["name"]: c for c in inspector.get_columns(table) if c["name"] not in before[table]} for table in before}
    assert set(added["users"]) == {"story_preference"} and added["users"]["story_preference"]["nullable"] is False
    assert set(added["script_versions"]) == {"audience"} and added["script_versions"]["audience"]["nullable"] is True
    assert str(added["users"]["story_preference"]["type"]).upper().startswith(("VARCHAR(10)", "NVARCHAR(10)"))
    with engine.connect() as conn:                                                           # 既有資料不變；既有帳號為 all，既有版本不回填
        user = conn.execute(select(Table("users", MetaData(), autoload_with=conn))).mappings().one()
        version = conn.execute(select(Table("script_versions", MetaData(), autoload_with=conn))).mappings().one()
    assert (user["display_name"], user["story_preference"]) == ("帳號1", "all")
    assert (version["title"], version["status"], version["content_hash"], version["audience"]) == ("舊劇本", "published", "a" * 64, None)

    command.downgrade(cfg, "0016_rpg_turn_engine")
    assert {table: {c["name"] for c in inspect(engine).get_columns(table)} for table in before} == before
    with engine.connect() as conn:                                                           # 回復後既有的帳號與版本仍在，沒有被串聯刪除
        for name in ("users", "scripts", "script_versions"):
            assert conn.execute(select(func.count()).select_from(Table(name, MetaData(), autoload_with=conn))).scalar_one() == 1, name

    command.upgrade(cfg, "0017_story_preference")                                           # 再升級一次
    with engine.connect() as conn:
        assert conn.execute(select(Table("users", MetaData(), autoload_with=conn).c.story_preference)).scalar_one() == "all"
    engine.dispose()


def test_migrated_story_preference_columns_have_the_right_defaults_on_both_databases(migrated_database):
    """SQLite 與 SQL Server 兩軌：沒有指定偏好的使用者列取得 all（舊版程式的 INSERT 形狀），沒有指定頻道的版本為空值。"""
    engine = migrated_database
    with engine.begin() as conn:
        _insert_old_script_version(conn)
    with engine.connect() as conn:
        user = conn.execute(select(Table("users", MetaData(), autoload_with=conn))).mappings().one()
        version = conn.execute(select(Table("script_versions", MetaData(), autoload_with=conn))).mappings().one()
    assert user["story_preference"] == "all"
    assert version["audience"] is None


def test_migrated_wallet_tables_enforce_their_constraints_and_cascade_with_the_user(migrated_database):
    """SQLite 與 SQL Server 兩軌：升級後有十六個設定（鍵唯一）；餘額不得為負、冪等鍵唯一、刪除使用者串聯刪除錢包與帳本。"""
    engine = migrated_database
    metadata = MetaData()
    now = utcnow().replace(tzinfo=None)
    with engine.begin() as conn:
        limits = Table("runtime_limits", metadata, autoload_with=conn)
        assert {row["key"]: row["int_value"] for row in conn.execute(select(limits)).mappings()} == dict(HEAD_LIMITS)
        users = Table("users", metadata, autoload_with=conn)
        _insert_user(conn, users, 61)
        user_id = conn.execute(select(users.c.id).where(users.c.email == "u61@example.com")).scalar_one()
    wallets = Table("wallets", metadata, autoload_with=engine)
    entries = Table("wallet_entries", metadata, autoload_with=engine)
    with pytest.raises(IntegrityError):                                     # 餘額不得為負（CHECK）
        with engine.begin() as conn:
            conn.execute(wallets.insert().values(user_id=user_id, free_balance=-1, paid_balance=0, version=0, created_at=now, updated_at=now))
    with engine.begin() as conn:
        conn.execute(wallets.insert().values(user_id=user_id, free_balance=200, paid_balance=0, version=1, created_at=now, updated_at=now))
        conn.execute(entries.insert().values(
            user_id=user_id, seq=1, kind="signup_grant", idempotency_key="signup:61", free_delta=200, paid_delta=0, free_after=200,
            paid_after=0, created_at=now,
        ))
    with pytest.raises(IntegrityError):                                     # 冪等鍵唯一
        with engine.begin() as conn:
            conn.execute(entries.insert().values(
                user_id=user_id, seq=2, kind="signup_grant", idempotency_key="signup:61", free_delta=0, paid_delta=0, free_after=200,
                paid_after=0, created_at=now,
            ))
    with engine.begin() as conn:
        conn.execute(users.delete().where(users.c.id == user_id))
        assert conn.execute(select(func.count()).select_from(wallets)).scalar_one() == 0
        assert conn.execute(select(func.count()).select_from(entries)).scalar_one() == 0


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
    assert {*MEMBER_TABLES, TOMBSTONE_TABLE, *CHAT_TABLES, *ADMIN_TABLES, *ALERT_TABLES, *LOCK_TABLES, *RPG_TABLES, *AUTHORING_TABLES, *WALLET_TABLES} <= removed


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
