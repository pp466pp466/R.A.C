"""session-authentication 規格：認證相關的 API 回應與含權杖的頁面禁止被快取。"""
import pytest

from app import create_app
from app.config import load_config


@pytest.fixture()
def client():
    app = create_app(load_config(environ={"APP_ENV": "testing"}))
    app.add_url_rule("/api/auth/_t/ok", "auth_ok", lambda: {"ok": True})
    app.add_url_rule("/api/auth/_t/boom", "auth_boom", lambda: 1 / 0)
    app.add_url_rule("/api/other", "other_api", lambda: {"ok": True})
    return app.test_client()


@pytest.mark.parametrize("path", ["/api/auth/_t/ok", "/api/auth/_t/boom", "/api/auth/_t/missing"])
def test_every_auth_api_response_including_errors_is_not_cacheable(client, path):
    assert client.get(path).headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("path", ["/", "/this-page-does-not-exist", "/dev/components"])
def test_html_pages_are_not_cacheable_because_they_embed_a_csrf_token(client, path):
    response = client.get(path)
    assert response.mimetype == "text/html"
    assert response.headers["Cache-Control"] == "no-store"


def test_static_assets_stay_cacheable(client):
    response = client.get("/static/css/theme.css")
    assert response.status_code == 200
    assert response.headers.get("Cache-Control") != "no-store"


def test_unrelated_json_endpoints_are_left_alone(client):
    assert client.get("/api/other").headers.get("Cache-Control") != "no-store"


def test_health_checks_keep_their_own_no_store(client):
    assert client.get("/healthz").headers["Cache-Control"] == "no-store"


def test_an_endpoint_that_chose_its_own_cache_policy_is_not_overridden(client):
    """例如 /favicon.ico 的長期快取：它的內容類型預設是 HTML，但不該被改成 no-store。"""
    assert "max-age" in client.get("/favicon.ico").headers["Cache-Control"]
