"""rpg-notebook 規格的純邏輯：容量與預備空間、已滿、清理、視窗邊界與追加（任務 1.1）。不需要資料庫。"""
import unicodedata

import pytest

from app.models.user import MEMBER_TIERS
from app.services import notebook_policy as policy
from app.services.quota_policy import TIER_LIMITS

# ---- 容量與預備空間 ---------------------------------------------------------------------------


@pytest.mark.parametrize(("tier", "capacity", "reserve"), [("free", 2000, 500), ("plus", 5000, 1250), ("vip", 10000, 2500)])
def test_each_tier_has_the_capacity_and_the_quarter_reserve_from_the_specification(tier, capacity, reserve):
    assert policy.capacity_for(tier) == capacity
    assert policy.reserve_for(tier) == reserve
    assert policy.ai_limit_for(tier) == capacity + reserve


@pytest.mark.parametrize("tier", ["gold", "", None, 7, "FREE", ["free"]])
def test_an_unknown_tier_gets_the_free_capacity(tier):
    assert policy.capacity_for(tier) == 2000 and policy.reserve_for(tier) == 500


def test_the_capacity_comes_from_the_single_quota_table_and_the_reserve_follows_it(monkeypatch):
    from dataclasses import replace

    monkeypatch.setitem(TIER_LIMITS, "free", replace(TIER_LIMITS["free"], notebook_capacity=1000))
    assert policy.capacity_for("free") == 1000 and policy.reserve_for("free") == 250 and policy.ai_limit_for("free") == 1250


def test_every_member_tier_has_a_capacity_that_grows_with_the_tier():
    capacities = [policy.capacity_for(tier) for tier in MEMBER_TIERS]
    assert capacities == sorted(capacities) and len(set(capacities)) == len(capacities)


# ---- 已滿：長度大於容量 -----------------------------------------------------------------------


@pytest.mark.parametrize(("length", "full"), [(0, False), (1999, False), (2000, False), (2001, True), (2399, True), (2500, True), (2501, True)])
def test_a_free_notebook_is_full_only_when_longer_than_the_capacity(length, full):
    assert policy.is_full(length, "free") is full


def test_a_length_equal_to_the_capacity_is_still_normal_and_one_more_is_full_for_every_tier():
    for tier in MEMBER_TIERS:
        capacity = policy.capacity_for(tier)
        assert policy.is_full(capacity, tier) is False
        assert policy.is_full(capacity + 1, tier) is True


def test_a_downgrade_makes_a_long_notebook_full_without_changing_it():
    assert policy.is_full(3000, "vip") is False
    assert policy.is_full(3000, "free") is True                       # 降級：內容不動，視同已滿


def test_an_upgrade_restores_the_normal_state_with_the_new_capacity():
    assert policy.is_full(2399, "free") is True
    assert policy.is_full(2399, "plus") is False and policy.capacity_for("plus") == 5000


def test_the_length_counts_nfc_characters():
    decomposed = "é" * 3                                       # 6 個碼位，NFC 後 3 個字
    assert len(decomposed) == 6 and policy.length_of(decomposed) == 3
    assert policy.length_of("") == 0 and policy.length_of(None) == 0
    assert policy.length_of("師妹的父親是長老") == 8


# ---- 還能追加多少 -----------------------------------------------------------------------------


def test_room_is_the_ai_limit_minus_the_length_and_the_separator_needed_after_existing_text():
    assert policy.room_for("", "free") == 2500 and policy.room_for(None, "free") == 2500
    assert policy.room_for("字" * 1999, "free") == 2500 - 1999 - 1          # 接在既有內容之後要補一個換行
    assert policy.room_for("字" * 1999 + "\n", "free") == 2500 - 2000      # 尾端已有換行，不再補
    assert policy.room_for("字" * 2500, "free") == 0 and policy.room_for("字" * 3000, "free") == 0


# ---- 清理 -------------------------------------------------------------------------------------


def test_cleaning_normalizes_newlines_tabs_control_characters_and_surrounding_whitespace():
    assert policy.clean_notebook_text("  第一行\r\n第二行\r第三行\t字\x00\x07尾  ") == "第一行\n第二行\n第三行 字尾"
    assert policy.clean_notebook_text("\n\n  \t ") == ""               # 全空白 → 空字串（玩家可以清空）
    assert policy.clean_notebook_text("") == ""


def test_cleaning_applies_nfc():
    cleaned = policy.clean_notebook_text("é")
    assert cleaned == unicodedata.normalize("NFC", "é") and len(cleaned) == 1


def test_cleaning_keeps_inner_newlines_and_blank_lines():
    assert policy.clean_notebook_text("甲\n\n乙") == "甲\n\n乙"


