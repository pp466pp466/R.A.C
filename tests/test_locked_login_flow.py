"""account-lockout 規格「被鎖定者只有受限登入狀態」：被鎖定的玩家仍可通過 Google 登入，登入後只能申訴；解除之後立刻恢復。

走真實的 Flask 應用程式、Google 回呼與資料庫（供應商是假的）。管理員解除鎖定的 API 本身見 `test_admin_lockout_api.py`；
這裡用 Repository 的解除來證明「同一個登入狀態在解除之後立刻恢復，不需要重新登入」。
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base, LockStrike, User
from app.repositories.user_repository import UserRepository
from app.services.google_oidc import GoogleProfile
from tests.support.auth import START, FakeClock, add_user
from tests.support.google import SUB, FakeProvider, google_sign_in

LOCKED = dict(locked_at=START, lock_reason="rate_limited")


@pytest.fixture()
def provider():
    return FakeProvider()


@pytest.fixture()
def app(provider):
    application = create_app(load_config(environ={"APP_ENV": "testing"}), identity_provider=provider, clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    with Session(application.extensions["db_engine"]) as s:
        add_user(s, 1, email_verified_at=START, **LOCKED)
    provider.profile = GoogleProfile(SUB, "user1@example.com", True, "被鎖定的人")
    return application


def sign_in(app):
    client = app.test_client()
    response = google_sign_in(app, client)
    return client, response


def unlock(app) -> None:
    with Session(app.extensions["db_engine"]) as s:
        assert UserRepository(s).unlock_player(1) is True
        s.commit()


def test_a_locked_player_passes_google_and_lands_on_the_appeal_page(app):
    client, response = sign_in(app)
    assert response.status_code == 302 and response.headers["Location"] == "/appeal"
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    user = me.get_json()["user"]
    assert user["restriction"] == "locked" and user["email"] == "user1@example.com"


def test_the_appeal_page_and_the_appeal_api_work_from_that_session(app):
    client, _ = sign_in(app)
    assert client.get("/appeal").status_code == 200
    appeal = client.post("/api/appeals", json={"content": "請解除鎖定"})
    assert appeal.status_code == 201
    assert client.get("/api/appeals/mine").get_json()["appeals"][0]["status"] == "pending"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/cs/conversations"), ("POST", "/api/cs/conversations"), ("GET", "/api/cs/usage"),
        ("POST", "/api/account/export"), ("POST", "/api/account/delete"), ("POST", "/api/auth/change-password"),
        ("POST", "/api/auth/logout-all"), ("GET", "/api/admin/overview"),
    ],
)
def test_every_other_api_answers_403_account_locked(app, method, path):
    client, _ = sign_in(app)
    response = client.open(path, method=method, json={} if method == "POST" else None)
    assert response.status_code == 403 and response.get_json()["error"]["code"] == "account_locked"


@pytest.mark.parametrize("path", ["/cs", "/account", "/admin", "/age"])
def test_every_other_page_is_sent_to_the_appeal_page(app, path):
    client, _ = sign_in(app)
    response = client.get(path)
    assert response.status_code == 302 and response.headers["Location"] == "/appeal"


def test_the_home_page_and_the_login_page_also_lead_to_the_appeal_page(app):
    client, _ = sign_in(app)
    for path in ("/", "/login"):
        assert client.get(path).headers["Location"] == "/appeal"


def test_logging_out_works_and_the_next_google_sign_in_is_again_restricted(app):
    client, _ = sign_in(app)
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401
    response = google_sign_in(app, client)
    assert response.headers["Location"] == "/appeal"


def test_signing_in_while_locked_adds_no_strike_and_does_not_unlock(app):
    sign_in(app)
    sign_in(app)
    with Session(app.extensions["db_engine"]) as s:
        user = s.get(User, 1)
        assert user.locked_at is not None and s.query(LockStrike).count() == 0


def test_after_the_unlock_the_same_session_is_whole_again_without_signing_in_once_more(app):
    client, _ = sign_in(app)
    assert client.get("/api/cs/conversations").status_code == 403
    unlock(app)
    assert client.get("/api/auth/me").get_json()["user"]["restriction"] is None
    assert client.get("/api/cs/conversations").status_code == 200
    assert client.get("/cs").status_code == 200
    assert client.get("/appeal").headers["Location"] == "/cs"                    # 申訴頁只對被鎖定的人有內容
    assert client.post("/api/appeals", json={"content": "x"}).status_code == 404


def test_after_the_unlock_the_next_google_sign_in_goes_to_the_default_page(app):
    sign_in(app)
    unlock(app)
    _, response = sign_in(app)
    assert response.headers["Location"] == "/cs"


def test_a_locked_account_found_through_a_changed_google_email_cannot_slip_past(app, provider):
    """已綁定的 Google 身分以穩定識別（sub）辨識，不因信箱變更而重新比對——被鎖定的帳號一樣是受限登入狀態。"""
    sign_in(app)                                                                  # 第一次登入綁定 sub
    provider.profile = GoogleProfile(SUB, "a-brand-new-address@example.com", True, "x")
    client, response = sign_in(app)
    assert response.headers["Location"] == "/appeal"
    assert client.get("/api/auth/me").get_json()["user"]["restriction"] == "locked"
