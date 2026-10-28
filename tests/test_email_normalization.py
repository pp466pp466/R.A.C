"""member-registration 規格：email 正規化與唯一性判斷用的正規形式。"""
import pytest

from app.services.normalization import canonical_email, is_valid_email, normalize_email


# ---- 正規化 -------------------------------------------------------------------

def test_normalize_lowercases_strips_and_applies_nfc():
    assert normalize_email("  Alice@Example.COM \n") == "alice@example.com"


def test_normalize_is_idempotent():
    once = normalize_email("A.B+x@Gmail.com")
    assert normalize_email(once) == once


# ---- 重複判斷用的正規形式 -----------------------------------------------------

def test_case_only_difference_is_the_same_mailbox():
    assert canonical_email("Alice@Example.com") == canonical_email("alice@example.com")


@pytest.mark.parametrize(
    "address",
    ["john.doe@gmail.com", "johndoe@gmail.com", "John.Doe+news@Gmail.com", "johndoe+a+b@googlemail.com", "j.o.h.n.d.o.e@googlemail.com"],
)
def test_gmail_and_googlemail_aliases_collapse_to_one_mailbox(address):
    assert canonical_email(address) == "johndoe@gmail.com"


def test_other_domains_keep_their_dots_and_plus_tags():
    assert canonical_email("a.b@example.com") != canonical_email("ab@example.com")
    assert canonical_email("a+tag@example.com") == "a+tag@example.com"
    assert canonical_email("A.B@Example.com") == "a.b@example.com"


def test_dots_and_plus_are_only_special_before_the_at_sign_of_gmail():
    assert canonical_email("someone@sub.gmail.com") == "someone@sub.gmail.com"
    assert canonical_email("someone@notgmail.com") == "someone@notgmail.com"


def test_canonical_rejects_text_that_is_not_an_address():
    with pytest.raises(ValueError):
        canonical_email("not-an-address")


# ---- 格式 ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "address",
    [
        "a@example.com", "user.name+tag@example.co.uk", "u_s-e%r@sub.example.org", "USER@EXAMPLE.COM",
        "  padded@example.com  ", "x@a-b.example.com", "1234567890@example.com",
    ],
)
def test_valid_addresses(address):
    assert is_valid_email(address)


@pytest.mark.parametrize(
    "address",
    [
        "", "plain", "@example.com", "user@", "user@localhost", "user@@example.com", "a@b@example.com",
        "us er@example.com", ".user@example.com", "user.@example.com", "us..er@example.com",
        "user@-example.com", "user@example-.com", "user@example..com", "user@exa mple.com",
        "user@example.com\r\nBcc: x@y.com", "us\ner@example.com", "us<er>@example.com", "user@exam_ple.com",
        "+news@gmail.com", "...@gmail.com", "+x@googlemail.com",
    ],
)
def test_invalid_addresses(address):
    assert not is_valid_email(address)


@pytest.mark.parametrize("address", ["用戶@example.com", "user@例子.com", "usér@example.com", "user@exämple.com"])
def test_non_ascii_addresses_are_rejected(address):
    assert not is_valid_email(address)


def test_kelvin_sign_does_not_become_a_valid_ascii_address_after_lowercasing():
    assert "K".lower() == "k"          # 前提：轉小寫後會變成 ASCII 的 k
    assert not is_valid_email("userK@example.com")


def test_length_limits():
    assert is_valid_email("a" * 64 + "@example.com")
    assert not is_valid_email("a" * 65 + "@example.com")
    long_domain = ".".join(["a" * 60] * 5) + ".com"
    assert len(f"u@{long_domain}") > 254
    assert not is_valid_email(f"u@{long_domain}")            # 總長超過 254
    assert is_valid_email("u@" + ".".join(["a" * 60] * 3) + ".com")  # 接近但沒超過
    assert is_valid_email("u@" + "a" * 63 + ".com")
    assert not is_valid_email("u@" + "a" * 64 + ".com")      # 標籤超過 63
