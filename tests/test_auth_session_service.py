"""session-authentication 規格：以 token_version 撤銷、登出所有裝置、查詢目前登入者、變更密碼（Service 層）。"""
import pytest

from app.models import User
from app.models.user import PROVIDER_GOOGLE, STATUS_DELETION_PENDING, STATUS_PENDING_VERIFICATION, STATUS_SUSPENDED, UserIdentity
from app.services.errors import Unauthenticated, ValidationError
from app.services.legal import CURRENT_AGE_POLICY_VERSION
from tests.support.auth import GOOD_PASSWORD, START, FakeClock, RecordingMailer, add_user, make_auth_service

NEW_PASSWORD = "a-brand-new-password"
FORBIDDEN_KEYS = {"id", "token_version", "password_hash", "password"}


def reload(session, user: User) -> User:
    session.expire_all()
    return session.get(User, user.id)


# ---- resolve_session ----------------------------------------------------------

def test_a_valid_cookie_resolves_to_the_member(member_session):
    user = add_user(member_session, 1)
    current = make_auth_service(member_session).resolve_session(user.id, 1)
    assert current is not None and current.id == user.id and current.email == "user1@example.com"


def test_a_pending_member_session_is_valid(member_session):
    user = add_user(member_session, 1, status=STATUS_PENDING_VERIFICATION)
    assert make_auth_service(member_session).resolve_session(user.id, 1) is not None


def test_a_stale_token_version_or_an_unknown_member_resolves_to_nothing(member_session):
    user = add_user(member_session, 1)
    service = make_auth_service(member_session)
    assert service.resolve_session(user.id, 2) is None
    assert service.resolve_session(user.id, 0) is None
    assert service.resolve_session(999999, 1) is None


@pytest.mark.parametrize("status", [STATUS_SUSPENDED, STATUS_DELETION_PENDING])
def test_a_session_of_a_disabled_member_stops_working_immediately(member_session, status):
    user = add_user(member_session, 1)
    service = make_auth_service(member_session)
    assert service.resolve_session(user.id, 1) is not None
    user.status = status
    member_session.commit()
    assert service.resolve_session(user.id, 1) is None


# ---- 登出所有裝置 -------------------------------------------------------------

def test_logging_out_everywhere_invalidates_every_existing_session(member_session):
    user = add_user(member_session, 1)
    service = make_auth_service(member_session)
    device_a = service.login(email="user1@example.com", password=GOOD_PASSWORD)
    device_b = service.login(email="user1@example.com", password=GOOD_PASSWORD)
    service.logout_all(device_a)
    assert service.resolve_session(device_a.id, device_a.token_version) is None
    assert service.resolve_session(device_b.id, device_b.token_version) is None
    assert reload(member_session, user).token_version == 2
    fresh = service.login(email="user1@example.com", password=GOOD_PASSWORD)
    assert fresh.token_version == 2 and service.resolve_session(fresh.id, 2) is not None


def test_logout_events_are_logged(member_session, log_lines):
    add_user(member_session, 1)
    service = make_auth_service(member_session)
    current = service.login(email="user1@example.com", password=GOOD_PASSWORD)
    service.record_logout(current)
    service.logout_all(current)
    events = [line["event"] for line in log_lines() if "event" in line]
    assert events[-2:] == ["auth.logout", "auth.logout_all"]


# ---- 查詢目前登入者 -----------------------------------------------------------

def test_the_profile_has_the_members_own_data_and_nothing_internal(member_session):
    user = add_user(member_session, 1, member_tier="plus")
    service = make_auth_service(member_session)
    profile = service.profile(service.resolve_session(user.id, 1))
    assert profile == {
        "public_id": user.public_id, "email": "user1@example.com", "display_name": "使用者1", "member_tier": "plus",
        "role": "user", "status": "active", "email_verified": False, "has_password": True, "age_confirmed": False,
        "google_linked": False,
    }
    assert not (FORBIDDEN_KEYS & set(profile))
    assert user.password_hash not in str(profile)


