"""member-registration 規格：註冊的行為（Service 層，SQLite 與 SQL Server 各跑一次）。"""
import re

import pytest
from sqlalchemy import func, select

from app.models.user import STATUS_ACTIVE, STATUS_PENDING_VERIFICATION, STATUS_SUSPENDED, User
from app.repositories.errors import DuplicateUserError
from app.repositories.user_repository import UserRepository
from app.services.errors import ValidationError
from app.services.legal import CURRENT_TERMS_VERSION
from tests.support.auth import GOOD_PASSWORD, START, FakeClock, RecordingMailer, add_user, make_auth_service

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def register(service, **overrides):
    values = dict(email="new@example.com", password=GOOD_PASSWORD, display_name="王小明", accept_terms=True)
    values.update(overrides)
    return service.register(**values)


def user_count(session) -> int:
    return session.scalar(select(func.count(User.id)))


def by_email(session, email) -> User:
    return UserRepository(session).get_by_email(email)


# ---- 會員資料模型 -------------------------------------------------------------

def test_a_new_account_gets_the_documented_defaults(member_session):
    register(make_auth_service(member_session))
    user = by_email(member_session, "new@example.com")
    assert (user.role, user.member_tier) == ("user", "free")
    assert UUID_RE.match(user.public_id)
    assert user.display_name == "王小明"
    assert user.token_version == 1 and user.failed_login_count == 0


def test_two_members_may_share_a_display_name(member_session):
    service = make_auth_service(member_session)
    register(service, email="a@example.com", display_name="同名")
    register(service, email="b@example.com", display_name="同名")
    assert user_count(member_session) == 2


def test_registration_returns_nothing_so_it_cannot_log_anyone_in(member_session):
    assert register(make_auth_service(member_session)) is None


# ---- 輸入驗證 -----------------------------------------------------------------

def test_a_nine_character_password_is_rejected_with_a_field_error_and_no_account(member_session):
    with pytest.raises(ValidationError) as exc:
        register(make_auth_service(member_session), password="a" * 9)
    assert exc.value.fields["password"] == "密碼長度須為 10 至 128 字。"
    assert user_count(member_session) == 0


def test_a_ten_letter_lowercase_password_is_accepted(member_session):
    register(make_auth_service(member_session), password="abcdefghij")
    assert user_count(member_session) == 1


def test_not_accepting_the_terms_creates_no_account(member_session):
    for value in (False, None, "true", 1, "on"):
        with pytest.raises(ValidationError) as exc:
            register(make_auth_service(member_session), accept_terms=value)
        assert "accept_terms" in exc.value.fields
    assert user_count(member_session) == 0


@pytest.mark.parametrize("email", ["", "plain", "a@b", "user@exam ple.com", "用戶@example.com", None, 123])
def test_an_invalid_email_is_rejected(member_session, email):
    with pytest.raises(ValidationError) as exc:
        register(make_auth_service(member_session), email=email)
    assert "email" in exc.value.fields


@pytest.mark.parametrize("name", ["", "   ", "\n\t", "x" * 51, None, 42])
def test_an_invalid_display_name_is_rejected(member_session, name):
    with pytest.raises(ValidationError) as exc:
        register(make_auth_service(member_session), display_name=name)
    assert "display_name" in exc.value.fields


def test_a_display_name_of_exactly_fifty_characters_is_accepted_and_is_cleaned(member_session):
    service = make_auth_service(member_session)
    register(service, email="a@example.com", display_name="名" * 50)
    register(service, email="b@example.com", display_name="  王\n小\x00明  ")
    assert by_email(member_session, "a@example.com").display_name == "名" * 50
    assert by_email(member_session, "b@example.com").display_name == "王小明"


def test_every_field_error_is_reported_at_once(member_session):
    with pytest.raises(ValidationError) as exc:
        register(make_auth_service(member_session), email="bad", password="short", display_name="", accept_terms=False)
    assert set(exc.value.fields) == {"email", "password", "display_name", "accept_terms"}


