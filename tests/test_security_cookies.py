"""簽章的短效 cookie：簽章、用途隔離、到期、金鑰輪替與屬性。"""
import pytest
from flask import Response

from app.config import load_config
from app.security.cookies import SignedCookies, cookie_name
from tests.support.auth import FakeClock
from tests.support.config_env import production_like_env


def cookies_for(env="testing", *, key=None, fallbacks=None, clock=None) -> SignedCookies:
    if env == "testing":
        environ = {"APP_ENV": "testing"}
        if key:
            environ["SECRET_KEY"] = key
    else:
        environ = production_like_env(env, **({"SECRET_KEY": key} if key else {}))
    if fallbacks:
        environ["SECRET_KEY_FALLBACKS"] = fallbacks
    return SignedCookies(load_config(environ=environ), clock or FakeClock())


def set_cookie_header(response: Response) -> str:
    return response.headers.getlist("Set-Cookie")[0]


# ---- 簽章與用途 ---------------------------------------------------------------

def test_a_signed_value_round_trips():
    cookies = cookies_for()
    payload = {"state": "abc", "n": 1, "名稱": "測試"}
    assert cookies.loads("oauth.v1", cookies.dumps("oauth.v1", payload), max_age_seconds=600) == payload


def test_a_value_signed_for_one_purpose_is_rejected_for_another():
    cookies = cookies_for()
    assert cookies.loads("csrf.v1", cookies.dumps("oauth.v1", {"x": 1}), max_age_seconds=600) is None


@pytest.mark.parametrize("value", [None, "", "garbage", "a.b.c", "x" * 500, "eyJ4IjoxfQ.invalid.signature"])
def test_missing_or_malformed_values_are_simply_invalid(value):
    assert cookies_for().loads("oauth.v1", value, max_age_seconds=600) is None


def test_tampering_invalidates_the_value():
    cookies = cookies_for()
    signed = cookies.dumps("oauth.v1", {"state": "abc"})
    tampered = signed[:-1] + ("A" if signed[-1] != "A" else "B")
    assert cookies.loads("oauth.v1", tampered, max_age_seconds=600) is None
    payload_part, _, rest = signed.partition(".")
    swapped = payload_part[:-2] + "xx" + "." + rest
    assert cookies.loads("oauth.v1", swapped, max_age_seconds=600) is None


def test_a_value_signed_with_an_unrelated_key_is_rejected():
    forged = cookies_for(key="attacker-controlled-key-0000000000000").dumps("oauth.v1", {"state": "abc"})
    assert cookies_for(key="the-real-server-key-00000000000000").loads("oauth.v1", forged, max_age_seconds=600) is None


# ---- 到期（假時鐘）------------------------------------------------------------

def test_a_value_expires_after_max_age_without_waiting():
    clock = FakeClock()
    cookies = cookies_for(clock=clock)
    signed = cookies.dumps("oauth.v1", {"state": "abc"})
    clock.advance(minutes=9, seconds=59)
    assert cookies.loads("oauth.v1", signed, max_age_seconds=600) == {"state": "abc"}
    clock.advance(seconds=2)
    assert cookies.loads("oauth.v1", signed, max_age_seconds=600) is None


# ---- 金鑰輪替 -----------------------------------------------------------------

def test_after_rotation_old_values_still_verify_but_new_values_use_the_new_key():
    old = cookies_for(key="the-old-signing-key-0000000000000000")
    rotated = cookies_for(key="the-new-signing-key-0000000000000000", fallbacks="the-old-signing-key-0000000000000000")
    signed_with_old = old.dumps("csrf.v1", {"t": 1})
    assert rotated.loads("csrf.v1", signed_with_old, max_age_seconds=600) == {"t": 1}

    signed_with_new = rotated.dumps("csrf.v1", {"t": 2})
    assert old.loads("csrf.v1", signed_with_new, max_age_seconds=600) is None      # 新簽發的內容舊金鑰驗不過


def test_a_key_that_is_not_listed_for_rotation_is_not_accepted():
    unlisted = cookies_for(key="some-other-old-key-00000000000000000").dumps("csrf.v1", {"t": 1})
    rotated = cookies_for(key="the-new-signing-key-0000000000000000", fallbacks="the-old-signing-key-0000000000000000")
    assert rotated.loads("csrf.v1", unlisted, max_age_seconds=600) is None


# ---- 屬性 ---------------------------------------------------------------------

def test_a_development_cookie_is_httponly_lax_host_only_and_not_secure():
    response = Response()
    cookies_for().set(response, "oauth_tx", "value", max_age_seconds=600)
    header = set_cookie_header(response)
    assert header.startswith("oauth_tx=value")
    assert "HttpOnly" in header and "SameSite=Lax" in header and "Path=/" in header and "Max-Age=600" in header
    assert "Secure" not in header and "Domain" not in header


@pytest.mark.parametrize("env", ["production", "staging"])
def test_a_production_like_cookie_has_the_host_prefix_and_the_secure_flag(env):
    response = Response()
    cookies_for(env).set(response, "csrf", "value")
    header = set_cookie_header(response)
    assert header.startswith("__Host-csrf=value")
    assert "Secure" in header and "HttpOnly" in header and "SameSite=Lax" in header and "Path=/" in header
    assert "Domain" not in header                 # __Host- 的要求


def test_deleting_expires_the_cookie_with_the_same_attributes():
    response = Response()
    cookies_for("production").delete(response, "oauth_tx")
    header = set_cookie_header(response)
    assert header.startswith("__Host-oauth_tx=;") and ("Max-Age=0" in header or "expires=" in header.lower())
    assert "Secure" in header and "Path=/" in header and "Domain" not in header


def test_the_cookie_name_helper_matches_what_is_actually_written():
    config = load_config(environ=production_like_env("production"))
    response = Response()
    SignedCookies(config, FakeClock()).set(response, "csrf", "v")
    assert set_cookie_header(response).startswith(cookie_name(config, "csrf") + "=")
