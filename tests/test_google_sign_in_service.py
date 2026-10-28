"""google-sign-in 規格：帳號對應、同信箱合併規則、首次註冊的條款同意、帳號狀態（Service 層，SQLite 與 SQL Server）。"""
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from app.models import User, UserIdentity
from app.models.user import STATUS_ACTIVE, STATUS_DELETION_PENDING, STATUS_PENDING_VERIFICATION, STATUS_SUSPENDED
from app.repositories.user_identity_repository import UserIdentityRepository
from app.repositories.user_repository import UserRepository
from app.services.auth_service import AuthService
from app.services.errors import Unauthenticated
from app.services.google_oidc import GoogleAuthError, GoogleProfile, code_challenge_for
from app.services.google_sign_in_service import (
    ERROR_AGE, ERROR_DISABLED, ERROR_REJECTED, ERROR_STATE, ERROR_TERMS, ERROR_UNAVAILABLE, OUTCOME_LINKED,
    OUTCOME_LINKED_UNVERIFIED, OUTCOME_LOGIN, OUTCOME_REGISTERED, GoogleLoginRefused, GoogleSignInService,
)
from app.services.legal import CURRENT_AGE_POLICY_VERSION, CURRENT_TERMS_VERSION
from tests.support.auth import FAST_HASHER, GOOD_PASSWORD, START, FakeClock, add_user
from tests.support.email_flow import DeferQueue, FakeSender, make_flow
from tests.support.google import SUB, FakeProvider

def make_service(session, provider=None, clock=None) -> GoogleSignInService:
    return GoogleSignInService(
        users=UserRepository(session), identities=UserIdentityRepository(session),
        provider=provider or FakeProvider(), clock=clock or FakeClock(),
    )


def login_with(service, *, accept_terms=True, confirm_adult=True, **overrides):
    """走一遍完整流程：begin 取得交易，再以正確的 state 完成回呼。"""
    start = service.begin(accept_terms=accept_terms, confirm_adult=confirm_adult)
    args = dict(code="code-from-google", state=start.transaction["s"], provider_error=None, transaction=start.transaction)
    args.update(overrides)
    return service.complete(**args)


def refusal_of(service, **kwargs) -> str:
    with pytest.raises(GoogleLoginRefused) as exc:
        login_with(service, **kwargs)
    return exc.value.code


def users(session) -> list[User]:
    session.expire_all()
    return list(session.query(User))


def identities(session) -> list[UserIdentity]:
    session.expire_all()
    return list(session.query(UserIdentity))


# =============================================================================
# 發起與 state
# =============================================================================

def test_begin_builds_the_url_from_fresh_random_values_and_returns_the_transaction(member_session):
    provider = FakeProvider()
    service = make_service(member_session, provider)
    start = service.begin(accept_terms=True, confirm_adult=True)
    tx = start.transaction
    assert set(tx) == {"s", "n", "v", "t", "a"} and tx["t"] is True and tx["a"] is True
    sent = provider.urls[0]
    assert (sent["state"], sent["nonce"]) == (tx["s"], tx["n"])
    assert sent["code_challenge"] == code_challenge_for(tx["v"])
    query = parse_qs(urlsplit(start.authorization_url).query)
    assert query["state"] == [tx["s"]] and tx["s"] != tx["n"]
    other = service.begin(accept_terms=True, confirm_adult=True).transaction
    assert {other["s"], other["n"], other["v"]}.isdisjoint({tx["s"], tx["n"], tx["v"]})


@pytest.mark.parametrize("value", [False, None, "true", 1, "yes"])
def test_only_a_real_true_counts_as_accepting_the_terms(member_session, value):
    assert make_service(member_session).begin(accept_terms=value, confirm_adult=True).transaction["t"] is False


@pytest.mark.parametrize("value", [False, None, "true", 1, "yes"])
def test_only_a_real_true_counts_as_confirming_adulthood(member_session, value):
    transaction = make_service(member_session).begin(accept_terms=True, confirm_adult=value).transaction
    assert transaction["a"] is False and transaction["t"] is True      # 兩個旗標各自獨立