# ---- email 正規化與唯一性 -----------------------------------------------------

def test_the_stored_email_is_lowercase_and_the_original_address_is_kept_for_mail(member_session):
    register(make_auth_service(member_session), email="  Alice@Example.COM ")
    user = by_email(member_session, "alice@example.com")
    assert user.email == "alice@example.com" and user.email_canonical == "alice@example.com"


def test_an_address_that_differs_only_in_case_is_treated_as_a_duplicate(member_session):
    service = make_auth_service(member_session)
    register(service, email="Alice@Example.com", password="first-password-1", display_name="第一位")
    original_hash = by_email(member_session, "alice@example.com").password_hash
    register(service, email="alice@example.com", password="second-password-2", display_name="第二位")
    assert user_count(member_session) == 1
    user = by_email(member_session, "alice@example.com")
    assert user.password_hash == original_hash and user.display_name == "第一位"


def test_gmail_aliases_are_duplicates(member_session):
    service = make_auth_service(member_session)
    register(service, email="john.doe@gmail.com")
    register(service, email="johndoe+news@googlemail.com")
    assert user_count(member_session) == 1
    assert by_email(member_session, "john.doe@gmail.com").email_canonical == "johndoe@gmail.com"


def test_other_domains_keep_dots_so_both_can_register(member_session):
    service = make_auth_service(member_session)
    register(service, email="a.b@example.com")
    register(service, email="ab@example.com")
    assert user_count(member_session) == 2


def test_a_deleted_accounts_address_can_be_registered_again(member_session):
    service = make_auth_service(member_session)
    register(service, email="again@example.com")
    member_session.delete(by_email(member_session, "again@example.com"))
    member_session.commit()
    register(service, email="again@example.com")
    assert user_count(member_session) == 1


# ---- 不洩漏帳號是否存在 -------------------------------------------------------

def test_registering_an_existing_address_changes_nothing_about_the_existing_account(member_session):
    service = make_auth_service(member_session)
    existing = add_user(member_session, 1, password="the-original-pw", display_name="原本的人")
    before = (existing.password_hash, existing.display_name, existing.token_version, existing.status)
    register(service, email="user1@example.com", password="attacker-password", display_name="攻擊者")
    member_session.refresh(existing)
    assert (existing.password_hash, existing.display_name, existing.token_version, existing.status) == before
    assert user_count(member_session) == 1


def test_registering_the_address_of_a_google_only_account_neither_creates_nor_sets_a_password(member_session):
    google_only = add_user(member_session, 1, password=None)
    register(make_auth_service(member_session), email="user1@example.com")
    member_session.refresh(google_only)
    assert google_only.password_hash is None
    assert user_count(member_session) == 1


def test_the_password_is_hashed_exactly_once_whether_or_not_the_address_exists(member_session):
    class CountingHasher:
        def __init__(self, inner):
            self.inner, self.hashed = inner, 0

        def hash(self, raw):
            self.hashed += 1
            return self.inner.hash(raw)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    from tests.support.auth import FAST_HASHER

    counting = CountingHasher(FAST_HASHER)
    service = make_auth_service(member_session, hasher=counting)
    register(service, email="fresh@example.com")
    assert counting.hashed == 1
    register(service, email="fresh@example.com")             # 已存在
    assert counting.hashed == 2


def test_losing_a_registration_race_is_treated_as_an_existing_account_not_an_error(member_session):
    """並行註冊同一信箱：第二個請求先查到「不存在」，寫入時被唯一索引擋下，必須無聲地當作已存在。"""
    add_user(member_session, 1)

    class StaleReads(UserRepository):
        stale = True

        def get_by_canonical_email(self, email_canonical):
            if StaleReads.stale:
                StaleReads.stale = False
                return None                         # 第一次查詢還看不到對方剛提交的帳號
            return super().get_by_canonical_email(email_canonical)

    service = make_auth_service(member_session)
    service._users = StaleReads(member_session)
    register(service, email="user1@example.com")
    assert user_count(member_session) == 1


