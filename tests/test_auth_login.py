"""session-authentication 規格：登入、失敗一致、鎖定、帳號狀態（Service 層，SQLite 與 SQL Server 各跑一次）。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy.orm import Session

from app.models import User
from app.models.user import STATUS_DELETION_PENDING, STATUS_PENDING_VERIFICATION, STATUS_SUSPENDED
from app.services.auth_service import (
    ACCOUNT_DISABLED_MESSAGE, INVALID_CREDENTIALS_MESSAGE, LOCK_DURATION, MAX_FAILED_LOGINS,
)
from app.services.errors import Forbidden, Unauthenticated, ValidationError
from app.services.password_hasher import PRODUCTION_PARAMETERS, PasswordHasherService
from tests.support.auth import GOOD_PASSWORD, START, FakeClock, add_user, make_auth_service

WRONG = "definitely-the-wrong-password"


def reload(session, user: User) -> User:
    session.expire_all()
    return session.get(User, user.id)


def fail(service, email="user1@example.com", password=WRONG, times=1):
    for _ in range(times):
        with pytest.raises(Unauthenticated):
            service.login(email=email, password=password)


# ---- 成功 ---------------------------------------------------------------------

def test_a_correct_login_returns_the_member_and_records_the_time(member_session):
    clock = FakeClock()
    user = add_user(member_session, 1)
    service = make_auth_service(member_session, clock=clock)
    clock.advance(minutes=5)
    current = service.login(email="  USER1@Example.com ", password=GOOD_PASSWORD)
    assert current.id == user.id and current.email == "user1@example.com"
    assert current.token_version == 1
    assert reload(member_session, user).last_login_at == START + timedelta(minutes=5)


def test_a_pending_member_may_log_in(member_session):
    add_user(member_session, 1, status=STATUS_PENDING_VERIFICATION)
    current = make_auth_service(member_session).login(email="user1@example.com", password=GOOD_PASSWORD)
    assert current.status == STATUS_PENDING_VERIFICATION and current.email_verified is False


def test_a_successful_login_clears_the_failure_count(member_session):
    user = add_user(member_session, 1)
    service = make_auth_service(member_session)
    fail(service, times=4)
    assert reload(member_session, user).failed_login_count == 4
    service.login(email="user1@example.com", password=GOOD_PASSWORD)
    assert reload(member_session, user).failed_login_count == 0
    fail(service, times=1)
    reloaded = reload(member_session, user)
    assert reloaded.failed_login_count == 1 and reloaded.locked_until is None      # 沒有被鎖定


# ---- 失敗回應一致 -------------------------------------------------------------

def failure_of(service, **kwargs) -> Unauthenticated:
    with pytest.raises(Unauthenticated) as exc:
        service.login(**kwargs)
    return exc.value


def test_unknown_address_wrong_password_no_password_and_locked_all_fail_identically(member_session):
    add_user(member_session, 1)
    add_user(member_session, 2, password=None)                       # 純 Google 帳號
    add_user(member_session, 3)
    service = make_auth_service(member_session)
    fail(service, email="user3@example.com", times=MAX_FAILED_LOGINS)  # 讓 user3 進入鎖定

    errors = [
        failure_of(service, email="nobody@example.com", password=WRONG),          # 信箱不存在
        failure_of(service, email="user1@example.com", password=WRONG),          # 密碼錯誤
        failure_of(service, email="user2@example.com", password=WRONG),          # 沒有密碼
        failure_of(service, email="user3@example.com", password=GOOD_PASSWORD),  # 鎖定中，即使密碼正確
        failure_of(service, email="not an address", password=WRONG),             # 格式根本不是信箱
    ]
    assert {type(e) for e in errors} == {Unauthenticated}
    assert {e.code for e in errors} == {"invalid_credentials"}
    assert {e.message for e in errors} == {INVALID_CREDENTIALS_MESSAGE}


def test_missing_credentials_are_a_validation_error_not_an_authentication_failure(member_session):
    service = make_auth_service(member_session)
    for kwargs in ({"email": "", "password": WRONG}, {"email": "a@example.com", "password": ""}, {"email": None, "password": None}):
        with pytest.raises(ValidationError):
            service.login(**kwargs)


def test_an_overlong_password_fails_like_any_other_wrong_password(member_session):
    user = add_user(member_session, 1)
    service = make_auth_service(member_session)
    assert failure_of(service, email="user1@example.com", password="x" * 5000).code == "invalid_credentials"
    assert reload(member_session, user).failed_login_count == 1


# ---- 鎖定 ---------------------------------------------------------------------

def test_the_fifth_consecutive_failure_locks_the_account_for_fifteen_minutes(member_session):
    clock = FakeClock()
    user = add_user(member_session, 1)
    service = make_auth_service(member_session, clock=clock)
    fail(service, times=MAX_FAILED_LOGINS - 1)
    assert reload(member_session, user).locked_until is None
    fail(service)
    locked = reload(member_session, user)
    assert locked.failed_login_count == 5
    assert locked.locked_until == START + timedelta(minutes=15) == START + LOCK_DURATION


def test_a_correct_password_is_refused_while_locked_and_the_lock_is_not_extended(member_session):
    clock = FakeClock()
    user = add_user(member_session, 1)
    service = make_auth_service(member_session, clock=clock)
    fail(service, times=5)
    locked_until = reload(member_session, user).locked_until
    clock.advance(minutes=5)
    fail(service, password=GOOD_PASSWORD)             # 鎖定中，即使密碼正確
    fail(service, password=WRONG)                     # 鎖定中的錯誤嘗試
    again = reload(member_session, user)
    assert again.locked_until == locked_until          # 沒有延長
    assert again.failed_login_count == 5               # 也沒有再遞增


def test_after_the_lock_expires_the_right_password_works_and_resets_the_count(member_session):
    clock = FakeClock()
    user = add_user(member_session, 1)
    service = make_auth_service(member_session, clock=clock)
    fail(service, times=5)
    clock.advance(minutes=15, seconds=1)
    service.login(email="user1@example.com", password=GOOD_PASSWORD)
    after = reload(member_session, user)
    assert after.failed_login_count == 0 and after.locked_until is None


def test_after_the_lock_expires_a_wrong_password_starts_a_fresh_count(member_session):
    clock = FakeClock()
    user = add_user(member_session, 1)
    service = make_auth_service(member_session, clock=clock)
    fail(service, times=5)
    clock.advance(minutes=16)
    fail(service)
    fresh = reload(member_session, user)
    assert fresh.failed_login_count == 1 and fresh.locked_until is None


def test_a_google_only_account_also_counts_password_attempts(member_session):
    user = add_user(member_session, 1, password=None)
    fail(make_auth_service(member_session), times=5)
    assert reload(member_session, user).locked_until is not None


# ---- 帳號狀態 -----------------------------------------------------------------

@pytest.mark.parametrize("status", [STATUS_SUSPENDED, STATUS_DELETION_PENDING])
def test_a_disabled_account_with_the_right_password_gets_a_403_style_refusal(member_session, status):
    add_user(member_session, 1, status=status)
    with pytest.raises(Forbidden) as exc:
        make_auth_service(member_session).login(email="user1@example.com", password=GOOD_PASSWORD)
    assert exc.value.code == "account_disabled" and exc.value.message == ACCOUNT_DISABLED_MESSAGE


def test_a_disabled_account_with_a_wrong_password_looks_like_any_other_failure(member_session):
    add_user(member_session, 1, status=STATUS_SUSPENDED)
    error = failure_of(make_auth_service(member_session), email="user1@example.com", password=WRONG)
    assert error.code == "invalid_credentials"          # 沒有正確密碼的人看不到「已停權」


# ---- 密碼雜湊升級 -------------------------------------------------------------

def test_a_hash_with_old_parameters_is_upgraded_after_a_successful_login(member_session):
    strong = PasswordHasherService(PRODUCTION_PARAMETERS)
    user = add_user(member_session, 1)                   # 以測試用的弱參數雜湊
    old_hash = user.password_hash
    assert strong.needs_rehash(old_hash)
    make_auth_service(member_session, hasher=strong).login(email="user1@example.com", password=GOOD_PASSWORD)
    upgraded = reload(member_session, user).password_hash
    assert upgraded != old_hash and not strong.needs_rehash(upgraded)
    assert strong.verify(upgraded, GOOD_PASSWORD)


def test_a_failed_login_does_not_touch_the_stored_hash(member_session):
    user = add_user(member_session, 1)
    before = user.password_hash
    fail(make_auth_service(member_session, hasher=PasswordHasherService(PRODUCTION_PARAMETERS)))
    assert reload(member_session, user).password_hash == before


# ---- 日誌 ---------------------------------------------------------------------

def test_failures_and_the_lock_are_logged_by_reason_without_personal_data(member_session, log_lines):
    user = add_user(member_session, 1)
    add_user(member_session, 2, password=None)
    service = make_auth_service(member_session)
    fail(service, email="ghost@example.com")
    fail(service, email="user2@example.com")
    fail(service, times=5)
    events = [(line["event"], line.get("user_id")) for line in log_lines() if "event" in line]
    assert events[0] == ("auth.login_failed.unknown_user", None)
    assert events[1][0] == "auth.login_failed.no_password"
    assert events[2:7] == [("auth.login_failed.bad_password", user.id)] * 5
    assert ("auth.locked", user.id) in events
    raw = log_lines.raw()
    assert "ghost@example.com" not in raw and "user1@example.com" not in raw and WRONG not in raw


def test_a_login_during_the_lock_is_logged_as_locked(member_session, log_lines):
    add_user(member_session, 1)
    service = make_auth_service(member_session)
    fail(service, times=5)
    fail(service, password=GOOD_PASSWORD)
    assert [line["event"] for line in log_lines() if "event" in line][-1] == "auth.login_failed.locked"


# ---- 並行 ---------------------------------------------------------------------

def test_two_simultaneous_wrong_passwords_count_two_failures(concurrent_engine):
    with Session(concurrent_engine) as s:
        user = add_user(s, 1)
        user_id = user.id
    barrier = Barrier(2)

    def attempt(_):
        with Session(concurrent_engine) as s:
            service = make_auth_service(s)
            barrier.wait(timeout=30)
            try:
                service.login(email="user1@example.com", password=WRONG)
            except Unauthenticated:
                pass
            s.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(attempt, i) for i in range(2)]:
            future.result(timeout=60)
    with Session(concurrent_engine) as s:
        assert s.get(User, user_id).failed_login_count == 2
