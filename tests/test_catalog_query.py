"""rpg-script-catalog 規格：目錄的查詢參數解析、驗證與游標（純函式，不碰資料庫）。

對照規格：關鍵字只比對一般欄位的詞數與長度上限、空字串視為沒有關鍵字、標籤與讀者頻道的篩選值、游標分頁（`limit`、損毀或被竄改的
`cursor` 一律 400 並指出 `cursor`、`offset` 不再被讀取）。`%`、`_` 的字面比對屬於 Repository（`test_script_repository_catalog`），
這裡只證明它們沒有被這一層改動。
"""
import ast
import base64
import inspect
import json
from dataclasses import fields as dataclass_fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import PROJECT_ROOT
from app.services import catalog_query
from app.services.catalog_query import (
    CURSOR_MAX_CHARS, CURSOR_MAX_ID, DEFAULT_LIMIT, MAX_LIMIT, Q_MAX_CHARS, Q_MAX_TOKENS, CatalogCursor, CatalogQuery,
    decode_cursor, encode_cursor, parse_catalog_query,
)
from app.services.errors import ValidationError
from app.services.script_spec import TAG_MAX

MOMENT = datetime(2026, 11, 20, 8, 30, 15, 123456, tzinfo=timezone.utc)


def fields_of(**kwargs) -> set[str]:
    with pytest.raises(ValidationError) as caught:
        parse_catalog_query(**kwargs)
    return set(caught.value.fields)


def raw_cursor(payload) -> str:
    """把任意內容編成游標的外觀（URL-safe base64、去掉等號），用來造出各種被竄改的游標。"""
    data = payload if isinstance(payload, bytes) else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


# ---- 預設值與常數 ---------------------------------------------------------------------------

def test_nothing_given_means_no_filter_the_default_page_size_and_the_first_page():
    assert parse_catalog_query() == CatalogQuery(terms=(), tag=None, audience=None, limit=12, cursor=None)


def test_the_documented_limits_are_the_ones_in_the_spec():
    assert (DEFAULT_LIMIT, MAX_LIMIT, Q_MAX_TOKENS, Q_MAX_CHARS, TAG_MAX) == (12, 24, 5, 50, 12)


def test_the_parsed_query_has_no_offset_and_the_parser_does_not_take_one():
    """規格「位移不再使用」：這一層沒有位移欄位；Controller 也不會把它傳進來（見 `test_game_api`）。"""
    assert "offset" not in {field.name for field in dataclass_fields(CatalogQuery)}
    assert "offset" not in inspect.signature(parse_catalog_query).parameters
    with pytest.raises(TypeError):
        parse_catalog_query(offset=5)


# ---- 關鍵字 q -----------------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    ("青雲", ("青雲",)),
    ("青雲 仙俠", ("青雲", "仙俠")),
    ("  青雲   仙俠  ", ("青雲", "仙俠")),                      # 多個空白合併、頭尾空白去除
    ("青雲\t仙俠\n江湖", ("青雲", "仙俠", "江湖")),               # Tab 與換行也是分隔
    ("青雲　仙俠", ("青雲", "仙俠")),                         # 全形空白
    ("SciFi", ("scifi",)),                                       # 不分大小寫
    ("Straße", ("strasse",)),                                    # casefold，與標籤比對同一種規則
    ("青\x00雲\x07", ("青雲",)),                                  # 其他控制字元直接去除（不留下空白）
    ("école", ("école",)),                            # NFC：分解形的 é 與組合形相同
    ("青雲 青雲 Ｑ", ("青雲", "ｑ")),                              # 重複的詞去重並保留順序（全形字母只 casefold，不做 NFKC）
    ("", ()), ("   ", ()), ("\t\n", ()), (None, ()),             # 空字串與空白視為沒有關鍵字
])
def test_a_keyword_is_normalized_into_distinct_folded_terms(value, expected):
    assert parse_catalog_query(q=value).terms == expected


