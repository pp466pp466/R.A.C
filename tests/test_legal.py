"""條款與年齡政策的版本常數：由伺服器決定，寫入 users 的欄位（Unicode(40)）。"""
import pytest

from app.models import User
from app.services.legal import CURRENT_AGE_POLICY_VERSION, CURRENT_TERMS_VERSION


@pytest.mark.parametrize("version", [CURRENT_TERMS_VERSION, CURRENT_AGE_POLICY_VERSION])
def test_versions_are_non_empty_strings_that_fit_their_columns(version):
    assert isinstance(version, str) and version.strip() == version and version
    assert len(version) <= User.__table__.c.terms_version.type.length
    assert len(version) <= User.__table__.c.age_policy_version.type.length


def test_the_age_policy_version_names_the_birthdate_and_range_approach_and_comes_from_the_policy():
    from app.services.age_policy import DEFAULT_AGE_POLICY

    assert CURRENT_AGE_POLICY_VERSION == DEFAULT_AGE_POLICY.version
    assert CURRENT_AGE_POLICY_VERSION.endswith("birthdate-range")
