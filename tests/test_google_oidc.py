"""google-sign-in 規格「授權碼流程的防護」與「ID token 驗證」：以測試產生的 RSA 金鑰與假傳輸層，不連網。"""
import base64
import hashlib
import json
import re
import time
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from app import create_app
from app.config import GoogleSettings, Secret, load_config
from app.services.google_oidc import (
    AUTHORIZATION_URL, HTTP_TIMEOUT_SECONDS, JWKS_URL, TOKEN_URL, GoogleAuthError, GoogleOidcClient, UrllibTransport,
    code_challenge_for, new_code_verifier, new_state_or_nonce,
)

CLIENT_ID = "538420027289-test.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-fake-client-secret-for-tests"
REDIRECT = "http://localhost:5000/auth/google/callback"
NONCE = "nonce-value-from-the-browser-binding"

PRIVATE = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_PRIVATE = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwk_for(key, kid="k1") -> dict:
    data = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    data.update(kid=kid, alg="RS256", use="sig")
    return data


def claims(**overrides) -> dict:
    now = int(time.time())
    base = {
        "iss": "https://accounts.google.com", "aud": CLIENT_ID, "sub": "110169484474386276334",
        "email": "person@example.com", "email_verified": True, "name": "王小明", "nonce": NONCE,
        "iat": now, "exp": now + 3600,
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def token(overrides=None, *, key=PRIVATE, kid="k1", alg="RS256", drop=()) -> str:
    payload = claims(**(overrides or {}))
    for name in drop:
        payload.pop(name, None)
    return jwt.encode(payload, key, algorithm=alg, headers={"kid": kid})


class FakeTransport:
    def __init__(self, id_token: str | None = None) -> None:
        self.posts: list[tuple] = []
        self.gets: list[tuple] = []
        self.token_response = (200, {"id_token": id_token if id_token is not None else token()})
        self.jwks_response = (200, {"keys": [jwk_for(PRIVATE)]})
        self.post_error: Exception | None = None
        self.get_error: Exception | None = None

    def post_form(self, url, data, timeout):
        self.posts.append((url, data, timeout))
        if self.post_error:
            raise self.post_error
        return self.token_response

    def get_json(self, url, timeout):
        self.gets.append((url, timeout))
        if self.get_error:
            raise self.get_error
        return self.jwks_response


def make_client(transport=None, **kwargs) -> tuple[GoogleOidcClient, FakeTransport]:
    transport = transport or FakeTransport()
    settings = GoogleSettings(client_id=CLIENT_ID, client_secret=Secret(CLIENT_SECRET))
    return GoogleOidcClient(settings, redirect_uri=REDIRECT, transport=transport, **kwargs), transport


def exchange(client):
    return client.exchange_code(code="auth-code-from-google", code_verifier="v" * 64, expected_nonce=NONCE)


def reason_of(client) -> str:
    with pytest.raises(GoogleAuthError) as exc:
        exchange(client)
    return exc.value.reason


# =============================================================================
# 授權網址與 PKCE
# =============================================================================

def test_the_authorization_url_carries_every_protection_parameter():
    client, _ = make_client()
    url = client.build_authorization_url(state="the-state", nonce="the-nonce", code_challenge="the-challenge")
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == AUTHORIZATION_URL
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert query == {
        "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT, "scope": "openid email profile",
        "state": "the-state", "nonce": "the-nonce", "code_challenge": "the-challenge",
        "code_challenge_method": "S256", "prompt": "select_account",
    }
    assert CLIENT_SECRET not in url


def test_the_scope_is_exactly_openid_email_profile():
    client, _ = make_client()
    query = parse_qs(urlsplit(client.build_authorization_url(state="s", nonce="n", code_challenge="c")).query)
    assert query["scope"] == ["openid email profile"]


def test_the_pkce_challenge_is_the_s256_of_the_verifier():
    verifier = new_code_verifier()
    assert 43 <= len(verifier) <= 128 and re.fullmatch(r"[A-Za-z0-9_\-]+", verifier)
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert code_challenge_for(verifier) == expected and "=" not in expected
    # RFC 7636 附錄 B 的範例
    assert code_challenge_for("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_verifiers_states_and_nonces_are_random():
    assert len({new_code_verifier() for _ in range(20)}) == 20
    assert len({new_state_or_nonce() for _ in range(20)}) == 20
    assert len(new_state_or_nonce()) >= 43


# =============================================================================
# 授權碼交換
# =============================================================================

def test_a_valid_exchange_returns_the_verified_profile_and_posts_the_right_form():
    client, transport = make_client()
    profile = exchange(client)
    assert (profile.subject, profile.email, profile.email_verified, profile.name) == (
        "110169484474386276334", "person@example.com", True, "王小明",
    )
    (url, form, timeout), = transport.posts
    assert url == TOKEN_URL and timeout == HTTP_TIMEOUT_SECONDS == 5.0
    assert form == {
        "code": "auth-code-from-google", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT, "grant_type": "authorization_code", "code_verifier": "v" * 64,
    }
    assert transport.gets == [(JWKS_URL, 5.0)]


def test_the_alternate_issuer_spelling_is_accepted():
    client, _ = make_client(FakeTransport(token({"iss": "accounts.google.com"})))
    assert exchange(client).subject


def test_a_missing_name_is_none():
    client, _ = make_client(FakeTransport(token(drop=("name",))))
    assert exchange(client).name is None


# =============================================================================
# ID token 驗證：每一個拒絕情境
# =============================================================================

@pytest.mark.parametrize(
    "overrides",
    [
        {"email_verified": False},
        {"email_verified": "false"},
        {"email_verified": None},                      # 欄位不存在
        {"aud": "someone-elses-client-id.apps.googleusercontent.com"},
        {"aud": ["a", "b"]},
        {"iss": "https://evil.example"},
        {"iss": "accounts.google.com.evil.example"},
        {"nonce": "a-different-nonce"},
        {"nonce": None},
        {"exp": int(time.time()) - 600},
        {"iat": int(time.time()) + 3600, "exp": int(time.time()) + 7200},
        {"sub": ""},
        {"sub": "x" * 256},
        {"sub": 12345},
        {"email": ""},
        {"email": None},
    ],
)
def test_a_token_with_a_bad_claim_is_rejected(overrides):
    client, _ = make_client(FakeTransport(token(overrides)))
    assert reason_of(client) == "token"


@pytest.mark.parametrize("missing", ["exp", "iat", "iss", "aud", "sub"])
def test_required_claims_must_be_present(missing):
    client, _ = make_client(FakeTransport(token(drop=(missing,))))
    assert reason_of(client) == "token"


def test_the_clock_leeway_is_sixty_seconds():
    just_expired = FakeTransport(token({"exp": int(time.time()) - 30}))
    assert exchange(make_client(just_expired)[0]).subject
    long_expired = FakeTransport(token({"exp": int(time.time()) - 120}))
    assert reason_of(make_client(long_expired)[0]) == "token"


def test_a_signature_from_another_key_is_rejected():
    client, _ = make_client(FakeTransport(token(key=OTHER_PRIVATE)))
    assert reason_of(client) == "token"


@pytest.mark.parametrize("alg", ["HS256", "none"])
def test_algorithms_other_than_rs256_are_rejected(alg):
    secret = "a-shared-secret-that-is-long-enough-for-hs256" if alg == "HS256" else None
    forged = jwt.encode(claims(), secret, algorithm=alg, headers={"kid": "k1"})
    assert reason_of(make_client(FakeTransport(forged))[0]) == "token"


def test_the_algorithm_confusion_attack_with_the_public_key_as_hmac_secret_is_rejected():
    """攻擊者拿公開金鑰當 HMAC 密鑰簽 HS256；只接受 RS256 就能擋下。"""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "k1"}).encode()).rstrip(b"=")
    body = base64.urlsafe_b64encode(json.dumps(claims()).encode()).rstrip(b"=")
    forged = (header + b"." + body + b"." + base64.urlsafe_b64encode(b"x" * 32).rstrip(b"=")).decode()
    assert reason_of(make_client(FakeTransport(forged))[0]) == "token"


