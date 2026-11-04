"""script-authoring 規格的頁面部分：`/scripts` 的外殼、初始狀態、存取規則與前端檔案的規則。

資料操作與驗證見 `test_script_authoring_api.py`、`test_script_submission.py`；草稿模型與預覽的邏輯見 `tests/js/script-draft.test.js` 等。
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser

import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import PROJECT_ROOT, load_config
from app.models import Base
from tests.support.auth import START, FakeClock, add_user
from tests.support.idor import logged_in_client

STATE_PATTERN = re.compile(r'<script type="application/json" id="initial-state">(.*?)</script>', re.S)
STATIC = PROJECT_ROOT / "app" / "static"
TEMPLATE = PROJECT_ROOT / "app" / "templates" / "scripts" / "workshop.html"
JS_DIR = STATIC / "js" / "scripts"


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


# ---- 存取與初始狀態 -----------------------------------------------------------------------------------


def test_an_anonymous_visitor_is_sent_to_the_login_page_and_comes_back_afterwards(app):
    response = app.test_client().get("/scripts")
    assert response.status_code == 302 and response.headers["Location"].startswith("/login")
    assert "scripts" in response.headers["Location"]


def test_a_player_without_age_confirmation_is_sent_to_the_age_page(app):
    client, _ = client_for(app, age=None)
    response = client.get("/scripts")
    assert response.status_code == 302 and response.headers["Location"].startswith("/age")


def test_a_locked_player_is_sent_to_the_appeal_page(app):
    client, _ = client_for(app, locked_at=START, lock_reason="rate_limited")
    response = client.get("/scripts")
    assert response.status_code == 302 and response.headers["Location"].startswith("/appeal")


def test_the_initial_state_carries_only_the_players_own_public_fields_the_gate_and_the_submit_timeout(app):
    client, _ = client_for(app, nickname="雲遊者", nickname_code="A1B2C3")
    response = client.get("/scripts")
    assert response.status_code == 200
    state = state_of(response)
    assert set(state) == {"user", "gate", "submit_timeout_ms"}
    assert state["user"] == {
        "public_id": state["user"]["public_id"], "member_tier": "free", "nickname": "雲遊者", "nickname_code": "A1B2C3", "publish_suspended": False,
    }
    assert state["gate"] is None and state["submit_timeout_ms"] == 35000
    text = response.get_data(as_text=True)
    assert "user1@example.com" not in text and "birth" not in text.lower()               # 不帶 email 與生日


def test_the_submit_timeout_follows_the_total_budget_plus_five_seconds(app):
    config = load_config(environ={"APP_ENV": "testing", "LLM_REQUEST_BUDGET_SECONDS": "40", "LLM_CALL_TIMEOUT_SECONDS": "25"})
    other = create_app(config, clock=FakeClock())
    Base.metadata.create_all(other.extensions["db_engine"])
    client, _ = client_for(other)
    assert state_of(client.get("/scripts"))["submit_timeout_ms"] == 45000


def test_a_player_who_is_too_young_for_the_models_gets_the_gate_code_from_the_server(app):
    client, _ = client_for(app, age=12)
    assert state_of(client.get("/scripts"))["gate"] == "age_restricted"


def test_the_page_is_not_cached(app):
    client, _ = client_for(app)
    assert "no-store" in client.get("/scripts").headers.get("Cache-Control", "")


# ---- 前端檔案的規則 ----------------------------------------------------------------------------------


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


def test_the_rendered_page_loads_only_local_resources_and_the_workshop_script_comes_last(app):
    client, _ = client_for(app)
    html = client.get("/scripts").get_data(as_text=True)
    collector = _Resources()
    collector.feed(html)
    assert not [ref for ref in collector.refs if re.match(r"(https?:)?//", ref)]
    assert collector.data_bs == [("data-bs-theme", "dark")]
    names = [ref.rsplit("/", 1)[-1] for ref in collector.refs if ref.endswith(".js")]
    for needed in ("vue.global.prod.js", "axios.min.js", "ui-components.js", "restriction.js", "auth-common.js", "script-draft.js",
                   "milestone-preview.js", "script-api.js", "script-workshop.js"):
        assert needed in names, needed
    assert names[-1] == "script-workshop.js"
    assert names.index("script-draft.js") < names.index("script-workshop.js") and names.index("script-api.js") < names.index("script-workshop.js")
    assert any(ref.endswith("css/scripts.css") for ref in collector.refs)
    assert 'id="script-app"' in html and "v-cloak" in html


def test_the_root_element_has_v_cloak_and_the_stylesheet_hides_it():
    assert re.search(r'<div id="script-app"[^>]*\bv-cloak\b', read(TEMPLATE))
    assert re.search(r"\[v-cloak\]\s*\{\s*display:\s*none", read(STATIC / "css" / "scripts.css")) or re.search(
        r"\[v-cloak\]\s*\{\s*display:\s*none", read(STATIC / "css" / "layout.css")
    )


def test_every_author_text_is_shown_as_plain_text_and_no_forbidden_pattern_is_used():
    html = read(TEMPLATE)
    js = "\n".join(strip_js_comments(read(path)) for path in sorted(JS_DIR.glob("*.js")))
    assert "v-html" not in html and "v-html" not in js and "innerHTML" not in js
    assert "data-bs-" not in html and "offcanvas" not in html.lower()
    assert "<ui-modal" in html and "<ui-toast-region" in html
    assert not re.search(r"\b(alert|confirm|prompt)\s*\(", js)
    assert "cdn" not in html.lower() and "googleapis" not in html.lower()


def test_the_editor_explains_the_rules_field_in_plain_words_and_gives_an_example_for_every_text_field():
    """人工驗收發現：「狀態轉移規則」玩家看不懂，而且大部分欄位沒有範例。"""
    html = read(TEMPLATE)
    assert "世界規則（狀態轉移規則，選填）" in html
    assert "「狀態轉移」就是從一個狀態變成另一個狀態" in html and "寫給 AI 的「行事準則」" in html
    fields = [tag for tag in re.findall(r"<(?:input|textarea)\b[^>]*>", html) if 'type="radio"' not in tag]
    assert len(fields) >= 20, "沒有掃到預期數量的欄位，比對可能是空洞的"
    missing = [tag[:80] for tag in fields if not re.search(r'\s:?placeholder=', tag)]
    assert missing == [], missing
    for section in ("catalog", "background", "characters", "locations", "milestones", "ending"):
        assert re.search(rf'v-show="open\.{section}" class="sw-section__body">\s*<p class="sw-hint sw-lead">', html), section


def test_nothing_the_author_writes_is_kept_in_the_browser():
    js = "\n".join(strip_js_comments(read(path)) for path in sorted(JS_DIR.glob("*.js")))
    for storage in ("localStorage", "sessionStorage", "indexedDB", "document.cookie"):
        assert storage not in js, storage                                                # 劇本內容是個資，只存在伺服器


def test_submitting_uses_the_server_timeout_an_abort_controller_and_always_restores_the_buttons():
    js = strip_js_comments(read(JS_DIR / "script-workshop.js"))
    api = strip_js_comments(read(JS_DIR / "script-api.js"))
    assert "state.submit_timeout_ms" in js and "AbortController" in js and "signal" in js
    assert not re.search(r"\b(35000|45000)\b", js + api)                                 # 逾時只能來自伺服器的初始狀態
    assert "editor.submitting = false" in js and ".finally(" in js
    assert "fetch(" not in js + api                                                      # 一般 API 一律走離線 axios
    assert "axios" in api


def test_the_script_page_does_not_use_pure_white_text():
    css = read(STATIC / "css" / "scripts.css").lower()
    assert "#fff" not in css and not re.search(r"(?<![\w-])white(?![\w-])", css) and "rgb(255, 255, 255)" not in css


def test_the_page_is_traditional_chinese_with_the_utf8_declarations(app):
    client, _ = client_for(app)
    html = client.get("/scripts").get_data(as_text=True)
    assert '<html lang="zh-Hant-TW"' in html and '<meta charset="UTF-8"' in html
    assert "新增劇本" in html
