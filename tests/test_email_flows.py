"""email-token-flows 規格：管理員重設密碼的權杖、頻率限制、延後寄信（Service 層）。

玩家只用 Google 登入：沒有信箱驗證、沒有玩家的忘記密碼，也不寄任何信給玩家；這裡只剩管理員在密碼驗證步驟按
「忘記密碼」之後的重設流程與密碼已變更的通知。HTTP 層見 test_email_flow_api.py 與 test_admin_two_step_login.py。
"""
import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuthToken, User
from app.models.user import STATUS_DELETION_PENDING, STATUS_SUSPENDED, TOKEN_RESET_PASSWORD
from app.repositories.auth_token_repository import AuthTokenRepository
from app.services.auth_service import AuthService
from app.services.email_flow_service import INVALID_RESET_LINK_MESSAGE, MAX_SENDS_PER_WINDOW, hash_token
from app.services.errors import Unauthenticated, ValidationError
from app.repositories.user_identity_repository import UserIdentityRepository
from app.repositories.user_repository import UserRepository
from tests.support.auth import FAST_HASHER, GOOD_PASSWORD, START, FakeClock, add_user
from tests.support.email_flow import BASE_URL, DeferQueue, FakeSender, make_flow

NEW_PASSWORD = "a-brand-new-password"


def reload(session, user: User) -> User:
    session.expire_all()
    return session.get(User, user.id)


def admin(session, n: int = 1, **overrides) -> User:
    return add_user(session, n, role="admin", **overrides)


def setup(session, **flow_kwargs):
    clock = flow_kwargs.pop("clock", None) or FakeClock()
    sender, queue = FakeSender(), DeferQueue()
    flow = make_flow(session, clock=clock, sender=sender, queue=queue, **flow_kwargs)
    return flow, sender, queue, clock


def request_reset(session, user, flow, queue):
    """管理員在密碼驗證步驟按「忘記密碼」：對象由簽章的中間狀態（uid、tv）決定。"""
    flow.request_admin_password_reset(user.id, session.get(User, user.id).token_version)
    queue.run()


# =============================================================================
# 權杖：只存雜湊、單次使用、有效期限、取代舊的
# =============================================================================

def test_the_database_holds_only_the_hash_never_the_token(member_session):
    user = admin(member_session)
    flow, sender, queue, _ = setup(member_session)
    request_reset(member_session, user, flow, queue)
    token = sender.last_token()
    (row,) = member_session.scalars(select(AuthToken)).all()
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest() == hash_token(token)
    assert row.purpose == TOKEN_RESET_PASSWORD
    assert token not in str(row.__dict__)
    assert len(token) >= 43                                    # 32 位元組隨機值的 base64 長度


def test_the_link_in_the_mail_uses_the_configured_base_url_and_a_fragment(member_session):
    user = admin(member_session)
    flow, sender, queue, _ = setup(member_session)
    request_reset(member_session, user, flow, queue)
    body = sender.sent[0]["body"]
    link = re.search(r"https?://\S+", body).group(0)
    assert link.startswith(f"{BASE_URL}/reset-password#token=")
    assert "?" not in link                                      # 權杖不在查詢字串
    assert sender.sent[0]["to"] == "user1@example.com"
    assert "1 小時" in body and "忽略這封信" in body


def test_a_token_works_once(member_session):
    user = admin(member_session)
    flow, sender, queue, _ = setup(member_session)
    request_reset(member_session, user, flow, queue)
    token = sender.last_token()
    flow.reset_password(token, NEW_PASSWORD)
    with pytest.raises(ValidationError) as again:
        flow.reset_password(token, "yet-another-password-1")
    assert again.value.code == "invalid_token"


def test_a_reset_token_lives_one_hour_not_a_day(member_session):
    user = admin(member_session)
    flow, sender, queue, clock = setup(member_session)
    request_reset(member_session, user, flow, queue)
    token = sender.last_token()
    clock.advance(minutes=59)
    flow.reset_password(token, NEW_PASSWORD)                     # 1 小時內仍有效
    request_reset(member_session, user, flow, queue)
    stale = sender.last_token()
    clock.advance(minutes=61)
    with pytest.raises(ValidationError):
        flow.reset_password(stale, "yet-another-password-1")     # 超過 1 小時


def test_a_new_token_replaces_the_old_one(member_session):
    user = admin(member_session)
    flow, sender, queue, clock = setup(member_session)
    request_reset(member_session, user, flow, queue)
    first = sender.last_token()
    clock.advance(minutes=2)
    request_reset(member_session, user, flow, queue)
    second = sender.last_token()
    assert first != second
    with pytest.raises(ValidationError):
        flow.reset_password(first, NEW_PASSWORD)
    flow.reset_password(second, NEW_PASSWORD)


