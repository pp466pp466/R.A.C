"""email-token-flows 規格：權杖、信箱驗證、重寄、忘記與重設密碼、延後寄信（Service 層）。"""
import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuthToken, User
from app.models.user import (
    STATUS_ACTIVE, STATUS_DELETION_PENDING, STATUS_PENDING_VERIFICATION, STATUS_SUSPENDED,
)
from app.services.auth_service import AuthService
from app.services.current_user import CurrentUser
from app.services.email_flow_service import (
    INVALID_RESET_LINK_MESSAGE, INVALID_VERIFY_LINK_MESSAGE, MAX_SENDS_PER_WINDOW, hash_token,
)
from app.services.errors import TooManyRequests, Unauthenticated, ValidationError
from app.repositories.user_identity_repository import UserIdentityRepository
from app.repositories.user_repository import UserRepository
from tests.support.auth import FAST_HASHER, GOOD_PASSWORD, START, FakeClock, add_user
from tests.support.email_flow import BASE_URL, TOKEN_PATTERN, DeferQueue, FakeSender, make_flow

NEW_PASSWORD = "a-brand-new-password"


def reload(session, user: User) -> User:
    session.expire_all()
    return session.get(User, user.id)


def setup(session, **flow_kwargs):
    clock = flow_kwargs.pop("clock", None) or FakeClock()
    sender, queue = FakeSender(), DeferQueue()
    flow = make_flow(session, clock=clock, sender=sender, queue=queue, **flow_kwargs)
    return flow, sender, queue, clock


def request_verification(session, user, flow, queue):
    flow.send_verification(session.get(User, user.id))
    queue.run()


# =============================================================================
# 權杖：只存雜湊、單次使用、有效期限、取代舊的
# =============================================================================

def test_the_database_holds_only_the_hash_never_the_token(member_session):
    user = add_user(member_session, 1, status=STATUS_PENDING_VERIFICATION)
    flow, sender, queue, _ = setup(member_session)
    request_verification(member_session, user, flow, queue)
    token = sender.last_token()
    (row,) = member_session.scalars(select(AuthToken)).all()
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest() == hash_token(token)
    assert token not in str(row.__dict__)
    assert len(token) >= 43                                    # 32 位元組隨機值的 base64 長度


def test_the_link_in_the_mail_uses_the_configured_base_url_and_a_fragment(member_session):
    user = add_user(member_session, 1, status=STATUS_PENDING_VERIFICATION)
    flow, sender, queue, _ = setup(member_session)
    request_verification(member_session, user, flow, queue)
    body = sender.sent[0]["body"]
    link = re.search(r"https?://\S+", body).group(0)
    assert link.startswith(f"{BASE_URL}/verify-email#token=")
    assert "?" not in link                                      # 權杖不在查詢字串
    assert sender.sent[0]["to"] == "user1@example.com"


def test_a_token_works_once(member_session):
    user = add_user(member_session, 1, status=STATUS_PENDING_VERIFICATION)
    flow, sender, queue, _ = setup(member_session)
    request_verification(member_session, user, flow, queue)
    token = sender.last_token()
    flow.verify_email(token)
    with pytest.raises(ValidationError) as exc:
        flow.verify_email(token)
    assert exc.value.code == "invalid_token"


def test_a_verification_token_lives_24_hours(member_session):
    user = add_user(member_session, 1, status=STATUS_PENDING_VERIFICATION)
    flow, sender, queue, clock = setup(member_session)
    request_verification(member_session, user, flow, queue)
    token = sender.last_token()
    clock.advance(hours=23, minutes=59)
    flow.verify_email(token)                                    # 24 小時內仍有效


def test_a_verification_token_is_refused_after_24_hours(member_session):
    user = add_user(member_session, 1, status=STATUS_PENDING_VERIFICATION)
    flow, sender, queue, clock = setup(member_session)
    request_verification(member_session, user, flow, queue)
    token = sender.last_token()
    clock.advance(hours=24, seconds=1)
    with pytest.raises(ValidationError):
        flow.verify_email(token)
    assert reload(member_session, user).status == STATUS_PENDING_VERIFICATION       # 帳號不變


def test_a_reset_token_lives_one_hour_not_a_day(member_session):
    add_user(member_session, 1)
    flow, sender, queue, clock = setup(member_session)
    flow.request_password_reset("user1@example.com")
    queue.run()
    token = sender.last_token()
    clock.advance(minutes=59)
    flow.reset_password(token, NEW_PASSWORD)                     # 1 小時內仍有效
    flow.request_password_reset("user1@example.com")
    queue.run()
    stale = sender.last_token()
    clock.advance(minutes=61)
    with pytest.raises(ValidationError):
        flow.reset_password(stale, "yet-another-password-1")     # 超過 1 小時


