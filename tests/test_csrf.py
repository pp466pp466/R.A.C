"""csrf-protection 規格：所有非 GET 請求的權杖與來源檢查、權杖的發放與輪替。

這裡一律使用 raw_client：它不會自動帶來源與權杖，才能精確控制每個請求帶了什麼。
"""
import re

import pytest
from flask import render_template_string, session

from app import create_app
from app.config import load_config
from app.security.csrf import CSRF_HEADER, FAILURE_MESSAGE, rotate_csrf_token
from tests.support.auth import FakeClock
from tests.support.client import raw_client
from tests.support.config_env import production_like_env

ORIGIN = "http://localhost"
UNSAFE = ("POST", "PUT", "PATCH", "DELETE")


def make_app(env="testing", **kwargs):
    config = load_config(environ={"APP_ENV": "testing"} if env == "testing" else production_like_env(env))
    app = create_app(config, **kwargs)
    app.calls = []

    @app.route("/api/_t/echo", methods=["GET", *UNSAFE])
    def echo():
        app.calls.append("echo")
        return {"ok": True}

    @app.post("/api/_t/login")
    def login():
        app.calls.append("login")
        session.clear()
        session["uid"] = 1
        rotate_csrf_token()
        return {"ok": True}

    @app.get("/_t/page")
    def page():
        return render_template_string("<meta name='csrf-token' content='{{ csrf_token() }}'>")

    return app


@pytest.fixture()
def app():
    return make_app()


@pytest.fixture()
def client(app):
    return raw_client(app)


def cookie_value(client, name="csrf") -> str:
    cookie = client.get_cookie(name)
    assert cookie is not None, f"沒有 {name} cookie"
    return cookie.value


def token_of(client) -> str:
    client.get("/")
    return cookie_value(client)


def send(client, method="POST", path="/api/_t/echo", *, token=None, origin=ORIGIN, referer=None, **kwargs):
    headers = {}
    if token is not None:
        headers[CSRF_HEADER] = token
    if origin is not None:
        headers["Origin"] = origin
    if referer is not None:
        headers["Referer"] = referer
    return client.open(path, method=method, headers=headers, **kwargs)


def assert_rejected(response):
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "csrf_failed"
    assert response.get_json()["error"]["message"] == FAILURE_MESSAGE


# ---- 所有非 GET 請求都要檢查 ---------------------------------------------------

def test_a_post_without_a_token_is_rejected_and_the_view_never_runs(app, client):
    assert_rejected(send(client, token=None))
    assert app.calls == []


@pytest.mark.parametrize("method", UNSAFE)
def test_every_unsafe_method_is_checked(app, client, method):
    assert_rejected(send(client, method))
    assert send(client, method, token=token_of(client)).status_code == 200


def test_a_wrong_token_is_rejected(app, client):
    token_of(client)
    assert_rejected(send(client, token="not-the-token-in-the-cookie"))
    assert app.calls == []


def test_the_right_token_and_origin_are_accepted(app, client):
    assert send(client, token=token_of(client)).status_code == 200
    assert app.calls == ["echo"]


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_safe_methods_need_no_token(app, client, method):
    assert client.open("/api/_t/echo", method=method).status_code in (200, 204)


def test_login_style_endpoints_need_a_token_too(app, client):
    assert_rejected(send(client, path="/api/_t/login"))
    assert app.calls == []


def test_no_unsafe_route_in_the_app_is_exempt(client):
    """走訪應用程式註冊的每一條可寫入路由：沒有權杖時全部必須是 403 csrf_failed。"""
    checked = 0
    for rule in client.application.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        for method in sorted(rule.methods & set(UNSAFE)):
            path = re.sub(r"<[^>]+>", "x", rule.rule)
            response = send(client, method, path)
            assert response.status_code == 403, f"{method} {rule.rule} 沒有受到 CSRF 保護"
            assert response.get_json()["error"]["code"] == "csrf_failed"
            checked += 1
    assert checked >= 3


# ---- 權杖不可偽造、只接受標頭 -------------------------------------------------

def test_a_token_the_server_never_issued_is_rejected_even_when_cookie_and_header_match(app, client):
    client.set_cookie("csrf", "attacker-chosen-value")
    assert_rejected(send(client, token="attacker-chosen-value"))


def test_a_token_signed_with_another_key_is_rejected(app, client):
    other = create_app(load_config(environ={"APP_ENV": "testing", "SECRET_KEY": "a-different-secret-key-for-attacker"}))
    other_client = raw_client(other)
    forged = token_of(other_client)
    client.set_cookie("csrf", forged)
    assert_rejected(send(client, token=forged))


def test_the_token_is_only_accepted_from_the_header(app, client):
    token = token_of(client)
    assert_rejected(send(client, path=f"/api/_t/echo?csrf_token={token}"))
    assert_rejected(send(client, data={"csrf_token": token}))
    assert_rejected(send(client, json={"csrf_token": token}))
    assert app.calls == []


def test_a_header_without_the_cookie_is_rejected(app, client):
    token = token_of(client)
    fresh = raw_client(app)                         # 另一個瀏覽器：沒有 cookie
    assert_rejected(send(fresh, token=token))


def test_an_expired_token_is_rejected():
    clock = FakeClock()
    app = make_app(clock=clock)
    client = raw_client(app)
    token = token_of(client)
    clock.advance(days=6)
    assert send(client, token=token).status_code == 200
    clock.advance(days=2)
    assert_rejected(send(client, token=token))


