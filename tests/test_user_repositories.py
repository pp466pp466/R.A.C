"""會員相關 Repository 的測試：查詢、重複處理、原子操作。

一般測試以 `db_engine` 同時在 SQLite 與 SQL Server（標記 sqlserver）上執行；並行測試另用 `concurrent_engine`
（檔案型 SQLite 與 SQL Server），以真實的執行緒與獨立連線證明原子性，而不是靠循序呼叫推論。
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy.orm import Session

from app.models import Base, User
from app.models.base import utcnow
from app.models.user import (
    PROVIDER_GOOGLE, STATUS_ACTIVE, TOKEN_RESET_PASSWORD, TOKEN_VERIFY_EMAIL, UserIdentity,
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