def test_a_verification_token_is_not_cut_short_to_the_reset_lifetime(member_session):
    user = add_user(member_session, 1, status=STATUS_PENDING_VERIFICATION)
    flow, sender, queue, clock = setup(member_session)
    request_verification(member_session, user, flow, queue)
    token = sender.last_token()
    clock.advance(minutes=61)                                    # 超過重設權杖的壽命，驗證權杖仍有效
    flow.verify_email(token)


def test_a_new_token_replaces_the_old_one(member_session):
    user = add_user(member_session, 1, status=STATUS_PENDING_VERIFICATION)
    flow, sender, queue, clock = setup(member_session)
    request_verification(member_session, user, flow, queue)
    first = sender.last_token()
    clock.advance(minutes=2)
    request_verification(member_session, user, flow, queue)
    second = sender.last_token()
    assert first != second
    with pytest.raises(ValidationError):
        flow.verify_email(first)
    flow.verify_email(second)


def test_tokens_cannot_be_used_for_the_other_purpose(member_session):
    user = add_user(member_session, 1, status=STATUS_PENDING_VERIFICATION)
    flow, sender, queue, _ = setup(member_session)
    request_verification(member_session, user, flow, queue)
    verify_token = sender.last_token()
    flow.request_password_reset("user1@example.com")
    queue.run()
    reset_token = sender.last_token()
    with pytest.raises(ValidationError):
        flow.reset_password(verify_token, NEW_PASSWORD)          # 驗證權杖不能重設密碼
    with pytest.raises(ValidationError):
        flow.verify_email(reset_token)                           # 重設權杖不能驗證信箱
    flow.verify_email(verify_token)                              # 前面被拒絕的嘗試沒有燒掉它們
    flow.reset_password(reset_token, NEW_PASSWORD)


@pytest.mark.parametrize("bad", [None, "", "x" * 201, 12345, ["a"], {"t": 1}, "not-a-real-token", "../../etc/passwd"])
def test_garbage_tokens_are_all_the_same_generic_error(member_session, bad):
    add_user(member_session, 1)
    flow, *_ = setup(member_session)
    with pytest.raises(ValidationError) as verify:
        flow.verify_email(bad)
    with pytest.raises(ValidationError) as reset:
        flow.reset_password(bad, NEW_PASSWORD)
    assert (verify.value.code, verify.value.message) == ("invalid_token", INVALID_VERIFY_LINK_MESSAGE)
    assert (reset.value.code, reset.value.message) == ("invalid_token", INVALID_RESET_LINK_MESSAGE)


def test_two_simultaneous_uses_of_one_token_let_only_one_through(concurrent_engine):
    with Session(concurrent_engine) as s:
        user = add_user(s, 1, status=STATUS_PENDING_VERIFICATION)
        flow, sender, queue, _ = setup(s)
        flow.send_verification(s.get(User, user.id))
        s.commit()
        queue.run()
        token = sender.last_token()
    barrier = Barrier(2)

    def attempt(_):
        with Session(concurrent_engine) as s:
            flow, *_ = setup(s)
            barrier.wait(timeout=30)
            try:
                flow.verify_email(token)
            except ValidationError:
                s.rollback()
                return "rejected"
            s.commit()
            return "verified"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result(timeout=60) for f in [pool.submit(attempt, i) for i in range(2)]]
    assert sorted(results) == ["rejected", "verified"]


# =============================================================================
# 信箱驗證
# =============================================================================

def test_verifying_activates_a_pending_member_records_the_time_and_forces_a_new_login(member_session):
    user = add_user(member_session, 1, status=STATUS_PENDING_VERIFICATION)
    flow, sender, queue, clock = setup(member_session)
    request_verification(member_session, user, flow, queue)
    clock.advance(hours=3)
    flow.verify_email(sender.last_token())
    verified = reload(member_session, user)
    assert verified.status == STATUS_ACTIVE
    assert verified.email_verified_at == START + timedelta(hours=3)
    assert verified.token_version == 2


def test_verifying_an_active_unverified_member_only_records_the_time(member_session):
    user = add_user(member_session, 1)                         # 驗證關閉期間註冊：active、未驗證
    flow, sender, queue, _ = setup(member_session)
    request_verification(member_session, user, flow, queue)
    flow.verify_email(sender.last_token())
    verified = reload(member_session, user)
    assert verified.status == STATUS_ACTIVE and verified.email_verified_at is not None