@pytest.mark.parametrize(
    "mutate",
    [
        lambda tx: {"state": "someone-elses-state"},
        lambda tx: {"state": None},
        lambda tx: {"state": ""},
        lambda tx: {"state": 12345},
        lambda tx: {"transaction": None},
        lambda tx: {"transaction": {}},
        lambda tx: {"transaction": {"s": tx["s"]}},
        lambda tx: {"transaction": {**tx, "v": ""}},
        lambda tx: {"transaction": "not-a-dict"},
    ],
)
def test_a_state_that_does_not_match_the_browser_binding_is_refused_before_google_is_contacted(member_session, mutate):
    provider = FakeProvider()
    service = make_service(member_session, provider)
    start = service.begin(accept_terms=True, confirm_adult=True)
    with pytest.raises(GoogleLoginRefused) as exc:
        service.complete(**{"code": "c", "state": start.transaction["s"], "provider_error": None,
                            "transaction": start.transaction, **mutate(start.transaction)})
    assert exc.value.code == ERROR_STATE
    assert provider.exchanges == [] and users(member_session) == []


def test_the_code_verifier_and_nonce_from_the_binding_are_what_google_is_asked_to_check(member_session):
    provider = FakeProvider()
    service = make_service(member_session, provider)
    start = service.begin(accept_terms=True, confirm_adult=True)
    service.complete(code="the-code", state=start.transaction["s"], provider_error=None, transaction=start.transaction)
    assert provider.exchanges == [{"code": "the-code", "code_verifier": start.transaction["v"], "expected_nonce": start.transaction["n"]}]


# =============================================================================
# Google 端的失敗
# =============================================================================

def test_google_problems_map_to_fixed_error_codes_and_create_nothing(member_session):
    provider = FakeProvider()
    service = make_service(member_session, provider)
    provider.error = GoogleAuthError("unavailable")
    assert refusal_of(service) == ERROR_UNAVAILABLE
    provider.error = GoogleAuthError("token")
    assert refusal_of(service) == ERROR_REJECTED
    provider.error = None
    assert refusal_of(service, provider_error="access_denied") == ERROR_REJECTED       # 使用者在 Google 按了取消
    assert refusal_of(service, code=None) == ERROR_REJECTED
    assert users(member_session) == [] and identities(member_session) == []


# =============================================================================
# 已綁定的身分
# =============================================================================

def test_a_bound_identity_logs_in_even_after_the_email_changed_at_google(member_session):
    user = add_user(member_session, 1, password=None, email_verified_at=START)
    member_session.add(UserIdentity(user_id=user.id, provider="google", provider_subject=SUB))
    member_session.commit()
    provider = FakeProvider()
    provider.profile = GoogleProfile(SUB, "a-completely-new-address@example.com", True, "改名了")
    clock = FakeClock()
    clock.advance(hours=2)
    result = login_with(make_service(member_session, provider, clock))
    assert result.outcome == OUTCOME_LOGIN and result.user.id == user.id
    assert len(users(member_session)) == 1 and len(identities(member_session)) == 1
    assert users(member_session)[0].email == "user1@example.com"                  # 不因 Google 端信箱變更而改動
    assert users(member_session)[0].last_login_at == START + timedelta(hours=2)


@pytest.mark.parametrize("status", [STATUS_SUSPENDED, STATUS_DELETION_PENDING])
def test_a_disabled_account_cannot_sign_in_through_google(member_session, status):
    user = add_user(member_session, 1, password=None, status=status, email_verified_at=START)
    member_session.add(UserIdentity(user_id=user.id, provider="google", provider_subject=SUB))
    member_session.commit()
    assert refusal_of(make_service(member_session)) == ERROR_DISABLED


# =============================================================================
# 全新信箱：建立帳號
# =============================================================================

def test_a_new_address_with_the_terms_accepted_creates_a_verified_passwordless_account(member_session):
    clock = FakeClock()
    result = login_with(make_service(member_session, clock=clock), accept_terms=True)
    assert result.outcome == OUTCOME_REGISTERED
    (user,) = users(member_session)
    assert user.email == "person@example.com" and user.display_name == "王小明"
    assert user.status == STATUS_ACTIVE and user.password_hash is None
    assert user.email_verified_at == START and (user.role, user.member_tier) == ("user", "free")
    assert user.terms_version == CURRENT_TERMS_VERSION and user.terms_accepted_at == START and user.last_login_at == START
    (identity,) = identities(member_session)
    assert (identity.user_id, identity.provider, identity.provider_subject) == (user.id, "google", SUB)
    assert result.user.has_password is False and result.user.email_verified is True