@pytest.mark.parametrize("value", ["孤立\ud800代理", None, 123, ["x"], b"bytes"])
def test_text_that_cannot_be_stored_cleans_to_none(value):
    assert policy.clean_notebook_text(value) is None


# ---- 視窗邊界 ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("completed", "boundary"),
    [
        (-3, None), (0, None), (1, None), (9, None), (10, None),        # 第 10 輪完成時不觸發
        (11, 10), (12, 10), (20, 10),                                    # 第 11 輪完成才抓第 1–10 輪；第 20 輪仍是同一個視窗
        (21, 20), (30, 20), (31, 30), (100, 90), (101, 100),
    ],
)
def test_the_window_boundary_follows_the_completed_turn_count(completed, boundary):
    assert policy.window_for(completed) == boundary


def test_a_non_integer_count_has_no_window():
    for value in (None, "11", 11.0, [11]):
        assert policy.window_for(value) is None


def test_the_window_size_is_ten_turns():
    assert policy.WINDOW_TURNS == 10
    boundary = policy.window_for(11)
    assert (boundary - policy.WINDOW_TURNS + 1, boundary) == (1, 10)


# ---- 追加 -------------------------------------------------------------------------------------


def test_new_entries_are_appended_at_the_end_and_existing_text_is_untouched():
    merged = policy.merge_entries("舊的一行\n舊的第二行", ["新一條", "新二條", "新三條"])
    assert merged.text == "舊的一行\n舊的第二行\n新一條\n新二條\n新三條"
    assert merged.text.startswith("舊的一行\n舊的第二行")
    assert merged.entries == ("新一條", "新二條", "新三條")


def test_the_first_entries_of_an_empty_notebook_have_no_leading_newline():
    for empty in ("", None):
        merged = policy.merge_entries(empty, ["甲", "乙"])
        assert merged.text == "甲\n乙" and merged.length == 3 and merged.added_length == 3


def test_a_missing_trailing_newline_is_added_and_counted_in_the_length():
    merged = policy.merge_entries("a" * 10, ["bcd"])
    assert merged.text == "a" * 10 + "\n" + "bcd"
    assert merged.length == 14 and merged.added_length == 4                  # 10 + 換行 1 + 條目 3


def test_an_existing_trailing_newline_is_not_doubled():
    merged = policy.merge_entries("舊\n", ["新"])
    assert merged.text == "舊\n新" and merged.added_length == 1


def test_lengths_are_reported_for_logging_and_the_cap_check():
    merged = policy.merge_entries("字" * 1999, ["字" * 399, "短"])
    assert merged.length == 1999 + 1 + 399 + 1 + 1
    assert merged.added_length == merged.length - 1999 and merged.longest_entry == 399


def test_a_long_entry_is_kept_whole_with_no_count_or_length_cap_here():
    entries = [f"第{n}條" + "字" * 90 for n in range(8)]                      # 8 條、每條 90 字以上：超出提示詞的要求，仍完整保留
    merged = policy.merge_entries("", entries)
    assert merged.entries == tuple(entries) and merged.longest_entry == len(entries[0])


def test_entries_are_cleaned_to_single_lines():
    merged = policy.merge_entries("", ["  前後空白  ", "含\n換行\r\n與\t控制\x00字元", "é"])
    assert merged.entries == ("前後空白", "含換行與控制字元", "é")
    assert "\r" not in merged.text and merged.text.count("\n") == 2


def test_empty_and_unusable_entries_are_dropped():
    merged = policy.merge_entries("", ["", "   ", "\n\t", None, 5, ["x"], "孤立\ud800", "有效"])
    assert merged.entries == ("有效",)


def test_an_entry_equal_to_an_existing_line_is_not_written_again():
    merged = policy.merge_entries("師妹是長老的女兒\n  主角欠宗門一個人情  ", ["師妹是長老的女兒", "主角欠宗門一個人情", "新的設定"])
    assert merged.entries == ("新的設定",)
    assert merged.text.count("師妹是長老的女兒") == 1


def test_duplicate_entries_within_one_batch_are_written_once():
    merged = policy.merge_entries("", ["相同", " 相同 ", "不同", "相同"])
    assert merged.entries == ("相同", "不同")


def test_a_substring_of_an_existing_line_is_not_a_duplicate():
    merged = policy.merge_entries("師妹是長老的女兒", ["師妹"])                    # 只有「整行相同」才去除
    assert merged.entries == ("師妹",)


def test_no_new_entries_leaves_the_text_and_length_unchanged():
    for entries in ([], ["", "  "], ["已有"]):
        merged = policy.merge_entries("已有", entries)
        assert merged.entries == () and merged.text == "已有" and merged.length == 2 and merged.added_length == 0 and merged.longest_entry == 0


def test_the_result_never_modifies_the_input_text():
    existing = "原本\n的內容"
    policy.merge_entries(existing, ["新"])
    assert existing == "原本\n的內容"