# ---- 來源 ---------------------------------------------------------------------

def test_a_request_from_another_site_is_rejected_even_with_a_valid_token(app, client):
    token = token_of(client)
    assert_rejected(send(client, token=token, origin="https://evil.example"))
    assert app.calls == []


@pytest.mark.parametrize("origin", ["null", "http://localhost:8080", "https://localhost", "http://localhost.evil.example", "not a url", ""])
def test_origins_that_do_not_match_this_site_exactly_are_rejected(app, client, origin):
    assert_rejected(send(client, token=token_of(client), origin=origin))


def test_the_origin_comparison_ignores_case(app, client):
    assert send(client, token=token_of(client), origin="HTTP://LOCALHOST").status_code == 200


def test_without_origin_the_referer_is_used(app, client):
    token = token_of(client)
    assert send(client, token=token, origin=None, referer="http://localhost/login").status_code == 200
    assert_rejected(send(client, token=token, origin=None, referer="https://evil.example/login"))


def test_without_origin_and_referer_the_request_is_rejected(app, client):
    assert_rejected(send(client, token=token_of(client), origin=None))


def test_a_present_but_foreign_origin_is_not_rescued_by_a_matching_referer(app, client):
    assert_rejected(send(client, token=token_of(client), origin="https://evil.example", referer="http://localhost/x"))


def test_staging_compares_against_the_public_https_origin_behind_the_proxy():
    app = make_app("staging")
    client = raw_client(app)
    proxy = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "cs.example.com"}
    client.get("/", headers=proxy)
    token = cookie_value(client, "__Host-csrf")
    good = client.post("/api/_t/echo", headers={**proxy, "Origin": "https://cs.example.com", CSRF_HEADER: token})
    assert good.status_code == 200
    plain_http = client.post("/api/_t/echo", headers={**proxy, "Origin": "http://cs.example.com", CSRF_HEADER: token})
    assert_rejected(plain_http)
    internal = client.post("/api/_t/echo", headers={**proxy, "Origin": "http://localhost", CSRF_HEADER: token})
    assert_rejected(internal)


# ---- 發放與輪替 ---------------------------------------------------------------

def test_the_first_visit_gets_a_token_in_the_page_and_a_matching_cookie(client):
    response = client.get("/_t/page")
    meta = re.search(r"content='([^']+)'", response.get_data(as_text=True)).group(1)
    assert meta == cookie_value(client)
    header = response.headers.get("Set-Cookie")
    assert header.startswith("csrf=") and "HttpOnly" in header and "SameSite=Lax" in header and "Path=/" in header


def test_a_valid_token_is_reused_and_not_reissued(client):
    first = client.get("/_t/page")
    token = cookie_value(client)
    second = client.get("/_t/page")
    assert re.search(r"content='([^']+)'", second.get_data(as_text=True)).group(1) == token
    assert second.headers.get("Set-Cookie") is None


def test_an_invalid_cookie_is_replaced_with_a_fresh_token(client):
    client.set_cookie("csrf", "tampered")
    response = client.get("/_t/page")
    assert response.headers.get("Set-Cookie", "").startswith("csrf=")
    assert cookie_value(client) != "tampered"


def test_the_token_is_not_in_the_url_and_the_cookie_is_not_readable_by_script(client):
    response = client.get("/_t/page")
    assert "csrf" not in response.request.full_path
    assert "HttpOnly" in response.headers["Set-Cookie"]


def test_production_like_cookies_have_the_host_prefix_and_secure():
    client = raw_client(make_app("production"))
    header = client.get("/").headers["Set-Cookie"]
    assert header.startswith("__Host-csrf=") and "Secure" in header and "Domain" not in header


def test_logging_in_rotates_the_token_so_the_one_from_before_login_stops_working(app, client):
    before = token_of(client)
    assert send(client, path="/api/_t/login", token=before).status_code == 200
    after = cookie_value(client)
    assert after != before
    assert_rejected(send(client, token=before))
    assert send(client, token=after).status_code == 200


def test_the_csrf_cookie_is_separate_from_the_login_cookie(app, client):
    token = token_of(client)
    send(client, path="/api/_t/login", token=token)
    session_cookie = cookie_value(client, "session")
    csrf_cookie = cookie_value(client, "csrf")
    assert session_cookie != csrf_cookie
    assert csrf_cookie not in session_cookie and token not in session_cookie     # 登入 cookie 內容不含 CSRF 權杖


def test_pages_that_embed_a_token_are_marked_as_varying_on_cookie(client):
    assert "Cookie" in client.get("/_t/page").headers.get("Vary", "")


# ---- 日誌 ---------------------------------------------------------------------

def test_a_rejection_is_logged_as_an_event_without_the_token(app, client, log_lines):
    token = token_of(client)
    send(client, token="a-wrong-token-value")
    send(client, token=token, origin="https://evil.example")
    events = [line["event"] for line in log_lines() if "event" in line]
    assert events == ["security.csrf_rejected", "security.csrf_rejected"]
    assert token not in log_lines.raw() and "a-wrong-token-value" not in log_lines.raw()


# ---- 回應格式 -----------------------------------------------------------------

def test_a_rejected_page_request_gets_a_chinese_error_page(app, client):
    app.add_url_rule("/_t/form", "form", lambda: "ok", methods=["POST"])
    response = client.post("/_t/form", headers={"Origin": ORIGIN})
    assert response.status_code == 403
    assert response.mimetype == "text/html"
    assert "重新整理" in response.get_data(as_text=True)
