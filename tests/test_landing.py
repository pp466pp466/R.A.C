"""auth-pages 規格「登入後的預設頁面與首頁導向」：已登入者開首頁與登入頁去該去的位置，`/register` 一律導向 `/login`。

預設頁面依角色決定：玩家為 `DEFAULT_AFTER_LOGIN`、啟用中的管理員為 `ADMIN_AFTER_LOGIN`（admin-default-landing 規格「管理員的預設頁面」）；
受限登入狀態各去自己的頁面，不依 `next`。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base
from app.models.user import STATUS_DELETION_PENDING, STATUS_SUSPENDED
from app.security.redirects import ADMIN_AFTER_LOGIN, DEFAULT_AFTER_LOGIN, landing_for, safe_next
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


def test_the_admin_default_page_is_the_console():
    assert ADMIN_AFTER_LOGIN == "/admin" and ADMIN_AFTER_LOGIN != DEFAULT_AFTER_LOGIN


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
    assert landing_for(current(member_session, age=None, role="admin")) == ADMIN_AFTER_LOGIN


# ---- 管理員的預設頁面（admin-default-landing）---------------------------------------------------------

UNSAFE_NEXT = ("https://evil.example/", "//evil.example", "javascript:alert(1)", "/\\evil", "/%2f/evil", None, 5, "")
EXPLICIT_NEXT = ("/game/6c1e0f56-3f0a-4e0e-9b53-0f3b7a8f6c11", "/account?tab=security", "/cs")


def test_an_active_admin_without_next_goes_to_the_console(member_session):
    assert landing_for(current(member_session, role="admin")) == ADMIN_AFTER_LOGIN


def test_for_an_admin_a_next_equal_to_the_player_default_page_counts_as_no_destination(member_session):
    """登入頁、google/start 與兩個登入 cookie 都以 `/game` 補上「沒有指定」，到這裡已分不開兩者。"""
    assert landing_for(current(member_session, role="admin"), DEFAULT_AFTER_LOGIN) == ADMIN_AFTER_LOGIN


@pytest.mark.parametrize("destination", EXPLICIT_NEXT)
def test_an_admin_with_an_explicit_safe_destination_goes_back_there(member_session, destination):
    assert landing_for(current(member_session, role="admin"), destination) == destination


def test_an_admin_with_an_explicit_destination_other_than_the_player_default_keeps_it_even_when_it_is_the_console(member_session):
    user = current(member_session, role="admin")
    assert landing_for(user, "/admin") == "/admin"
    assert landing_for(user, "/admin?tab=x") == "/admin?tab=x"


@pytest.mark.parametrize("bad", UNSAFE_NEXT, ids=repr)
def test_an_admin_with_an_unsafe_destination_goes_to_the_console(member_session, bad):
    assert landing_for(current(member_session, role="admin"), bad) == ADMIN_AFTER_LOGIN


def test_a_suspended_admin_is_not_sent_to_a_page_that_would_be_a_404(member_session):
    """與後台權限同一個定義（角色為 admin 且帳號啟用中）：停權的管理員仍照玩家的預設頁面。"""
    user = current(member_session, role="admin", status=STATUS_SUSPENDED)
    assert not user.is_active_admin
    assert landing_for(user) == DEFAULT_AFTER_LOGIN and landing_for(user, DEFAULT_AFTER_LOGIN) == DEFAULT_AFTER_LOGIN


def test_an_admin_waiting_for_deletion_still_goes_to_the_account_page(member_session):
    user = current(member_session, role="admin", **DELETING)
    assert landing_for(user) == "/account" and landing_for(user, "/admin") == "/account"


def test_a_player_gets_the_same_result_in_every_case_the_admin_is_sent_to_the_console(member_session):
    user = current(member_session)
    assert landing_for(user) == DEFAULT_AFTER_LOGIN and landing_for(user, DEFAULT_AFTER_LOGIN) == DEFAULT_AFTER_LOGIN
    for bad in UNSAFE_NEXT:
        assert landing_for(user, bad) == DEFAULT_AFTER_LOGIN, bad
    for destination in EXPLICIT_NEXT:
        assert landing_for(user, destination) == destination
    assert landing_for(user, "/admin") == "/admin"                                 # 玩家若自己帶 /admin，照原樣：後台頁面本身回 404，導向不授權


# ---- 首頁與登入頁 ------------------------------------------------------------------------------------

def test_an_anonymous_visitor_sees_the_welcome_page_with_the_way_to_log_in(app):
    """未登入者看到歡迎頁（取代 C-1 的占位頁）：標題與簡介、前往 /login 的入口、條款與隱私權政策；沒有占位頁的文字。"""
    response = app.test_client().get("/")
    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "雙核心平台" in page and "開放世界文字 RPG" in page and "會員制智能客服" in page
    for link in ('href="/login"', 'href="/terms"', 'href="/privacy"'):
        assert link in page, link
    assert "css/home.css" in page and "專案地基" not in page and "系統運作中" not in page


def test_the_welcome_page_takes_the_minimum_age_from_the_age_policy_and_the_template_has_no_age_number(app):
    """頁面與範本都不內嵌年齡數字（與登入頁、條款頁相同的規則）：數字來自 AgePolicy。"""
    import re

    from app.services.age_policy import DEFAULT_AGE_POLICY

    page = app.test_client().get("/").get_data(as_text=True)
    assert f"僅限滿 {DEFAULT_AGE_POLICY.min_register_age} 歲者使用" in page
    source = (Path(__file__).resolve().parent.parent / "app" / "templates" / "home.html").read_text(encoding="utf-8")
    assert "{{ min_age }}" in source and not re.search(r"滿\s*\d+\s*歲", source)


def test_the_welcome_page_is_a_plain_server_rendered_page_without_forms_or_inline_scripts(app):
    """登入與勾選條款都在 /login：歡迎頁沒有表單、沒有行內腳本，也沒有會把使用者帶去 Google 的連結。"""
    page = app.test_client().get("/").get_data(as_text=True)
    assert "<form" not in page and "<script>" not in page and "accounts.google.com" not in page and "/api/auth/google" not in page


def test_a_signed_in_member_opening_the_home_page_or_the_login_page_goes_to_the_default_page(app):
    client = client_with(app)
    for path in ("/", "/login"):
        response = client.get(path)
        assert response.status_code == 302 and response.headers["Location"] == DEFAULT_AFTER_LOGIN, path


def test_a_signed_in_admin_opening_the_home_page_or_the_login_page_goes_to_the_console(app):
    client = client_with(app, role="admin")
    for path in ("/", "/login", "/login?next=/game", "/login?next=https://evil.example/"):
        response = client.get(path)
        assert response.status_code == 302 and response.headers["Location"] == ADMIN_AFTER_LOGIN, path


def test_a_signed_in_admin_can_still_open_the_player_pages_directly(app):
    client = client_with(app, role="admin")
    assert client.get("/game").status_code == 200 and client.get("/cs").status_code == 200


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
