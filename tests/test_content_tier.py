"""內容模式的順序與取較保守者（content-tier-modes 規格：三種內容模式與順序）。"""
import itertools

import pytest

from app.models.user import ADULT_CONFIRM_SELF_DECLARED, CONTENT_TIERS
from app.services.content_tier import (
    ADULT_RULES_VERSION, CONTENT_TIER_ADULT, CONTENT_TIER_SAFE, CONTENT_TIER_STANDARD,
    is_tier, most_conservative, parse_tier, tier_rank,
)


def test_there_are_exactly_three_tiers_ordered_from_conservative_to_permissive():
    assert CONTENT_TIERS == ("safe", "standard", "adult")
    assert [tier_rank(tier) for tier in CONTENT_TIERS] == [0, 1, 2]


def test_standard_is_more_conservative_than_adult():
    assert most_conservative(CONTENT_TIER_STANDARD, CONTENT_TIER_ADULT) == CONTENT_TIER_STANDARD
    assert most_conservative(CONTENT_TIER_ADULT, CONTENT_TIER_STANDARD) == CONTENT_TIER_STANDARD


@pytest.mark.parametrize(("first", "second"), list(itertools.product(CONTENT_TIERS, repeat=2)))
def test_the_more_conservative_of_any_two_tiers_does_not_depend_on_their_order(first, second):
    result = most_conservative(first, second)
    assert result == most_conservative(second, first)
    assert tier_rank(result) == min(tier_rank(first), tier_rank(second))


def test_the_most_conservative_of_four_values_is_the_lowest_of_them():
    # 偏好 adult、年齡上限 adult、劇本宣告 standard、存檔 safe
    assert most_conservative("adult", "adult", "standard", "safe") == "safe"


def test_a_single_value_is_returned_unchanged():
    assert [most_conservative(tier) for tier in CONTENT_TIERS] == list(CONTENT_TIERS)


@pytest.mark.parametrize("value", [None, "", "mature", "SAFE", "Standard", " adult", 0, 1, True, ["adult"], {"tier": "adult"}])
def test_an_unknown_value_is_treated_as_safe(value):
    assert parse_tier(value) == CONTENT_TIER_SAFE
    assert tier_rank(value) == 0
    assert not is_tier(value)


@pytest.mark.parametrize("tier", CONTENT_TIERS)
def test_a_known_value_parses_to_itself(tier):
    assert parse_tier(tier) == tier
    assert is_tier(tier)


@pytest.mark.parametrize("unknown", [None, "", "mature", "ADULT"])
def test_one_unknown_input_makes_the_result_safe_instead_of_being_ignored(unknown):
    # 髒資料不得讓結果變寬鬆：不能判定的輸入以最保守處理（呼叫端沒有劇本或存檔時，是「不傳」而不是傳 None）
    assert most_conservative(CONTENT_TIER_ADULT, unknown) == CONTENT_TIER_SAFE


def test_no_input_at_all_is_safe():
    assert most_conservative() == CONTENT_TIER_SAFE


def test_the_adult_confirmation_method_is_self_declared_and_the_rules_have_a_version():
    assert ADULT_CONFIRM_SELF_DECLARED == "self_declared"
    assert isinstance(ADULT_RULES_VERSION, str) and ADULT_RULES_VERSION.strip() == ADULT_RULES_VERSION != ""
    assert len(ADULT_RULES_VERSION) <= 40          # users.adult_rules_version 的欄位長度


def test_the_module_embeds_no_age_numbers_and_imports_no_infrastructure():
    # 年齡只來自 AgePolicy；這個模組是純邏輯，不依賴資料庫、設定、網路或時間
    import ast
    import inspect

    import app.services.content_tier as module

    tree = ast.parse(inspect.getsource(module))
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    } | {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert imported <= {"__future__", "typing", "app.models.user"}
    numbers = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, int)
               and not isinstance(node.value, bool)]
    assert all(value < 13 for value in numbers), numbers          # 只有順序值與長度，沒有 13、18 這類年齡