def test_a_new_address_without_the_terms_creates_nothing(member_session):
    provider = FakeProvider()
    assert refusal_of(make_service(member_session, provider), accept_terms=False) == ERROR_TERMS
    assert users(member_session) == [] and identities(member_session) == []


@pytest.mark.parametrize(
    ("accept_terms", "confirm_adult", "expected_refusal"),
    [(True, True, None), (True, False, ERROR_AGE), (False, True, ERROR_TERMS), (False, False, ERROR_TERMS)],
)
def test_a_new_account_needs_both_confirmations_and_the_missing_one_is_named(
    member_session, accept_terms, confirm_adult, expected_refusal
):
    service = make_service(member_session)
    if expected_refusal is None:
        assert login_with(service, accept_terms=accept_terms, confirm_adult=confirm_adult).outcome == OUTCOME_REGISTERED
        assert len(users(member_session)) == 1 and len(identities(member_session)) == 1
    else:
        assert refusal_of(service, accept_terms=accept_terms, confirm_adult=confirm_adult) == expected_refusal
        assert users(member_session) == [] and identities(member_session) == []


def test_a_new_google_account_records_the_age_confirmation_with_the_terms(member_session):
    login_with(make_service(member_session, clock=FakeClock()))
    (user,) = users(member_session)
    assert user.age_confirmed_at == START and user.age_policy_version == CURRENT_AGE_POLICY_VERSION
    assert user.age_confirmed_at == user.terms_accepted_at


def test_a_transaction_without_the_age_flag_counts_as_unconfirmed(member_session):
    """更新前發起、尚未完成的流程沒有 `a`：不能因此建立帳號。"""
    service = make_service(member_session)
    start = service.begin(accept_terms=True, confirm_adult=True)
    legacy = {key: value for key, value in start.transaction.items() if key != "a"}
    with pytest.raises(GoogleLoginRefused) as exc:
        service.complete(code="c", state=legacy["s"], provider_error=None, transaction=legacy)
    assert exc.value.code == ERROR_AGE
    assert users(member_session) == [] and identities(member_session) == []


def test_refusing_for_age_is_a_warning_event_and_creates_nothing(member_session, log_lines):
    assert refusal_of(make_service(member_session), confirm_adult=False) == ERROR_AGE
    refused = [line for line in log_lines() if line.get("event") == "auth.google_rejected.age"]
    assert len(refused) == 1 and refused[0]["level"] == "WARNING"
    assert not [line for line in log_lines() if line.get("event", "").startswith("auth.age_confirmed")]


def test_existing_accounts_signing_in_are_never_marked_as_age_confirmed(member_session):
    """已綁定、以信箱合併（已驗證與未驗證）三種既有帳號：登入成功，但年齡確認欄位維持為空。"""
    bound = add_user(member_session, 1, password=None, email_verified_at=START)
    member_session.add(UserIdentity(user_id=bound.id, provider="google", provider_subject=SUB))
    verified = add_user(member_session, 2, email_verified_at=START)
    unverified = add_user(member_session, 3)
    member_session.commit()

    provider = FakeProvider()
    login_with(make_service(member_session, provider), accept_terms=True, confirm_adult=True)               # 已綁定
    provider.profile = GoogleProfile("sub-merge-verified", "user2@example.com", True, "x")
    login_with(make_service(member_session, provider), accept_terms=True, confirm_adult=True)               # 合併（已驗證）
    provider.profile = GoogleProfile("sub-merge-unverified", "user3@example.com", True, "x")
    login_with(make_service(member_session, provider), accept_terms=True, confirm_adult=True)               # 合併（未驗證）

    for user in (bound, verified, unverified):
        member_session.refresh(user)
        assert user.age_confirmed_at is None and user.age_policy_version is None


def test_signing_in_does_not_overwrite_an_existing_age_confirmation(member_session):
    confirmed_at = START - timedelta(days=3)
    user = add_user(member_session, 1, email_verified_at=START, age_confirmed_at=confirmed_at, age_policy_version="old")
    result = login_with(make_service(member_session, _provider_for("user1@example.com")), confirm_adult=False)
    assert result.user.age_confirmed is True
    member_session.refresh(user)
    assert user.age_confirmed_at == confirmed_at and user.age_policy_version == "old"


