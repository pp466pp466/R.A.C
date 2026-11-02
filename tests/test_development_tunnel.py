"""development-tunnel 規格：development 經 HTTPS 通道（例如 ngrok）使用時，選擇信任轉送標頭；其餘環境拒絕這個設定。

通道只改變「請求看起來從哪裡來」（協定與主機），不改變 HSTS：瀏覽器會把 HSTS 記在通道網域，牽連之後的本機開發。
"""
from urllib.parse import parse_qs, urlsplit

import pytest

from app import create_app
from app.config import PROJECT_ROOT, ConfigError, load_config
from app.security.csrf import CSRF_HEADER
from tests.support.client import raw_client
from tests.support.config_env import production_like_env

TUNNEL_HOST = "abc-123.ngrok-free.dev"
TUNNEL_URL = f"https://{TUNNEL_HOST}"
FORWARDED = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": TUNNEL_HOST}
LEAK_MARKER = "leak-marker-value"
OPERATIONS = (PROJECT_ROOT / "docs" / "營運與部署-operations.md").read_text(encoding="utf-8-sig")


def dev_environ(tmp_path, **overrides: str) -> dict:
    return {"APP_ENV": "development", "SECRET_KEY": "k" * 32, "DATABASE_URL": "sqlite+pysqlite:///:memory:", **overrides}


def make_dev_app(tmp_path, **overrides: str):
    config = load_config(environ=dev_environ(tmp_path, **overrides), dotenv_path=tmp_path / "missing.env")
    app = create_app(config)
    app.add_url_rule("/api/_t/echo", "echo", lambda: {"ok": True}, methods=["POST"])
    return app


def post_through_tunnel(app, *, origin: str = TUNNEL_URL):
    """經通道的完整往返：先 GET 取得 CSRF cookie，再以同一個權杖與通道的 Origin 送出 POST。"""
    client = raw_client(app)
    client.get("/", headers=FORWARDED)
    token = client.get_cookie("csrf").value
    return client, client.post("/api/_t/echo", headers={**FORWARDED, "Origin": origin, CSRF_HEADER: token})


# ---- 開發環境可選擇信任通道的轉送標頭 ----------------------------------------------------------


def test_a_post_through_the_tunnel_passes_the_csrf_check_when_the_headers_are_trusted(tmp_path):
    app = make_dev_app(tmp_path, TRUST_PROXY_HEADERS="true", BASE_URL=TUNNEL_URL)
    _, response = post_through_tunnel(app)
    assert response.status_code == 200 and response.get_json() == {"ok": True}


def test_without_the_setting_the_same_post_is_still_rejected_because_origin_and_request_disagree(tmp_path):
    app = make_dev_app(tmp_path)
    _, response = post_through_tunnel(app)
    assert response.status_code == 403 and response.get_json()["error"]["code"] == "csrf_failed"


def test_trusting_the_headers_does_not_accept_a_foreign_origin(tmp_path):
    app = make_dev_app(tmp_path, TRUST_PROXY_HEADERS="true")
    _, response = post_through_tunnel(app, origin="https://evil.example.com")
    assert response.status_code == 403 and response.get_json()["error"]["code"] == "csrf_failed"


def test_the_setting_is_case_insensitive_and_false_keeps_the_old_behaviour(tmp_path):
    assert load_config(environ=dev_environ(tmp_path, TRUST_PROXY_HEADERS="TRUE"), dotenv_path=tmp_path / "x.env").trust_proxy is True
    assert load_config(environ=dev_environ(tmp_path, TRUST_PROXY_HEADERS="false"), dotenv_path=tmp_path / "x.env").trust_proxy is False
    assert load_config(environ=dev_environ(tmp_path), dotenv_path=tmp_path / "x.env").trust_proxy is False
    assert load_config(environ=dev_environ(tmp_path, TRUST_PROXY_HEADERS="  "), dotenv_path=tmp_path / "x.env").trust_proxy is False


# ---- 其他環境拒絕這個設定 ---------------------------------------------------------------------


