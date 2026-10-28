"""CurrentUser 快照：年齡只對外呈現「是否已確認」，時間與政策版本是內部紀錄。"""
from app.models import User
from app.models.base import utcnow
from app.services.current_user import CurrentUser


def make_model(**overrides) -> User:
    values = dict(
        id=7, public_id="00000000-0000-4000-8000-000000000007", email="a@example.com", email_canonical="a@example.com",
        display_name="甲", status="active", role="user", member_tier="free", token_version=3,
    )
    values.update(overrides)
    return User(**values)


def test_a_confirmed_member_reports_age_confirmed_true():
    current = CurrentUser.from_model(make_model(age_confirmed_at=utcnow(), age_policy_version="v1"))
    assert current.age_confirmed is True


def test_an_unconfirmed_member_reports_age_confirmed_false():
    assert CurrentUser.from_model(make_model()).age_confirmed is False


def test_the_public_dict_has_the_boolean_but_never_the_time_or_the_version():
    confirmed = CurrentUser.from_model(make_model(age_confirmed_at=utcnow(), age_policy_version="v1")).public_dict()
    unconfirmed = CurrentUser.from_model(make_model()).public_dict()

    assert confirmed["age_confirmed"] is True and unconfirmed["age_confirmed"] is False
    for data in (confirmed, unconfirmed):
        assert "age_confirmed_at" not in data and "age_policy_version" not in data