def test_a_token_without_a_kid_or_with_an_unknown_kid_is_rejected():
    no_kid = jwt.encode(claims(), PRIVATE, algorithm="RS256")
    assert reason_of(make_client(FakeTransport(no_kid))[0]) == "token"
    assert reason_of(make_client(FakeTransport(token(kid="unknown-key-id")))[0]) == "token"


@pytest.mark.parametrize("garbage", ["", "not.a.jwt", "x" * 50, "a.b", "....."])
def test_a_malformed_token_is_rejected(garbage):
    assert reason_of(make_client(FakeTransport(garbage))[0]) == "token"


# =============================================================================
# 交換授權碼的失敗
# =============================================================================

@pytest.mark.parametrize("response", [(400, {"error": "invalid_grant"}), (401, {"error": "invalid_client"}), (200, {}), (200, None), (200, {"id_token": 5}), (200, "text")])
def test_google_refusing_the_code_or_an_unexpected_body_is_a_rejection(response):
    transport = FakeTransport()
    transport.token_response = response
    assert reason_of(make_client(transport)[0]) == "token"


@pytest.mark.parametrize("error", [TimeoutError("timed out"), ConnectionRefusedError(), OSError("network down")])
def test_network_problems_exchanging_the_code_are_unavailable(error):
    transport = FakeTransport()
    transport.post_error = error
    assert reason_of(make_client(transport)[0]) == "unavailable"