@pytest.mark.parametrize(
    ("name", "expected"),
    [(None, "person"), ("", "person"), ("   ", "person"), ("王小明", "王小明"), ("名" * 80, "名" * 50), ("王\n小\x00明", "王小明")],
)
def test_the_display_name_comes_from_google_with_a_fallback_to_the_address(member_session, name, expected):
    provider = FakeProvider()
    provider.profile = GoogleProfile(SUB, "person@example.com", True, name)
    login_with(make_service(member_session, provider))
    assert users(member_session)[0].display_name == expected


def test_a_non_ascii_google_address_is_refused_because_the_system_cannot_mail_it(member_session):
    provider = FakeProvider()
    provider.profile = GoogleProfile(SUB, "用戶@example.com", True, "x")
    assert refusal_of(make_service(member_session, provider)) == ERROR_REJECTED
    assert users(member_session) == []


# =============================================================================
# 同信箱的本機帳號：合併規則
# =============================================================================

def test_a_verified_local_account_gets_the_google_identity_and_keeps_its_password(member_session):
    user = add_user(member_session, 1, email_verified_at=START)
    provider = FakeProvider()
    provider.profile = GoogleProfile(SUB, "USER1@example.com", True, "x")
    before = (user.password_hash, user.token_version, user.status)
    # 已有帳號：不需要再勾選條款，也不需要確認年齡
    result = login_with(make_service(member_session, provider), accept_terms=False, confirm_adult=False)
    assert result.outcome == OUTCOME_LINKED and result.user.id == user.id
    member_session.refresh(user)
    assert (user.password_hash, user.token_version, user.status) == before
    assert len(users(member_session)) == 1 and len(identities(member_session)) == 1


def test_an_unverified_local_account_loses_its_password_is_verified_and_its_sessions_die(member_session):
    """預先佔用信箱：攻擊者以受害者的信箱註冊並設了密碼；受害者之後用 Google 登入。"""
    user = add_user(member_session, 1, status=STATUS_PENDING_VERIFICATION, password="attackers-chosen-pw")
    attacker_auth = AuthService(
        users=UserRepository(member_session), identities=UserIdentityRepository(member_session), hasher=FAST_HASHER,
        clock=FakeClock(), email_verification_required=False,
    )
    attacker_session = attacker_auth.login(email="user1@example.com", password="attackers-chosen-pw")
    assert attacker_auth.resolve_session(attacker_session.id, attacker_session.token_version)

    provider = FakeProvider()
    provider.profile = GoogleProfile(SUB, "user1@example.com", True, "真正的主人")
    result = login_with(make_service(member_session, provider))
    assert result.outcome == OUTCOME_LINKED_UNVERIFIED

    member_session.refresh(user)
    assert user.password_hash is None and user.email_verified_at == START
    assert user.status == STATUS_ACTIVE and user.token_version == 2
    assert attacker_auth.resolve_session(attacker_session.id, attacker_session.token_version) is None   # 攻擊者的登入狀態失效
    with pytest.raises(Unauthenticated):
        attacker_auth.login(email="user1@example.com", password="attackers-chosen-pw")                  # 密碼也不能再用
    assert result.user.token_version == 2                                                               # 新的登入狀態使用新版本


def test_an_active_unverified_account_from_the_verification_off_era_is_treated_the_same(member_session):
    user = add_user(member_session, 1)             # active，email_verified_at 為空
    result = login_with(make_service(member_session, _provider_for("user1@example.com")))
    assert result.outcome == OUTCOME_LINKED_UNVERIFIED
    member_session.refresh(user)
    assert user.password_hash is None and user.email_verified_at is not None and user.status == STATUS_ACTIVE


def _provider_for(email: str) -> FakeProvider:
    provider = FakeProvider()
    provider.profile = GoogleProfile(SUB, email, True, "x")
    return provider


def test_gmail_aliases_match_the_same_account(member_session):
    user = add_user(member_session, 1, email="john.doe@gmail.com", email_canonical="johndoe@gmail.com", email_verified_at=START)
    result = login_with(make_service(member_session, _provider_for("JohnDoe+news@googlemail.com")))
    assert result.user.id == user.id and result.outcome == OUTCOME_LINKED
    assert len(users(member_session)) == 1


