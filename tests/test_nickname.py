"""player-nickname 規格：暱稱的格式與標準化、短代號。"""
import re

import pytest

from app.services.nickname import (
    CODE_ALPHABET, CODE_LENGTH, MESSAGE_LENGTH, MESSAGE_OFFICIAL, MESSAGE_PATTERN, MESSAGE_REQUIRED, check_nickname,
    display_name, new_code,
)


def ok(raw):
    cleaned, error = check_nickname(raw)
    assert error is None, error
    return cleaned


def bad(raw):
    cleaned, error = check_nickname(raw)
    assert cleaned is None and error, raw
    return error


@pytest.mark.parametrize("raw", ["雲遊者", "ab", "Mr.Smith", "小明_2024", "A B C", "龍傲天", "x" * 20, "雲" * 20, "😀😀"])
def test_a_valid_nickname_passes(raw):
    assert ok(raw)


def test_the_value_is_normalised_to_nfc_without_controls_zero_width_characters_or_extra_spaces():
    assert ok("  雲​遊\u0007者  ") == "雲遊者"
    assert ok("Café") == "Café"                    # NFC
    assert ok("雲   遊\t者") == "雲 遊 者"


def test_the_length_is_counted_in_unicode_characters_after_cleaning():
    assert bad("雲") == MESSAGE_LENGTH and bad("x" * 21) == MESSAGE_LENGTH
    assert bad("​​雲​") == MESSAGE_LENGTH      # 去除零寬字元後只剩 1 個字
    assert ok("😀" * 20)                                       # 一個表情符號算一個字
    assert bad("😀" * 21) == MESSAGE_LENGTH


@pytest.mark.parametrize("raw", [None, 123, ["a"], b"abc"])
def test_a_non_text_value_is_refused(raw):
    assert bad(raw) == MESSAGE_REQUIRED


@pytest.mark.parametrize("raw", ["", "   ", "​​"])
def test_an_empty_nickname_is_refused(raw):
    assert bad(raw) == MESSAGE_REQUIRED


@pytest.mark.parametrize("raw", [
    "https://x.io", "http://a", "www.abc", "my.site.com", "go.to/me", "ｈｔｔｐｓ：／／ｘ", "Ｗｗｗ．ａｂｃ", "abc。com",
    "h t t p s : / / x", "wWw .abc",
])
def test_a_url_like_nickname_is_refused(raw):
    assert bad(raw) == MESSAGE_PATTERN


@pytest.mark.parametrize("raw", ["a@b.com", "我是a@b", "ａ＠ｂ"])
def test_an_email_like_nickname_is_refused(raw):
    assert bad(raw) == MESSAGE_PATTERN


def test_an_at_sign_with_a_space_around_it_is_not_an_email_address():
    assert ok("a @b")


@pytest.mark.parametrize("raw", ["0912345678", "0912-345-678", "(02) 2345 6789", "+886 912 345 678", "０９１２３４５６７８", "09.12.34.56.78"])
def test_a_phone_like_nickname_is_refused(raw):
    assert bad(raw) == MESSAGE_PATTERN


@pytest.mark.parametrize("raw", ["小明1234567", "2024", "No.1234567", "a1b2c3d4e5f6"])
def test_a_short_digit_run_is_not_a_phone_number(raw):
    assert ok(raw)


@pytest.mark.parametrize("raw", ["官方客服", "我是官方", "官 方", "ＯＦＦ官方", "官​方"])
def test_impersonating_the_official_name_is_refused(raw):
    assert bad(raw) == MESSAGE_OFFICIAL


def test_the_error_message_never_repeats_the_input():
    secret = "https://secret-host.example/路徑"
    assert secret not in bad(secret) and "secret-host" not in bad(secret)


def test_a_lone_surrogate_cannot_be_stored_and_is_refused():
    assert check_nickname("雲\ud800遊")[0] is None


# ---- 短代號 ---------------------------------------------------------------------------


def test_a_code_has_the_fixed_length_and_no_confusable_characters():
    assert CODE_LENGTH == 4
    assert not set(CODE_ALPHABET) & set("0O1IL")
    for _ in range(200):
        code = new_code()
        assert re.fullmatch(rf"[{CODE_ALPHABET}]{{{CODE_LENGTH}}}", code)


def test_codes_are_random_not_derived_from_anything():
    assert len({new_code() for _ in range(200)}) > 100         # 31^4 種組合，200 次幾乎不會重複


def test_the_public_display_is_nickname_hash_code():
    assert display_name("雲遊者", "7K2M") == "雲遊者#7K2M"
