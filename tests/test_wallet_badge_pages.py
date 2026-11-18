"""shared-wallet-balance 規格「標籤出現的頁面」：頂列的遊戲幣餘額標籤只出現在六個已登入且可正常使用的頁面。

- 有標籤：`/game`、`/game/start/<版本>`、`/game/<存檔>`、`/cs`、`/scripts`、`/account`（正常的帳號頁，放在頁內導覽列）。
- 沒有標籤：`/login`、`/age`、`/appeal`、`/terms`、`/privacy`、`/admin`、首頁，以及等待刪除的 `/account` 簡化畫面。
標籤是自訂元件 `<ui-wallet-balance>`（ui-components.js 註冊），頁面外殼本身不含餘額；所有頁面都經 base.html 載入狀態庫的兩個腳本（在 ui-components.js 之前）。
標籤的文字、請求與失敗行為在 `tests/js/wallet-balance.test.js`，360px 的排版量測由 Playwright 在驗收時記錄。
"""
from __future__ import annotations

import re

import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import PROJECT_ROOT, load_config
from app.models import Base
from app.models.user import ROLE_ADMIN, STATUS_DELETION_PENDING
from app.repositories.script_repository import ScriptRepository
from app.repositories.session_repository import SessionRepository
from tests.support.auth import START, FakeClock, add_user
from tests.support.idor import logged_in_client
from tests.support.protagonist import protagonist as stored_protagonist
from tests.support.script_data import validated

BADGE = "<ui-wallet-balance></ui-wallet-balance>"
TEMPLATES = PROJECT_ROOT / "app" / "templates"


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


def open_save(app, owner) -> str:
    with Session(app.extensions["db_engine"]) as s:
        save = SessionRepository(s).create(
            owner.id, source_version_id=None, snapshot=validated(title="青雲問道").content, snapshot_spec_version=1,
            protagonist=stored_protagonist("林初"), content_tier="safe", now=START,
        )
        s.commit()
        return save.public_id


def publish_builtin(app) -> str:
    result = validated(title="內建的劇本")
    with Session(app.extensions["db_engine"]) as s:
        version, _ = ScriptRepository(s).publish_builtin(
            "seed-badge", content=result.content, content_hash=result.content_hash, total_chars=result.total_chars, now=START,
        )
        s.commit()
        return version.public_id


def html_of(response) -> str:
    assert response.status_code == 200, response.status_code
    return response.get_data(as_text=True)


# ---- 六個頁面都有標籤 -----------------------------------------------------------------------------------------


def test_each_of_the_six_pages_has_exactly_one_wallet_label(app):
    client, user = client_for(app, age=30)
    pages = {
        "/game": client.get("/game"),
        f"/game/start/{publish_builtin(app)}": None,
        f"/game/{open_save(app, user)}": None,
        "/cs": client.get("/cs"),
        "/scripts": client.get("/scripts"),
        "/account": client.get("/account"),
    }
    for path, response in pages.items():
        html = html_of(response or client.get(path))
        assert html.count(BADGE) == 1, path


def test_the_label_sits_in_the_top_bar_of_each_page_and_not_in_a_second_place(app):
    client, user = client_for(app, age=30)
    expectations = {
        "/game": 'class="ge-topbar"',
        f"/game/start/{publish_builtin(app)}": 'class="ge-topbar"',
        f"/game/{open_save(app, user)}": 'class="gm-topbar"',
        "/cs": 'class="cs-topbar"',
        "/scripts": 'class="sw-topbar"',
        "/account": 'class="account-nav"',
    }
    for path, marker in expectations.items():
        html = html_of(client.get(path))
        header_end = html.index("</header>", html.index(marker)) if "topbar" in marker else html.index("</nav>", html.index(marker))
        assert BADGE in html[html.index(marker):header_end], path


def test_the_game_screen_no_longer_has_its_own_balance_paragraph(app):
    client, user = client_for(app, age=30)
    html = html_of(client.get(f"/game/{open_save(app, user)}"))
    assert "gm-balance" not in html and "balanceText" not in html


# ---- 其他頁面沒有標籤 -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/login", "/terms", "/privacy"])
def test_public_pages_have_no_wallet_label(app, path):
    assert BADGE not in html_of(app.test_client().get(path))


def test_the_age_page_has_no_wallet_label(app):
    client, _ = client_for(app, age=None)
    assert BADGE not in html_of(client.get("/age"))


def test_the_appeal_page_has_no_wallet_label(app):
    client, _ = client_for(app, age=30, locked_at=START, lock_reason="rate_limited")
    assert BADGE not in html_of(client.get("/appeal"))


def test_the_admin_page_has_no_wallet_label(app):
    client, _ = client_for(app, age=30, role=ROLE_ADMIN)
    assert BADGE not in html_of(client.get("/admin"))


def test_an_account_awaiting_deletion_gets_no_label_and_so_no_wallet_request(app):
    client, _ = client_for(app, age=30, status=STATUS_DELETION_PENDING, deletion_requested_at=START, status_before_deletion="active")
    assert BADGE not in html_of(client.get("/account"))


def test_no_template_other_than_the_six_pages_uses_the_label():
    users = sorted(str(path.relative_to(TEMPLATES)).replace("\\", "/") for path in TEMPLATES.rglob("*.html") if "<ui-wallet-balance" in path.read_text(encoding="utf-8-sig"))
    assert users == ["auth/account.html", "cs/chat.html", "game/index.html", "game/session.html", "game/start.html", "scripts/workshop.html"]


# ---- 載入順序與離線資源 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/login", "/terms", "/privacy"])
def test_every_page_loads_the_wallet_modules_before_the_components_and_only_from_the_site(app, path):
    html = html_of(app.test_client().get(path))
    names = re.findall(r'<script src="([^"]+)"', html)
    order = [next(index for index, name in enumerate(names) if name.endswith(file)) for file in ("js/ui/restriction.js", "js/ui/wallet-api.js", "js/ui/wallet-store.js", "js/ui/ui-components.js")]
    assert order == sorted(order), names
    assert all(name.startswith("/static/") for name in names), names


def test_the_label_styles_live_in_the_shared_components_file_and_use_only_theme_variables():
    css = (PROJECT_ROOT / "app" / "static" / "css" / "components.css").read_text(encoding="utf-8-sig")
    block = css[css.index(".ui-wallet-balance {"):css.index("/* ---- Modal ---")]
    assert "@media (max-width: 575.98px)" in block and ".ui-wallet-balance__word { display: none; }" in block
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", block), "標籤的樣式只能用 theme.css 的變數，不寫色碼"
    assert "var(--color-accent)" in block and "var(--border-hairline)" in block