def test_the_profile_reports_a_linked_google_identity_and_a_missing_password(member_session):
    user = add_user(member_session, 1, password=None)
    member_session.add(UserIdentity(user_id=user.id, provider=PROVIDER_GOOGLE, provider_subject="sub-1"))
    member_session.commit()
    service = make_auth_service(member_session)
    profile = service.profile(service.resolve_session(user.id, 1))
    assert profile["google_linked"] is True and profile["has_password"] is False


# ---- 補確認年滿 18 歲 ---------------------------------------------------------

def test_confirming_adulthood_records_the_time_and_policy_version_and_returns_the_fresh_snapshot(member_session):
    user = add_user(member_session, 1)
    service = make_auth_service(member_session, clock=FakeClock())
    current = service.resolve_session(user.id, 1)
    assert current.age_confirmed is False

    updated = service.confirm_adult(current, confirm=True)

    assert updated.age_confirmed is True and updated.id == user.id
    stored = reload(member_session, user)
    assert stored.age_confirmed_at == START and stored.age_policy_version == CURRENT_AGE_POLICY_VERSION


@pytest.mark.parametrize("value", [False, None, "true", 1, "on", 0, [], {}])
def test_anything_but_an_explicit_true_is_refused_and_writes_nothing(member_session, value):
    user = add_user(member_session, 1)
    service = make_auth_service(member_session)
    with pytest.raises(ValidationError) as exc:
        service.confirm_adult(service.resolve_session(user.id, 1), confirm=value)
    assert set(exc.value.fields) == {"confirm_adult"}
    stored = reload(member_session, user)
    assert stored.age_confirmed_at is None and stored.age_policy_version is None


def test_confirming_again_succeeds_but_keeps_the_original_time_and_version(member_session):
    user = add_user(member_session, 1)
    clock = FakeClock()
    service = make_auth_service(member_session, clock=clock)
    current = service.resolve_session(user.id, 1)
    service.confirm_adult(current, confirm=True)

    clock.advance(days=30)
    again = service.confirm_adult(service.resolve_session(user.id, 1), confirm=True)

    assert again.age_confirmed is True
    stored = reload(member_session, user)
    assert stored.age_confirmed_at == START and stored.age_policy_version == CURRENT_AGE_POLICY_VERSION


def test_the_event_is_logged_only_for_the_first_confirmation_and_never_carries_personal_data(member_session, log_lines):
    user = add_user(member_session, 1)
    service = make_auth_service(member_session)
    current = service.resolve_session(user.id, 1)
    service.confirm_adult(current, confirm=True)
    service.confirm_adult(current, confirm=True)
    with pytest.raises(ValidationError):
        service.confirm_adult(current, confirm=False)

    events = [line for line in log_lines() if line.get("event", "").startswith("auth.age_confirmed")]
    assert [(e["event"], e["user_id"]) for e in events] == [("auth.age_confirmed.later", user.id)]
    assert "user1@example.com" not in log_lines.raw()


def test_confirming_adulthood_touches_nobody_else(member_session):
    me, other = add_user(member_session, 1), add_user(member_session, 2)
    service = make_auth_service(member_session)
    service.confirm_adult(service.resolve_session(me.id, 1), confirm=True)
    assert reload(member_session, other).age_confirmed_at is None


# ---- 變更密碼 -----------------------------------------------------------------

def logged_in(session, service):
    return service.login(email="user1@example.com", password=GOOD_PASSWORD)


def test_changing_the_password_keeps_this_device_and_logs_out_the_others(member_session):
    user = add_user(member_session, 1)
    service = make_auth_service(member_session)
    this_device = logged_in(member_session, service)
    other_device = logged_in(member_session, service)
    old_hash = reload(member_session, user).password_hash

    updated = service.change_password(this_device, current_password=GOOD_PASSWORD, new_password=NEW_PASSWORD)

    assert updated.token_version == 2
    assert service.resolve_session(updated.id, updated.token_version) is not None      # 目前裝置以新版本維持登入
    assert service.resolve_session(other_device.id, other_device.token_version) is None  # 其他裝置失效
    assert reload(member_session, user).password_hash != old_hash
    with pytest.raises(Unauthenticated):
        service.login(email="user1@example.com", password=GOOD_PASSWORD)                # 舊密碼不能再用
    assert service.login(email="user1@example.com", password=NEW_PASSWORD).id == user.id