@pytest.mark.parametrize("value", ["%", "_", "50%", "a_b", "\\", "100%_\\x", "[abc]"])
def test_like_wildcards_and_escape_characters_are_left_untouched_here(value):
    """`%`、`_` 與跳脫字元在這一層原樣保留；Repository 以 `autoescape` 把它們當一般字元比對。"""
    assert parse_catalog_query(q=value).terms == (value,)


def test_five_words_are_accepted_and_six_are_rejected_naming_q():
    assert len(parse_catalog_query(q="一 二 三 四 五").terms) == 5
    assert fields_of(q="一 二 三 四 五 六") == {"q"}


def test_six_identical_words_are_still_six_words():
    assert fields_of(q="甲 甲 甲 甲 甲 甲") == {"q"}


def test_the_total_length_limit_counts_words_joined_by_single_spaces():
    assert parse_catalog_query(q="字" * Q_MAX_CHARS).terms == ("字" * Q_MAX_CHARS,)
    assert fields_of(q="字" * (Q_MAX_CHARS + 1)) == {"q"}
    assert parse_catalog_query(q="字" * 24 + "      " + "字" * 25).terms          # 49 字加一個空白＝50，多餘的空白不重複計
    assert fields_of(q="字" * 25 + " " + "字" * 25) == {"q"}                       # 25＋1＋25＝51


@pytest.mark.parametrize("value", [123, 1.5, ["青雲"], {"q": "x"}, True])
def test_a_keyword_that_is_not_text_is_rejected(value):
    assert fields_of(q=value) == {"q"}


def test_a_keyword_with_a_lone_surrogate_is_rejected_not_crashed():
    assert fields_of(q="青\ud800雲") == {"q"}


# ---- 標籤 tag -----------------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    ("仙俠", "仙俠"), ("SciFi", "scifi"), ("  SciFi ", "scifi"), ("Sci\x00Fi", "scifi"), ("", None), ("   ", None), (None, None),
    ("é", "é"),
])
def test_a_tag_is_trimmed_and_folded_or_absent(value, expected):
    assert parse_catalog_query(tag=value).tag == expected


def test_a_tag_may_be_as_long_as_the_script_spec_allows_and_no_longer():
    assert parse_catalog_query(tag="字" * TAG_MAX).tag == "字" * TAG_MAX
    assert fields_of(tag="字" * (TAG_MAX + 1)) == {"tag"}


@pytest.mark.parametrize("value", [5, ["仙俠"], False])
def test_a_tag_that_is_not_text_is_rejected(value):
    assert fields_of(tag=value) == {"tag"}


def test_a_tag_with_a_lone_surrogate_is_rejected_not_crashed():
    assert fields_of(tag="仙\udfff") == {"tag"}


# ---- 讀者頻道 audience ------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["all", "male", "female"])
def test_the_three_channels_are_accepted_exactly(value):
    assert parse_catalog_query(audience=value).audience == value


@pytest.mark.parametrize("value", [None, ""])
def test_no_channel_means_no_filter(value):
    assert parse_catalog_query(audience=value).audience is None


@pytest.mark.parametrize("value", ["other", "ALL", " all", "Male", "boys", 1, True, ["all"]])
def test_any_other_channel_is_rejected_naming_the_field(value):
    assert fields_of(audience=value) == {"audience"}


# ---- limit --------------------------------------------------------------------------------

@pytest.mark.parametrize("value", [1, 2, 12, 23, 24])
def test_a_limit_from_one_to_the_maximum_is_accepted(value):
    assert parse_catalog_query(limit=value).limit == value


@pytest.mark.parametrize("value", [0, -1, 25, 1000, "abc", "12", None, True, False, 1.5, [12]])
def test_any_other_limit_is_rejected_naming_the_field(value):
    assert fields_of(limit=value) == {"limit"}


# ---- 一次回報全部不合法的欄位 -------------------------------------------------------------------

