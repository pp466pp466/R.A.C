"""admin-user-management 規格「危險操作須重新輸入密碼」：`AuthService.confirm_password`。

與登入共用失敗計數與鎖定；成功不重設計數；鎖定中即使密碼正確也拒絕；沒有密碼的帳號回 `no_password`。
"""
from datetime import timedelta

import pytest

from app.repositories.user_identity_repository import UserIdentityRepository
from app.repositories.user_repository import UserRepository
from app.services.auth_service import LOCK_DURATION, MAX_FAILED_LOGINS, AuthService
from app.services.current_user import CurrentUser
from app.services.errors import ValidationError
from tests.support.auth import FAST_HASHER, GOOD_PASSWORD, FakeClock, add_user


class Persisted:
    """記錄「這個請求即使以 4xx 結束也要保留變更」被要求了幾次。"""

    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1


def build(session, clock=None, persisted=None):
    clock = clock or FakeClock()
    persisted = persisted or Persisted()
    service = AuthService(
        users=UserRepository(session), identities=UserIdentityRepository(session), hasher=FAST_HASHER, clock=clock,
        email_verification_required=False, persist_failures=persisted,
    )
    return service, clock, persisted


def as_current(session, user) -> CurrentUser:
    session.expire_all()
    return CurrentUser.from_model(session.get(type(user), user.id))


@pytest.fixture()
def admin(member_session):
    return add_user(member_session, 1, role="admin")


def failures(session, user):
    session.expire_all()
    row = session.get(type(user), user.id)
    return row.failed_login_count, row.locked_until


def test_the_right_password_passes_and_changes_nothing(member_session, admin):
    service, _, persisted = build(member_session)
    version = admin.token_version
    service.confirm_password(as_current(member_session, admin), GOOD_PASSWORD)
    assert failures(member_session, admin) == (0, None)
    assert member_session.get(type(admin), admin.id).token_version == version
    assert persisted.calls == 0


@pytest.mark.parametrize("password", ["wrong-password-123", GOOD_PASSWORD + "x", GOOD_PASSWORD.upper()])
def test_a_wrong_password_names_the_password_field_counts_a_failure_and_asks_to_keep_it(member_session, admin, password):
    service, _, persisted = build(member_session)
    with pytest.raises(ValidationError) as exc:
        service.confirm_password(as_current(member_session, admin), password)
    assert set(exc.value.fields) == {"password"} and exc.value.code == "validation_error"
    assert failures(member_session, admin)[0] == 1
    assert persisted.calls == 1                                       # 失敗計數不會因為 400 而被回復


@pytest.mark.parametrize("password", [None, "", 123, ["x"], {"a": 1}])
def test_a_missing_or_non_text_password_is_refused_without_counting(member_session, admin, password):
    service, _, _ = build(member_session)
    with pytest.raises(ValidationError) as exc:
        service.confirm_password(as_current(member_session, admin), password)
    assert set(exc.value.fields) == {"password"}
    assert failures(member_session, admin) == (0, None)               # 沒有嘗試就不算失敗


def test_five_failures_lock_the_account_for_fifteen_minutes(member_session, admin):
    service, clock, _ = build(member_session)
    for _ in range(MAX_FAILED_LOGINS):
        with pytest.raises(ValidationError):
            service.confirm_password(as_current(member_session, admin), "wrong-password-123")
    count, locked_until = failures(member_session, admin)
    assert count == MAX_FAILED_LOGINS and locked_until == clock() + LOCK_DURATION


def test_while_locked_even_the_correct_password_is_refused_and_nothing_more_is_counted(member_session, admin):
    service, clock, _ = build(member_session)
    for _ in range(MAX_FAILED_LOGINS):
        with pytest.raises(ValidationError):
            service.confirm_password(as_current(member_session, admin), "wrong-password-123")
    clock.advance(minutes=14)
    with pytest.raises(ValidationError) as exc:
        service.confirm_password(as_current(member_session, admin), GOOD_PASSWORD)
    assert set(exc.value.fields) == {"password"}
    assert failures(member_session, admin)[0] == MAX_FAILED_LOGINS


