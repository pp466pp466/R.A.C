"""member-registration、google-sign-in、email-token-flows 規格的資料層：三張會員資料表的結構與約束。

同一組測試分別在 SQLite 與 SQL Server（標記 sqlserver）上執行，確認雙軌行為一致。
"""
from datetime import timedelta

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AuthToken, Base, User, UserIdentity
from app.models.base import utcnow
from app.models.user import (
    IDENTITY_PROVIDERS, MEMBER_TIERS, ROLES, STATUS_ACTIVE, TOKEN_PURPOSES, TOKEN_VERIFY_EMAIL, USER_STATUSES,
)

NAMES = ("users", "user_identities", "auth_tokens")


def make_user(n: int = 1, **overrides) -> User:
    values = dict(
        email=f"user{n}@example.com", email_canonical=f"user{n}@example.com",
        display_name=f"使用者{n}", status=STATUS_ACTIVE,
    )
    values.update(overrides)
    return User(**values)


@pytest.fixture()
def session(db_engine):
    Base.metadata.drop_all(db_engine)
    Base.metadata.create_all(db_engine)
    with Session(db_engine) as s:
        yield s
    Base.metadata.drop_all(db_engine)


# ---- 結構（不需要資料庫）------------------------------------------------------

def test_metadata_contains_the_three_member_tables():
    assert set(NAMES) <= set(Base.metadata.tables)


def test_value_domains_are_the_documented_ones():
    assert ROLES == ("user", "admin")
    assert MEMBER_TIERS == ("free", "plus", "vip")
    assert USER_STATUSES == ("pending_verification", "active", "suspended", "deletion_pending")
    assert IDENTITY_PROVIDERS == ("google",)
    assert TOKEN_PURPOSES == ("verify_email", "reset_password")


def test_nullable_columns_are_exactly_the_documented_ones():
    columns = Base.metadata.tables["users"].columns
    nullable = {c.name for c in columns if c.nullable}
    assert nullable == {
        "password_hash", "email_verified_at", "locked_until", "terms_version", "terms_accepted_at",
        "age_confirmed_at", "age_policy_version", "last_login_at",
    }
    tokens = {c.name for c in Base.metadata.tables["auth_tokens"].columns if c.nullable}
    assert tokens == {"used_at"}


def test_users_does_not_expose_internal_id_as_the_public_identifier():
    user = Base.metadata.tables["users"]
    assert "public_id" in user.columns and user.columns["public_id"].unique
    for name in ("user_identities", "auth_tokens"):
        assert "public_id" not in Base.metadata.tables[name].columns


# ---- 唯一約束（兩種資料庫）----------------------------------------------------

def unique_column_sets(engine, table: str) -> set[tuple[str, ...]]:
    inspector = inspect(engine)
    sets = {tuple(i["column_names"]) for i in inspector.get_indexes(table) if i.get("unique")}
    try:
        sets |= {tuple(u["column_names"]) for u in inspector.get_unique_constraints(table)}
    except NotImplementedError:  # SQL Server 的方言不實作它；唯一約束會以唯一索引的形式出現在上面
        pass
    return sets


def test_unique_constraints_exist_where_the_spec_needs_them(session, db_engine):
    assert ("email",) in unique_column_sets(db_engine, "users")
    assert ("email_canonical",) in unique_column_sets(db_engine, "users")
    assert ("public_id",) in unique_column_sets(db_engine, "users")
    assert ("token_hash",) in unique_column_sets(db_engine, "auth_tokens")
    assert ("provider", "provider_subject") in unique_column_sets(db_engine, "user_identities")
    assert ("user_id", "provider") in unique_column_sets(db_engine, "user_identities")


@pytest.mark.parametrize("field", ["email", "email_canonical"])
def test_duplicate_email_or_canonical_is_rejected(session, field):
    session.add(make_user(1))
    session.commit()
    session.add(make_user(2, **{field: "user1@example.com"}))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_a_second_google_identity_for_the_same_subject_or_user_is_rejected(session):
    a, b = make_user(1), make_user(2)
    session.add_all([a, b])
    session.commit()
    session.add(UserIdentity(user_id=a.id, provider="google", provider_subject="sub-1"))
    session.commit()

    session.add(UserIdentity(user_id=b.id, provider="google", provider_subject="sub-1"))  # 同一個 Google 身分
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    session.add(UserIdentity(user_id=a.id, provider="google", provider_subject="sub-2"))  # 同一個帳號第二個身分
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_duplicate_token_hash_is_rejected(session):
    user = make_user()
    session.add(user)
    session.commit()
    expires = utcnow() + timedelta(hours=1)
    session.add(AuthToken(user_id=user.id, purpose=TOKEN_VERIFY_EMAIL, token_hash="a" * 64, expires_at=expires))
    session.commit()
    session.add(AuthToken(user_id=user.id, purpose=TOKEN_VERIFY_EMAIL, token_hash="a" * 64, expires_at=expires))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


# ---- 預設值與必填 -------------------------------------------------------------

def test_new_user_gets_the_documented_defaults(session):
    user = make_user()
    session.add(user)
    session.commit()
    session.refresh(user)
    assert (user.role, user.member_tier, user.token_version, user.failed_login_count) == ("user", "free", 1, 0)
    assert user.password_hash is None and user.email_verified_at is None and user.locked_until is None
    assert user.terms_version is None and user.last_login_at is None
    assert user.age_confirmed_at is None and user.age_policy_version is None     # 預設視為尚未確認年滿 18 歲
    assert len(user.public_id) == 36
    assert user.created_at.tzinfo is not None


def test_status_is_required_so_services_must_choose_it_explicitly(session):
    session.add(User(email="x@example.com", email_canonical="x@example.com", display_name="x"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_traditional_chinese_display_name_round_trips(session, db_engine):
    name = "王小明 😀 𠮷"
    session.add(make_user(display_name=name))
    session.commit()
    with Session(db_engine) as other:
        assert other.scalar(select(User.display_name)) == name


# ---- 外鍵與串聯刪除 -----------------------------------------------------------

def test_deleting_a_user_removes_identities_and_tokens(session, db_engine):
    user = make_user()
    session.add(user)
    session.commit()
    session.add(UserIdentity(user_id=user.id, provider="google", provider_subject="sub-1"))
    session.add(AuthToken(user_id=user.id, purpose=TOKEN_VERIFY_EMAIL, token_hash="b" * 64, expires_at=utcnow()))
    session.commit()
    session.delete(user)
    session.commit()
    with Session(db_engine) as other:
        assert other.scalars(select(UserIdentity)).all() == []
        assert other.scalars(select(AuthToken)).all() == []


def test_foreign_keys_reject_orphans(session):
    session.add(AuthToken(user_id=999, purpose=TOKEN_VERIFY_EMAIL, token_hash="c" * 64, expires_at=utcnow()))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
