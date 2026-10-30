"""CurrentUser 快照：年齡只對外呈現「是否已完成」與年齡群組，生日、區間、時間與政策版本都是內部紀錄。"""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from app.models import User
from app.models.base import utcnow
from app.services.current_user import CurrentUser
from tests.support.age import age_columns


def make_model(**overrides) -> User:
    values = dict(
        id=7, public_id="00000000-0000-4000-8000-000000000007", email="a@example.com", email_canonical="a@example.com",
        display_name="甲", status="active", role="user", member_tier="free", token_version=3,
    )
    values.update(overrides)
    return User(**values)


def test_a_member_with_a_birthday_and_range_reports_age_confirmed_true_and_keeps_the_birthday_internally():
    current = CurrentUser.from_model(make_model(**age_columns(30)))
    assert current.age_confirmed is True and current.birth_date == date(1996, 10, 27)


def test_a_member_without_age_data_reports_age_confirmed_false():
    current = CurrentUser.from_model(make_model())
    assert current.age_confirmed is False and current.birth_date is None


def test_the_old_checkbox_confirmation_alone_does_not_count_as_completed():
    """勾選版時代的帳號只有 age_confirmed_at：完成與否只看生日與區間。"""
    current = CurrentUser.from_model(make_model(age_confirmed_at=utcnow(), age_policy_version="2026-10-28-self-declared"))
    assert current.age_confirmed is False


@pytest.mark.parametrize("columns", [{"birth_date": date(1996, 1, 5)}, {"age_range": "25_34"}])
def test_a_birthday_without_a_range_or_the_reverse_is_not_complete(columns):
    assert CurrentUser.from_model(make_model(**columns)).age_confirmed is False


@pytest.mark.parametrize("group", ["adult", "minor", None])
def test_the_public_dict_has_the_boolean_and_the_group_but_never_the_birthday_range_time_or_version(group):
    current = CurrentUser.from_model(make_model(**age_columns(30)))
    data = current.public_dict(age_group=group, google_linked=False)
    assert data["age_confirmed"] is True and data["age_group"] == group
    text = json.dumps(data, default=str)
    for secret in ("birth_date", "age_range", "age_confirmed_at", "age_policy_version", "is_adult", "1996", "25_34"):
        assert secret not in text


def test_only_an_account_awaiting_deletion_reports_when_it_will_be_deleted():
    requested = datetime(2026, 10, 30, 8, 30, 15, tzinfo=timezone.utc)
    pending = CurrentUser.from_model(make_model(status="deletion_pending", deletion_requested_at=requested))
    assert pending.deletion_scheduled_at == requested + timedelta(days=7)
    assert pending.public_dict(age_group=None)["deletion_scheduled_at"] == "2026-11-06T08:30:15Z"
    # 其餘狀態即使殘留申請時間也不回報
    stray = CurrentUser.from_model(make_model(status="active", deletion_requested_at=requested))
    assert stray.deletion_scheduled_at is None and "deletion_scheduled_at" not in stray.public_dict(age_group=None)
    assert "deletion_requested_at" not in json.dumps(pending.public_dict(age_group=None))


def test_the_age_group_must_be_passed_explicitly():
    """沒有預設值：忘了傳會立刻出錯，而不是悄悄把成年人顯示成未知。"""
    with pytest.raises(TypeError):
        CurrentUser.from_model(make_model()).public_dict()          # type: ignore[call-arg]