@pytest.mark.parametrize("value", ["true", "false", LEAK_MARKER])
@pytest.mark.parametrize("env", ["testing", "staging", "production"])
def test_the_other_environments_refuse_to_start_and_name_only_the_variable(env, value):
    environ = {"APP_ENV": "testing"} if env == "testing" else production_like_env(env)
    environ["TRUST_PROXY_HEADERS"] = value
    with pytest.raises(ConfigError) as error:
        load_config(environ=environ)
    assert "TRUST_PROXY_HEADERS" in str(error.value)
    assert LEAK_MARKER not in str(error.value)


@pytest.mark.parametrize("value", ["yes", "1", "maybe", LEAK_MARKER])
def test_an_invalid_value_in_development_is_rejected_without_echoing_it(tmp_path, value):
    with pytest.raises(ConfigError) as error:
        load_config(environ=dev_environ(tmp_path, TRUST_PROXY_HEADERS=value), dotenv_path=tmp_path / "x.env")
    message = str(error.value)
    assert "TRUST_PROXY_HEADERS" in message and "true" in message and "false" in message
    assert value not in message


def test_staging_and_production_still_trust_the_proxy_without_any_switch():
    for env in ("staging", "production"):
        assert load_config(environ=production_like_env(env)).trust_proxy is True


# ---- 通道不讓開發環境送 HSTS -----------------------------------------------------------------


def test_development_through_the_tunnel_never_sends_hsts_but_keeps_the_other_four(tmp_path):
    app = make_dev_app(tmp_path, TRUST_PROXY_HEADERS="true", BASE_URL=TUNNEL_URL)
    client = raw_client(app)
    for path in ("/", "/healthz", "/this-page-does-not-exist"):
        headers = client.get(path, headers=FORWARDED).headers
        assert "Strict-Transport-Security" not in headers, path
        for name in ("X-Content-Type-Options", "Referrer-Policy", "X-Frame-Options", "Permissions-Policy"):
            assert name in headers


def test_development_cookies_stay_unprefixed_and_not_secure_even_through_the_tunnel(tmp_path):
    app = make_dev_app(tmp_path, TRUST_PROXY_HEADERS="true")
    response = raw_client(app).get("/", headers=FORWARDED)
    cookies = response.headers.getlist("Set-Cookie")
    assert cookies and all(not cookie.startswith("__Host-") and "Secure" not in cookie for cookie in cookies)


# ---- 通道網址作為對外網址並附使用注意事項 ---------------------------------------------------------


def test_the_google_callback_is_built_from_the_tunnel_base_url(tmp_path):
    app = make_dev_app(
        tmp_path, TRUST_PROXY_HEADERS="true", BASE_URL=TUNNEL_URL + "/",
        GOOGLE_OAUTH_CLIENT_ID="client-id.apps.example", GOOGLE_OAUTH_CLIENT_SECRET="client-secret-for-tests",
    )
    url = app.extensions["identity_provider"].build_authorization_url(state="s", nonce="n", code_challenge="c")
    redirect = parse_qs(urlsplit(url).query)["redirect_uri"][0]
    assert redirect == f"{TUNNEL_URL}/auth/google/callback"


def section(text: str, heading: str) -> str:
    lines = text.split("\n")
    start = next(i for i, line in enumerate(lines) if line.strip() == heading)
    level = len(heading) - len(heading.lstrip("#"))
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("#") and len(lines[i]) - len(lines[i].lstrip("#")) <= level), len(lines))
    return "\n".join(lines[start:end])


def test_the_operations_manual_documents_the_tunnel_steps_the_exposure_risk_and_the_access_control():
    text = section(OPERATIONS, "### 開發用通道（ngrok）")
    for step in ("ngrok http", "BASE_URL", "TRUST_PROXY_HEADERS=true", "/auth/google/callback"):
        assert step in text, f"通道小節缺少步驟：{step}"
    assert "暴露" in text and "網際網路" in text and "開發工具" in text and "測試帳號" in text
    assert "存取控制" in text and "基本驗證" in text
    assert "授權重新導向 URI" in text and "同步" in text
    assert "不用於預備" in text or "不得用於預備" in text or "不可用於預備" in text
    assert "HSTS" in text
