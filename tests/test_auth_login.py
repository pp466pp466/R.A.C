"""session-authentication 規格：管理員的密碼驗證步驟、失敗一致、鎖定、帳號狀態（Service 層，SQLite 與 SQL Server 各跑一次）。

玩家只用 Google 登入，沒有密碼登入；密碼只屬於管理員，由 `verify_admin_password` 在 Google 通過之後驗證。
HTTP 層的兩階段流程見 test_admin_two_step_login.py。
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy.orm import Session

from app.models import User
from app.models.user import STATUS_SUSPENDED
from app.services.auth_service import INVALID_CREDENTIALS_MESSAGE, LOCK_DURATION, MAX_FAILED_LOGINS
from app.services.errors import Unauthenticated, ValidationError
from app.services.password_hasher import PRODUCTION_PARAMETERS, PasswordHasherService
from tests.support.auth import GOOD_PASSWORD, START, FakeClock, add_user, make_auth_service

WRONG = "definitely-the-wrong-password"


def reload(session, user: User) -> User:
    session.expire_all()
    return session.get(User, user.id)


def admin(session, n: int = 1, **overrides) -> User:
    return add_user(session, n, role="admin", **overrides)


def verify(service, user: User, password=GOOD_PASSWORD, *, token_version: int | None = None):
    """管理員通過 Google 之後的密碼驗證（中間狀態的 uid 與 tv 就是這兩個參數）。"""
    return service.verify_admin_password(user.id, user.token_version if token_version is None else token_version, password)


def fail(service, user: User, password=WRONG, times=1):
    for _ in range(times):
        with pytest.raises(Unauthenticated):
            verify(service, user, password)


def failure_of(service, user: User, password=WRONG, **kwargs) -> Unauthenticated:
    with pytest.raises(Unauthenticated) as exc:
        verify(service, user, password, **kwargs)
    return exc.value


# ---- 成功 ---------------------------------------------------------------------

def test_a_correct_password_returns_the_admin_and_records_the_time(member_session):
    clock = FakeClock()
    user = admin(member_session)
    service = make_auth_service(member_session, clock=clock)
    clock.advance(minutes=5)
    current = verify(service, user)
    assert current.id == user.id and current.email == "user1@example.com" and current.role == "admin"
    assert current.token_version == 1
    assert reload(member_session, user).last_login_at == START + timedelta(minutes=5)


def test_a_successful_verification_clears_the_failure_count(member_session):
    user = admin(member_session)
    service = make_auth_service(member_session)
    fail(service, user, times=4)
    assert reload(member_session, user).failed_login_count == 4
    verify(service, user)
    assert reload(member_session, user).failed_login_count == 0
    fail(service, user, times=1)
    reloaded = reload(member_session, user)
    assert reloaded.failed_login_count == 1 and reloaded.locked_until is None      # 沒有被鎖定


# ---- 失敗回應一致 -------------------------------------------------------------

def test_a_wrong_password_and_a_locked_account_fail_identically(member_session):
    wrong_user, locked_user = admin(member_session, 1), admin(member_session, 2)
    service = make_auth_service(member_session)
    fail(service, locked_user, times=MAX_FAILED_LOGINS)                  # 讓第二位進入鎖定

    errors = [
        failure_of(service, wrong_user, WRONG),                          # 密碼錯誤
        failure_of(service, locked_user, GOOD_PASSWORD),                 # 鎖定中，即使密碼正確
    ]
    assert {type(e) for e in errors} == {Unauthenticated}
    assert {e.code for e in errors} == {"invalid_credentials"}
    assert {e.message for e in errors} == {INVALID_CREDENTIALS_MESSAGE}


def test_a_middle_state_that_no_longer_matches_the_account_asks_for_a_new_google_login_without_counting(member_session):
    user = admin(member_session, 1)
    player = add_user(member_session, 2)
    suspended = admin(member_session, 3, status=STATUS_SUSPENDED)
    service = make_auth_service(member_session)
    errors = [
        failure_of(service, user, GOOD_PASSWORD, token_version=user.token_version + 1),      # 這 10 分鐘內被改了密碼
        failure_of(service, player, GOOD_PASSWORD),                                          # 不是管理員
        failure_of(service, suspended, GOOD_PASSWORD),                                       # 被停權
    ]
    with pytest.raises(Unauthenticated) as unknown:
        service.verify_admin_password(999999, 1, GOOD_PASSWORD)                              # 帳號不存在
    errors.append(unknown.value)
    assert {e.code for e in errors} == {"unauthorized"}
    assert reload(member_session, user).failed_login_count == 0 and reload(member_session, player).failed_login_count == 0


def test_a_missing_password_is_a_validation_error_not_an_authentication_failure(member_session):
    user = admin(member_session)
    service = make_auth_service(member_session)
    for password in ("", None, 5):
        with pytest.raises(ValidationError):
            verify(service, user, password)
    assert reload(member_session, user).failed_login_count == 0


def test_an_overlong_password_fails_like_any_other_wrong_password(member_session):
    user = admin(member_session)
    service = make_auth_service(member_session)
    assert failure_of(service, user, "x" * 5000).code == "invalid_credentials"
    assert reload(member_session, user).failed_login_count == 1


# ---- 鎖定 ---------------------------------------------------------------------

def test_the_fifth_consecutive_failure_locks_the_account_for_fifteen_minutes(member_session):
    clock = FakeClock()
    user = admin(member_session)
    service = make_auth_service(member_session, clock=clock)
    fail(service, user, times=MAX_FAILED_LOGINS - 1)
    assert reload(member_session, user).locked_until is None
    fail(service, user)
    locked = reload(member_session, user)
    assert locked.failed_login_count == 5
    assert locked.locked_until == START + timedelta(minutes=15) == START + LOCK_DURATION


def test_a_correct_password_is_refused_while_locked_and_the_lock_is_not_extended(member_session):
    clock = FakeClock()
    user = admin(member_session)
    service = make_auth_service(member_session, clock=clock)
    fail(service, user, times=5)
    locked_until = reload(member_session, user).locked_until
    clock.advance(minutes=5)
    fail(service, user, GOOD_PASSWORD)                # 鎖定中，即使密碼正確
    fail(service, user, WRONG)                        # 鎖定中的錯誤嘗試
    again = reload(member_session, user)
    assert again.locked_until == locked_until          # 沒有延長
    assert again.failed_login_count == 5               # 也沒有再遞增


def test_after_the_lock_expires_the_right_password_works_and_resets_the_count(member_session):
    clock = FakeClock()
    user = admin(member_session)
    service = make_auth_service(member_session, clock=clock)
    fail(service, user, times=5)
    clock.advance(minutes=15, seconds=1)
    verify(service, user)
    after = reload(member_session, user)
    assert after.failed_login_count == 0 and after.locked_until is None


def test_after_the_lock_expires_a_wrong_password_starts_a_fresh_count(member_session):
    clock = FakeClock()
    user = admin(member_session)
    service = make_auth_service(member_session, clock=clock)
    fail(service, user, times=5)
    clock.advance(minutes=16)
    fail(service, user)
    fresh = reload(member_session, user)
    assert fresh.failed_login_count == 1 and fresh.locked_until is None


def test_an_admin_without_a_password_also_counts_attempts(member_session):
    user = admin(member_session, password=None)
    fail(make_auth_service(member_session), user, times=5)
    assert reload(member_session, user).locked_until is not None


# ---- 密碼雜湊升級 -------------------------------------------------------------

def test_a_hash_with_old_parameters_is_upgraded_after_a_successful_verification(member_session):
    strong = PasswordHasherService(PRODUCTION_PARAMETERS)
    user = admin(member_session)                         # 以測試用的弱參數雜湊
    old_hash = user.password_hash
    assert strong.needs_rehash(old_hash)
    verify(make_auth_service(member_session, hasher=strong), user)
    upgraded = reload(member_session, user).password_hash
    assert upgraded != old_hash and not strong.needs_rehash(upgraded)
    assert strong.verify(upgraded, GOOD_PASSWORD)


def test_a_failed_verification_does_not_touch_the_stored_hash(member_session):
    user = admin(member_session)
    before = user.password_hash
    fail(make_auth_service(member_session, hasher=PasswordHasherService(PRODUCTION_PARAMETERS)), user)
    assert reload(member_session, user).password_hash == before


# ---- 日誌 ---------------------------------------------------------------------

def test_failures_and_the_lock_are_logged_by_reason_without_personal_data(member_session, log_lines):
    user = admin(member_session)
    no_password = admin(member_session, 2, password=None)
    service = make_auth_service(member_session)
    fail(service, no_password)
    fail(service, user, times=5)
    events = [(line["event"], line.get("user_id")) for line in log_lines() if "event" in line]
    assert events[0][0] == "auth.login_failed.no_password"
    assert events[1:6] == [("auth.login_failed.bad_password", user.id)] * 5
    assert ("auth.locked", user.id) in events
    raw = log_lines.raw()
    assert "user1@example.com" not in raw and WRONG not in raw


def test_a_verification_during_the_lock_is_logged_as_locked(member_session, log_lines):
    user = admin(member_session)
    service = make_auth_service(member_session)
    fail(service, user, times=5)
    fail(service, user, GOOD_PASSWORD)
    assert [line["event"] for line in log_lines() if "event" in line][-1] == "auth.login_failed.locked"


def test_an_expired_middle_state_is_logged_without_counting(member_session, log_lines):
    user = admin(member_session)
    failure_of(make_auth_service(member_session), user, GOOD_PASSWORD, token_version=99)
    assert [line["event"] for line in log_lines() if "event" in line][-1] == "auth.admin_step_expired"


# ---- 並行 ---------------------------------------------------------------------

def test_two_simultaneous_wrong_passwords_count_two_failures(concurrent_engine):
    with Session(concurrent_engine) as s:
        user = admin(s, 1)
        user_id, token_version = user.id, user.token_version
    barrier = Barrier(2)

    def attempt(_):
        with Session(concurrent_engine) as s:
            service = make_auth_service(s)
            barrier.wait(timeout=30)
            try:
                service.verify_admin_password(user_id, token_version, WRONG)
            except Unauthenticated:
                pass
            s.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(attempt, i) for i in range(2)]:
            future.result(timeout=60)
    with Session(concurrent_engine) as s:
        assert s.get(User, user_id).failed_login_count == 2