def test_add_user_failure_in_a_race_leaves_the_session_usable(member_session):
    add_user(member_session, 1)
    repo = UserRepository(member_session)
    with pytest.raises(DuplicateUserError):
        repo.add_user(User(email="user1@example.com", email_canonical="user1@example.com", display_name="x", status=STATUS_ACTIVE))
    register(make_auth_service(member_session), email="someone-else@example.com")
    assert user_count(member_session) == 2


# ---- 信箱驗證兩種模式 ---------------------------------------------------------

def test_with_verification_on_a_new_account_is_pending_and_a_verification_mail_is_requested(member_session):
    mailer = RecordingMailer()
    register(make_auth_service(member_session, verification_required=True, mailer=mailer))
    user = by_email(member_session, "new@example.com")
    assert user.status == STATUS_PENDING_VERIFICATION and user.email_verified_at is None
    assert mailer.verifications == [user.id]


def test_with_verification_off_a_new_account_is_active_unverified_and_no_mail_is_requested(member_session):
    mailer = RecordingMailer()
    register(make_auth_service(member_session, verification_required=False, mailer=mailer))
    user = by_email(member_session, "new@example.com")
    assert user.status == STATUS_ACTIVE and user.email_verified_at is None
    assert mailer.verifications == []


def test_registering_again_while_pending_keeps_the_original_password_and_asks_for_a_new_mail(member_session):
    mailer = RecordingMailer()
    service = make_auth_service(member_session, verification_required=True, mailer=mailer)
    register(service, password="victims-own-password")
    pending = by_email(member_session, "new@example.com")
    original_hash = pending.password_hash
    register(service, password="attackers-chosen-pw")
    member_session.refresh(pending)
    assert pending.password_hash == original_hash
    assert mailer.verifications == [pending.id, pending.id]


def test_a_verified_account_gets_no_mail_when_someone_registers_its_address(member_session):
    mailer = RecordingMailer()
    verified = add_user(member_session, 1, email_verified_at=START)
    register(make_auth_service(member_session, verification_required=True, mailer=mailer), email="user1@example.com")
    assert mailer.verifications == [] and verified.status == STATUS_ACTIVE


def test_an_active_but_unverified_account_from_the_verification_off_era_is_asked_to_verify(member_session):
    mailer = RecordingMailer()
    legacy = add_user(member_session, 1)       # active、email_verified_at 為空
    register(make_auth_service(member_session, verification_required=True, mailer=mailer), email="user1@example.com")
    assert mailer.verifications == [legacy.id]


def test_a_suspended_account_gets_no_mail(member_session):
    mailer = RecordingMailer()
    add_user(member_session, 1, status=STATUS_SUSPENDED)
    register(make_auth_service(member_session, verification_required=True, mailer=mailer), email="user1@example.com")
    assert mailer.verifications == []


def test_verification_on_without_a_mailer_is_a_programming_error_not_a_silent_skip(member_session):
    service = make_auth_service(member_session, verification_required=True)
    service._mailer = None
    with pytest.raises(RuntimeError):
        register(service)


# ---- 條款同意 -----------------------------------------------------------------

def test_the_accepted_terms_version_and_time_are_recorded(member_session):
    clock = FakeClock()
    register(make_auth_service(member_session, clock=clock))
    user = by_email(member_session, "new@example.com")
    assert user.terms_version == CURRENT_TERMS_VERSION
    assert user.terms_accepted_at == START


# ---- 日誌 ---------------------------------------------------------------------

def test_registration_events_are_logged_without_the_address_or_the_password(member_session, log_lines):
    service = make_auth_service(member_session)
    register(service, email="secret.person@example.com", password="very-secret-password-xyz")
    register(service, email="secret.person@example.com", password="another-secret-password")
    events = [line["event"] for line in log_lines() if "event" in line]
    assert events == ["auth.register", "auth.register_existing"]
    assert "secret.person" not in log_lines.raw() and "secret-password" not in log_lines.raw()