def test_verification_does_not_change_the_time_of_an_already_verified_member(member_session):
    user = add_user(member_session, 1, email_verified_at=START - timedelta(days=30))
    flow, sender, queue, _ = setup(member_session)
    request_verification(member_session, user, flow, queue)
    flow.verify_email(sender.last_token())
    assert reload(member_session, user).email_verified_at == START - timedelta(days=30)


# =============================================================================
# 重寄驗證信與頻率限制
# =============================================================================

def current_of(user) -> CurrentUser:
    return CurrentUser.from_model(user)


def test_resend_sends_a_new_mail_then_limits_by_a_60_second_gap(member_session):
    user = add_user(member_session, 1, status=STATUS_PENDING_VERIFICATION)
    flow, sender, queue, clock = setup(member_session)
    current = current_of(user)
    flow.resend_verification(current)
    queue.run()
    assert len(sender.sent) == 1
    clock.advance(seconds=59)
    with pytest.raises(TooManyRequests):
        flow.resend_verification(current)
    queue.run()
    assert len(sender.sent) == 1                               # 受限時不寄信
    clock.advance(seconds=2)
    flow.resend_verification(current)
    queue.run()
    assert len(sender.sent) == 2


def test_at_most_five_mails_per_hour_for_the_same_purpose(member_session):
    user = add_user(member_session, 1, status=STATUS_PENDING_VERIFICATION)
    flow, sender, queue, clock = setup(member_session)
    current = current_of(user)
    for _ in range(MAX_SENDS_PER_WINDOW):
        flow.resend_verification(current)
        clock.advance(seconds=61)
    queue.run()
    assert len(sender.sent) == 5
    with pytest.raises(TooManyRequests):
        flow.resend_verification(current)                       # 第 6 封，距第一封還不到一小時
    clock.advance(minutes=56)                                   # 第一封已超過一小時
    flow.resend_verification(current)


def test_the_limits_are_per_user_and_per_purpose(member_session):
    a = add_user(member_session, 1, status=STATUS_PENDING_VERIFICATION)
    b = add_user(member_session, 2, status=STATUS_PENDING_VERIFICATION)
    flow, sender, queue, _ = setup(member_session)
    flow.resend_verification(current_of(a))
    flow.resend_verification(current_of(b))                     # 另一個人不受影響
    flow.request_password_reset("user1@example.com")            # 同一個人的另一種用途不受影響
    queue.run()
    assert len(sender.sent) == 3


def test_resend_for_a_verified_member_is_refused_and_sends_nothing(member_session):
    user = add_user(member_session, 1, email_verified_at=START)
    flow, sender, queue, _ = setup(member_session)
    with pytest.raises(ValidationError) as exc:
        flow.resend_verification(current_of(user))
    assert exc.value.code == "already_verified"
    queue.run()
    assert sender.sent == []


def test_a_member_that_no_longer_exists_is_treated_as_logged_out(member_session):
    user = add_user(member_session, 1)
    current = current_of(user)
    flow, *_ = setup(member_session)
    member_session.delete(member_session.get(User, user.id))
    member_session.commit()
    with pytest.raises(Unauthenticated):
        flow.resend_verification(current)


# =============================================================================
# 忘記密碼與重設密碼
# =============================================================================

def test_forgot_password_for_an_unknown_address_sends_nothing_and_looks_the_same(member_session):
    add_user(member_session, 1)
    flow, sender, queue, _ = setup(member_session)
    assert flow.request_password_reset("nobody@example.com") is None
    assert flow.request_password_reset("user1@example.com") is None
    queue.run()
    assert [m["to"] for m in sender.sent] == ["user1@example.com"]


@pytest.mark.parametrize("status", [STATUS_SUSPENDED, STATUS_DELETION_PENDING])
def test_blocked_accounts_get_no_reset_mail_but_the_answer_is_the_same(member_session, status):
    add_user(member_session, 1, status=status)
    flow, sender, queue, _ = setup(member_session)
    assert flow.request_password_reset("user1@example.com") is None
    queue.run()
    assert sender.sent == []


def test_a_rate_limited_forgot_password_stays_silent(member_session):
    add_user(member_session, 1)
    flow, sender, queue, clock = setup(member_session)
    flow.request_password_reset("user1@example.com")
    clock.advance(seconds=10)
    assert flow.request_password_reset("user1@example.com") is None      # 不拋錯、不洩漏「被限制了」
    queue.run()
    assert len(sender.sent) == 1


def test_forgot_password_validates_the_shape_of_the_address_only(member_session):
    flow, *_ = setup(member_session)
    for bad in (None, "", "not an address", 42):
        with pytest.raises(ValidationError) as exc:
            flow.request_password_reset(bad)
        assert "email" in exc.value.fields


