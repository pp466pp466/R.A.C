"""session-authentication 與 app-configuration 規格：登入 cookie 的設定、簽章金鑰輪替、可注入的執行期依賴。"""
from datetime import datetime, timedelta, timezone

import pytest

from app import MAX_REQUEST_BYTES, create_app
from app import runtime
from app.config import load_config
from app.security.cookies import SESSION_LIFETIME, cookie_name
from tests.support.config_env import production_like_env


def make_app(env: str, **overrides: str):
    if env == "testing":
        environ = {"APP_ENV": "testing", **overrides}
    elif env == "development":
        environ = {"APP_ENV": "development", "SECRET_KEY": "k", "DATABASE_URL": "sqlite+pysqlite:///:memory:", **overrides}
    else:
        environ = production_like_env(env, **overrides)
    return create_app(load_config(environ=environ, dotenv_path="__none__.env"))


# ---- 登入 cookie 的屬性 -------------------------------------------------------

@pytest.mark.parametrize("env", ["production", "staging"])
def test_production_like_cookie_is_host_prefixed_secure_and_host_only(env):
    config = make_app(env).config
    assert config["SESSION_COOKIE_NAME"] == "__Host-session"
    assert config["SESSION_COOKIE_SECURE"] is True
    assert config["SESSION_COOKIE_HTTPONLY"] is True
    assert config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert config["SESSION_COOKIE_PATH"] == "/"          # __Host- 要求 Path=/
    assert config["SESSION_COOKIE_DOMAIN"] is None       # __Host- 禁止 Domain


@pytest.mark.parametrize("env", ["development", "testing"])
def test_development_cookie_has_plain_name_and_no_secure_flag(env):
    config = make_app(env).config
    assert config["SESSION_COOKIE_NAME"] == "session"
    assert config["SESSION_COOKIE_SECURE"] is False
    assert config["SESSION_COOKIE_HTTPONLY"] is True
    assert config["SESSION_COOKIE_SAMESITE"] == "Lax"


def test_cookie_lives_thirty_idle_days_and_is_renewed_by_use():
    app = make_app("testing")
    assert app.config["PERMANENT_SESSION_LIFETIME"] == timedelta(days=30) == SESSION_LIFETIME
    from app.security.session import SlidingSessionInterface

    assert isinstance(app.session_interface, SlidingSessionInterface)


def test_cookie_name_helper_follows_the_environment():
    assert cookie_name(load_config(environ=production_like_env("production")), "csrf") == "__Host-csrf"
    assert cookie_name(load_config(environ={"APP_ENV": "testing"}), "csrf") == "csrf"


# ---- 簽章金鑰輪替 -------------------------------------------------------------

def test_secret_key_fallbacks_are_passed_to_flask_and_the_current_key_signs():
    app = make_app("development", SECRET_KEY_FALLBACKS="old-key-1,old-key-2")
    assert app.config["SECRET_KEY_FALLBACKS"] == ["old-key-1", "old-key-2"]
    assert app.config["SECRET_KEY"] == "k"


def test_old_key_signed_cookie_is_still_accepted_and_resigned_with_the_new_key():
    old = make_app("development", SECRET_KEY="the-old-signing-key")
    old.add_url_rule("/_set", "set", lambda: (__import__("flask").session.update(uid=7) or "ok"))
    with old.test_client() as client:
        client.get("/_set")
        cookie = client.get_cookie("session")
    assert cookie is not None

    rotated = make_app("development", SECRET_KEY="the-new-signing-key", SECRET_KEY_FALLBACKS="the-old-signing-key")
    rotated.add_url_rule("/_read", "read", lambda: str(__import__("flask").session.get("uid")))
    client = rotated.test_client()
    client.set_cookie("session", cookie.value)
    assert client.get("/_read").get_data(as_text=True) == "7"        # 舊金鑰簽的仍有效

    unrelated = make_app("development", SECRET_KEY="a-completely-different-key")
    unrelated.add_url_rule("/_read", "read", lambda: str(__import__("flask").session.get("uid")))
    other = unrelated.test_client()
    other.set_cookie("session", cookie.value)
    assert other.get("/_read").get_data(as_text=True) == "None"      # 沒有列入輪替的金鑰不被接受


# ---- 請求本文上限 -------------------------------------------------------------

def test_request_body_limit_is_64_kb():
    assert MAX_REQUEST_BYTES == 64 * 1024
    assert make_app("testing").config["MAX_CONTENT_LENGTH"] == 64 * 1024


# ---- 可注入的執行期依賴 -------------------------------------------------------

def test_default_clock_is_timezone_aware_utc():
    app = make_app("testing")
    with app.app_context():
        now = runtime.get_clock()()
    assert now.tzinfo is not None and now.utcoffset() == timedelta(0)
    assert abs((datetime.now(timezone.utc) - now).total_seconds()) < 5


def test_injected_dependencies_are_returned_by_the_runtime_accessors():
    class FakeMail:
        pass

    class FakeProvider:
        pass

    fixed = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    mail, provider = FakeMail(), FakeProvider()
    app = create_app(
        load_config(environ={"APP_ENV": "testing"}),
        mail_sender=mail, identity_provider=provider, clock=lambda: fixed,
    )
    with app.app_context():
        assert runtime.get_mail_sender() is mail
        assert runtime.get_identity_provider() is provider
        assert runtime.get_clock()() == fixed
        assert runtime.get_config().env == "testing"


def test_without_injection_and_without_settings_the_optional_services_are_absent():
    app = make_app("testing")
    with app.app_context():
        assert runtime.get_mail_sender() is None
        assert runtime.get_identity_provider() is None