@pytest.mark.parametrize("bad", [None, "", "x" * 201, 12345, ["a"], {"t": 1}, "not-a-real-token", "../../etc/passwd"])
def test_garbage_tokens_are_all_the_same_generic_error(member_session, bad):
    admin(member_session)
    flow, *_ = setup(member_session)
    with pytest.raises(ValidationError) as reset:
        flow.reset_password(bad, NEW_PASSWORD)
    assert (reset.value.code, reset.value.message) == ("invalid_token", INVALID_RESET_LINK_MESSAGE)


def test_a_reset_token_that_belongs_to_a_player_is_as_invalid_as_a_garbage_one_and_changes_nothing(member_session):
    """遷移前遺留的玩家重設權杖：回應與無效權杖相同，密碼不變（玩家不持有密碼）。"""
    player = add_user(member_session, 1, password=None)
    flow, *_ = setup(member_session)
    AuthTokenRepository(member_session).add_token(
        user_id=player.id, purpose=TOKEN_RESET_PASSWORD, token_hash=hash_token("legacy-player-token"),
        created_at=START, expires_at=START + timedelta(hours=1),
    )
    with pytest.raises(ValidationError) as legacy:
        flow.reset_password("legacy-player-token", NEW_PASSWORD)
    assert (legacy.value.code, legacy.value.message) == ("invalid_token", INVALID_RESET_LINK_MESSAGE)
    after = reload(member_session, player)
    assert after.password_hash is None and after.token_version == 1


def test_two_simultaneous_uses_of_one_token_let_only_one_through(concurrent_engine):
    with Session(concurrent_engine) as s:
        user = admin(s)
        flow, sender, queue, _ = setup(s)
        flow.request_admin_password_reset(user.id, user.token_version)
        s.commit()
        queue.run()
        token = sender.last_token()
    barrier = Barrier(2)

    def attempt(_):
        with Session(concurrent_engine) as s:
            flow, *_ = setup(s)
            barrier.wait(timeout=30)
            try:
                flow.reset_password(token, NEW_PASSWORD)
            except ValidationError:
                s.rollback()
                return "rejected"
            s.commit()
            return "reset"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result(timeout=60) for f in [pool.submit(attempt, i) for i in range(2)]]
    assert sorted(results) == ["rejected", "reset"]


# =============================================================================
# 要求重設：對象由中間狀態決定；頻率限制；停用的帳號不寄
# =============================================================================

def test_reset_mails_are_limited_by_a_60_second_gap(member_session):
    user = admin(member_session)
    flow, sender, queue, clock = setup(member_session)
    request_reset(member_session, user, flow, queue)
    assert len(sender.sent) == 1
    clock.advance(seconds=59)
    request_reset(member_session, user, flow, queue)           # 回應相同，只是不寄
    assert len(sender.sent) == 1
    clock.advance(seconds=2)
    request_reset(member_session, user, flow, queue)
    assert len(sender.sent) == 2


def test_at_most_five_mails_per_hour(member_session):
    user = admin(member_session)
    flow, sender, queue, clock = setup(member_session)
    for _ in range(MAX_SENDS_PER_WINDOW):
        request_reset(member_session, user, flow, queue)
        clock.advance(seconds=61)
    assert len(sender.sent) == 5
    request_reset(member_session, user, flow, queue)           # 第 6 封，距第一封還不到一小時
    assert len(sender.sent) == 5
    clock.advance(minutes=56)                                   # 第一封已超過一小時
    request_reset(member_session, user, flow, queue)
    assert len(sender.sent) == 6


def test_the_limits_are_per_user(member_session):
    a, b = admin(member_session, 1), admin(member_session, 2)
    flow, sender, queue, _ = setup(member_session)
    request_reset(member_session, a, flow, queue)
    request_reset(member_session, b, flow, queue)               # 另一位管理員不受影響
    assert len(sender.sent) == 2


def test_a_middle_state_that_does_not_match_an_admin_sends_nothing(member_session):
    admin_user = admin(member_session, 1)
    player = add_user(member_session, 2)
    flow, sender, queue, _ = setup(member_session)
    flow.request_admin_password_reset(player.id, 1)             # 不是管理員
    flow.request_admin_password_reset(admin_user.id, 99)         # token_version 已變
    flow.request_admin_password_reset(999999, 1)                 # 不存在
    queue.run()
    assert sender.sent == []