def test_forgot_password_finds_the_account_through_a_gmail_alias_and_mails_the_stored_address(member_session):
    add_user(member_session, 1, email="john.doe@gmail.com", email_canonical="johndoe@gmail.com")
    flow, sender, queue, _ = setup(member_session)
    flow.request_password_reset("JohnDoe+shopping@googlemail.com")
    queue.run()
    assert [m["to"] for m in sender.sent] == ["john.doe@gmail.com"]


def test_a_successful_reset_changes_the_password_revokes_sessions_clears_the_lock_and_sends_a_notice(member_session):
    user = add_user(member_session, 1, failed_login_count=5, locked_until=START + timedelta(minutes=10))
    flow, sender, queue, _ = setup(member_session)
    flow.request_password_reset("user1@example.com")
    queue.run()
    token = sender.last_token()
    flow.reset_password(token, NEW_PASSWORD)
    queue.run()
    after = reload(member_session, user)
    assert FAST_HASHER.verify(after.password_hash, NEW_PASSWORD) and not FAST_HASHER.verify(after.password_hash, GOOD_PASSWORD)
    assert after.token_version == 2
    assert after.failed_login_count == 0 and after.locked_until is None
    assert "已經變更" in sender.sent[-1]["body"] and sender.sent[-1]["to"] == "user1@example.com"
    assert token not in sender.sent[-1]["body"]
    with pytest.raises(ValidationError):
        flow.reset_password(token, "yet-another-password-1")     # 權杖不能重複使用


def test_a_google_only_account_can_set_a_password_through_the_reset_flow(member_session):
    user = add_user(member_session, 1, password=None, email_verified_at=START)
    flow, sender, queue, _ = setup(member_session)
    flow.request_password_reset("user1@example.com")
    queue.run()
    flow.reset_password(sender.last_token(), NEW_PASSWORD)
    assert FAST_HASHER.verify(reload(member_session, user).password_hash, NEW_PASSWORD)


def test_resetting_proves_control_of_the_mailbox_so_a_pending_account_becomes_active_and_verified(member_session):
    user = add_user(member_session, 1, status=STATUS_PENDING_VERIFICATION, password="the-attackers-password")
    flow, sender, queue, _ = setup(member_session)
    flow.request_password_reset("user1@example.com")
    queue.run()
    flow.reset_password(sender.last_token(), NEW_PASSWORD)
    healed = reload(member_session, user)
    assert healed.status == STATUS_ACTIVE and healed.email_verified_at is not None
    assert not FAST_HASHER.verify(healed.password_hash, "the-attackers-password")     # 預先佔用者的密碼被取代
    assert healed.token_version == 2                                                   # 預先佔用者的登入狀態失效


def test_a_weak_new_password_is_refused_without_burning_the_token(member_session):
    add_user(member_session, 1)
    flow, sender, queue, _ = setup(member_session)
    flow.request_password_reset("user1@example.com")
    queue.run()
    token = sender.last_token()
    with pytest.raises(ValidationError) as exc:
        flow.reset_password(token, "short")
    assert exc.value.fields["new_password"] == "密碼長度須為 8 至 128 字。"
    flow.reset_password(token, NEW_PASSWORD)                      # 同一個權杖仍然可用


def test_resetting_invalidates_other_unused_reset_tokens(member_session):
    add_user(member_session, 1)
    flow, sender, queue, clock = setup(member_session)
    flow.request_password_reset("user1@example.com")
    queue.run()
    first = sender.last_token()
    clock.advance(minutes=2)
    flow.request_password_reset("user1@example.com")
    queue.run()
    second = sender.last_token()
    flow.reset_password(second, NEW_PASSWORD)
    with pytest.raises(ValidationError):
        flow.reset_password(first, "yet-another-password-1")


# =============================================================================
# 提交成功後才寄信；寄信失敗不影響結果
# =============================================================================

def test_nothing_is_sent_until_the_deferred_actions_run_and_a_rollback_discards_them(member_session):
    user = add_user(member_session, 1, status=STATUS_PENDING_VERIFICATION)
    flow, sender, queue, _ = setup(member_session)
    flow.send_verification(member_session.get(User, user.id))
    assert sender.sent == [] and len(queue.actions) == 1       # 只登記，還沒寄
    queue.discard()                                             # 交易回復：丟棄
    queue.run()
    assert sender.sent == []


