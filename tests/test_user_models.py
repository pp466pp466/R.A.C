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
    IDENTITY_PROVIDERS, MEMBER_TIERS, ROLES, STATUS_ACTIVE, TOKEN_PURPOSES, TOKEN_RESET_PASSWORD, USER_STATUSES,
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
    assert USER_STATUSES == ("active", "suspended", "deletion_pending")
    assert IDENTITY_PROVIDERS == ("google",)
    assert TOKEN_PURPOSES == ("reset_password",)


def test_nullable_columns_are_exactly_the_documented_ones():
    columns = Base.metadata.tables["users"].columns
    nullable = {c.name for c in columns if c.nullable}
    assert nullable == {
        "password_hash", "email_verified_at", "locked_until", "terms_version", "terms_accepted_at",
        "age_confirmed_at", "age_policy_version", "birth_date", "age_range", "is_adult_at_confirmation", "last_login_at",
        "deletion_requested_at", "status_before_deletion", "age_deadline_at", "locked_at", "lock_reason",
        "adult_confirmed_at", "adult_rules_version", "adult_confirm_method",       # rpg-data-and-script：成年確認紀錄
    }
    # 內容模式偏好不可為空，且預設是最保守的 safe（伺服器端與資料庫預設都是）
    preference = columns["content_tier_preference"]
    assert not preference.nullable and preference.default.arg == "safe" and str(preference.server_default.arg) == "safe"
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
    session.add(AuthToken(user_id=user.id, purpose=TOKEN_RESET_PASSWORD, token_hash="a" * 64, expires_at=expires))
    session.commit()
    session.add(AuthToken(user_id=user.id, purpose=TOKEN_RESET_PASSWORD, token_hash="a" * 64, expires_at=expires))
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
    assert user.age_confirmed_at is None and user.age_policy_version is None     # 預設視為尚未完成年齡確認
    assert user.birth_date is None and user.age_range is None and user.is_adult_at_confirmation is None
    assert len(user.public_id) == 36
    assert user.created_at.tzinfo is not None


def test_the_birthday_range_and_adult_flag_round_trip_as_a_plain_date(session):
    """生日是 `Date`（沒有時間、沒有時區），兩種資料庫讀回的都是同一個日期；布林可為真、假或空。"""
    from datetime import date

    session.add_all([
        make_user(1, birth_date=date(1996, 1, 5), age_range="25_34", is_adult_at_confirmation=True),
        make_user(2, birth_date=date(2011, 10, 28), age_range="13_17", is_adult_at_confirmation=False),
        make_user(3),
    ])
    session.commit()
    session.expire_all()
    adult, minor, blank = (session.scalar(select(User).where(User.email == f"user{n}@example.com")) for n in (1, 2, 3))
    assert (adult.birth_date, adult.age_range, adult.is_adult_at_confirmation) == (date(1996, 1, 5), "25_34", True)
    assert type(adult.birth_date) is date
    assert (minor.birth_date, minor.is_adult_at_confirmation) == (date(2011, 10, 28), False)
    assert blank.birth_date is None and blank.is_adult_at_confirmation is None


# ---- 刪除紀錄 -----------------------------------------------------------------

def test_the_deletion_tombstone_holds_only_an_id_a_time_and_a_reason_and_has_no_foreign_key():
    table = Base.metadata.tables["deletion_tombstones"]
    assert {c.name for c in table.columns} == {"id", "user_id", "deleted_at", "reason"}
    assert not table.foreign_keys                      # 被指向的帳號已不存在，外鍵會讓紀錄寫不進去
    assert not any(c.nullable for c in table.columns if c.name != "id" and not c.primary_key)


def test_a_tombstone_can_be_written_after_its_user_is_gone(session):
    from app.models import DeletionTombstone
    from app.models.deletion_tombstone import REASON_INACTIVE_12_MONTHS

    user = make_user()
    session.add(user)
    session.commit()
    user_id = user.id
    session.delete(user)
    session.add(DeletionTombstone(user_id=user_id, reason=REASON_INACTIVE_12_MONTHS))
    session.commit()
    stored = session.scalars(select(DeletionTombstone)).one()
    assert (stored.user_id, stored.reason) == (user_id, "inactive_12_months")
    assert stored.deleted_at.tzinfo is not None


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
    session.add(AuthToken(user_id=user.id, purpose=TOKEN_RESET_PASSWORD, token_hash="b" * 64, expires_at=utcnow()))
    session.commit()
    session.delete(user)
    session.commit()
    with Session(db_engine) as other:
        assert other.scalars(select(UserIdentity)).all() == []
        assert other.scalars(select(AuthToken)).all() == []


def test_foreign_keys_reject_orphans(session):
    session.add(AuthToken(user_id=999, purpose=TOKEN_RESET_PASSWORD, token_hash="c" * 64, expires_at=utcnow()))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
