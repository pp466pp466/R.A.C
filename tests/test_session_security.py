"""session-authentication 規格：登入 cookie 的屬性、辨識與撤銷、登出、存活檢查不碰資料庫。"""
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import unquote

import pytest
from flask import session
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.controllers.request_helpers import read_json_object
from app.models import Base, User
from app.models.user import STATUS_SUSPENDED
from app.security.redirects import safe_next
from app.security.session import current_user, login_required_api, login_required_page, login_user, logout_user
from app.repositories.user_repository import UserRepository
from app.db.session import get_session
from app.services.registry import get_auth_service
from tests.support.auth import GOOD_PASSWORD, add_user
from tests.support.config_env import production_like_env

CREDENTIALS = {"email": "user1@example.com", "password": GOOD_PASSWORD}


def make_app(env="testing"):
    config = load_config(environ={"APP_ENV": "testing"} if env == "testing" else production_like_env(env))
    app = create_app(config)
    engine = app.extensions["db_engine"]
    Base.metadata.create_all(engine)
    app.resolutions = 0

    @app.post("/api/_t/login")
    def t_login():
        """代表「通過登入」（玩家的 Google 登入或管理員的兩步驟都在別處測試）：以該信箱目前的帳號快照建立登入狀態。"""
        body = read_json_object()
        user = UserRepository(get_session()).get_by_email(body["email"])
        login_user(get_auth_service().resolve_session(user.id, user.token_version))
        return {"ok": True}

    @app.post("/api/_t/logout")
    def t_logout():
        logout_user()
        return {"ok": True}

    @app.post("/api/_t/junk")
    def t_junk():
        session["junk"] = "left-over-from-before-login"
        return {"ok": True}

    @app.get("/api/_t/me")
    @login_required_api
    def t_me():
        return {"id": current_user().id, "again": current_user().id}

    @app.get("/_t/secret")
    @login_required_page
    def t_secret():
        return "secret page"

    return app


@pytest.fixture()
def app():
    app = make_app()
    with Session(app.extensions["db_engine"]) as s:
        add_user(s, 1)
    return app


def serializer(app):
    return app.session_interface.get_signing_serializer(app)


def login(client, **overrides):
    return client.post("/api/_t/login", json={**CREDENTIALS, **overrides})


def session_cookie_header(response) -> str | None:
    return next((h for h in response.headers.getlist("Set-Cookie") if h.startswith(("session=", "__Host-session="))), None)


# ---- 登入 cookie 的內容與屬性 --------------------------------------------------

def test_login_sets_a_signed_cookie_holding_only_the_user_id_and_token_version(app):
    client = app.test_client()
    response = login(client)
    assert response.status_code == 200
    payload = serializer(app).loads(client.get_cookie("session").value)
    assert set(payload) == {"uid", "tv"}                      # 不含 _permanent 或任何其他東西
    assert payload["tv"] == 1 and isinstance(payload["uid"], int)


def test_the_cookie_attributes_in_development(app):
    header = session_cookie_header(login(app.test_client()))
    assert "HttpOnly" in header and "SameSite=Lax" in header and "Path=/" in header
    assert "Secure" not in header and "Domain" not in header and header.startswith("session=")
    expires = parsedate_to_datetime(next(p.split("=", 1)[1] for p in header.split("; ") if p.startswith("Expires=")))
    assert abs((expires - (datetime.now(timezone.utc) + timedelta(days=30))).total_seconds()) < 120


@pytest.mark.parametrize("env", ["production", "staging"])
def test_the_cookie_attributes_in_production_like_environments(env):
    app = make_app(env)
    with Session(app.extensions["db_engine"]) as s:
        add_user(s, 1)
    header = session_cookie_header(login(app.test_client()))
    assert header.startswith("__Host-session=")
    assert "Secure" in header and "HttpOnly" in header and "SameSite=Lax" in header and "Path=/" in header
    assert "Domain" not in header


def test_every_use_renews_the_cookie_with_a_fresh_signature_and_expiry(app):
    client = app.test_client()
    login(client)
    for _ in range(3):
        response = client.get("/api/_t/me")
        assert response.status_code == 200
        header = session_cookie_header(response)
        assert header is not None                              # 每次辨識出登入者的回應都重發
        expires = parsedate_to_datetime(next(p.split("=", 1)[1] for p in header.split("; ") if p.startswith("Expires=")))
        assert abs((expires - (datetime.now(timezone.utc) + timedelta(days=30))).total_seconds()) < 120
        assert set(serializer(app).loads(client.get_cookie("session").value)) == {"uid", "tv"}      # 內容仍只有 uid、tv


def test_a_response_that_never_touched_the_session_does_not_reissue_the_cookie(app):
    client = app.test_client()
    login(client)
    assert session_cookie_header(client.get("/healthz")) is None
    assert session_cookie_header(client.get("/static/css/theme.css")) is None


def test_login_discards_anything_the_session_held_before(app):
    client = app.test_client()
    client.post("/api/_t/junk")
    assert "junk" in serializer(app).loads(client.get_cookie("session").value)
    login(client)
    assert set(serializer(app).loads(client.get_cookie("session").value)) == {"uid", "tv"}


# ---- 辨識與無效的 cookie -------------------------------------------------------

def test_a_logged_in_member_is_recognised_and_the_lookup_happens_once_per_request(app, monkeypatch):
    calls = []
    client = app.test_client()
    login(client)

    from app.services.auth_service import AuthService

    real = AuthService.resolve_session

    def counting(self, user_id, version):
        calls.append((user_id, version))
        return real(self, user_id, version)

    monkeypatch.setattr(AuthService, "resolve_session", counting)
    response = client.get("/api/_t/me")
    assert response.status_code == 200 and response.get_json()["id"] == response.get_json()["again"]
    assert len(calls) == 1


