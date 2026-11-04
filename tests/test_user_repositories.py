"""會員相關 Repository 的測試：查詢、重複處理、原子操作。

一般測試以 `db_engine` 同時在 SQLite 與 SQL Server（標記 sqlserver）上執行；並行測試另用 `concurrent_engine`
（檔案型 SQLite 與 SQL Server），以真實的執行緒與獨立連線證明原子性，而不是靠循序呼叫推論。
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from threading import Barrier

import pytest
from sqlalchemy.orm import Session

from app.models import Base, User
from app.models.base import utcnow
from app.models.user import (
    PROVIDER_GOOGLE, STATUS_ACTIVE, TOKEN_RESET_PASSWORD, UserIdentity,
)
from app.repositories.auth_token_repository import AuthTokenRepository
from app.repositories.errors import DuplicateIdentityError, DuplicateUserError
from app.repositories.user_identity_repository import UserIdentityRepository
from app.repositories.user_repository import UserRepository


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


@pytest.fixture()
def users(session):
    return UserRepository(session)


@pytest.fixture()
def tokens(session):
    return AuthTokenRepository(session)


# =============================================================================
# UserRepository
# =============================================================================

def test_add_and_look_up_a_user_by_every_key(session, users):
    user = users.add_user(make_user(1))
    session.commit()
    assert users.get_by_id(user.id).email == "user1@example.com"
    assert users.get_by_email("user1@example.com").id == user.id
    assert users.get_by_canonical_email("user1@example.com").id == user.id
    assert users.get_by_public_id(user.public_id).id == user.id
    assert users.get_by_email("nobody@example.com") is None
    assert users.get_by_id(999999) is None


@pytest.mark.parametrize("field", ["email", "email_canonical"])
def test_duplicate_user_raises_a_domain_error_and_the_session_stays_usable(session, users, field):
    users.add_user(make_user(1))
    session.commit()
    with pytest.raises(DuplicateUserError):
        users.add_user(make_user(2, **{field: "user1@example.com"}))
    # 失敗只回復這一筆（SAVEPOINT），之後仍可在同一個交易繼續寫入並提交
    users.add_user(make_user(3))
    session.commit()
    assert users.get_by_email("user3@example.com") is not None
    assert users.get_by_email("user2@example.com") is None


def test_a_failed_add_does_not_roll_back_earlier_work_in_the_same_transaction(session, users):
    first = users.add_user(make_user(1))
    with pytest.raises(DuplicateUserError):
        users.add_user(make_user(2, email="user1@example.com"))
    session.commit()
    assert users.get_by_id(first.id) is not None


def test_increment_failed_login_is_atomic_even_when_the_loaded_object_is_stale(session, db_engine, users):
    user = users.add_user(make_user())
    session.commit()
    with Session(db_engine) as other:
        stale = other.get(User, user.id)            # 讀到計數 0 的舊物件
        assert stale.failed_login_count == 0
        assert users.increment_failed_login(user) == 1
        session.commit()
        assert UserRepository(other).increment_failed_login(stale) == 2   # 以資料庫為準，不是 0 + 1
        other.commit()
    assert users.increment_failed_login(user) == 3


def test_increment_keeps_unflushed_changes_made_before_it(session, db_engine, users):
    user = users.add_user(make_user())
    session.commit()
    user.display_name = "改名了"
    users.increment_failed_login(user)
    session.commit()
    with Session(db_engine) as other:
        assert other.get(User, user.id).display_name == "改名了"


def test_lock_account_requires_the_threshold_and_does_not_extend_an_active_lock(session, users):
    user = users.add_user(make_user())
    now = utcnow()
    for _ in range(4):
        users.increment_failed_login(user)
    assert users.lock_account(user, locked_until=now + timedelta(minutes=15), now=now, min_failures=5) is False
    assert users.increment_failed_login(user) == 5
    assert users.lock_account(user, locked_until=now + timedelta(minutes=15), now=now, min_failures=5) is True
    assert user.locked_until == (now + timedelta(minutes=15)).replace(microsecond=user.locked_until.microsecond)
    first_lock = user.locked_until
    # 鎖定中再次要求鎖定：不延長
    later = now + timedelta(minutes=5)
    assert users.lock_account(user, locked_until=later + timedelta(minutes=15), now=later, min_failures=5) is False
    assert user.locked_until == first_lock
    # 鎖定到期之後可以重新鎖定
    after = now + timedelta(minutes=16)
    assert users.lock_account(user, locked_until=after + timedelta(minutes=15), now=after, min_failures=5) is True


def test_release_expired_lock_only_acts_after_expiry_and_restarts_the_count(session, users):
    user = users.add_user(make_user())
    now = utcnow()
    for _ in range(5):
        users.increment_failed_login(user)
    users.lock_account(user, locked_until=now + timedelta(minutes=15), now=now, min_failures=5)
    assert users.release_expired_lock(user, now=now + timedelta(minutes=14)) is False
    assert user.failed_login_count == 5
    assert users.release_expired_lock(user, now=now + timedelta(minutes=15)) is True    # 剛好到期的那一刻
    assert user.failed_login_count == 0 and user.locked_until is None
    assert users.release_expired_lock(user, now=now + timedelta(minutes=30)) is False    # 沒有鎖定可解


def test_reset_login_failures_clears_count_and_lock(session, users):
    user = users.add_user(make_user())
    now = utcnow()
    for _ in range(5):
        users.increment_failed_login(user)
    users.lock_account(user, locked_until=now + timedelta(minutes=15), now=now, min_failures=5)
    users.reset_login_failures(user)
    assert user.failed_login_count == 0 and user.locked_until is None


def test_bump_token_version_returns_the_new_value_each_time(session, users):
    user = users.add_user(make_user())
    assert user.token_version == 1
    assert users.bump_token_version(user) == 2
    assert users.bump_token_version(user) == 3
    assert user.token_version == 3


def age_args(**overrides):
    values = dict(
        birth_date=date(1996, 1, 5), age_range="25_34", is_adult=True, confirmed_at=utcnow(), policy_version="v1",
    )
    values.update(overrides)
    return values


def test_completing_the_age_verification_writes_every_field_once_and_never_overwrites_it(session, users):
    user = users.add_user(make_user(1))
    other = users.add_user(make_user(2))
    first = utcnow()

    assert users.complete_age_verification_if_unset(user, **age_args(confirmed_at=first)) is True
    assert (user.birth_date, user.age_range, user.is_adult_at_confirmation) == (date(1996, 1, 5), "25_34", True)
    assert user.age_confirmed_at == first and user.age_policy_version == "v1"

    again = age_args(birth_date=date(2011, 10, 28), age_range="13_17", is_adult=False,
                     confirmed_at=first + timedelta(hours=1), policy_version="v2")
    assert users.complete_age_verification_if_unset(user, **again) is False
    assert (user.birth_date, user.age_range, user.is_adult_at_confirmation) == (date(1996, 1, 5), "25_34", True)
    assert user.age_confirmed_at == first and user.age_policy_version == "v1"          # 原本的紀錄一個欄位都不變
    assert other.birth_date is None and other.age_range is None and other.age_confirmed_at is None    # 不影響別人


def test_an_account_with_only_the_old_checkbox_time_still_counts_as_not_completed(session, users):
    """勾選版時代的帳號只有 age_confirmed_at：完成與否只看生日，所以補填可以寫入，並覆蓋成新的完整紀錄。"""
    old_time = utcnow() - timedelta(days=30)
    user = users.add_user(make_user(1, age_confirmed_at=old_time, age_policy_version="2026-10-28-self-declared"))
    assert users.complete_age_verification_if_unset(user, **age_args(policy_version="v2")) is True
    assert user.birth_date == date(1996, 1, 5) and user.age_policy_version == "v2" and user.age_confirmed_at != old_time


# ---- 內容模式偏好與成年確認（content-tier-modes 規格）------------------------------------

MINOR_ARGS = dict(birth_date=date(2011, 10, 28), age_range="13_17", is_adult=False)


def test_the_preference_defaults_to_safe_until_the_age_is_confirmed_and_then_follows_the_age_at_confirmation(session, users):
    adult = users.add_user(make_user(1))
    minor = users.add_user(make_user(2))
    assert (adult.content_tier_preference, minor.content_tier_preference) == ("safe", "safe")
    assert users.complete_age_verification_if_unset(adult, **age_args()) is True
    assert users.complete_age_verification_if_unset(minor, **age_args(**MINOR_ARGS)) is True
    assert (adult.content_tier_preference, minor.content_tier_preference) == ("standard", "safe")


def test_a_repeated_age_confirmation_neither_overwrites_nor_changes_the_preference(session, users):
    user = users.add_user(make_user(1))
    users.complete_age_verification_if_unset(user, **age_args())
    assert users.set_content_preference(user, "safe") is True               # 成年玩家自行改成安全模式
    assert users.complete_age_verification_if_unset(user, **age_args()) is False
    assert user.content_tier_preference == "safe"


def test_setting_the_preference_writes_only_that_column_for_that_user(session, users):
    user = users.add_user(make_user(1))
    other = users.add_user(make_user(2))
    for member in (user, other):
        users.complete_age_verification_if_unset(member, **age_args())
    assert users.set_content_preference(user, "adult") is True
    assert (user.content_tier_preference, other.content_tier_preference) == ("adult", "standard")
    assert user.adult_confirmed_at is None and user.birth_date == date(1996, 1, 5)


def test_an_account_without_a_completed_age_check_cannot_get_any_preference_or_confirmation(session, users):
    user = users.add_user(make_user(1))
    assert users.set_content_preference(user, "standard") is False
    assert users.record_adult_confirmation(user, confirmed_at=utcnow(), rules_version="r1", method="self_declared") is False
    assert user.content_tier_preference == "safe"
    assert (user.adult_confirmed_at, user.adult_rules_version, user.adult_confirm_method) == (None, None, None)


@pytest.mark.parametrize("tier", ["mature", "", None, "SAFE", 1])
def test_an_unknown_tier_is_a_programming_error_and_writes_nothing(session, users, tier):
    user = users.add_user(make_user(1))
    users.complete_age_verification_if_unset(user, **age_args())
    with pytest.raises(ValueError):
        users.set_content_preference(user, tier)
    assert user.content_tier_preference == "standard"


def test_the_adult_confirmation_is_recorded_replaced_and_kept_when_the_preference_changes(session, users):
    user = users.add_user(make_user(1))
    users.complete_age_verification_if_unset(user, **age_args())
    first, second = utcnow(), utcnow() + timedelta(days=30)
    assert users.record_adult_confirmation(user, confirmed_at=first, rules_version="r1", method="self_declared") is True
    users.set_content_preference(user, "adult")
    users.set_content_preference(user, "standard")                      # 離開成人模式不清除確認紀錄
    assert (user.adult_confirmed_at, user.adult_rules_version, user.adult_confirm_method) == (first, "r1", "self_declared")
    users.record_adult_confirmation(user, confirmed_at=second, rules_version="r2", method="self_declared")
    assert (user.adult_confirmed_at, user.adult_rules_version) == (second, "r2")      # 只保存目前這一次


def test_the_age_statistics_count_per_range_and_expose_nothing_individual(session, users):
    for n, (birth, chosen) in enumerate(
        [(date(2011, 1, 1), "13_17"), (date(2010, 2, 2), "13_17"), (date(1996, 1, 5), "25_34"), (date(1990, 3, 3), "25_34"),
         (date(1991, 4, 4), "25_34"), (None, None)], start=1
    ):
        user = users.add_user(make_user(n))
        if birth:
            users.complete_age_verification_if_unset(user, **age_args(birth_date=birth, age_range=chosen, is_adult=chosen != "13_17"))
    stats = users.count_by_age_range()
    assert stats == {"13_17": 2, "25_34": 3, None: 1}
    assert all(isinstance(key, (str, type(None))) and isinstance(value, int) for key, value in stats.items())
    flat = repr(stats)
    for secret in ("1996", "2011", "user1@example.com", "使用者"):
        assert secret not in flat


def test_the_age_statistics_of_an_empty_table_are_empty(session, users):
    assert users.count_by_age_range() == {}


# ---- 閒置帳號 -----------------------------------------------------------------

CUTOFF = utcnow()


def seen(days_before_cutoff: int | None, **overrides):
    """最後登入在 cutoff 之前 N 天（負數 = 之後）；None 代表從未登入。建立時間預設也是很久以前。"""
    last_login = None if days_before_cutoff is None else CUTOFF - timedelta(days=days_before_cutoff)
    return dict(last_login_at=last_login, created_at=CUTOFF - timedelta(days=800), **overrides)


def test_only_accounts_last_seen_strictly_before_the_cutoff_are_inactive(session, users):
    old = users.add_user(make_user(1, **seen(1)))
    users.add_user(make_user(2, **seen(0)))                          # 剛好等於 cutoff：還沒超過，不算
    users.add_user(make_user(3, **seen(-1)))                         # 在 cutoff 之後登入
    assert users.find_inactive_ids(CUTOFF) == [old.id]
    assert users.count_inactive(CUTOFF) == 1


def test_an_account_that_never_logged_in_is_judged_by_its_creation_time(session, users):
    stale = users.add_user(make_user(1, last_login_at=None, created_at=CUTOFF - timedelta(days=2)))
    users.add_user(make_user(2, last_login_at=None, created_at=CUTOFF + timedelta(days=2)))
    assert users.find_inactive_ids(CUTOFF) == [stale.id]


def test_a_recent_login_outweighs_an_old_creation_time(session, users):
    users.add_user(make_user(1, last_login_at=CUTOFF + timedelta(days=1), created_at=CUTOFF - timedelta(days=900)))
    assert users.find_inactive_ids(CUTOFF) == []


def test_admins_are_never_inactive(session, users):
    users.add_user(make_user(1, role="admin", **seen(900)))
    regular = users.add_user(make_user(2, **seen(900)))
    assert users.find_inactive_ids(CUTOFF) == [regular.id] and users.count_inactive(CUTOFF) == 1


def test_every_status_is_covered_not_only_active_accounts(session, users):
    ids = [users.add_user(make_user(n, status=status, **seen(10))).id for n, status in enumerate(
        ("active", "suspended", "deletion_pending"), start=1)]
    assert users.find_inactive_ids(CUTOFF) == ids


def test_find_returns_ids_in_order_up_to_the_limit_and_nothing_else(session, users):
    ids = [users.add_user(make_user(n, **seen(10))).id for n in range(1, 6)]
    assert users.find_inactive_ids(CUTOFF, limit=3) == ids[:3]
    assert all(type(value) is int for value in users.find_inactive_ids(CUTOFF))           # 只有編號，沒有載入任何個資


def test_deleting_an_inactive_account_removes_its_identities_and_tokens_but_nobody_elses(session, users):
    from app.models.user import AuthToken

    doomed, keeper = users.add_user(make_user(1, **seen(10))), users.add_user(make_user(2, **seen(-5)))
    for owner in (doomed, keeper):
        session.add(UserIdentity(user_id=owner.id, provider=PROVIDER_GOOGLE, provider_subject=f"s{owner.id}"))
        session.add(AuthToken(user_id=owner.id, purpose=TOKEN_VERIFY_EMAIL, token_hash=str(owner.id) * 64, expires_at=utcnow()))
    session.flush()
    doomed_id = doomed.id

    assert users.delete_inactive(doomed_id, CUTOFF) is True
    session.expire_all()

    assert session.get(User, doomed_id) is None and session.get(User, keeper.id) is not None
    assert [i.user_id for i in session.query(UserIdentity)] == [keeper.id]
    assert [tk.user_id for tk in session.query(AuthToken)] == [keeper.id]


def test_an_account_that_logged_in_after_it_was_listed_is_not_deleted(session, users):
    """列出之後、刪除之前剛好登入：刪除時再確認一次條件，所以不會被刪，也沒有任何副作用。"""
    user = users.add_user(make_user(1, **seen(10)))
    (listed,) = users.find_inactive_ids(CUTOFF)
    user.last_login_at = CUTOFF + timedelta(hours=1)
    session.flush()

    assert users.delete_inactive(listed, CUTOFF) is False
    session.expire_all()
    assert session.get(User, listed) is not None


def test_deleting_an_admin_or_a_missing_id_is_refused(session, users):
    admin = users.add_user(make_user(1, role="admin", **seen(900)))
    assert users.delete_inactive(admin.id, CUTOFF) is False
    assert users.delete_inactive(999999, CUTOFF) is False
    session.expire_all()
    assert session.get(User, admin.id) is not None


def test_a_second_delete_of_the_same_account_reports_false(session, users):
    user = users.add_user(make_user(1, **seen(10)))
    user_id = user.id
    assert users.delete_inactive(user_id, CUTOFF) is True
    assert users.delete_inactive(user_id, CUTOFF) is False


def test_the_tombstone_repository_stores_only_an_id_a_reason_and_a_time(session):
    from app.repositories.deletion_tombstone_repository import DeletionTombstoneRepository

    tombstones = DeletionTombstoneRepository(session)
    row = tombstones.record(user_id=42, reason="inactive_12_months", deleted_at=CUTOFF)
    assert (row.user_id, row.reason, row.deleted_at) == (42, "inactive_12_months", CUTOFF)
    assert tombstones.deleted_user_ids() == {42}
    assert {column.name for column in row.__table__.columns} == {"id", "user_id", "deleted_at", "reason"}


# =============================================================================
# UserIdentityRepository
# =============================================================================

def test_identity_lookup_and_duplicates(session, users):
    identities = UserIdentityRepository(session)
    a, b = users.add_user(make_user(1)), users.add_user(make_user(2))
    identities.add_identity(UserIdentity(user_id=a.id, provider=PROVIDER_GOOGLE, provider_subject="sub-1"))
    session.commit()

    assert identities.get_by_subject(PROVIDER_GOOGLE, "sub-1").user_id == a.id
    assert identities.get_by_subject(PROVIDER_GOOGLE, "sub-x") is None
    assert identities.get_for_user(a.id, PROVIDER_GOOGLE).provider_subject == "sub-1"
    assert identities.get_for_user(b.id, PROVIDER_GOOGLE) is None

    with pytest.raises(DuplicateIdentityError):        # 同一個 Google 身分綁給另一個帳號
        identities.add_identity(UserIdentity(user_id=b.id, provider=PROVIDER_GOOGLE, provider_subject="sub-1"))
    with pytest.raises(DuplicateIdentityError):        # 同一個帳號綁第二個 Google 身分
        identities.add_identity(UserIdentity(user_id=a.id, provider=PROVIDER_GOOGLE, provider_subject="sub-2"))
    identities.add_identity(UserIdentity(user_id=b.id, provider=PROVIDER_GOOGLE, provider_subject="sub-3"))
    session.commit()
    assert identities.get_by_subject(PROVIDER_GOOGLE, "sub-3").user_id == b.id


# =============================================================================
# AuthTokenRepository
# =============================================================================

# Repository 不認得用途的含義，只比對字串；這裡用一個測試專用的用途，與 TOKEN_RESET_PASSWORD 互為「另一個用途」
TOKEN_VERIFY_EMAIL = "test_purpose"


def issue(tokens, user, token_hash="h" * 64, *, purpose=TOKEN_VERIFY_EMAIL, created=None, lifetime=timedelta(hours=1)):
    created = created or utcnow()
    return tokens.add_token(
        user_id=user.id, purpose=purpose, token_hash=token_hash, created_at=created, expires_at=created + lifetime
    )


def test_consume_succeeds_once_and_returns_the_owner(session, users, tokens):
    user = users.add_user(make_user())
    issue(tokens, user)
    now = utcnow()
    assert tokens.consume(token_hash="h" * 64, purpose=TOKEN_VERIFY_EMAIL, now=now) == user.id
    assert tokens.consume(token_hash="h" * 64, purpose=TOKEN_VERIFY_EMAIL, now=now) is None


def test_consume_rejects_wrong_purpose_unknown_hash_and_expired_tokens(session, users, tokens):
    user = users.add_user(make_user())
    created = utcnow()
    issue(tokens, user, created=created, lifetime=timedelta(hours=1))
    assert tokens.consume(token_hash="h" * 64, purpose=TOKEN_RESET_PASSWORD, now=created) is None
    assert tokens.consume(token_hash="x" * 64, purpose=TOKEN_VERIFY_EMAIL, now=created) is None
    assert tokens.consume(token_hash="h" * 64, purpose=TOKEN_VERIFY_EMAIL, now=created + timedelta(hours=1)) is None  # 到期當下
    assert tokens.consume(token_hash="h" * 64, purpose=TOKEN_VERIFY_EMAIL, now=created + timedelta(minutes=59)) == user.id


def test_a_rejected_wrong_purpose_attempt_does_not_burn_the_token(session, users, tokens):
    user = users.add_user(make_user())
    issue(tokens, user)
    assert tokens.consume(token_hash="h" * 64, purpose=TOKEN_RESET_PASSWORD, now=utcnow()) is None
    assert tokens.consume(token_hash="h" * 64, purpose=TOKEN_VERIFY_EMAIL, now=utcnow()) == user.id


def test_invalidate_unused_only_touches_the_same_user_and_purpose(session, users, tokens):
    a, b = users.add_user(make_user(1)), users.add_user(make_user(2))
    now = utcnow()
    issue(tokens, a, "1" * 64)
    issue(tokens, a, "2" * 64)
    issue(tokens, a, "3" * 64, purpose=TOKEN_RESET_PASSWORD)
    issue(tokens, b, "4" * 64)
    assert tokens.invalidate_unused(user_id=a.id, purpose=TOKEN_VERIFY_EMAIL, now=now) == 2
    assert tokens.consume(token_hash="1" * 64, purpose=TOKEN_VERIFY_EMAIL, now=now) is None
    assert tokens.consume(token_hash="2" * 64, purpose=TOKEN_VERIFY_EMAIL, now=now) is None
    assert tokens.consume(token_hash="3" * 64, purpose=TOKEN_RESET_PASSWORD, now=now) == a.id
    assert tokens.consume(token_hash="4" * 64, purpose=TOKEN_VERIFY_EMAIL, now=now) == b.id
    assert tokens.invalidate_unused(user_id=a.id, purpose=TOKEN_VERIFY_EMAIL, now=now) == 0


def test_latest_created_at_and_counts_support_the_send_frequency_limits(session, users, tokens):
    user, other = users.add_user(make_user(1)), users.add_user(make_user(2))
    base = utcnow()
    assert tokens.latest_created_at(user_id=user.id, purpose=TOKEN_VERIFY_EMAIL) is None
    for i, minutes_ago in enumerate((90, 30, 10)):
        issue(tokens, user, str(i) * 64, created=base - timedelta(minutes=minutes_ago))
    issue(tokens, user, "9" * 64, purpose=TOKEN_RESET_PASSWORD, created=base)
    issue(tokens, other, "8" * 64, created=base)

    latest = tokens.latest_created_at(user_id=user.id, purpose=TOKEN_VERIFY_EMAIL)
    assert latest.tzinfo is not None
    assert abs((latest - (base - timedelta(minutes=10))).total_seconds()) < 1
    assert tokens.count_created_since(user_id=user.id, purpose=TOKEN_VERIFY_EMAIL, since=base - timedelta(hours=1)) == 2
    assert tokens.count_created_since(user_id=user.id, purpose=TOKEN_VERIFY_EMAIL, since=base - timedelta(hours=2)) == 3
    assert tokens.count_created_since(user_id=user.id, purpose=TOKEN_RESET_PASSWORD, since=base - timedelta(hours=1)) == 1


def test_superseded_tokens_still_count_toward_the_send_limits(session, users, tokens):
    user = users.add_user(make_user())
    now = utcnow()
    issue(tokens, user, "1" * 64, created=now)
    tokens.invalidate_unused(user_id=user.id, purpose=TOKEN_VERIFY_EMAIL, now=now)
    issue(tokens, user, "2" * 64, created=now)
    assert tokens.count_created_since(user_id=user.id, purpose=TOKEN_VERIFY_EMAIL, since=now - timedelta(minutes=1)) == 2


# =============================================================================
# 並行：真實的執行緒與獨立連線
# =============================================================================

def run_in_threads(engine, worker, count: int = 8):
    """讓 count 個執行緒在同一個時間點開始，各自使用獨立的 Session（獨立連線）。"""
    barrier = Barrier(count)

    def task(index: int):
        with Session(engine) as s:
            barrier.wait(timeout=30)
            result = worker(s, index)
            s.commit()
            return result

    with ThreadPoolExecutor(max_workers=count) as pool:
        return [f.result(timeout=60) for f in [pool.submit(task, i) for i in range(count)]]


def test_parallel_failed_logins_are_all_counted(concurrent_engine):
    with Session(concurrent_engine) as s:
        user = UserRepository(s).add_user(make_user())
        s.commit()
        user_id = user.id

    def worker(s: Session, _index: int) -> int:
        stale = s.get(User, user_id)               # 每個執行緒都先讀到計數 0
        return UserRepository(s).increment_failed_login(stale)

    results = run_in_threads(concurrent_engine, worker, count=8)
    with Session(concurrent_engine) as s:
        assert s.get(User, user_id).failed_login_count == 8
    assert len(results) == 8


def test_parallel_use_of_one_token_succeeds_exactly_once(concurrent_engine):
    with Session(concurrent_engine) as s:
        user = UserRepository(s).add_user(make_user())
        AuthTokenRepository(s).add_token(
            user_id=user.id, purpose=TOKEN_RESET_PASSWORD, token_hash="t" * 64,
            created_at=utcnow(), expires_at=utcnow() + timedelta(hours=1),
        )
        s.commit()
        user_id = user.id

    def worker(s: Session, _index: int):
        return AuthTokenRepository(s).consume(token_hash="t" * 64, purpose=TOKEN_RESET_PASSWORD, now=utcnow())

    results = run_in_threads(concurrent_engine, worker, count=8)
    assert results.count(user_id) == 1
    assert results.count(None) == 7


def test_parallel_registration_of_the_same_email_creates_one_user(concurrent_engine):
    def worker(s: Session, index: int) -> str:
        try:
            UserRepository(s).add_user(make_user(index + 1, email="same@example.com", email_canonical="same@example.com"))
        except DuplicateUserError:
            return "duplicate"
        return "created"

    results = run_in_threads(concurrent_engine, worker, count=6)
    assert results.count("created") == 1
    assert results.count("duplicate") == 5


def test_parallel_token_version_bumps_never_lose_an_update(concurrent_engine):
    with Session(concurrent_engine) as s:
        user = UserRepository(s).add_user(make_user())
        s.commit()
        user_id = user.id

    def worker(s: Session, _index: int) -> int:
        return UserRepository(s).bump_token_version(s.get(User, user_id))

    run_in_threads(concurrent_engine, worker, count=8)
    with Session(concurrent_engine) as s:
        assert s.get(User, user_id).token_version == 9


def test_parallel_age_verifications_write_exactly_once(concurrent_engine):
    with Session(concurrent_engine) as s:
        user = UserRepository(s).add_user(make_user())
        s.commit()
        user_id = user.id

    def worker(s: Session, index: int) -> bool:
        stale = s.get(User, user_id)                 # 每個執行緒都先讀到「尚未完成」
        return UserRepository(s).complete_age_verification_if_unset(
            stale, **age_args(age_range="25_34", policy_version=f"v{index}")
        )

    results = run_in_threads(concurrent_engine, worker, count=8)
    assert results.count(True) == 1 and results.count(False) == 7
    with Session(concurrent_engine) as s:
        stored = s.get(User, user_id)
        assert stored.birth_date == date(1996, 1, 5) and stored.age_range == "25_34" and stored.age_confirmed_at is not None
        assert stored.age_policy_version in {f"v{i}" for i in range(8)}


def test_parallel_deletions_of_the_same_inactive_account_delete_it_exactly_once(concurrent_engine):
    with Session(concurrent_engine) as s:
        user = UserRepository(s).add_user(make_user(1, **seen(10)))
        s.commit()
        user_id = user.id

    def worker(s: Session, _index: int) -> bool:
        result = UserRepository(s).delete_inactive(user_id, CUTOFF)
        s.commit()
        return result

    results = run_in_threads(concurrent_engine, worker, count=8)
    assert results.count(True) == 1 and results.count(False) == 7
    with Session(concurrent_engine) as s:
        assert s.get(User, user_id) is None


def test_a_login_racing_with_the_purge_either_keeps_the_account_or_the_account_is_gone_never_a_half_state(concurrent_engine):
    """兩個連線同時：一個刪除、一個把最後登入改成現在。結果只能是「帳號還在且已登入」或「帳號已刪除」。"""
    with Session(concurrent_engine) as s:
        user = UserRepository(s).add_user(make_user(1, **seen(10)))
        s.commit()
        user_id = user.id

    def worker(s: Session, index: int):
        if index == 0:
            deleted = UserRepository(s).delete_inactive(user_id, CUTOFF)
            s.commit()
            return ("delete", deleted)
        stored = s.get(User, user_id)
        if stored is not None:
            stored.last_login_at = CUTOFF + timedelta(hours=1)
        try:
            s.commit()
        except Exception:                                   # 帳號已被刪除：更新不到列，視為競爭中輸掉
            s.rollback()
        return ("login", None)

    run_in_threads(concurrent_engine, worker, count=2)
    with Session(concurrent_engine) as s:
        stored = s.get(User, user_id)
        assert stored is None or stored.last_login_at >= CUTOFF
