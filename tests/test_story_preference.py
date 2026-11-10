"""故事偏好與讀者頻道的值域與排序（純函式，story-preference 規格）。"""
import pytest

from app.models.user import AUDIENCES, User
from app.services import story_preference as sp


def test_the_value_domain_is_exactly_all_male_female_in_that_order():
    assert AUDIENCES == ("all", "male", "female")
    assert (sp.AUDIENCE_ALL, sp.AUDIENCE_MALE, sp.AUDIENCE_FEMALE) == AUDIENCES
    assert sp.AUDIENCES is AUDIENCES


@pytest.mark.parametrize("value", ["all", "male", "female"])
def test_parse_accepts_each_enum_value(value):
    assert sp.parse_audience(value) == value


@pytest.mark.parametrize("value", ["Male", "FEMALE", " male", "male ", "boys", "", None, 1, True, ["male"], b"male"])
def test_parse_is_strict_and_returns_none_for_anything_else(value):
    assert sp.parse_audience(value) is None


@pytest.mark.parametrize("value", [None, "", "Male", 1, "boys"])
def test_normalize_treats_missing_or_unrecognised_as_all(value):
    assert sp.normalize_audience(value) == "all"


@pytest.mark.parametrize("value", ["male", "female", "all"])
def test_normalize_keeps_valid_values(value):
    assert sp.normalize_audience(value) == value


# 偏好 × 頻道（None 代表遷移之前的空值）的完整對照表：0 = 同頻，1 = 全讀者，2 = 另一頻
EXPECTED = {
    ("male", "male"): 0, ("male", "all"): 1, ("male", None): 1, ("male", "female"): 2,
    ("female", "female"): 0, ("female", "all"): 1, ("female", None): 1, ("female", "male"): 2,
    ("all", "male"): 0, ("all", "female"): 0, ("all", "all"): 0, ("all", None): 0,
}


@pytest.mark.parametrize(("preference", "audience"), list(EXPECTED))
def test_rank_table(preference, audience):
    assert sp.rank(preference, audience) == EXPECTED[(preference, audience)]


def test_the_all_preference_never_reorders_anything():
    assert {sp.rank("all", audience) for audience in ("male", "female", "all", None, "weird")} == {0}


def test_an_unrecognised_preference_does_not_reorder_either():
    assert {sp.rank(preference, "male") for preference in (None, "", "Male", 3)} == {0}


def test_ordering_a_list_by_rank_keeps_equal_layers_in_their_original_order():
    items = [("a", "female"), ("b", "all"), ("c", "male"), ("d", None), ("e", "male"), ("f", "female")]
    assert [name for name, _ in sorted(items, key=lambda item: sp.rank("male", item[1]))] == ["c", "e", "b", "d", "a", "f"]
    assert [name for name, _ in sorted(items, key=lambda item: sp.rank("female", item[1]))] == ["a", "f", "b", "d", "c", "e"]
    assert [name for name, _ in sorted(items, key=lambda item: sp.rank("all", item[1]))] == ["a", "b", "c", "d", "e", "f"]


def test_the_user_model_defaults_to_all_in_both_the_orm_and_the_database():
    column = User.__table__.c.story_preference
    assert column.default.arg == "all"
    assert str(column.server_default.arg) == "all"
    assert column.nullable is False


# ---- 讀者頻道不影響審查（story-preference 任務 2.4）-----------------------------------------------------

def test_the_review_prompt_is_word_for_word_the_same_with_or_without_an_audience():
    from app.services.script_review_prompt import build_review_contents, build_review_instruction
    from tests.support.script_data import validated

    base = validated()
    expected = build_review_contents(base.content, "safe")
    for channel in ("male", "female"):
        with_channel = validated(audience=channel)
        assert "audience" in with_channel.content
        assert build_review_contents(with_channel.content, "safe") == expected
    assert build_review_instruction("MARKER") == build_review_instruction("MARKER")


def test_the_rules_layer_gives_the_same_result_with_or_without_an_audience():
    from app.services.script_rules import check_rules
    from tests.support.script_data import validated

    for changes in ({}, {"content_tier": "standard", "characters": [{"name": "林曉", "personality": "活潑", "age": 12, "appearance": "瘦小"}]}):
        base = validated(**changes)
        for channel in ("male", "female"):
            assert check_rules(validated(audience=channel, **changes).content, tier="standard") == check_rules(base.content, tier="standard")
