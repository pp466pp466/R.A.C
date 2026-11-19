"""wallet-hub-page 規格「錢包頁的路由與存取規則」與「錢包頁遵守前端規範」：`GET /wallet` 的頁面外殼。

- 只渲染外殼，初始狀態只有會員等級：**沒有餘額、帳本、email、生日與年齡**；頁面渲染也不碰錢包（`GET /api/wallet` 才會補發贈幣與退回預扣，頁面不該做寫入）。
- 未登入導向登入頁；受限登入狀態沿用既有的導向（被鎖定 `/appeal`、待確認年齡 `/age`、等待刪除 `/account`）；回應不快取；只接受 GET。
- 只載入本機資源、繁體中文與 UTF-8、標籤成對、沒有 `v-html`、`data-bs-*`、CDN 與寫死的 `/static/`；腳本的載入順序（`wallet-hub.js` 最後）。
- 頁面沒有餘額標籤（它自己顯示餘額），所以不在「六個頁面」的清單內（`tests/test_wallet_badge_pages.py`）。
純函式與掛載邏輯在 `tests/js/wallet-hub-state.test.js`、`tests/js/wallet-hub-template.test.js`；`/api/wallet*` 的規則在 `tests/test_wallet_api.py`。
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import create_app
from app.config import PROJECT_ROOT, load_config
from app.models import Base, Wallet
from app.models.user import ROLE_ADMIN
from tests.support.auth import START, FakeClock, add_user
from tests.support.idor import logged_in_client

STATE_PATTERN = re.compile(r'<script type="application/json" id="initial-state">(.*?)</script>', re.S)
TEMPLATE = PROJECT_ROOT / "app" / "templates" / "wallet" / "hub.html"
STATIC = PROJECT_ROOT / "app" / "static"


@pytest.fixture()
def app():
    application = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


def client_for(app, n: int = 1, **overrides):
    with Session(app.extensions["db_engine"]) as s:
        user = add_user(s, n, **overrides)
        s.refresh(user)
        s.expunge(user)
    return logged_in_client(app, user), user


def state_of(response) -> dict:
    match = STATE_PATTERN.search(response.get_data(as_text=True))
    assert match, "頁面沒有初始狀態區塊"
    return json.loads(match.group(1))


def wallet_count(app) -> int:
    with Session(app.extensions["db_engine"]) as s:
        return s.scalar(select(func.count()).select_from(Wallet))


# ---- 路由與存取規則 ---------------------------------------------------------------------------------------------


def test_a_signed_in_player_gets_the_page_shell_and_the_initial_state_holds_only_the_member_tier(app):
    client, _ = client_for(app)
    response = client.get("/wallet")
    assert response.status_code == 200
    assert state_of(response) == {"member_tier": "free"}
    html = response.get_data(as_text=True)
    assert 'id="wallet-app"' in html and re.search(r'<div id="wallet-app"[^>]*\bv-cloak\b', html)
    assert "user1@example.com" not in html and "birth" not in html.lower()
    assert "free_balance" not in html and "paid_balance" not in html and "coin_exempt" not in html     # 沒有任何錢包欄位


def test_a_member_tier_other_than_free_is_carried_as_is(app):
    client, _ = client_for(app, member_tier="vip")
    assert state_of(client.get("/wallet")) == {"member_tier": "vip"}


def test_rendering_the_page_does_not_touch_the_wallet(app):
    """`GET /api/wallet` 會建錢包、補贈幣與退回預扣；頁面外殼不能有這些寫入的副作用，餘額由前端另外請求。"""
    client, _ = client_for(app)
    assert wallet_count(app) == 0
    assert client.get("/wallet").status_code == 200
    assert wallet_count(app) == 0


def test_an_anonymous_visitor_is_sent_to_the_login_page_and_comes_back_to_the_wallet(app):
    response = app.test_client().get("/wallet")
    assert response.status_code == 302
    assert response.headers["Location"] == "/login?next=%2Fwallet"


@pytest.mark.parametrize(("overrides", "target"), [
    ({"locked_at": START, "lock_reason": "rate_limited"}, "/appeal"),
    ({"age": None}, "/age"),
    ({"status": "deletion_pending"}, "/account"),
])
def test_a_restricted_login_is_sent_to_its_own_page(app, overrides, target):
    client, _ = client_for(app, **overrides)
    response = client.get("/wallet")
    assert response.status_code == 302 and response.headers["Location"].startswith(target)


def test_an_admin_who_has_not_confirmed_the_age_can_still_open_the_page(app):
    client, _ = client_for(app, role=ROLE_ADMIN, age=None)
    response = client.get("/wallet")
    assert response.status_code == 200                                         # 管理員沒有受限登入狀態；錢包 API 也不擋管理員
    assert state_of(response) == {"member_tier": "free"}


def test_the_page_is_not_cached(app):
    client, _ = client_for(app)
    assert "no-store" in client.get("/wallet").headers.get("Cache-Control", "")


def test_the_page_accepts_only_get(app):
    client, _ = client_for(app)
    for method in ("post", "put", "delete"):
        assert getattr(client, method)("/wallet").status_code in (400, 403, 405), method          # 沒有 CSRF 權杖時可能先被擋


# ---- 前端規範 ---------------------------------------------------------------------------------------------------


class _Resources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.refs, self.data_bs = [], []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name in ("src", "href") and value:
                self.refs.append(value)
            if name.startswith("data-bs-"):
                self.data_bs.append((name, value))


def collect(html: str) -> _Resources:
    collector = _Resources()
    collector.feed(html)
    return collector


def test_the_page_loads_only_local_resources_in_traditional_chinese_utf8(app):
    client, _ = client_for(app)
    html = client.get("/wallet").get_data(as_text=True)
    collector = collect(html)
    assert not [ref for ref in collector.refs if re.match(r"(https?:)?//", ref)]
    assert collector.data_bs == [("data-bs-theme", "dark")]
    assert any(ref.endswith("css/wallet.css") for ref in collector.refs)
    assert '<html lang="zh-Hant-TW"' in html and '<meta charset="UTF-8"' in html
    assert "<title>錢包</title>" in html


def test_the_scripts_load_in_dependency_order_and_the_page_script_comes_last(app):
    client, _ = client_for(app)
    names = [ref.rsplit("/", 1)[-1] for ref in collect(client.get("/wallet").get_data(as_text=True)).refs if ref.endswith(".js")]
    expected = [
        "vue.global.prod.js", "axios.min.js", "wallet-api.js", "wallet-store.js", "ui-components.js",       # base.html：全站共用
        "auth-common.js", "game-labels.js", "game-state.js", "wallet-checkin.js", "wallet-demo-packages.js", "wallet-hub-state.js", "wallet-hub.js",
    ]
    positions = [names.index(name) for name in expected]
    assert positions == sorted(positions), names
    assert names[-1] == "wallet-hub.js"
    assert "game-api.js" not in names                                           # 不載入整套遊戲 API（design D4、D5）


class _Balance(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__()
        self.stack, self.problems = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.problems.append(f"</{tag}> 前面是 {self.stack[-1] if self.stack else '（空）'}")
            if tag in self.stack:
                while self.stack and self.stack.pop() != tag:
                    pass
        else:
            self.stack.pop()


def test_the_template_has_balanced_tags():
    balance = _Balance()
    balance.feed(TEMPLATE.read_text(encoding="utf-8-sig"))
    assert balance.problems == [] and balance.stack == [], (balance.problems, balance.stack)


def test_the_template_follows_the_front_end_rules_and_has_the_tabs_the_navigation_and_the_shared_components():
    html = TEMPLATE.read_text(encoding="utf-8-sig")
    assert "v-html" not in html and "data-bs-" not in html and "|safe" not in html and "offcanvas" not in html.lower()
    assert "cdn" not in html.lower() and "googleapis" not in html.lower() and "/static/" not in html     # 資源一律用 url_for
    assert re.search(r"\{\{\s*initial_state\|tojson\s*\}\}", html)             # 初始狀態經 tojson 跳脫
    assert 'role="tablist"' in html and html.count('role="tab"') == 2 and html.count('role="tabpanel"') == 2
    assert ">免費<" in html and ">儲值<" in html
    for href in ("/game", "/cs", "/scripts", "/account"):
        assert re.search(rf'<a class="wh-topbar__link" href="{href}">', html), href
    assert "<ui-restriction-notice" in html and "<ui-toast-region" in html
    assert "<ui-wallet-balance" not in html                                     # 本頁自己顯示餘額，不放全站的標籤


def test_the_stylesheet_uses_only_theme_variables_and_never_pure_white():
    css = (STATIC / "css" / "wallet.css").read_text(encoding="utf-8-sig")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css), "錢包頁的樣式只能用 theme.css 的變數，不寫色碼"
    assert not re.search(r"\b(?:white|#fff)\b", css, flags=re.I)
    assert "var(--color-accent)" in css and "var(--border-hairline)" in css and "var(--color-surface)" in css


def test_the_page_scripts_keep_nothing_in_the_browser_and_never_write_anywhere_but_the_check_in():
    """整個 `js/wallet/` 與簽到狀態機：不碰瀏覽器儲存與主控台、沒有 v-html、不直接呼叫 axios 或 fetch（請求只經 `UI.walletApi`）。"""
    sources = {path.name: path.read_text(encoding="utf-8-sig") for path in sorted((STATIC / "js" / "wallet").glob("*.js"))}
    assert set(sources) >= {"wallet-hub.js", "wallet-hub-state.js"}
    for name, source in sources.items():
        code = re.sub(r"(?<![:\w])//[^\n]*", "", re.sub(r"/\*.*?\*/", "", source, flags=re.S))
        for forbidden in ("localStorage", "sessionStorage", "indexedDB", "document.cookie", "console.", "v-html", "innerHTML", "eval(", "axios", "fetch("):
            assert forbidden not in code, (name, forbidden)
        assert not re.search(r"\b(alert|confirm|prompt)\s*\(", code), name
