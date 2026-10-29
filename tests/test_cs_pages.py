"""cs-chat-ui 規格：`/cs` 頁面的外殼、初始狀態與前端檔案的規則（逾時不寫死、v-cloak、純文字渲染、離線資源）。"""
import json
import re
from html.parser import HTMLParser

import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import PROJECT_ROOT, load_config
from app.models import Base, User
from app.services.access_policy import AccessPolicy
from app.services.current_user import CurrentUser
from app.services.errors import ServiceError
from tests.support.auth import FakeClock, add_user
from tests.support.config_env import AUTH_ENV
from tests.support.email_flow import FakeSender
from tests.support.idor import logged_in_client
from tests.support.llm import FakeLLMProvider

STATIC = PROJECT_ROOT / "app" / "static"
CHAT_JS = STATIC / "js" / "cs" / "chat.js"
CHAT_HTML = PROJECT_ROOT / "app" / "templates" / "cs" / "chat.html"


def initial_state_of(response):
    match = re.search(r'<script type="application/json" id="initial-state">(.*?)</script>', response.get_data(as_text=True), re.S)
    assert match, "頁面沒有初始狀態"
    return json.loads(match.group(1))


@pytest.fixture()
def world():
    """信箱驗證開啟（才有「信箱未驗證」這一種帳號）；四種帳號：可用、沒有生日、年齡不足、信箱未驗證。"""
    config = load_config(environ={"APP_ENV": "testing", "EMAIL_VERIFICATION_REQUIRED": "true", **AUTH_ENV, "BASE_URL": "http://localhost:5000"})
    app = create_app(config, mail_sender=FakeSender(), clock=FakeClock(), llm_provider=FakeLLMProvider())
    Base.metadata.create_all(app.extensions["db_engine"])
    from datetime import timedelta

    from tests.support.auth import START

    verified = START - timedelta(days=1)
    with Session(app.extensions["db_engine"]) as session:
        app.accounts = {
            "usable": add_user(session, 1, age=30, email_verified_at=verified).id,
            "no-birthday": add_user(session, 2, email_verified_at=verified).id,
            "too-young": add_user(session, 3, age=12, email_verified_at=verified).id,
            "unverified": add_user(session, 4, age=30).id,
        }
    return app


def account(app, name):
    with Session(app.extensions["db_engine"]) as session:
        return session.get(User, app.accounts[name])


@pytest.mark.parametrize("name, expected", [
    ("usable", None), ("no-birthday", "age_confirmation_required"), ("too-young", "age_restricted"), ("unverified", "forbidden"),
])
def test_the_page_reports_the_gate_result_and_it_matches_what_the_api_would_enforce(world, name, expected):
    app = world
    state = initial_state_of(logged_in_client(app, account(app, name)).get("/cs"))
    assert state["chat_gate"] == expected
    # 與 require_chat_access 的結果一致：頁面的閘門代碼就是 API 會拋出的錯誤代碼
    policy = AccessPolicy(email_verification_required=True, clock=app.extensions["clock"])
    user = CurrentUser.from_model(account(app, name))
    if expected is None:
        policy.require_chat_access(user)
    else:
        with pytest.raises(ServiceError) as denied:
            policy.require_chat_access(user)
        assert denied.value.code == expected
        response = logged_in_client(app, account(app, name)).post(
            "/api/cs/conversations", json={"client_id": "00000000-0000-4000-8000-000000000001"}
        )
        assert response.status_code == 403 and response.get_json()["error"]["code"] == expected


def test_the_default_request_timeout_is_the_total_budget_plus_five_seconds_and_follows_the_setting(world):
    assert initial_state_of(logged_in_client(world, account(world, "usable")).get("/cs"))["request_timeout_ms"] == 35000
    config = load_config(environ={"APP_ENV": "testing", "LLM_REQUEST_BUDGET_SECONDS": "40", "LLM_CALL_TIMEOUT_SECONDS": "25"})
    app = create_app(config, clock=FakeClock())
    Base.metadata.create_all(app.extensions["db_engine"])
    with Session(app.extensions["db_engine"]) as session:
        user = add_user(session, 1, age=30)
        user = session.get(User, user.id)
    assert initial_state_of(logged_in_client(app, user).get("/cs"))["request_timeout_ms"] == 45000


# ---- 前端檔案的規則 ---------------------------------------------------------------------------------


def strip_js_comments(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?<![:\w])//[^\n]*", "", source)


def test_no_timeout_number_is_hard_coded_in_the_chat_page_or_its_template():
    """逾時只能來自伺服器放進初始狀態的 request_timeout_ms；前端不得自己寫死 35 秒或任何數字。"""
    js = strip_js_comments(CHAT_JS.read_text(encoding="utf-8-sig"))
    html = CHAT_HTML.read_text(encoding="utf-8-sig")
    for source in (js, html):
        assert not re.search(r"\b(35000|45000|35_000)\b", source)
        assert not re.search(r"timeout\w*\s*[:=]\s*\d", source, re.I)
        assert not re.search(r"\b3[05]\s*\*\s*1000\b", source)
    assert "state.request_timeout_ms" in js and "sendTimeoutMs" in js


