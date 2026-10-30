"""security-headers 規格：staging 與 production 的每個回應都帶安全標頭；開發與測試不送 HSTS；不送 CSP。"""
import pytest

from app import create_app
from app.config import PROJECT_ROOT, load_config
from tests.support.config_env import production_like_env

BASE = ("X-Content-Type-Options", "Referrer-Policy", "X-Frame-Options", "Permissions-Policy")
ALL_FIVE = (*BASE, "Strict-Transport-Security")


def make_client(env: str):
    environ = {"APP_ENV": env} if env in ("testing", "development") else production_like_env(env)
    if env == "development":
        environ.update({"SECRET_KEY": "k" * 32, "DATABASE_URL": "sqlite+pysqlite:///:memory:"})
    app = create_app(load_config(environ=environ))
    app.add_url_rule("/_t/boom", "boom", lambda: 1 / 0)
    app.add_url_rule("/_t/own-referrer", "own_referrer", lambda: ("ok", 200, {"Referrer-Policy": "no-referrer"}))
    return app.test_client()


@pytest.fixture(params=["production", "staging"])
def secure_client(request):
    return make_client(request.param)


@pytest.mark.parametrize("path", ["/", "/healthz", "/readyz", "/login", "/this-page-does-not-exist", "/static/css/theme.css", "/_t/boom"])
def test_every_response_in_staging_and_production_has_all_five_headers(secure_client, path):
    response = secure_client.get(path)
    for name in ALL_FIVE:
        assert name in response.headers, f"{path} 缺少 {name}（狀態 {response.status_code}）"


def test_the_error_paths_also_carry_the_headers(secure_client):
    rejected = secure_client.post("/api/auth/login", json={})              # 沒有 CSRF 權杖或來源 → 被擋下（4xx）
    assert 400 <= rejected.status_code < 500
    broken = secure_client.get("/_t/boom")
    assert broken.status_code == 500
    for response in (rejected, broken):
        for name in ALL_FIVE:
            assert name in response.headers


def test_header_values(secure_client):
    headers = secure_client.get("/").headers
    assert headers["Strict-Transport-Security"] == "max-age=31536000"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert headers["X-Frame-Options"] == "DENY"
    for feature in ("camera", "microphone", "geolocation", "payment", "usb"):
        assert f"{feature}=()" in headers["Permissions-Policy"]


def test_hsts_has_neither_subdomains_nor_preload(secure_client):
    value = secure_client.get("/").headers["Strict-Transport-Security"].lower()
    assert "includesubdomains" not in value and "preload" not in value


def test_an_endpoint_that_set_its_own_header_is_not_overridden(secure_client):
    assert secure_client.get("/_t/own-referrer").headers["Referrer-Policy"] == "no-referrer"


@pytest.mark.parametrize("env", ["development", "testing"])
def test_local_environments_never_send_hsts_but_keep_the_other_four(env):
    client = make_client(env)
    for path in ("/", "/healthz", "/this-page-does-not-exist"):
        headers = client.get(path).headers
        assert "Strict-Transport-Security" not in headers, f"{env} {path} 不該送 HSTS"
        for name in BASE:
            assert name in headers


@pytest.mark.parametrize("env", ["development", "testing", "staging", "production"])
def test_no_content_security_policy_is_sent_anywhere(env):
    client = make_client(env)
    for path in ("/", "/login", "/this-page-does-not-exist"):
        headers = client.get(path).headers
        assert "Content-Security-Policy" not in headers and "Content-Security-Policy-Report-Only" not in headers


def test_the_missing_csp_and_its_reason_are_documented():
    text = (PROJECT_ROOT / "docs" / "營運與部署-operations.md").read_text(encoding="utf-8-sig")
    assert "Content-Security-Policy" in text and "unsafe-eval" in text