def test_a_google_server_error_is_unavailable():
    transport = FakeTransport()
    transport.token_response = (503, {"error": "backend_error"})
    assert reason_of(make_client(transport)[0]) == "unavailable"


@pytest.mark.parametrize("failure", ["error", "status", "body"])
def test_failing_to_fetch_the_public_keys_is_unavailable(failure):
    transport = FakeTransport()
    if failure == "error":
        transport.get_error = TimeoutError("timed out")
    elif failure == "status":
        transport.jwks_response = (503, None)
    else:
        transport.jwks_response = (200, {"unexpected": True})
    assert reason_of(make_client(transport)[0]) == "unavailable"


def test_the_client_secret_never_appears_in_errors():
    transport = FakeTransport()
    transport.post_error = TimeoutError(f"timed out while sending {CLIENT_SECRET}")
    client, _ = make_client(transport)
    with pytest.raises(GoogleAuthError) as exc:
        exchange(client)
    assert CLIENT_SECRET not in str(exc.value) and CLIENT_SECRET not in repr(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


# =============================================================================
# 公開金鑰的快取
# =============================================================================

def test_the_public_keys_are_cached_between_logins_and_refetched_after_an_hour():
    now = [0.0]
    client, transport = make_client(monotonic=lambda: now[0])
    exchange(client)
    transport.token_response = (200, {"id_token": token()})
    exchange(client)
    assert len(transport.gets) == 1                          # 第二次登入沒有重新抓
    now[0] = 3601.0
    exchange(client)
    assert len(transport.gets) == 2


def test_an_unknown_key_id_triggers_one_refetch_to_follow_key_rotation():
    client, transport = make_client(FakeTransport(token(kid="k1")))
    exchange(client)                                         # 先快取 k1
    transport.jwks_response = (200, {"keys": [jwk_for(PRIVATE, "k1"), jwk_for(OTHER_PRIVATE, "k2")]})
    transport.token_response = (200, {"id_token": token(key=OTHER_PRIVATE, kid="k2")})
    assert exchange(client).subject
    assert len(transport.gets) == 2


def test_a_key_id_missing_even_after_a_refetch_is_rejected_without_endless_refetching():
    client, transport = make_client(FakeTransport(token(kid="never-published")))
    assert reason_of(client) == "token"
    assert len(transport.gets) == 1


# =============================================================================
# 真實的傳輸層與由設定建立
# =============================================================================

def test_the_real_transport_uses_tls_verification_and_a_timeout(monkeypatch):
    captured = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(request, timeout, context):
        captured.update(url=request.full_url, method=request.get_method(), timeout=timeout, context=context, data=request.data)
        return Response()

    monkeypatch.setattr("app.services.google_oidc.urllib.request.urlopen", fake_urlopen)
    status, body = UrllibTransport().post_form(TOKEN_URL, {"code": "a b&c"}, 5.0)
    assert (status, body) == (200, {"ok": True})
    assert captured["url"] == TOKEN_URL and captured["method"] == "POST" and captured["timeout"] == 5.0
    assert captured["data"] == b"code=a+b%26c"
    import ssl
    assert captured["context"].verify_mode == ssl.CERT_REQUIRED and captured["context"].check_hostname


def test_create_app_builds_the_real_client_only_when_google_is_configured():
    configured = load_config(environ={
        "APP_ENV": "testing", "BASE_URL": "http://localhost:5000",
        "GOOGLE_OAUTH_CLIENT_ID": CLIENT_ID, "GOOGLE_OAUTH_CLIENT_SECRET": CLIENT_SECRET,
    })
    client = create_app(configured).extensions["identity_provider"]
    assert isinstance(client, GoogleOidcClient) and client._redirect_uri == "http://localhost:5000/auth/google/callback"

    class Fake:
        pass

    fake = Fake()
    assert create_app(configured, identity_provider=fake).extensions["identity_provider"] is fake
    assert create_app(load_config(environ={"APP_ENV": "testing"})).extensions["identity_provider"] is None
