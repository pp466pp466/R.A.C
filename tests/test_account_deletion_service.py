"""account-deletion 規格：申請與撤銷刪除的服務（確認本人、管理員、狀態、通知信、事件）。

用真的 Repository 與真的 `AuthService.confirm_password`（讓「密碼錯誤計入失敗並鎖定」被實際走過），假的只有時鐘與寄信器。
以 `member_session` 同時在 SQLite 與真實 SQL Server 上執行。
"""
from datetime import timedelta

import pytest

from app.models import User
from app.models.user import STATUS_ACTIVE, STATUS_DELETION_PENDING, STATUS_SUSPENDED
from app.repositories.user_repository import UserRepository
from app.services.account_deletion_service import CONFIRM_TEXT, AccountDeletionService
from app.services.current_user import CurrentUser
from app.services.errors import Conflict, Forbidden, ValidationError
from tests.support.auth import GOOD_PASSWORD, START, FakeClock, add_user, make_auth_service

GRACE_END = START + timedelta(days=7)


class Kit:
    def __init__(self, session) -> None:
        self.session = session
        self.clock = FakeClock()
        auth = make_auth_service(session, clock=self.clock)
        self.service = AccountDeletionService(
            users=UserRepository(session), confirm_password=auth.confirm_password, clock=self.clock,
        )

    def me(self, user: User) -> CurrentUser:
        self.session.expire_all()
        return CurrentUser.from_model(self.session.get(User, user.id))

    def row(self, user: User) -> User:
        self.session.expire_all()
        return self.session.get(User, user.id)


# ---- 確認本人 ---------------------------------------------------------------------

def test_an_account_with_a_password_confirms_by_entering_it(member_session):
    kit = Kit(member_session)
    user = add_user(member_session, 1)
    scheduled = kit.service.request(kit.me(user), password=GOOD_PASSWORD)
    assert scheduled == GRACE_END
    row = kit.row(user)
    assert (row.status, row.status_before_deletion, row.deletion_requested_at, row.token_version) == (
        STATUS_DELETION_PENDING, STATUS_ACTIVE, START, 2,
    )


def test_a_wrong_password_is_a_field_error_changes_nothing_and_counts_as_a_failed_login(member_session):
    kit = Kit(member_session)
    user = add_user(member_session, 1)
    with pytest.raises(ValidationError) as exc:
        kit.service.request(kit.me(user), password="definitely-not-the-password")
    assert "password" in exc.value.fields
    row = kit.row(user)
    assert row.status == STATUS_ACTIVE and row.deletion_requested_at is None and row.failed_login_count == 1


def test_five_wrong_passwords_lock_the_account_and_then_even_the_right_one_is_refused(member_session):
    kit = Kit(member_session)
    user = add_user(member_session, 1)
    for _ in range(5):
        with pytest.raises(ValidationError):
            kit.service.request(kit.me(user), password="wrong-wrong-wrong")
    assert kit.row(user).locked_until is not None
    with pytest.raises(ValidationError):
        kit.service.request(kit.me(user), password=GOOD_PASSWORD)
    assert kit.row(user).status == STATUS_ACTIVE


def test_an_account_with_a_password_cannot_confirm_with_the_text_alone(member_session):
    kit = Kit(member_session)
    user = add_user(member_session, 1)
    with pytest.raises(ValidationError) as exc:
        kit.service.request(kit.me(user), confirm_text=CONFIRM_TEXT)
    assert "password" in exc.value.fields and kit.row(user).status == STATUS_ACTIVE


def test_a_google_only_account_confirms_by_typing_the_fixed_text(member_session):
    kit = Kit(member_session)
    user = add_user(member_session, 1, password=None)
    assert kit.service.request(kit.me(user), confirm_text=CONFIRM_TEXT) == GRACE_END
    assert kit.row(user).status == STATUS_DELETION_PENDING


@pytest.mark.parametrize("typed", [f"　{CONFIRM_TEXT}　", f"  {CONFIRM_TEXT}\n"])
def test_the_confirmation_text_tolerates_surrounding_whitespace_including_full_width(member_session, typed):
    kit = Kit(member_session)
    user = add_user(member_session, 1, password=None)
    kit.service.request(kit.me(user), confirm_text=typed)
    assert kit.row(user).status == STATUS_DELETION_PENDING


@pytest.mark.parametrize("typed", [None, "", "刪除帳號", "刪除 我的帳號", "刪除我的帳號。", 123, ["刪除我的帳號"]])
def test_any_other_text_is_a_field_error_and_changes_nothing(member_session, typed):
    kit = Kit(member_session)
    user = add_user(member_session, 1, password=None)
    with pytest.raises(ValidationError) as exc:
        kit.service.request(kit.me(user), confirm_text=typed)
    assert list(exc.value.fields) == ["confirm_text"] and CONFIRM_TEXT in exc.value.fields["confirm_text"]
    assert kit.row(user).status == STATUS_ACTIVE


