"""post-login-age-confirmation 與 account-lockout 規格（Service 層）：年齡確認頁送出時的三種結果。

- 補填時未滿 13 歲的玩家：立即抹除帳號與全部個資（走單一抹除出口），不保存填寫的生日，留下只有內部編號的刪除紀錄；
  管理員只拒絕、不抹除。
- 生日與區間不一致：通用說明、帳號保留、不留年齡資料，並計入帳號鎖定的計次（15 分鐘內 5 次鎖定）。
- 其他欄位錯誤（缺少、格式不合法）是可就地修正的錯誤，不計次。

以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行。
"""
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import AdminAuditLog, ChatConversation, DeletionTombstone, LockStrike, User, UserIdentity
from app.models.deletion_tombstone import REASON_UNDERAGE
from app.models.lock_strike import STRIKE_IDENTITY_REJECTED
from app.services.auth_service import AGE_STEP_MISMATCH_MESSAGE
from app.services.errors import UnderageAccountErased, ValidationError
from tests.support.age import age_fields, birth_date_for_age
from tests.support.auth import START, FakeClock, add_user, make_auth_service


def count(session, model) -> int:
    session.expire_all()
    return session.scalar(select(func.count()).select_from(model))


def submit(session, user: User, clock=None, **age_input):
    service = make_auth_service(session, clock=clock)
    return service.verify_age(service.resolve_session(user.id, user.token_version), **age_input)


def refusal(session, user: User, clock=None, **age_input) -> ValidationError:
    with pytest.raises(ValidationError) as exc:
        submit(session, user, clock, **age_input)
    return exc.value


# ---- 未滿 13 歲：立即抹除 -----------------------------------------------------------------------------

@pytest.mark.parametrize(
    "age_input",
    [
        age_fields(12, age_range="under_13"),                  # 一致的未滿 13 歲
        age_fields(12, age_range="18_24"),                     # 生日未滿但區間填成年：以未滿 13 歲為準
        age_fields(30, age_range="under_13"),                  # 生日是成年但區間選未滿 13 歲：同樣以未滿 13 歲為準
    ],
    ids=["consistent", "range-claims-adult", "birthday-claims-adult"],
)
def test_a_player_under_13_is_erased_at_the_age_step_with_everything_attached(member_session, age_input):
    s = member_session
    user = add_user(s, 1, age=None, password=None)
    s.add(UserIdentity(user_id=user.id, provider="google", provider_subject="sub-under-13"))
    s.add(ChatConversation(user_id=user.id, client_id="00000000-0000-4000-8000-000000000001", state_json={}))
    s.commit()
    user_id = user.id

    error = refusal(s, user, **age_input)

    assert isinstance(error, UnderageAccountErased) and error.code == "age_not_eligible" and error.fields == {}
    assert count(s, User) == 0 and count(s, UserIdentity) == 0 and count(s, ChatConversation) == 0
    (tombstone,) = s.scalars(select(DeletionTombstone))
    assert (tombstone.user_id, tombstone.reason, tombstone.deleted_at) == (user_id, REASON_UNDERAGE, START)


def test_the_erased_players_birthday_is_nowhere_in_the_database_or_the_log(member_session, log_lines):
    s = member_session
    user = add_user(s, 1, age=None, password=None)
    birthday = birth_date_for_age(12).isoformat()
    refusal(s, user, **age_fields(12))
    # 資料庫唯一留下的是刪除紀錄：只有內部編號、時間與原因
    s.expire_all()
    (tombstone,) = s.scalars(select(DeletionTombstone))
    assert birthday not in repr((tombstone.user_id, tombstone.reason, tombstone.deleted_at))
    raw = log_lines.raw()
    assert birthday not in raw and "under_13" not in raw
    events = [line["event"] for line in log_lines() if "event" in line]
    assert "auth.age_rejected.underage" in events and "account.underage_erased" in events


def test_an_admin_under_13_is_refused_but_never_erased(member_session):
    s = member_session
    admin = add_user(s, 1, age=None, role="admin")
    error = refusal(s, admin, **age_fields(12))
    assert not isinstance(error, UnderageAccountErased) and error.code == "age_not_eligible"
    assert count(s, User) == 1 and count(s, DeletionTombstone) == 0
    stored = s.get(User, admin.id)
    assert stored.birth_date is None and stored.age_range is None


