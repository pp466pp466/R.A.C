"""account-deletion 規格：申請與撤銷刪除的服務（確認本人、管理員、狀態、通知信、事件、匿名保留偏好的一併寫入）。

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


# ---- 刪除視窗的「匿名保留我已發佈的劇本」（script-anonymous-retention 規格，任務 4.1）---------------------------

def keep_of(kit: Kit, user: User) -> bool:
    return kit.row(user).keep_published_scripts


@pytest.mark.parametrize("before", [False, True], ids=["off-to-on", "on-to-on"])
def test_keep_true_is_written_together_with_the_request(member_session, before):
    kit = Kit(member_session)
    user = add_user(member_session, 1, keep_published_scripts=before)
    kit.service.request(kit.me(user), password=GOOD_PASSWORD, keep_published_scripts=True)
    row = kit.row(user)
    assert row.keep_published_scripts is True and row.status == STATUS_DELETION_PENDING and row.deletion_requested_at == START


def test_keep_false_overwrites_an_earlier_on(member_session):
    kit = Kit(member_session)
    user = add_user(member_session, 1, keep_published_scripts=True)
    kit.service.request(kit.me(user), password=GOOD_PASSWORD, keep_published_scripts=False)
    row = kit.row(user)
    assert row.keep_published_scripts is False and row.status == STATUS_DELETION_PENDING


@pytest.mark.parametrize("before", [False, True], ids=["off", "on"])
def test_leaving_the_field_out_does_not_touch_the_preference(member_session, before):
    kit = Kit(member_session)
    user = add_user(member_session, 1, keep_published_scripts=before)
    kit.service.request(kit.me(user), password=GOOD_PASSWORD)
    assert keep_of(kit, user) is before


def test_a_google_only_account_writes_it_with_the_confirmation_text(member_session):
    kit = Kit(member_session)
    user = add_user(member_session, 1, password=None)
    kit.service.request(kit.me(user), confirm_text=CONFIRM_TEXT, keep_published_scripts=True)
    assert keep_of(kit, user) is True


def test_a_wrong_password_does_not_write_the_preference(member_session):
    kit = Kit(member_session)
    user = add_user(member_session, 1)
    with pytest.raises(ValidationError) as exc:
        kit.service.request(kit.me(user), password="definitely-not-the-password", keep_published_scripts=True)
    assert "password" in exc.value.fields
    row = kit.row(user)
    assert row.keep_published_scripts is False and row.status == STATUS_ACTIVE and row.failed_login_count == 1


def test_a_wrong_confirmation_text_does_not_write_the_preference(member_session):
    kit = Kit(member_session)
    user = add_user(member_session, 1, password=None)
    with pytest.raises(ValidationError) as exc:
        kit.service.request(kit.me(user), confirm_text="不對", keep_published_scripts=True)
    assert list(exc.value.fields) == ["confirm_text"]
    assert keep_of(kit, user) is False and kit.row(user).status == STATUS_ACTIVE


@pytest.mark.parametrize("value", ["true", "yes", 1, 0, "", [True], {"keep": True}], ids=repr)
def test_a_non_boolean_is_a_field_error_before_the_password_is_checked(member_session, value):
    """型別錯誤是請求本身的錯，不是「確認本人失敗」：不計入登入失敗，也不會因此鎖定。"""
    kit = Kit(member_session)
    user = add_user(member_session, 1)
    with pytest.raises(ValidationError) as exc:
        kit.service.request(kit.me(user), password="definitely-not-the-password", keep_published_scripts=value)
    assert list(exc.value.fields) == ["keep_published_scripts"]
    row = kit.row(user)
    assert row.failed_login_count == 0 and row.keep_published_scripts is False and row.status == STATUS_ACTIVE


def test_an_admin_is_refused_before_anything_is_written(member_session):
    kit = Kit(member_session)
    admin = add_user(member_session, 1, role="admin")
    with pytest.raises(Forbidden):
        kit.service.request(kit.me(admin), password=GOOD_PASSWORD, keep_published_scripts=True)
    assert keep_of(kit, admin) is False


@pytest.mark.parametrize("status", [STATUS_SUSPENDED, STATUS_DELETION_PENDING])
def test_an_account_that_may_not_request_does_not_get_the_preference_written(member_session, status):
    kit = Kit(member_session)
    user = add_user(member_session, 1, status=status, deletion_requested_at=START, status_before_deletion=STATUS_ACTIVE)
    with pytest.raises(Conflict):
        kit.service.request(kit.me(user), password=GOOD_PASSWORD, keep_published_scripts=True)
    assert keep_of(kit, user) is False


class _LosesTheRace(UserRepository):
    """另一個請求在「確認本人」之後、這個請求的條件式 UPDATE 之前搶先申請成立：第一次呼叫（對手）成立，第二次（自己）沒有命中。"""

    def request_deletion(self, user, *, now):
        super().request_deletion(user, now=now)
        return super().request_deletion(user, now=now)


def test_losing_the_race_to_a_concurrent_request_leaves_the_preference_unchanged(member_session):
    kit = Kit(member_session)
    user = add_user(member_session, 1, keep_published_scripts=True)
    auth = make_auth_service(member_session, clock=kit.clock)
    service = AccountDeletionService(
        users=_LosesTheRace(member_session), confirm_password=auth.confirm_password, clock=kit.clock,
    )
    with pytest.raises(Conflict) as exc:
        service.request(kit.me(user), password=GOOD_PASSWORD, keep_published_scripts=False)
    assert exc.value.code == "deletion_not_allowed"
    assert keep_of(kit, user) is True                      # 搶先失敗的請求沒有寫入偏好（寫入在條件式 UPDATE 成功之後）
    member_session.rollback()                              # 請求以 4xx 結束時整個交易回復
    assert keep_of(kit, user) is True


def test_a_failed_preference_write_rolls_the_whole_request_back(member_session, monkeypatch):
    """偏好與申請在同一個交易：寫入失敗（帳號此刻不存在）時回 409，交易回復後申請也不存在。"""
    kit = Kit(member_session)
    user = add_user(member_session, 1)
    monkeypatch.setattr(UserRepository, "set_keep_published_scripts", lambda self, user, keep: False)
    with pytest.raises(Conflict) as exc:
        kit.service.request(kit.me(user), password=GOOD_PASSWORD, keep_published_scripts=True)
    assert exc.value.code == "deletion_not_allowed"
    member_session.rollback()
    row = kit.row(user)
    assert (row.status, row.token_version, row.deletion_requested_at, row.keep_published_scripts) == (STATUS_ACTIVE, 1, None, False)


def test_the_event_still_carries_the_internal_id_only_when_the_preference_is_sent(member_session, log_lines):
    kit = Kit(member_session)
    user = add_user(member_session, 1)
    kit.service.request(kit.me(user), password=GOOD_PASSWORD, keep_published_scripts=True)
    events = [(line["event"], line["user_id"]) for line in log_lines() if line.get("event", "").startswith("account.deletion")]
    assert events == [("account.deletion_requested", user.id)]
    assert "keep_published_scripts" not in log_lines.raw()