def test_every_invalid_field_is_reported_at_once():
    assert fields_of(q="一 二 三 四 五 六", tag="字" * 13, audience="other", limit=0, cursor="@@@") == {"q", "tag", "audience", "limit", "cursor"}


def test_a_valid_field_is_not_reported_next_to_an_invalid_one():
    assert fields_of(q="青雲", tag="仙俠", audience="all", limit=0) == {"limit"}


def test_the_error_messages_are_traditional_chinese_and_never_echo_the_input():
    with pytest.raises(ValidationError) as caught:
        parse_catalog_query(q="祕密關鍵字 二 三 四 五 六", tag="祕密標籤" * 5, audience="祕密頻道", cursor="祕密游標")
    for message in caught.value.fields.values():
        assert "祕密" not in message and any("一" <= char <= "鿿" for char in message)


# ---- 游標：往返 -----------------------------------------------------------------------------

def test_a_cursor_round_trips_with_the_microseconds_kept():
    cursor = decode_cursor(encode_cursor(2, MOMENT, 77))
    assert cursor == CatalogCursor(rank=2, published_at=MOMENT, version_id=77)
    assert cursor.published_at.microsecond == 123456 and cursor.published_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize("rank, version_id", [(0, 1), (1, 5), (2, CURSOR_MAX_ID)])
def test_the_extreme_valid_positions_round_trip(rank, version_id):
    assert decode_cursor(encode_cursor(rank, MOMENT, version_id)) == CatalogCursor(rank, MOMENT, version_id)


def test_a_naive_time_is_taken_as_utc_and_another_zone_is_converted():
    naive = MOMENT.replace(tzinfo=None)
    taipei = MOMENT.astimezone(timezone(timedelta(hours=8)))
    assert encode_cursor(0, naive, 3) == encode_cursor(0, MOMENT, 3) == encode_cursor(0, taipei, 3)


def test_a_cursor_is_plain_url_safe_text_without_padding():
    token = encode_cursor(1, MOMENT, 99)
    assert token.isascii() and "=" not in token and "+" not in token and "/" not in token and len(token) < CURSOR_MAX_CHARS


def test_the_cursor_is_accepted_through_the_query_parser():
    token = encode_cursor(1, MOMENT, 5)
    assert parse_catalog_query(cursor=token).cursor == CatalogCursor(1, MOMENT, 5)
    assert parse_catalog_query(cursor="").cursor is None and parse_catalog_query(cursor=None).cursor is None


# ---- 游標：損毀與竄改 -----------------------------------------------------------------------------

GOOD = [1, "2026-11-20T08:30:15.123456Z", 5]
_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def loose_tail(token: str) -> str:
    """同一份內容的另一種寫法：最後一個字元只改動「補位」用的位元（解碼結果相同，文字不同）。"""
    assert len(token) % 4 in (2, 3), "只有最後一組不滿四個字元時才有補位位元"
    return token[:-1] + _ALPHABET[_ALPHABET.index(token[-1]) ^ 1]