def test_a_google_only_account_ignores_a_password_field(member_session):
    kit = Kit(member_session)
    user = add_user(member_session, 1, password=None)
    with pytest.raises(ValidationError) as exc:
        kit.service.request(kit.me(user), password=GOOD_PASSWORD)
    assert list(exc.value.fields) == ["confirm_text"]


# ---- 誰可以申請 -------------------------------------------------------------------

def test_an_admin_cannot_delete_their_own_account_even_with_the_right_password(member_session):
    kit = Kit(member_session)
    admin = add_user(member_session, 1, role="admin")
    with pytest.raises(Forbidden) as exc:
        kit.service.request(kit.me(admin), password=GOOD_PASSWORD)
    assert exc.value.code == "admin_delete_forbidden"
    row = kit.row(admin)
    assert row.status == STATUS_ACTIVE and row.token_version == 1


@pytest.mark.parametrize("status", [STATUS_SUSPENDED, STATUS_DELETION_PENDING])
def test_a_suspended_or_already_pending_account_cannot_request(member_session, status):
    kit = Kit(member_session)
    user = add_user(member_session, 1, status=status, deletion_requested_at=START, status_before_deletion=STATUS_ACTIVE)
    with pytest.raises(Conflict) as exc:
        kit.service.request(kit.me(user), password=GOOD_PASSWORD)
    assert exc.value.code == "deletion_not_allowed"


def test_requesting_invalidates_every_existing_login(member_session):
    kit = Kit(member_session)
    user = add_user(member_session, 1)
    auth = make_auth_service(member_session, clock=kit.clock)
    assert auth.resolve_session(user.id, 1) is not None
    kit.service.request(kit.me(user), password=GOOD_PASSWORD)
    assert auth.resolve_session(user.id, 1) is None


# ---- 不寄信 -----------------------------------------------------------------------

def test_the_service_has_no_mailer_so_no_notice_can_be_sent(member_session):
    """玩家端不寄任何信（含申請刪除的通知）：服務不接受寄信元件，申請成功只回傳預定刪除的時間。"""
    import inspect

    assert "mailer" not in inspect.signature(AccountDeletionService).parameters
    kit = Kit(member_session)
    user = add_user(member_session, 1)
    assert kit.service.request(kit.me(user), password=GOOD_PASSWORD) == GRACE_END


# ---- 撤銷 -------------------------------------------------------------------------

def requested_user(session, n: int = 1, *, at=START, before=STATUS_ACTIVE) -> User:
    return add_user(session, n, status=STATUS_DELETION_PENDING, deletion_requested_at=at, status_before_deletion=before)


def test_cancelling_within_the_grace_period_restores_the_account_and_returns_a_fresh_snapshot(member_session):
    kit = Kit(member_session)
    user = requested_user(member_session)
    kit.clock.advance(days=3)
    snapshot = kit.service.cancel(kit.me(user))
    assert snapshot.status == STATUS_ACTIVE and snapshot.deletion_scheduled_at is None
    assert kit.row(user).status == STATUS_ACTIVE


def test_cancelling_after_seven_days_is_refused_and_the_account_stays_pending(member_session):
    kit = Kit(member_session)
    user = requested_user(member_session)
    kit.clock.advance(days=7)
    with pytest.raises(Conflict) as exc:
        kit.service.cancel(kit.me(user))
    assert exc.value.code == "grace_expired" and kit.row(user).status == STATUS_DELETION_PENDING


def test_cancelling_when_nothing_was_requested_is_a_conflict(member_session):
    kit = Kit(member_session)
    user = add_user(member_session, 1)
    with pytest.raises(Conflict) as exc:
        kit.service.cancel(kit.me(user))
    assert exc.value.code == "not_pending"


# ---- 事件 -------------------------------------------------------------------------

def test_events_carry_the_internal_id_only(member_session, log_lines):
    kit = Kit(member_session)
    user = add_user(member_session, 1, email="secret.person@example.com", email_canonical="secret.person@example.com", display_name="機密姓名")
    kit.service.request(kit.me(user), password=GOOD_PASSWORD)
    kit.clock.advance(days=1)
    kit.service.cancel(kit.me(user))
    events = [(line["event"], line["user_id"]) for line in log_lines() if line.get("event", "").startswith("account.deletion")]
    assert events == [("account.deletion_requested", user.id), ("account.deletion_cancelled", user.id)]
    for secret in ("secret.person@example.com", "機密姓名", GOOD_PASSWORD, CONFIRM_TEXT):
        assert secret not in log_lines.raw()
