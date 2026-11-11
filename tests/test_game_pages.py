"""rpg-entry-pages 規格：`/game` 一族頁面的外殼、初始狀態、存取規則與前端檔案的規則。

- `/game`：入口頁（選劇本與我的存檔）。初始狀態只有會員等級、故事偏好與閘門結果，**沒有清單內容**。
- `/game/<存檔>`：存檔頁的空殼。標題與主角姓名由伺服器渲染（自動跳脫），非擁有者與不存在的得到逐字相同的 404。
- `/game/start/<版本>`：開存檔精靈的外殼；與存檔頁的路徑不衝突。

清單端點的資料與規則在 `test_session_api.py`、`test_game_entry_service.py`；前端純函式與掛載邏輯在 `tests/js/game-labels.test.js`、`tests/js/entry.test.js`。
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
from app.models import Base, RpgSession, Wallet
from app.models.rpg_session import SESSION_ENDED
from app.models.script import REVIEW_CATEGORIES
from app.models.user import CONTENT_TIERS, MEMBER_TIERS, ROLE_ADMIN
from app.repositories.script_repository import ScriptRepository
from app.repositories.session_repository import SessionRepository
from app.services.protagonist import ORIGINS
from app.services.story_preference import AUDIENCES
from tests.support.auth import START, FakeClock, add_user
from tests.support.idor import logged_in_client
from tests.support.protagonist import protagonist as stored_protagonist
from tests.support.script_data import validated

STATE_PATTERN = re.compile(r'<script type="application/json" id="initial-state">(.*?)</script>', re.S)
STATIC = PROJECT_ROOT / "app" / "static"
TEMPLATES = PROJECT_ROOT / "app" / "templates" / "game"
JS_DIR = STATIC / "js" / "game"
GHOST = "00000000-0000-4000-8000-000000000000"


@pytest.fixture()
def app():
    application = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


def read(path) -> str:
    return path.read_text(encoding="utf-8-sig")


def strip_js_comments(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?<![:\w])//[^\n]*", "", source)


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


def open_save(app, owner, title="青雲問道", name="林初", ended=False) -> str:
    """直接寫入一個存檔（不經開存檔的審查）；回傳存檔的對外識別碼。"""
    with Session(app.extensions["db_engine"]) as s:
        save = SessionRepository(s).create(
            owner.id, source_version_id=None, snapshot=validated(title=title).content, snapshot_spec_version=1,
            protagonist=stored_protagonist(name), content_tier="safe", now=START,
        )
        if ended:
            save.status = SESSION_ENDED
        s.commit()
        return save.public_id


def publish_builtin(app, title="內建的劇本") -> str:
    result = validated(title=title)
    with Session(app.extensions["db_engine"]) as s:
        version, _ = ScriptRepository(s).publish_builtin(
            f"seed-{title}", content=result.content, content_hash=result.content_hash, total_chars=result.total_chars, now=START,
        )
        s.commit()
        return version.public_id


# ---- /game：存取與初始狀態 ---------------------------------------------------------------------------------------


def test_an_anonymous_visitor_is_sent_to_the_login_page_and_comes_back_to_game(app):
    response = app.test_client().get("/game")
    assert response.status_code == 302
    assert response.headers["Location"] == "/login?next=%2Fgame"


@pytest.mark.parametrize(("overrides", "target"), [
    ({"locked_at": START, "lock_reason": "rate_limited"}, "/appeal"),
    ({"age": None}, "/age"),
])
def test_a_restricted_login_is_sent_to_its_own_page(app, overrides, target):
    client, _ = client_for(app, **overrides)
    for path in ("/game", f"/game/{GHOST}", f"/game/start/{GHOST}"):
        response = client.get(path)
        assert response.status_code == 302 and response.headers["Location"].startswith(target), path


def test_a_player_waiting_for_deletion_is_sent_to_the_account_page(app):
    client, _ = client_for(app, status="deletion_pending")
    response = client.get("/game")
    assert response.status_code == 302 and response.headers["Location"].startswith("/account")


def test_the_initial_state_holds_the_tier_the_story_preference_and_the_gate_and_no_list_content(app):
    client, user = client_for(app, story_preference="female")
    publish_builtin(app, title="不該出現在初始狀態的劇本")
    open_save(app, user, title="不該出現在初始狀態的存檔")
    response = client.get("/game")
    assert response.status_code == 200
    state = state_of(response)
    assert state == {"user": {"member_tier": "free", "story_preference": "female"}, "gate": None}
    html = response.get_data(as_text=True)
    assert "不該出現在初始狀態" not in html                                      # 清單內容由前端呼叫唯讀端點取得，不在頁面裡
    assert "user1@example.com" not in html and "birth" not in html.lower()


def test_a_member_tier_other_than_free_is_carried_as_is(app):
    client, _ = client_for(app, member_tier="vip")
    assert state_of(client.get("/game"))["user"]["member_tier"] == "vip"


def test_an_admin_who_has_not_confirmed_the_age_gets_the_gate_code_from_the_server(app):
    client, _ = client_for(app, role=ROLE_ADMIN, age=None)
    response = client.get("/game")
    assert response.status_code == 200                                        # 管理員沒有受限登入狀態：頁面可以開，開始由閘門禁用
    assert state_of(response)["gate"] == "age_confirmation_required"


def test_a_player_who_is_too_young_for_the_models_gets_the_age_restricted_gate(app):
    client, _ = client_for(app, age=12)
    assert state_of(client.get("/game"))["gate"] == "age_restricted"


def test_every_game_page_is_not_cached(app):
    client, user = client_for(app)
    save_id = open_save(app, user)
    for path in ("/game", f"/game/{save_id}", f"/game/start/{GHOST}"):
        assert "no-store" in client.get(path).headers.get("Cache-Control", ""), path


def test_the_game_pages_accept_only_get(app):
    client, _ = client_for(app)
    for path in ("/game", f"/game/{GHOST}", f"/game/start/{GHOST}"):
        for method in ("post", "put", "delete"):
            assert getattr(client, method)(path).status_code in (400, 403, 405), (method, path)   # 沒有 CSRF 權杖時可能先被擋


# ---- /game/<存檔>：存檔頁的空殼 --------------------------------------------------------------------------------


def test_the_owner_sees_the_title_the_protagonist_the_building_notice_and_a_way_back(app):
    client, user = client_for(app)
    save_id = open_save(app, user, title="青雲問道", name="林初")
    response = client.get(f"/game/{save_id}")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "青雲問道" in html and "林初" in html and "劇情畫面建置中" in html
    assert re.search(r'<a\b[^>]*href="/game"', html)
    assert "進行中" in html and "安全模式" in html


def test_an_ended_save_says_so(app):
    client, user = client_for(app)
    html = client.get(f"/game/{open_save(app, user, ended=True)}").get_data(as_text=True)
    assert "已完結" in html and "進行中" not in html


def test_markup_in_the_title_and_the_protagonist_name_is_shown_as_text(app):
    client, user = client_for(app)
    html = client.get(f"/game/{open_save(app, user, title='<b>粗體</b>劇本', name='<i>斜體</i>')}").get_data(as_text=True)
    assert "&lt;b&gt;粗體&lt;/b&gt;劇本" in html and "&lt;i&gt;斜體&lt;/i&gt;" in html
    assert "<b>粗體</b>" not in html and "<i>斜體</i>" not in html


def test_someone_elses_save_and_a_missing_one_get_the_identical_404(app):
    owner_client, owner = client_for(app, 1)
    other_client, _ = client_for(app, 2)
    save_id = open_save(app, owner, title="只有擁有者看得到的標題", name="只有擁有者看得到的姓名")
    theirs = other_client.get(f"/game/{save_id}")
    missing = other_client.get(f"/game/{GHOST}")
    assert theirs.status_code == missing.status_code == 404
    assert theirs.get_data() == missing.get_data()                              # 逐字相同：不洩漏資源是否存在
    assert "只有擁有者看得到" not in theirs.get_data(as_text=True)
    assert owner_client.get(f"/game/{save_id}").status_code == 200


@pytest.mark.parametrize("junk", ["start", "x", "0", "%20", "null"])
def test_a_path_that_is_not_a_save_id_is_a_404_not_an_error(app, junk):
    client, _ = client_for(app)
    assert client.get(f"/game/{junk}").status_code == 404


def test_the_save_shell_creates_nothing_and_calls_no_api(app):
    client, user = client_for(app)
    save_id = open_save(app, user)
    with Session(app.extensions["db_engine"]) as s:
        before = (s.scalar(select(func.count()).select_from(Wallet)), s.scalar(select(func.count()).select_from(RpgSession)))
    html = client.get(f"/game/{save_id}").get_data(as_text=True)
    assert "/api/" not in html and "game-api.js" not in html and "entry.js" not in html     # 空殼沒有腳本與請求
    with Session(app.extensions["db_engine"]) as s:
        after = (s.scalar(select(func.count()).select_from(Wallet)), s.scalar(select(func.count()).select_from(RpgSession)))
    assert before == after                                                       # 沒有開錢包、沒有改動存檔


# ---- /game/start/<版本>：與存檔頁互不衝突 ------------------------------------------------------------------------


def test_the_start_path_is_the_wizard_shell_and_never_mistaken_for_a_save(app):
    client, user = client_for(app)
    version_id = publish_builtin(app)
    response = client.get(f"/game/start/{version_id}")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "開始冒險" in html and "劇情畫面建置中" not in html
    assert version_id not in html                                                # 版本識別碼只在網址裡
    assert client.get(f"/game/start/{GHOST}").status_code == 200                 # 版本是否存在由精靈載入時的 setup 端點判斷
    assert client.get("/game/start").status_code == 404                          # 單一路徑段 `start` 是存檔識別碼的位置，不是精靈


def test_the_three_game_routes_are_distinct_rules(app):
    rules = {rule.rule: rule for rule in app.url_map.iter_rules() if rule.rule.startswith("/game")}
    assert set(rules) == {"/game", "/game/start/<version_public_id>", "/game/<session_public_id>"}
    for rule in rules.values():
        assert rule.methods - {"HEAD", "OPTIONS"} == {"GET"}, rule.rule


# ---- 前端檔案的規則 --------------------------------------------------------------------------------------------------


class _Resources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.refs, self.data_bs, self.hrefs = [], [], []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name in ("src", "href") and value:
                self.refs.append(value)
            if name == "href" and value:
                self.hrefs.append(value)
            if name.startswith("data-bs-"):
                self.data_bs.append((name, value))


def collect(html: str) -> _Resources:
    collector = _Resources()
    collector.feed(html)
    return collector


@pytest.mark.parametrize("path_of", [
    lambda save_id: "/game", lambda save_id: f"/game/{save_id}", lambda save_id: f"/game/start/{save_id}",
])
def test_every_game_page_loads_only_local_resources_in_traditional_chinese_utf8(app, path_of):
    client, user = client_for(app)
    html = client.get(path_of(open_save(app, user))).get_data(as_text=True)
    collector = collect(html)
    assert not [ref for ref in collector.refs if re.match(r"(https?:)?//", ref)]
    assert collector.data_bs == [("data-bs-theme", "dark")]
    assert any(ref.endswith("css/game.css") for ref in collector.refs)
    assert '<html lang="zh-Hant-TW"' in html and '<meta charset="UTF-8"' in html


def test_the_index_page_scripts_load_in_dependency_order_and_the_entry_script_comes_last(app):
    client, _ = client_for(app)
    html = client.get("/game").get_data(as_text=True)
    names = [ref.rsplit("/", 1)[-1] for ref in collect(html).refs if ref.endswith(".js")]
    for needed in ("vue.global.prod.js", "axios.min.js", "ui-components.js", "restriction.js", "auth-common.js", "game-labels.js", "game-api.js", "entry.js"):
        assert needed in names, needed
    assert names[-1] == "entry.js"
    assert names.index("auth-common.js") < names.index("game-api.js") < names.index("entry.js")
    assert names.index("game-labels.js") < names.index("entry.js")
    assert 'id="game-app"' in html and re.search(r'<div id="game-app"[^>]*\bv-cloak\b', html)


def test_the_index_template_has_the_two_tabs_the_navigation_and_the_plain_text_interpolations():
    html = read(TEMPLATES / "index.html")
    assert 'role="tablist"' in html and ">選劇本<" in html and ">我的存檔<" in html
    assert "{{ card.title }}" in html and "{{ save.title" in html                # 作者與玩家的文字以插值顯示
    for href in ("/cs", "/scripts", "/account"):
        assert re.search(rf'<a class="ge-topbar__link" href="{href}">', html), href
    assert "<ui-restriction-notice" in html and "<ui-toast-region" in html
    assert "v-html" not in html and "data-bs-" not in html and "offcanvas" not in html.lower()
    assert re.search(r"\{\{\s*initial_state\|tojson\s*\}\}", html)               # 初始狀態經 tojson 跳脫


VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class _Balance(HTMLParser):
    """檢查標籤成對：Vue 的正式版編譯器不會對沒關閉的標籤拋錯，瀏覽器則會悄悄改寫結構，所以要在這裡先擋。"""

    def __init__(self):
        super().__init__()
        self.stack, self.problems = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if tag in VOID_TAGS:
            return
        if not self.stack or self.stack[-1] != tag:
            self.problems.append(f"</{tag}> 前面是 {self.stack[-1] if self.stack else '（空）'}")
            if tag in self.stack:
                while self.stack and self.stack.pop() != tag:
                    pass
        else:
            self.stack.pop()


@pytest.mark.parametrize("name", ["index.html", "session.html", "start.html"])
def test_every_game_template_has_balanced_tags(name):
    balance = _Balance()
    balance.feed(read(TEMPLATES / name))
    assert balance.problems == [] and balance.stack == [], (balance.problems, balance.stack)


def test_the_balance_check_really_catches_a_broken_template():
    for broken in ("<div><p>text</div>", "<div><span></div></span>", "<ul><li>a</ul>", "<div>"):
        balance = _Balance()
        balance.feed(broken)
        assert balance.problems or balance.stack, broken


def test_no_game_template_or_script_uses_forbidden_patterns():
    for path in sorted(TEMPLATES.glob("*.html")):
        text = read(path)
        assert "v-html" not in text and "data-bs-" not in text and "|safe" not in text, path.name
        assert "cdn" not in text.lower() and "googleapis" not in text.lower(), path.name
        assert "/static/" not in text, path.name                                 # 資源一律用 url_for
    js = "\n".join(strip_js_comments(read(path)) for path in sorted(JS_DIR.glob("*.js")))
    assert js, "沒有找到前端腳本"
    for forbidden in ("v-html", "innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "document.write", "console.log"):
        assert forbidden not in js, forbidden
    assert not re.search(r"\b(alert|confirm|prompt)\s*\(", js)
    assert "fetch(" not in js                                                    # 一般 API 一律走離線 axios
    assert "bootstrap" not in js.lower()


def test_the_entry_page_keeps_nothing_in_the_browser_and_never_writes(app):
    js = "\n".join(strip_js_comments(read(path)) for path in sorted(JS_DIR.glob("*.js")))
    for storage in ("localStorage", "sessionStorage", "indexedDB", "document.cookie"):
        assert storage not in js, storage
    api = strip_js_comments(read(JS_DIR / "game-api.js"))
    assert "axios" in api and not re.search(r"""method:\s*['"](post|put|patch|delete)""", api, re.I)   # 入口頁只讀
    assert "/api/sessions" in api


def test_every_loading_path_restores_its_state_in_a_finally_block():
    js = strip_js_comments(read(JS_DIR / "entry.js"))
    assert ".finally(" in js and "list.loading = false" in js
    assert "window.location.assign" in js and "Auth.loginUrlFor(PAGE_PATH)" in js     # 401 導向登入並保留位置


def test_the_stylesheet_hides_cloaked_elements_never_uses_pure_white_and_never_forces_a_wide_layout():
    css = read(STATIC / "css" / "game.css")
    assert re.search(r"\[v-cloak\]\s*\{\s*display:\s*none", css)
    lowered = css.lower()
    assert "#fff" not in lowered and not re.search(r"(?<![\w-])white(?![\w-])", lowered) and "rgb(255, 255, 255)" not in lowered
    assert not re.search(r"(?<!max-)(?<!min-)width:\s*\d{3,}px", css)                # 沒有固定的寬度（360px 不橫向溢出）
    assert "grid-template-columns: minmax(0, 1fr)" in css                            # 預設（手機）單欄，桌面才分兩欄
    assert re.search(r"@media \(min-width: 768px\)\s*\{\s*\.ge-cards \{ grid-template-columns: repeat\(2", css)


def labels_in(source: str, name: str) -> list[str]:
    """取出 `var NAME = { key: '…', … };` 的鍵（不執行 JavaScript）。"""
    match = re.search(rf"var {name} = \{{(.*?)\}};", source, re.S)
    assert match, name
    return re.findall(r"(\w+):\s*'", match.group(1))


def test_the_label_tables_cover_exactly_the_enums_the_backend_uses():
    """後端新增或改名列舉值時，這裡會失敗，提醒同步 `game-labels.js`（否則畫面會退回預設文字而不是正確的名稱）。"""
    source = read(JS_DIR / "game-labels.js")
    assert sorted(labels_in(source, "MEMBER_LABELS")) == sorted(MEMBER_TIERS)
    assert sorted(labels_in(source, "TIER_LABELS")) == sorted(CONTENT_TIERS)
    assert sorted(labels_in(source, "AUDIENCE_LABELS")) == sorted(AUDIENCES)
    assert sorted(labels_in(source, "ORIGIN_LABELS")) == sorted(ORIGINS)
    assert sorted(labels_in(source, "STATUS_LABELS")) == ["active", "ended"]
    assert sorted(labels_in(source, "CATEGORY_LABELS")) == sorted(REVIEW_CATEGORIES)