@pytest.mark.parametrize("status", [STATUS_SUSPENDED, STATUS_DELETION_PENDING])
def test_blocked_accounts_get_no_reset_mail(member_session, status):
    user = admin(member_session, status=status)
    flow, sender, queue, _ = setup(member_session)
    request_reset(member_session, user, flow, queue)
    assert sender.sent == []


# =============================================================================
# 重設密碼
# =============================================================================

def test_a_successful_reset_changes_the_password_revokes_sessions_clears_the_lock_and_sends_a_notice(member_session):
    user = admin(member_session, failed_login_count=5, locked_until=START + timedelta(minutes=10))
    flow, sender, queue, _ = setup(member_session)
    request_reset(member_session, user, flow, queue)
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


def test_after_a_reset_the_new_password_passes_the_admin_password_step_and_the_old_one_does_not(member_session):
    user = admin(member_session)
    flow, sender, queue, clock = setup(member_session)
    request_reset(member_session, user, flow, queue)
    flow.reset_password(sender.last_token(), NEW_PASSWORD)
    auth = AuthService(
        users=UserRepository(member_session), identities=UserIdentityRepository(member_session), hasher=FAST_HASHER, clock=clock,
    )
    version = reload(member_session, user).token_version
    with pytest.raises(Unauthenticated):
        auth.verify_admin_password(user.id, version, GOOD_PASSWORD)                    # 舊密碼不能再用
    assert auth.verify_admin_password(user.id, version, NEW_PASSWORD).id == user.id
    assert auth.resolve_session(user.id, 1) is None                                     # 重設前的登入狀態失效


def test_a_weak_new_password_is_refused_without_burning_the_token(member_session):
    user = admin(member_session)
    flow, sender, queue, _ = setup(member_session)
    request_reset(member_session, user, flow, queue)
    token = sender.last_token()
    with pytest.raises(ValidationError) as exc:
        flow.reset_password(token, "short")
    assert exc.value.fields["new_password"] == "密碼長度須為 8 至 128 字。"
    flow.reset_password(token, NEW_PASSWORD)                      # 同一個權杖仍然可用


def test_resetting_invalidates_other_unused_reset_tokens(member_session):
    user = admin(member_session)
    flow, sender, queue, clock = setup(member_session)
    request_reset(member_session, user, flow, queue)
    first = sender.last_token()
    clock.advance(minutes=2)
    request_reset(member_session, user, flow, queue)
    second = sender.last_token()
    flow.reset_password(second, NEW_PASSWORD)
    with pytest.raises(ValidationError):
        flow.reset_password(first, "yet-another-password-1")


# =============================================================================
# 提交成功後才寄信；寄信失敗不影響結果
# =============================================================================

def test_nothing_is_sent_until_the_deferred_actions_run_and_a_rollback_discards_them(member_session):
    user = admin(member_session)
    flow, sender, queue, _ = setup(member_session)
    flow.request_admin_password_reset(user.id, user.token_version)
    assert sender.sent == [] and len(queue.actions) == 1       # 只登記，還沒寄
    queue.discard()                                             # 交易回復：丟棄
    queue.run()
    assert sender.sent == []


def test_a_failing_mail_server_is_logged_without_the_address_or_token_and_does_not_raise(member_session, log_lines):
    user = admin(member_session)
    flow, sender, queue, _ = setup(member_session, request_id="req-abcdef123456")
    sender.fail = True
    flow.request_admin_password_reset(user.id, user.token_version)
    queue.run()                                                 # 不拋出
    failed = [line for line in log_lines() if line.get("event") == "mail.send_failed"]
    assert len(failed) == 1 and failed[0]["request_id"] == "req-abcdef123456"
    raw = log_lines.raw()
    assert "user1@example.com" not in raw and "s3cret-should-never-be-logged" not in raw
    assert len(member_session.scalars(select(AuthToken)).all()) == 1       # 權杖仍已發放，使用者可以重試


def test_log_events_for_the_flow_never_contain_addresses_or_tokens(member_session, log_lines):
    user = admin(member_session)
    flow, sender, queue, clock = setup(member_session)
    request_reset(member_session, user, flow, queue)
    reset_token = sender.last_token()
    flow.reset_password(reset_token, NEW_PASSWORD)
    with pytest.raises(ValidationError):
        flow.reset_password("no-such-token", NEW_PASSWORD)
    events = [line["event"] for line in log_lines() if "event" in line]
    assert events == ["auth.password_reset_requested", "auth.password_reset", "auth.email_token_rejected"]
    raw = log_lines.raw()
    for secret in ("user1@example.com", reset_token, NEW_PASSWORD):
        assert secret not in raw