@pytest.mark.parametrize("label, token", [
    ("任意字串", "hello"),
    ("不是字串", 12345),
    ("列表", ["x"]),
    ("字元不在字母表", "abc$def"),
    ("含等號", encode_cursor(1, MOMENT, 5) + "="),
    ("含空白", " " + encode_cursor(1, MOMENT, 5)),
    ("過長", "A" * (CURSOR_MAX_CHARS + 1)),
    ("長度除以四餘一", "A" * 5),
    ("不是 JSON", raw_cursor(b"not json at all")),
    ("不是 UTF-8", raw_cursor(b"\xff\xfe\xfd")),
    ("JSON 不是列表", raw_cursor({"rank": 1})),
    ("太少元素", raw_cursor([1, "2026-11-20T08:30:15.123456Z"])),
    ("太多元素", raw_cursor(GOOD + [1])),
    ("舊的位移寫法", raw_cursor({"offset": 5})),
    ("位移數字", raw_cursor(5)),
    ("層級是布林", raw_cursor([True, GOOD[1], 5])),
    ("層級是字串", raw_cursor(["1", GOOD[1], 5])),
    ("層級是小數", raw_cursor([1.0, GOOD[1], 5])),
    ("層級為負", raw_cursor([-1, GOOD[1], 5])),
    ("層級超過 2", raw_cursor([3, GOOD[1], 5])),
    ("編號為零", raw_cursor([1, GOOD[1], 0])),
    ("編號為負", raw_cursor([1, GOOD[1], -4])),
    ("編號太大", raw_cursor([1, GOOD[1], CURSOR_MAX_ID + 1])),
    ("編號是布林", raw_cursor([1, GOOD[1], True])),
    ("編號是字串", raw_cursor([1, GOOD[1], "5"])),
    ("編號是小數", raw_cursor([1, GOOD[1], 5.5])),
    ("時間不是字串", raw_cursor([1, 20261120, 5])),
    ("時間少了微秒", raw_cursor([1, "2026-11-20T08:30:15Z", 5])),
    ("時間有時區寫法", raw_cursor([1, "2026-11-20T08:30:15.123456+00:00", 5])),
    ("月份 13", raw_cursor([1, "2026-13-20T08:30:15.123456Z", 5])),
    ("日期不存在", raw_cursor([1, "2026-02-30T08:30:15.123456Z", 5])),
    ("時間是亂字", raw_cursor([1, "yesterday", 5])),
    ("JSON 有多餘空白（非標準寫法）", raw_cursor(json.dumps(GOOD).encode("utf-8"))),
    ("JSON 鍵順序以外的跳脫（非標準寫法）", raw_cursor(b'[1,"2026-11-20T08:30:15.123456Z",5]\n')),
    ("無意義的結尾位元（非標準寫法）", loose_tail(encode_cursor(1, MOMENT, 5))),
])
def test_a_damaged_or_tampered_cursor_is_a_400_that_names_cursor(label, token):
    with pytest.raises(ValidationError) as caught:
        decode_cursor(token)
    assert set(caught.value.fields) == {"cursor"}, label
    assert fields_of(cursor=token) == {"cursor"}, label


def test_the_tail_tampering_case_really_is_a_different_text_for_the_same_payload():
    """上一組的「無意義的結尾位元」案例要成立，前提是它解碼出的內容與原本相同、只有文字不同。"""
    good = encode_cursor(1, MOMENT, 5)
    tampered = loose_tail(good)
    assert tampered != good
    assert base64.urlsafe_b64decode(tampered + "=" * (-len(tampered) % 4)) == base64.urlsafe_b64decode(good + "=" * (-len(good) % 4))


def test_a_tampered_but_well_formed_cursor_is_just_a_valid_other_position():
    """游標不簽章：竄改成另一個合法的位置只會得到另一個起點（不授權任何東西，也不會 500）。"""
    other = encode_cursor(0, MOMENT - timedelta(days=30), 1)
    assert decode_cursor(other) == CatalogCursor(0, MOMENT - timedelta(days=30), 1)


def test_the_cursor_error_never_echoes_the_cursor_text():
    with pytest.raises(ValidationError) as caught:
        decode_cursor("secret$token")
    assert "secret" not in caught.value.fields["cursor"] and "secret" not in str(caught.value)


# ---- 純函式模組的邊界 -----------------------------------------------------------------------------

def test_the_module_has_no_database_http_or_model_dependency():
    tree = ast.parse(Path(catalog_query.__file__).read_text(encoding="utf-8-sig"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    forbidden = ("sqlalchemy", "flask", "app.models", "app.repositories", "app.controllers", "app.db")
    assert not [name for name in imported if any(name == bad or name.startswith(bad + ".") for bad in forbidden)]
    assert Path(catalog_query.__file__).is_relative_to(PROJECT_ROOT / "app" / "services")
