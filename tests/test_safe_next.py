"""auth-pages 規格「轉址的安全」：登入後的返回位置只接受站內路徑。"""
import json

import pytest

from app.config import PROJECT_ROOT
from app.security.redirects import DEFAULT_AFTER_LOGIN, MAX_NEXT_LENGTH, MAX_RETURN_LENGTH, RETURN_PREFIX, safe_next, safe_return_path


@pytest.mark.parametrize(
    "value",
    ["/account", "/", "/cs", "/a/b/c", "/account?tab=security", "/account#top", "/search?q=繁體中文", "/path?next=https://example.com"],
)
def test_site_paths_are_kept_as_they_are(value):
    assert safe_next(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "https://evil.example/", "http://evil.example", "//evil.example/x", "///evil.example", "evil.example/x",
        "javascript:alert(1)", "data:text/html,<script>1</script>", "mailto:a@b.c", "account", " /account", "",
        "/\\evil.example", "/\\\\evil.example", "\\\\evil.example", "/%2f/evil.example", "/%2F%2Fevil.example",
        "/%5Cevil.example", "/account\r\nSet-Cookie: pwn=1", "/account%0d%0aLocation:%20https://evil.example",
        "/a\x00b", "/a\tb", "/a\x7fb", "/x" + "a" * MAX_NEXT_LENGTH,
    ],
)
def test_anything_that_could_leave_the_site_falls_back_to_the_default(value):
    assert safe_next(value) == DEFAULT_AFTER_LOGIN


@pytest.mark.parametrize("not_text", [None, 123, ["/account"], {"a": 1}, b"/account"])
def test_non_text_falls_back_to_the_default(not_text):
    assert safe_next(not_text) == DEFAULT_AFTER_LOGIN


def test_a_different_default_can_be_given():
    assert safe_next("https://evil.example", "/cs") == "/cs"
    assert safe_next(None, "/cs") == "/cs"


def test_the_length_limit_is_inclusive():
    assert safe_next("/" + "a" * (MAX_NEXT_LENGTH - 1)) != DEFAULT_AFTER_LOGIN
    assert safe_next("/" + "a" * MAX_NEXT_LENGTH) == DEFAULT_AFTER_LOGIN


# ---- 帳號頁的返回位置（account-safe-mode-setting 規格）：只能回到 /game/ 之內 ------------------------------------------------

RETURN_CASES = json.loads((PROJECT_ROOT / "tests" / "js" / "fixtures" / "safe-return-path-cases.json").read_text(encoding="utf-8-sig"))


def test_the_shared_case_table_is_valid_and_invalid_exactly_as_the_front_end_judges_it():
    """前端的 `safeReturnPath` 與這裡讀同一張案例表；兩邊任何一邊改了規則，另一邊的測試都會失敗。"""
    for value in RETURN_CASES["valid"]:
        assert safe_return_path(value) == value, value
    for value in RETURN_CASES["invalid"]:
        assert safe_return_path(value) is None, repr(value)
    for value in RETURN_CASES["notStrings"]:
        assert safe_return_path(value) is None, repr(value)


def test_the_length_limit_is_two_hundred_characters():
    longest = RETURN_PREFIX + "a" * (MAX_RETURN_LENGTH - len(RETURN_PREFIX))
    assert len(longest) == 200 and safe_return_path(longest) == longest
    assert safe_return_path(longest + "a") is None
    assert RETURN_CASES["tooLongLength"] == MAX_RETURN_LENGTH + 1


def test_a_missing_value_is_none_and_the_login_default_is_not_used_for_returns():
    assert safe_return_path(None) is None
    assert safe_return_path("/") is None and safe_return_path(DEFAULT_AFTER_LOGIN) is None       # /game 本身不在 /game/ 之內