def test_a_wrong_current_password_is_refused_counts_as_a_failure_and_changes_nothing(member_session, log_lines):
    user = add_user(member_session, 1)
    service = make_auth_service(member_session)
    current = logged_in(member_session, service)
    before = reload(member_session, user).password_hash
    with pytest.raises(ValidationError) as exc:
        service.change_password(current, current_password="not-my-password", new_password=NEW_PASSWORD)
    assert exc.value.fields == {"current_password": "目前的密碼不正確。"}
    after = reload(member_session, user)
    assert after.password_hash == before and after.token_version == 1
    assert after.failed_login_count == 1
    events = [line["event"] for line in log_lines() if "event" in line]
    assert "auth.password_change_refused" in events
    assert events.count("auth.login_failed.bad_password") == 0        # 這不是登入失敗


def test_repeated_wrong_current_passwords_lock_the_account(member_session):
    user = add_user(member_session, 1)
    service = make_auth_service(member_session)
    current = logged_in(member_session, service)
    for _ in range(5):
        with pytest.raises(ValidationError):
            service.change_password(current, current_password="not-my-password", new_password=NEW_PASSWORD)
    assert reload(member_session, user).locked_until is not None


def test_while_locked_even_the_correct_current_password_is_refused_without_counting(member_session):
    clock = FakeClock()
    user = add_user(member_session, 1)
    service = make_auth_service(member_session, clock=clock)
    current = logged_in(member_session, service)
    for _ in range(5):
        with pytest.raises(ValidationError):
            service.change_password(current, current_password="not-my-password", new_password=NEW_PASSWORD)
    with pytest.raises(ValidationError):
        service.change_password(current, current_password=GOOD_PASSWORD, new_password=NEW_PASSWORD)
    assert reload(member_session, user).failed_login_count == 5


def test_the_new_password_must_follow_the_length_rule_and_differ_from_the_current_one(member_session):
    add_user(member_session, 1)
    service = make_auth_service(member_session)
    current = logged_in(member_session, service)
    with pytest.raises(ValidationError) as short:
        service.change_password(current, current_password=GOOD_PASSWORD, new_password="short")
    assert short.value.fields["new_password"] == "密碼長度須為 8 至 128 字。"
    with pytest.raises(ValidationError) as same:
        service.change_password(current, current_password=GOOD_PASSWORD, new_password=GOOD_PASSWORD)
    assert same.value.fields == {"new_password": "新密碼不可與目前的密碼相同。"}


def test_missing_fields_are_reported_per_field(member_session):
    add_user(member_session, 1)
    service = make_auth_service(member_session)
    current = logged_in(member_session, service)
    with pytest.raises(ValidationError) as exc:
        service.change_password(current, current_password="", new_password=None)
    assert set(exc.value.fields) == {"current_password", "new_password"}


def test_an_account_without_a_password_cannot_use_this_path(member_session):
    user = add_user(member_session, 1, password=None)
    service = make_auth_service(member_session)
    current = service.resolve_session(user.id, 1)
    with pytest.raises(ValidationError) as exc:
        service.change_password(current, current_password="anything-at-all", new_password=NEW_PASSWORD)
    assert exc.value.code == "no_password"


def test_a_notice_mail_is_requested_only_when_verification_is_on(member_session):
    user = add_user(member_session, 1)
    off, on = RecordingMailer(), RecordingMailer()
    service_off = make_auth_service(member_session, verification_required=False, mailer=off)
    current = logged_in(member_session, service_off)
    current = service_off.change_password(current, current_password=GOOD_PASSWORD, new_password=NEW_PASSWORD)
    assert off.notices == []

    service_on = make_auth_service(member_session, verification_required=True, mailer=on)
    service_on.change_password(current, current_password=NEW_PASSWORD, new_password="yet-another-password")
    assert on.notices == [user.id]


def test_the_public_dict_never_exposes_internal_fields(member_session):
    add_user(member_session, 1)
    current = logged_in(member_session, make_auth_service(member_session))
    assert not (FORBIDDEN_KEYS & set(current.public_dict()))
    assert "google_linked" not in current.public_dict()
