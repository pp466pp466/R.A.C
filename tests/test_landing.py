"""auth-pages 規格「登入後的預設頁面與首頁導向」：已登入者開首頁與登入頁去該去的位置，`/register` 一律導向 `/login`。

預設頁面由 `DEFAULT_AFTER_LOGIN` 一個常數決定；受限登入狀態各去自己的頁面，不依 `next`。
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base
from app.models.user import STATUS_DELETION_PENDING
from app.security.redirects import DEFAULT_AFTER_LOGIN, landing_for, safe_next
from app.services.current_user import CurrentUser
from tests.support.auth import START, FakeClock, add_user
from tests.support.idor import logged_in_client

LOCKED = dict(locked_at=START, lock_reason="rate_limited")
DELETING = dict(status=STATUS_DELETION_PENDING, deletion_requested_at=START, status_before_deletion="active")


@pytest.fixture()
def app():
    application = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


def client_with(app, **overrides):
    with Session(app.extensions["db_engine"]) as s:
        return logged_in_client(app, add_user(s, 1, **overrides))


def current(member_session, **overrides) -> CurrentUser:
    return CurrentUser.from_model(add_user(member_session, 1, **overrides))


# ---- landing_for ------------------------------------------------------------------------------------

def test_the_default_page_is_the_game_entry_not_the_chat():
    assert DEFAULT_AFTER_LOGIN == "/game"


def test_an_unrestricted_member_goes_to_next_when_it_is_safe_and_to_the_default_page_otherwise(member_session):
    user = current(member_session)
    assert landing_for(user) == DEFAULT_AFTER_LOGIN
    assert landing_for(user, "/account?tab=security") == "/account?tab=security"
    for bad in ("https://evil.example/", "//evil.example", "javascript:alert(1)", "/\\evil", "/%2f/evil", None, 5, ""):
        assert landing_for(user, bad) == DEFAULT_AFTER_LOGIN, bad


@pytest.mark.parametrize(
    ("overrides", "page"),
    [(DELETING, "/account"), (LOCKED, "/appeal"), (dict(age=None), "/age")],
    ids=["deletion-pending", "locked", "age-pending"],
)
def test_a_restricted_member_goes_to_the_page_of_that_state(member_session, overrides, page):
    assert landing_for(current(member_session, **overrides)) == page


def test_the_restricted_pages_other_than_the_age_step_ignore_next(member_session):
    assert landing_for(current(member_session, **DELETING), "/cs") == "/account"


def test_the_age_step_carries_a_safe_next_so_the_original_destination_survives_it(member_session):
    user = current(member_session, age=None)
    assert landing_for(user, "/account?tab=1") == "/age?next=%2Faccount%3Ftab%3D1"
    assert landing_for(user, DEFAULT_AFTER_LOGIN) == "/age"                        # 預設頁面不必帶
    for bad in ("https://evil.example/", "//evil.example", "javascript:alert(1)", None):
        assert landing_for(user, bad) == "/age", bad
    carried = landing_for(user, "/account?tab=1").split("next=", 1)[1]
    from urllib.parse import unquote

    assert safe_next(unquote(carried)) == "/account?tab=1"                         # 帶過去的值，到了 /age 仍通過同一個檢查


def test_when_several_restrictions_apply_the_most_specific_page_wins(member_session):
    assert landing_for(current(member_session, age=None, **LOCKED)) == "/appeal"


def test_an_admin_without_age_data_is_not_sent_to_the_age_step(member_session):
    assert landing_for(current(member_session, age=None, role="admin")) == DEFAULT_AFTER_LOGIN


# ---- 首頁與登入頁 ------------------------------------------------------------------------------------

def test_an_anonymous_visitor_still_sees_the_home_page(app):
    response = app.test_client().get("/")
    assert response.status_code == 200 and "系統運作中" in response.get_data(as_text=True)


def test_a_signed_in_member_opening_the_home_page_or_the_login_page_goes_to_the_default_page(app):
    client = client_with(app)
    for path in ("/", "/login"):
        response = client.get(path)
        assert response.status_code == 302 and response.headers["Location"] == DEFAULT_AFTER_LOGIN, path


@pytest.mark.parametrize(
    ("overrides", "page"),
    [(DELETING, "/account"), (LOCKED, "/appeal"), (dict(age=None), "/age")],
    ids=["deletion-pending", "locked", "age-pending"],
)
def test_a_restricted_member_opening_the_home_page_or_the_login_page_goes_to_the_page_of_that_state(app, overrides, page):
    client = client_with(app, **overrides)
    for path in ("/", "/login"):
        response = client.get(path)
        assert response.status_code == 302 and response.headers["Location"] == page, path


def test_the_login_page_ignores_next_for_a_signed_in_member(app):
    response = client_with(app).get("/login?next=https://evil.example/")
    assert response.status_code == 302 and response.headers["Location"] == DEFAULT_AFTER_LOGIN


def test_a_revoked_cookie_gets_the_home_page_and_is_cleared(app):
    client = client_with(app)
    with Session(app.extensions["db_engine"]) as s:
        from app.models import User

        s.get(User, 1).token_version += 1
        s.commit()
    response = client.get("/")
    assert response.status_code == 200
    assert any(h.startswith("session=;") for h in response.headers.getlist("Set-Cookie"))


# ---- /register ---------------------------------------------------------------------------------------

def test_opening_the_chat_directly_is_not_redirected_to_the_game_entry(app):
    """rpg-entry-ui：預設頁面改成 /game，但客服 /cs 仍可直接開啟（改由導覽列進入）；只有首頁與登入頁才導向預設頁面。"""
    client = client_with(app)
    response = client.get("/cs")
    assert response.status_code == 200
    assert client.get("/").headers["Location"] == "/game"                      # 對照：首頁才導向 /game


def test_the_register_page_is_gone_and_always_leads_to_the_login_page(app):
    for client in (app.test_client(), client_with(app)):
        response = client.get("/register?next=/cs")
        assert response.status_code == 302 and response.headers["Location"] == "/login"


# ---- CSRF 權杖的取得 ----------------------------------------------------------------------------------

def test_a_signed_in_test_client_still_gets_a_csrf_token_from_a_public_page(app):
    client = client_with(app)
    client.get("/")
    assert client.get_cookie("csrf") is None                                      # 首頁只是導向，不發放 CSRF cookie
    assert client.csrf_token() and client.get_cookie("csrf") is not None          # 改取 /terms
