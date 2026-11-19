"""內容模式的順序與取較保守者（content-tier-modes 規格：兩種內容模式與順序）。"""
import itertools

import pytest

from app.models.user import ADULT_CONFIRM_SELF_DECLARED, CONTENT_MODES
from app.services.content_tier import (
    CONTENT_TIER_ADULT, CONTENT_TIER_SAFE, CONTENT_TIER_STANDARD, CONTENT_TIERS, SAFE_RULES_VERSION,
    is_tier, legacy_most_conservative, legacy_parse_tier, legacy_tier_rank, most_conservative, parse_tier, tier_rank,
)


def test_there_are_exactly_two_modes_ordered_from_conservative_to_permissive():
    assert CONTENT_MODES == ("safe", "adult")
    assert [tier_rank(mode) for mode in CONTENT_MODES] == [0, 1]


def test_safe_is_more_conservative_than_adult():
    assert most_conservative(CONTENT_TIER_SAFE, CONTENT_TIER_ADULT) == CONTENT_TIER_SAFE
    assert most_conservative(CONTENT_TIER_ADULT, CONTENT_TIER_SAFE) == CONTENT_TIER_SAFE


@pytest.mark.parametrize(("first", "second"), list(itertools.product(CONTENT_MODES, repeat=2)))
def test_the_more_conservative_of_any_two_modes_does_not_depend_on_their_order(first, second):
    result = most_conservative(first, second)
    assert result == most_conservative(second, first)
    assert tier_rank(result) == min(tier_rank(first), tier_rank(second))


def test_the_most_conservative_of_several_values_is_the_lowest_of_them():
    # 偏好 adult、劇本未綁定（adult）、舊值 standard、存檔 safe
    assert most_conservative("adult", "adult", "standard", "safe") == "safe"
    assert most_conservative("adult", "adult", "standard") == "adult"


def test_a_single_value_is_returned_unchanged():
    assert [most_conservative(mode) for mode in CONTENT_MODES] == list(CONTENT_MODES)


@pytest.mark.parametrize("value", [None, "", "mature", "SAFE", "Standard", " adult", 0, 1, True, ["adult"], {"tier": "adult"}])
def test_an_unknown_value_is_treated_as_safe(value):
    assert parse_tier(value) == CONTENT_TIER_SAFE
    assert tier_rank(value) == 0
    assert not is_tier(value)


def test_the_legacy_standard_value_is_read_as_adult():
    # 舊值 standard（原一般模式）一律讀作 adult：不論出現在玩家偏好、劇本內容或其他儲存的資料，既有資料不必改寫
    assert parse_tier("standard") == CONTENT_TIER_ADULT
    assert tier_rank("standard") == tier_rank("adult") == 1
    assert most_conservative("standard", "safe") == "safe"
    assert most_conservative("standard", "adult") == "adult"
    assert not is_tier("standard")                    # 它不是現行的模式，只是被讀作 adult


@pytest.mark.parametrize("mode", CONTENT_MODES)
def test_a_known_value_parses_to_itself(mode):
    assert parse_tier(mode) == mode
    assert is_tier(mode)


@pytest.mark.parametrize("unknown", [None, "", "mature", "ADULT"])
def test_one_unknown_input_makes_the_result_safe_instead_of_being_ignored(unknown):
    # 髒資料不得讓結果變寬鬆：不能判定的輸入以最保守處理（呼叫端沒有劇本或存檔時，是「不傳」而不是傳 None）
    assert most_conservative(CONTENT_TIER_ADULT, unknown) == CONTENT_TIER_SAFE


def test_no_input_at_all_is_safe():
    assert most_conservative() == CONTENT_TIER_SAFE


def test_the_safe_rules_have_a_version_and_the_confirmation_method_is_self_declared():
    assert ADULT_CONFIRM_SELF_DECLARED == "self_declared"
    assert isinstance(SAFE_RULES_VERSION, str) and SAFE_RULES_VERSION.strip() == SAFE_RULES_VERSION != ""
    assert len(SAFE_RULES_VERSION) <= 40          # users.adult_rules_version 的欄位長度
    assert SAFE_RULES_VERSION != "2026-11-03-draft-1"      # 換了新版本：所有舊的同意因版本不符而失效


# ---- 過渡相容層（safe-mode-toggle 2.5 移除；這組測試隨之刪除）---------------------------------------------

def test_the_transitional_three_value_layer_still_orders_safe_standard_adult():
    assert CONTENT_TIERS == ("safe", "standard", "adult")
    assert [legacy_tier_rank(tier) for tier in CONTENT_TIERS] == [0, 1, 2]
    assert legacy_parse_tier(CONTENT_TIER_STANDARD) == CONTENT_TIER_STANDARD
    assert legacy_most_conservative("standard", "adult") == "standard"
    assert legacy_most_conservative("adult", "bogus") == "safe"
    assert legacy_most_conservative() == "safe"


def test_the_module_embeds_no_age_numbers_and_imports_no_infrastructure():
    # 全站只限滿 18 歲，沒有依年齡的上限；這個模組是純邏輯，不依賴資料庫、設定、網路或時間
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