def test_an_account_already_bound_to_another_google_identity_is_not_rebound(member_session):
    user = add_user(member_session, 1, email_verified_at=START)
    member_session.add(UserIdentity(user_id=user.id, provider="google", provider_subject="the-original-subject"))
    member_session.commit()
    assert refusal_of(make_service(member_session, _provider_for("user1@example.com"))) == ERROR_REJECTED
    assert [i.provider_subject for i in identities(member_session)] == ["the-original-subject"]


@pytest.mark.parametrize("status", [STATUS_SUSPENDED, STATUS_DELETION_PENDING])
def test_a_disabled_account_found_by_email_is_refused_and_not_bound(member_session, status):
    add_user(member_session, 1, status=status, email_verified_at=START)
    assert refusal_of(make_service(member_session, _provider_for("user1@example.com"))) == ERROR_DISABLED
    assert identities(member_session) == []


def test_losing_a_registration_race_falls_back_to_linking_instead_of_failing(member_session):
    add_user(member_session, 1, email_verified_at=START)

    class StaleReads(UserRepository):
        stale = True

        def get_by_canonical_email(self, email_canonical):
            if StaleReads.stale:
                StaleReads.stale = False
                return None
            return super().get_by_canonical_email(email_canonical)

    service = make_service(member_session, _provider_for("user1@example.com"))
    service._users = StaleReads(member_session)
    result = login_with(service)
    assert result.outcome == OUTCOME_LINKED and len(users(member_session)) == 1 and len(identities(member_session)) == 1


# =============================================================================
# 與其他流程的銜接
# =============================================================================

def test_a_google_account_that_later_sets_a_password_can_sign_in_both_ways(member_session):
    clock = FakeClock()
    service = make_service(member_session, clock=clock)
    first = login_with(service)
    assert first.outcome == OUTCOME_REGISTERED

    sender, queue = FakeSender(), DeferQueue()
    flow = make_flow(member_session, clock=clock, sender=sender, queue=queue)
    clock.advance(minutes=5)
    flow.request_password_reset("person@example.com")
    queue.run()
    flow.reset_password(sender.last_token(), "my-new-password-123")

    auth = AuthService(
        users=UserRepository(member_session), identities=UserIdentityRepository(member_session), hasher=FAST_HASHER,
        clock=clock, email_verification_required=False,
    )
    assert auth.login(email="person@example.com", password="my-new-password-123").id == first.user.id    # 密碼登入
    second = login_with(service)                                                                          # Google 登入仍然可用
    assert second.outcome == OUTCOME_LOGIN and second.user.id == first.user.id
    assert len(identities(member_session)) == 1


def test_registering_with_the_address_of_a_google_account_creates_nothing_and_sets_no_password(member_session):
    google_user = login_with(make_service(member_session)).user
    auth = AuthService(
        users=UserRepository(member_session), identities=UserIdentityRepository(member_session), hasher=FAST_HASHER,
        clock=FakeClock(), email_verification_required=False,
    )
    auth.register(email="person@example.com", password=GOOD_PASSWORD, display_name="冒名者", accept_terms=True, confirm_adult=True)
    assert len(users(member_session)) == 1
    assert users(member_session)[0].password_hash is None and users(member_session)[0].id == google_user.id


# =============================================================================
# 日誌
# =============================================================================

def test_events_are_logged_by_outcome_without_the_address_the_subject_or_the_state(member_session, log_lines):
    service = make_service(member_session)
    start = service.begin(accept_terms=True, confirm_adult=True)
    service.complete(code="c", state=start.transaction["s"], provider_error=None, transaction=start.transaction)
    login_with(service)                                                                     # 第二次：已綁定
    with pytest.raises(GoogleLoginRefused):
        service.complete(code="c", state="forged", provider_error=None, transaction=start.transaction)
    events = [line["event"] for line in log_lines() if "event" in line]
    assert events == [
        "auth.google_started", "auth.age_confirmed.google", "auth.google_registered", "auth.google_started",
        "auth.google_login", "auth.google_rejected.state",
    ]
    raw = log_lines.raw()
    for secret in ("person@example.com", SUB, start.transaction["s"], start.transaction["v"], start.transaction["n"]):
        assert secret not in raw