def test_a_failing_mail_server_is_logged_without_the_address_or_token_and_does_not_raise(member_session, log_lines):
    user = add_user(member_session, 1, status=STATUS_PENDING_VERIFICATION)
    flow, sender, queue, _ = setup(member_session, request_id="req-abcdef123456")
    sender.fail = True
    flow.send_verification(member_session.get(User, user.id))
    queue.run()                                                 # 不拋出
    failed = [line for line in log_lines() if line.get("event") == "mail.send_failed"]
    assert len(failed) == 1 and failed[0]["request_id"] == "req-abcdef123456"
    raw = log_lines.raw()
    assert "user1@example.com" not in raw and "s3cret-should-never-be-logged" not in raw
    assert len(member_session.scalars(select(AuthToken)).all()) == 1       # 權杖仍已發放，使用者可以重寄


def test_log_events_for_the_flows_never_contain_addresses_or_tokens(member_session, log_lines):
    user = add_user(member_session, 1, status=STATUS_PENDING_VERIFICATION)
    flow, sender, queue, clock = setup(member_session)
    request_verification(member_session, user, flow, queue)
    verify_token = sender.last_token()
    flow.verify_email(verify_token)
    clock.advance(minutes=5)
    flow.request_password_reset("user1@example.com")
    queue.run()
    reset_token = sender.last_token()
    flow.reset_password(reset_token, NEW_PASSWORD)
    with pytest.raises(ValidationError):
        flow.reset_password("no-such-token", NEW_PASSWORD)
    events = [line["event"] for line in log_lines() if "event" in line]
    assert events == [
        "auth.verification_sent", "auth.email_verified", "auth.password_reset_requested", "auth.password_reset",
        "auth.email_token_rejected",
    ]
    raw = log_lines.raw()
    for secret in ("user1@example.com", verify_token, reset_token, NEW_PASSWORD):
        assert secret not in raw


# =============================================================================
# 與註冊串接：真實的 EmailFlowService 當作 AuthService 的寄信元件
# =============================================================================

def test_registering_with_verification_on_sends_a_mail_and_the_link_completes_the_account(member_session):
    flow, sender, queue, clock = setup(member_session)
    auth = AuthService(
        users=UserRepository(member_session), identities=UserIdentityRepository(member_session), hasher=FAST_HASHER,
        clock=clock, email_verification_required=True, mailer=flow,
    )
    auth.register(email="New.Person@Example.com", password=GOOD_PASSWORD, display_name="新朋友", accept_terms=True, confirm_adult=True)
    assert sender.sent == []                                    # 還沒提交，不寄
    queue.run()
    assert [m["to"] for m in sender.sent] == ["new.person@example.com"]
    assert "新朋友" in sender.sent[0]["body"] and "24 小時" in sender.sent[0]["body"]

    member = member_session.query(User).one()
    assert member.status == STATUS_PENDING_VERIFICATION
    flow.verify_email(sender.last_token())
    assert reload(member_session, member).status == STATUS_ACTIVE
    assert auth.login(email="new.person@example.com", password=GOOD_PASSWORD).email_verified is True


def test_the_pre_hijack_scenario_heals_itself(member_session):
    """攻擊者先用受害者的信箱註冊；受害者再註冊（密碼不被覆蓋）；受害者改走忘記密碼，攻擊者從此無法登入。"""
    flow, sender, queue, clock = setup(member_session)
    auth = AuthService(
        users=UserRepository(member_session), identities=UserIdentityRepository(member_session), hasher=FAST_HASHER,
        clock=clock, email_verification_required=True, mailer=flow,
    )
    auth.register(email="victim@example.com", password="attackers-chosen-pw", display_name="攻擊者", accept_terms=True, confirm_adult=True)
    queue.run()
    attacker_session = auth.login(email="victim@example.com", password="attackers-chosen-pw")   # 待驗證帳號可登入

    clock.advance(minutes=5)
    auth.register(email="victim@example.com", password="victims-real-password", display_name="受害者", accept_terms=True, confirm_adult=True)
    queue.run()
    with pytest.raises(Unauthenticated):
        auth.login(email="victim@example.com", password="victims-real-password")   # 原密碼沒被覆蓋，受害者登入失敗

    clock.advance(minutes=5)
    flow.request_password_reset("victim@example.com")
    queue.run()
    flow.reset_password(sender.last_token(), "victims-real-password")
    assert auth.resolve_session(attacker_session.id, attacker_session.token_version) is None      # 攻擊者的登入狀態失效
    with pytest.raises(Unauthenticated):
        auth.login(email="victim@example.com", password="attackers-chosen-pw")
    assert auth.login(email="victim@example.com", password="victims-real-password").email_verified is True