def test_no_cookie_means_not_logged_in(app):
    response = app.test_client().get("/api/_t/me")
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "unauthorized"


def test_a_tampered_cookie_is_treated_as_logged_out_and_cleared(app):
    client = app.test_client()
    login(client)
    good = client.get_cookie("session").value
    client.set_cookie("session", good[:-2] + ("AA" if not good.endswith("AA") else "BB"))
    response = client.get("/api/_t/me")
    assert response.status_code == 401
    assert "session=;" in session_cookie_header(response)      # 清除無效的 cookie


def test_a_cookie_signed_with_another_key_is_treated_as_logged_out(app):
    foreign = create_app(load_config(environ={"APP_ENV": "testing", "SECRET_KEY": "an-attackers-own-secret-key"}))
    forged = serializer(foreign).dumps({"uid": 1, "tv": 1})
    client = app.test_client()
    client.set_cookie("session", forged)
    assert client.get("/api/_t/me").status_code == 401


@pytest.mark.parametrize("payload", [{"uid": "1", "tv": 1}, {"uid": True, "tv": 1}, {"uid": 1, "tv": "1"}, {"uid": 1}, {"tv": 1}, {}, {"uid": 1.0, "tv": 1}])
def test_a_validly_signed_cookie_with_the_wrong_shape_is_not_a_login(app, payload):
    client = app.test_client()
    client.set_cookie("session", serializer(app).dumps(payload))
    assert client.get("/api/_t/me").status_code == 401


def test_the_cookie_expires_after_thirty_idle_days_but_use_keeps_it_alive(app, monkeypatch):
    client = app.test_client()
    login(client)
    real_time = time.time
    offset = [0]
    monkeypatch.setattr(time, "time", lambda: real_time() + offset[0])
    for day in (20, 45, 70, 95, 120):                # 每次相隔 25 天：持續使用，總共超過 100 天仍有效（沒有絕對上限）
        offset[0] = day * 86400
        assert client.get("/api/_t/me").status_code == 200, f"第 {day} 天應仍登入"


def test_thirty_one_idle_days_log_the_member_out(app, monkeypatch):
    client = app.test_client()
    login(client)
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 29 * 86400)
    assert client.get("/api/_t/me").status_code == 200
    monkeypatch.setattr(time, "time", lambda: real_time() + (29 + 31) * 86400)     # 上次使用之後閒置 31 天
    assert client.get("/api/_t/me").status_code == 401


def test_a_cookie_that_was_never_used_again_dies_after_thirty_days(app, monkeypatch):
    client = app.test_client()
    login(client)
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 31 * 86400)
    assert client.get("/api/_t/me").status_code == 401


# ---- 撤銷 ---------------------------------------------------------------------

def test_a_token_version_bump_logs_out_every_device(app):
    device_a, device_b = app.test_client(), app.test_client()
    login(device_a)
    login(device_b)
    with Session(app.extensions["db_engine"]) as s:
        user = s.query(User).one()
        user.token_version += 1
        s.commit()
    assert device_a.get("/api/_t/me").status_code == 401
    assert device_b.get("/api/_t/me").status_code == 401


def test_suspending_the_account_ends_the_session_on_the_next_request(app):
    client = app.test_client()
    login(client)
    assert client.get("/api/_t/me").status_code == 200
    with Session(app.extensions["db_engine"]) as s:
        s.query(User).one().status = STATUS_SUSPENDED
        s.commit()
    assert client.get("/api/_t/me").status_code == 401


# ---- 登出 ---------------------------------------------------------------------

def test_logout_ends_this_device_only(app):
    this_device, other_device = app.test_client(), app.test_client()
    login(this_device)
    login(other_device)
    response = this_device.post("/api/_t/logout")
    assert "session=;" in session_cookie_header(response)
    assert this_device.get("/api/_t/me").status_code == 401
    assert other_device.get("/api/_t/me").status_code == 200


def test_login_and_logout_both_replace_the_csrf_token(app):
    client = app.test_client()
    before = client.csrf_token()
    login(client)
    after_login = client.get_cookie("csrf").value
    assert after_login != before
    client.post("/api/_t/logout")
    assert client.get_cookie("csrf").value not in (before, after_login)


# ---- 裝飾器 -------------------------------------------------------------------

def test_a_protected_page_redirects_to_login_and_keeps_the_destination(app):
    client = app.test_client()
    response = client.get("/_t/secret?tab=security&x=1")
    assert response.status_code == 302
    location = response.headers["Location"]
    assert location.startswith("/login?next=")
    assert safe_next(unquote(location.split("next=", 1)[1])) == "/_t/secret?tab=security&x=1"
    login(client)
    assert client.get("/_t/secret").get_data(as_text=True) == "secret page"


# ---- 日誌與存活檢查 -----------------------------------------------------------

def test_the_request_log_carries_the_internal_user_id_once_the_member_is_resolved(app, log_lines):
    client = app.test_client()
    login(client)
    client.get("/api/_t/me")
    last = [line for line in log_lines() if line.get("path") == "/api/_t/me"][-1]
    assert isinstance(last["user_id"], int)


def test_liveness_and_static_requests_never_touch_the_database_even_with_a_cookie(app, monkeypatch):
    client = app.test_client()
    login(client)

    def forbidden(*args, **kwargs):
        raise AssertionError("這個請求不該為了辨識登入狀態而查詢資料庫")

    monkeypatch.setattr("app.security.session.get_auth_service", forbidden)
    assert client.get("/healthz").status_code == 200
    assert client.get("/static/css/theme.css").status_code == 200
    assert client.get("/favicon.ico").status_code == 204
    assert client.get("/terms").status_code == 200                     # 公開頁面不辨識登入者（首頁要，因為已登入者會被導向）
