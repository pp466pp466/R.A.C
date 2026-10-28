"""auth-pages 規格「轉址的安全」：登入後的返回位置只接受站內路徑。"""
import pytest

from app.security.redirects import DEFAULT_AFTER_LOGIN, MAX_NEXT_LENGTH, safe_next


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