def test_the_root_element_has_v_cloak_so_the_raw_template_is_never_shown():
    html = CHAT_HTML.read_text(encoding="utf-8-sig")
    assert re.search(r'<div id="cs-app"[^>]*\bv-cloak\b', html)
    assert re.search(r"\[v-cloak\]\s*\{\s*display:\s*none", (STATIC / "css" / "cs.css").read_text(encoding="utf-8-sig"))


def test_every_message_and_title_is_interpolated_as_plain_text():
    html = CHAT_HTML.read_text(encoding="utf-8-sig")
    js = strip_js_comments(CHAT_JS.read_text(encoding="utf-8-sig"))         # 說明文字可以提到被禁用的寫法
    assert "v-html" not in html and "v-html" not in js and "innerHTML" not in js
    for expression in ("{{ item.text }}", "{{ item.title }}", "{{ chip.label }}", "{{ activeTitle }}"):
        assert expression in html
    assert re.search(r"\.cs-msg__text\s*\{[^}]*white-space:\s*pre-wrap", (STATIC / "css" / "cs.css").read_text(encoding="utf-8-sig"), re.S)


def test_the_page_uses_its_own_modal_and_toast_and_no_browser_dialogs():
    html = CHAT_HTML.read_text(encoding="utf-8-sig")
    js = strip_js_comments(CHAT_JS.read_text(encoding="utf-8-sig"))
    assert "<ui-modal" in html and "<ui-toast-region" in html
    assert not re.search(r"\b(alert|confirm|prompt)\s*\(", js)
    assert "offcanvas" not in html.lower() and "data-bs-" not in html


def test_the_sidebar_is_280px_and_the_desktop_breakpoint_is_992px():
    css = (STATIC / "css" / "cs.css").read_text(encoding="utf-8-sig")
    assert "--cs-sidebar-width: 280px" in css and "min-width: 992px" in css


def test_the_chips_are_pills_with_the_documented_colours():
    css = (STATIC / "css" / "cs.css").read_text(encoding="utf-8-sig")
    chip = re.search(r"\.cs-chip\s*\{(.*?)\}", css, re.S).group(1)
    assert "border-radius: 999px" in chip and "border: 1px solid var(--color-border)" in chip
    assert "background: transparent" in chip and "color: var(--color-text-muted)" in chip
    hover = re.search(r"\.cs-chip:hover[^{]*\{(.*?)\}", css, re.S).group(1)
    assert "color: var(--color-accent)" in hover and "border-color: var(--color-accent)" in hover
    theme = (STATIC / "css" / "theme.css").read_text(encoding="utf-8-sig")
    assert "--color-border: #2A2D34" in theme and "--color-text-muted: #8A8F99" in theme and "--color-accent: #FF6B35" in theme


def test_the_page_never_uses_a_pure_white_text_colour():
    css = (STATIC / "css" / "cs.css").read_text(encoding="utf-8-sig").lower()
    assert "#fff" not in css and not re.search(r"(?<![\w-])white(?![\w-])", css) and "rgb(255, 255, 255)" not in css


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


def test_the_rendered_page_loads_only_local_scripts_in_the_right_order(world):
    html = logged_in_client(world, account(world, "usable")).get("/cs").get_data(as_text=True)
    collector = _Resources()
    collector.feed(html)
    assert not [ref for ref in collector.refs if re.match(r"(https?:)?//", ref)]
    assert collector.data_bs == [("data-bs-theme", "dark")]
    scripts = [ref for ref in collector.refs if ref.endswith(".js")]
    names = [ref.rsplit("/", 1)[-1] for ref in scripts]
    for needed in ("vue.global.prod.js", "axios.min.js", "typewriter.js", "ui-components.js", "auth-common.js", "cs-sidebar.js", "cs-store.js", "cs-sync.js", "chat.js"):
        assert needed in names, needed
    assert names.index("chat.js") == len(names) - 1                                   # 最後載入，前面的模組都已就緒
    assert names.index("cs-store.js") < names.index("cs-sync.js") < names.index("chat.js")
    assert any(ref.endswith("css/cs.css") for ref in collector.refs)
    assert 'id="cs-app"' in html and "v-cloak" in html and "{{ item.text }}" in html


def test_the_page_html_contains_no_private_text_only_the_initial_state(world):
    html = logged_in_client(world, account(world, "usable")).get("/cs").get_data(as_text=True)
    assert "user1@example.com" not in html and "birth" not in html.lower()
