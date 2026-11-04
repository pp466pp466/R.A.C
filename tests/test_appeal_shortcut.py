"""appeal-shortcut 規格的頁面部分：被鎖定或被暫停發佈的玩家，所在頁面直接給「前往申訴」的按鈕。

是否顯示只依伺服器的回應（使用者資料的 `restriction` / `publish_suspended` 與 API 的 403 代碼）；按鈕是指向 `/appeal` 的一般連結，
不帶查詢字串、不使用 `data-bs-*`。判定與文字的純函式見 `tests/js/restriction.test.js`；真正的點擊與畫面在瀏覽器驗收。
"""
from __future__ import annotations

import json
import re

import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import PROJECT_ROOT, load_config
from app.models import Base, User
from tests.support.auth import START, FakeClock, add_user
from tests.support.idor import logged_in_client

STATE_PATTERN = re.compile(r'<script type="application/json" id="initial-state">(.*?)</script>', re.S)
STATIC = PROJECT_ROOT / "app" / "static"
TEMPLATES = PROJECT_ROOT / "app" / "templates"


@pytest.fixture()
def app():
    application = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


def read(path) -> str:
    return path.read_text(encoding="utf-8-sig")


def state_of(response) -> dict:
    match = STATE_PATTERN.search(response.get_data(as_text=True))
    assert match, "頁面沒有初始狀態區塊"
    return json.loads(match.group(1))


def client_for(app, n: int = 1, **overrides):
    with Session(app.extensions["db_engine"]) as s:
        user = add_user(s, n, **overrides)
        s.refresh(user)
        s.expunge(user)
    return logged_in_client(app, user), user


def set_columns(app, user_id: int, **values) -> None:
    with Session(app.extensions["db_engine"]) as s:
        row = s.get(User, user_id)
        for name, value in values.items():
            setattr(row, name, value)
        s.commit()


# ---- 被暫停發佈：/scripts 與 /account 的頁面狀態 ------------------------------------------------


def test_a_suspended_player_gets_the_flag_in_the_scripts_page_state_and_the_account_profile(app):
    client, _ = client_for(app, publish_suspended_at=START)
    assert state_of(client.get("/scripts"))["user"]["publish_suspended"] is True
    assert state_of(client.get("/account"))["user"]["publish_suspended"] is True
    assert client.get("/api/auth/me").get_json()["user"]["publish_suspended"] is True


def test_a_player_who_is_not_suspended_gets_false_everywhere_so_no_shortcut_shows(app):
    client, _ = client_for(app)
    assert state_of(client.get("/scripts"))["user"]["publish_suspended"] is False
    assert state_of(client.get("/account"))["user"]["publish_suspended"] is False
    assert client.get("/api/auth/me").get_json()["user"]["publish_suspended"] is False


def test_the_shortcut_disappears_once_the_suspension_is_lifted(app):
    client, user = client_for(app, publish_suspended_at=START)
    assert state_of(client.get("/scripts"))["user"]["publish_suspended"] is True
    set_columns(app, user.id, publish_suspended_at=None)
    assert state_of(client.get("/scripts"))["user"]["publish_suspended"] is False
    assert state_of(client.get("/account"))["user"]["publish_suspended"] is False


def test_a_suspended_player_keeps_every_other_page_and_the_appeal_page_is_reachable(app):
    client, _ = client_for(app, publish_suspended_at=START)
    for path in ("/scripts", "/account", "/cs", "/appeal"):
        assert client.get(path).status_code == 200, path


def test_the_account_and_scripts_pages_render_the_notice_component_with_the_server_state(app):
    client, _ = client_for(app, publish_suspended_at=START)
    scripts = client.get("/scripts").get_data(as_text=True)
    account = client.get("/account").get_data(as_text=True)
    assert '<ui-restriction-notice :kind="restriction"></ui-restriction-notice>' in scripts
    assert '<ui-restriction-notice :kind="restrictionKind"></ui-restriction-notice>' in account


# ---- 被鎖定：受限登入狀態與使用中才被鎖定 ----------------------------------------------------------


def test_a_locked_player_on_a_normal_page_is_sent_to_the_appeal_page(app):
    client, _ = client_for(app, locked_at=START, lock_reason="rate_limited")
    for path in ("/cs", "/account", "/scripts"):
        response = client.get(path)
        assert response.status_code == 302 and response.headers["Location"].startswith("/appeal"), path


def test_a_player_locked_while_using_the_chat_gets_the_code_the_page_turns_into_the_button(app):
    client, user = client_for(app)
    assert state_of(client.get("/cs"))["chat_gate"] is None
    set_columns(app, user.id, locked_at=START, lock_reason="rate_limited")
    response = client.post("/api/cs/conversations", json={"client_id": "00000000-0000-4000-8000-000000000001"})
    assert response.status_code == 403 and response.get_json()["error"]["code"] == "account_locked"


def test_the_profile_of_a_locked_player_says_locked_so_any_page_can_show_the_shortcut(app):
    client, _ = client_for(app, locked_at=START, lock_reason="rate_limited")
    response = client.get("/api/auth/me")
    assert response.status_code == 200 and response.get_json()["user"]["restriction"] == "locked"


# ---- 按鈕本身 ---------------------------------------------------------------------------------------


def test_the_notice_component_renders_a_plain_link_to_the_appeal_page_as_plain_text():
    source = read(STATIC / "js" / "ui" / "ui-components.js")
    block = source[source.index("var RestrictionNotice"):source.index("root.UI.RestrictionNotice")]
    assert '<a class="ui-btn ui-btn--primary ui-restriction__link" :href="notice.href">{{ notice.linkText }}</a>' in block
    assert "{{ notice.text }}" in block and "v-html" not in block and "innerHTML" not in block
    assert "data-bs-" not in block and "@click" not in block                      # 一般連結，不經任何腳本導向
    restriction = read(STATIC / "js" / "ui" / "restriction.js")
    assert "APPEAL_PATH = '/appeal'" in restriction and "'/appeal?" not in restriction


def test_every_page_with_the_shortcut_loads_the_helper_before_the_components(app):
    base = read(TEMPLATES / "base.html")
    assert base.index("js/ui/restriction.js") < base.index("js/ui/ui-components.js")
    client, _ = client_for(app)
    for path in ("/cs", "/account", "/scripts"):
        html = client.get(path).get_data(as_text=True)
        assert "js/ui/restriction.js" in html, path


def test_the_chat_page_turns_the_locked_code_into_the_appeal_button_without_storing_anything():
    js = read(STATIC / "js" / "cs" / "chat.js")
    html = read(TEMPLATES / "cs" / "chat.html")
    assert "account_locked: true" in js and "UI.restrictionNoticeOf('locked')" in js
    assert ":class=\"gateNotice.button ? 'ui-btn ui-btn--primary cs-banner__button' : null\"" in html
    assert "data-bs-" not in html and "v-html" not in html
    assert not re.search(r"localStorage\.setItem\([^)]*(locked|suspended|restriction)", js)


def test_the_account_page_reads_the_state_from_the_server_and_remembers_nothing():
    js = read(STATIC / "js" / "auth" / "account.js")
    assert "UI.restrictionOf(user)" in js and "UI.restrictionOf(info)" in js
    assert not re.search(r"(localStorage|sessionStorage|indexedDB)[^;\n]*(locked|suspended|restriction)", js)


def test_the_restriction_styles_follow_the_palette():
    css = read(STATIC / "css" / "components.css")
    block = re.search(r"\.ui-restriction\s*\{(.*?)\}", css, re.S).group(1)
    assert "border-left: 2px solid var(--color-accent)" in block
    assert "#fff" not in block.lower()