def test_the_refusal_while_locked_is_indistinguishable_from_a_wrong_password(member_session, admin):
    service, _, _ = build(member_session)
    with pytest.raises(ValidationError) as wrong:
        service.confirm_password(as_current(member_session, admin), "wrong-password-123")
    for _ in range(MAX_FAILED_LOGINS):
        with pytest.raises(ValidationError):
            service.confirm_password(as_current(member_session, admin), "wrong-password-123")
    with pytest.raises(ValidationError) as locked:
        service.confirm_password(as_current(member_session, admin), GOOD_PASSWORD)
    assert (wrong.value.code, wrong.value.fields, wrong.value.message) == (locked.value.code, locked.value.fields, locked.value.message)


def test_after_the_lock_expires_the_correct_password_works_and_the_counter_starts_over(member_session, admin):
    service, clock, _ = build(member_session)
    for _ in range(MAX_FAILED_LOGINS):
        with pytest.raises(ValidationError):
            service.confirm_password(as_current(member_session, admin), "wrong-password-123")
    clock.advance(minutes=15, seconds=1)
    service.confirm_password(as_current(member_session, admin), GOOD_PASSWORD)
    assert failures(member_session, admin) == (0, None)


def test_a_success_does_not_reset_the_failure_counter(member_session, admin):
    """只有登入成功才重設；否則攻擊者可以交錯一次正確與多次錯誤的嘗試。"""
    service, _, _ = build(member_session)
    for _ in range(3):
        with pytest.raises(ValidationError):
            service.confirm_password(as_current(member_session, admin), "wrong-password-123")
    service.confirm_password(as_current(member_session, admin), GOOD_PASSWORD)
    assert failures(member_session, admin)[0] == 3


def test_the_counter_is_shared_with_the_admin_password_step(member_session, admin):
    """管理員不能只靠密碼登入，所以與「重新輸入密碼」共用計數的是 Google 之後的密碼驗證步驟。"""
    service, _, _ = build(member_session)
    for _ in range(2):
        with pytest.raises(ValidationError):
            service.confirm_password(as_current(member_session, admin), "wrong-password-123")
    with pytest.raises(Exception):
        service.verify_admin_password(admin.id, admin.token_version, "wrong-password-123")
    assert failures(member_session, admin)[0] == 3


def test_an_account_without_a_password_gets_the_dedicated_code_and_nothing_is_counted(member_session):
    google_only = add_user(member_session, 2, role="admin", password=None)
    service, _, persisted = build(member_session)
    with pytest.raises(ValidationError) as exc:
        service.confirm_password(as_current(member_session, google_only), "anything")
    assert exc.value.code == "no_password"
    assert failures(member_session, google_only) == (0, None) and persisted.calls == 0


def test_the_wrong_password_never_appears_in_the_error(member_session, admin):
    service, _, _ = build(member_session)
    secret = "very-secret-wrong-password-xyz"
    with pytest.raises(ValidationError) as exc:
        service.confirm_password(as_current(member_session, admin), secret)
    assert secret not in str(exc.value) and secret not in repr(exc.value.fields)


def test_a_password_longer_than_the_limit_is_refused_like_any_wrong_password(member_session, admin):
    service, _, _ = build(member_session)
    with pytest.raises(ValidationError):
        service.confirm_password(as_current(member_session, admin), "a" * 500)
    assert failures(member_session, admin)[0] == 1


def test_a_user_that_no_longer_exists_is_treated_as_logged_out(member_session, admin):
    from app.services.errors import Unauthenticated

    service, _, _ = build(member_session)
    current = as_current(member_session, admin)
    member_session.delete(member_session.get(type(admin), admin.id))
    member_session.commit()
    with pytest.raises(Unauthenticated):
        service.confirm_password(current, GOOD_PASSWORD)