def test_a_player_who_already_confirmed_is_never_erased_by_a_later_submission(member_session):
    s = member_session
    user = add_user(s, 1, age=30)
    assert submit(s, user, **age_fields(12)).age_confirmed is True        # 重複提交一律成功、不改動原紀錄
    assert count(s, User) == 1 and count(s, DeletionTombstone) == 0
    assert s.get(User, user.id).birth_date == birth_date_for_age(30)


# ---- 不一致：通用說明並計次 -----------------------------------------------------------------------------

def test_a_mismatch_gets_the_generic_message_keeps_the_account_and_counts_once(member_session):
    s = member_session
    user = add_user(s, 1, age=None)
    error = refusal(s, user, **age_fields(30, age_range="13_17"))
    assert error.code == "age_mismatch" and error.message == AGE_STEP_MISMATCH_MESSAGE and error.fields == {}
    stored = s.get(User, user.id)
    assert stored.birth_date is None and stored.age_range is None and stored.age_confirmed_at is None
    assert stored.locked_at is None
    (strike,) = s.scalars(select(LockStrike))
    assert (strike.user_id, strike.kind, strike.created_at) == (user.id, STRIKE_IDENTITY_REJECTED, START)


def test_the_same_account_can_fill_it_in_correctly_right_after_a_mismatch(member_session):
    s = member_session
    user = add_user(s, 1, age=None)
    refusal(s, user, **age_fields(30, age_range="13_17"))
    assert submit(s, user, **age_fields(30)).age_confirmed is True


@pytest.mark.parametrize(
    "age_input",
    [{"birth_date": None, "age_range": None}, {"birth_date": "2026-02-30", "age_range": "25_34"}, age_fields(30, age_range=None)],
    ids=["empty", "impossible-date", "no-range"],
)
def test_fields_that_are_missing_or_malformed_are_not_counted(member_session, age_input):
    s = member_session
    user = add_user(s, 1, age=None)
    assert refusal(s, user, **age_input).code == "validation_error"
    assert count(s, LockStrike) == 0


def test_five_mismatches_within_fifteen_minutes_lock_the_account_and_leave_an_audit_row_without_an_admin(member_session):
    s = member_session
    user = add_user(s, 1, age=None)
    clock = FakeClock()
    for _ in range(4):
        refusal(s, user, clock, **age_fields(30, age_range="13_17"))
        clock.advance(minutes=1)
    assert s.get(User, user.id).locked_at is None
    refusal(s, user, clock, **age_fields(30, age_range="13_17"))

    s.expire_all()
    stored = s.get(User, user.id)
    assert stored.locked_at is not None and stored.lock_reason == STRIKE_IDENTITY_REJECTED
    (audit,) = s.scalars(select(AdminAuditLog))
    assert audit.action == "account.lock" and audit.admin_user_id is None
    assert audit.target_user_public_id == user.public_id and audit.detail_json == {"reason": STRIKE_IDENTITY_REJECTED}
    assert "@" not in repr(audit.detail_json) and audit.reason is None


def test_mismatches_older_than_fifteen_minutes_do_not_add_up(member_session):
    s = member_session
    user = add_user(s, 1, age=None)
    clock = FakeClock()
    for _ in range(4):
        refusal(s, user, clock, **age_fields(30, age_range="13_17"))
    clock.advance(minutes=16)                                       # 前四次已在視窗之外
    refusal(s, user, clock, **age_fields(30, age_range="13_17"))
    assert s.get(User, user.id).locked_at is None


def test_an_admin_is_never_counted_or_locked_for_mismatches(member_session):
    s = member_session
    admin = add_user(s, 1, age=None, role="admin")
    for _ in range(6):
        assert refusal(s, admin, **age_fields(30, age_range="13_17")).code == "age_mismatch"
    assert count(s, LockStrike) == 0 and s.get(User, admin.id).locked_at is None


def test_a_locked_account_stops_collecting_strikes(member_session):
    s = member_session
    user = add_user(s, 1, age=None, locked_at=START, lock_reason="rate_limited")
    refusal(s, user, **age_fields(30, age_range="13_17"))
    assert count(s, LockStrike) == 0
