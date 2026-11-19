"""account-safe-mode-setting 規格：帳號頁「安全模式」區塊的樣板與初始狀態。

涵蓋：區塊的位置與開關、兩種狀態的說明、同意 Modal（只有安全規範的勾選框、沒有年齡勾選）、初始狀態只有列舉值與布林
（不含生日、年齡區間、同意時間戳記）、舊的同意失效、緊急煞車、尚未完成年齡確認的管理員與等待刪除者沒有區塊、
返回位置的驗證（與前端共用同一張案例表），以及腳本的載入順序與端點。真正的點擊流程在 Node 測試與瀏覽器驗收。
"""
import json
import re
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import create_app
from app.config import PROJECT_ROOT, load_config
from app.models import Base, User
from app.models.user import STATUS_DELETION_PENDING
from app.services.content_tier import SAFE_RULES_VERSION
from tests.support.age import TODAY_UTC, birth_date_for_age
from tests.support.auth import START, FakeClock, add_user
from tests.support.idor import logged_in_client

STATE_PATTERN = re.compile(r'<script type="application/json" id="initial-state">(.*?)</script>', re.S)
JS_DIR = PROJECT_ROOT / "app" / "static" / "js" / "auth"
CONFIRMED = {"adult_confirmed_at": TODAY_UTC - timedelta(days=3), "adult_rules_version": SAFE_RULES_VERSION, "adult_confirm_method": "self_declared"}
MODE_KEYS = {"effective", "confirmed", "forced", "returnPath"}
RETURN_CASES = json.loads((PROJECT_ROOT / "tests" / "js" / "fixtures" / "safe-return-path-cases.json").read_text(encoding="utf-8-sig"))


def make_app(**environ):
    application = create_app(load_config(environ={"APP_ENV": "testing", **environ}), clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


@pytest.fixture()
def app():
    return make_app()


def page_for(app, *, query=None, **overrides):
    with Session(app.extensions["db_engine"]) as s:
        user = add_user(s, 1, **{"age": 30, **overrides})
        s.refresh(user)
        s.expunge(user)
    return logged_in_client(app, user).get("/account", query_string=query)


def state_of(response) -> dict:
    match = STATE_PATTERN.search(response.get_data(as_text=True))
    assert match, "頁面沒有初始狀態區塊"
    return json.loads(match.group(1))


def mode_of(response) -> dict | None:
    return state_of(response)["contentMode"]


def safe_mode_block(html: str) -> str:
    start = html.index('aria-labelledby="safe-mode-title"')
    return html[start:html.index("</section>", start)]


def safe_mode_modal(html: str) -> str:
    start = html.index('v-model="safeMode.modalOpen"')
    return html[start:html.index("</ui-modal>", start)]


# ---- 樣板 --------------------------------------------------------------------------------------

def test_the_block_sits_between_the_age_section_and_the_nickname_section_and_has_a_real_switch(app):
    html = page_for(app).get_data(as_text=True)
    assert html.index('aria-labelledby="age-title"') < html.index('aria-labelledby="safe-mode-title"') < html.index('aria-labelledby="nickname-title"')
    assert '<section v-if="safeMode.available" class="account-section" aria-labelledby="safe-mode-title">' in html
    block = safe_mode_block(html)
    assert 'role="switch"' in block and '@click="toggleSafeMode"' in block
    assert ":aria-checked=\"safeMode.safe ? 'true' : 'false'\"" in block                       # 螢幕閱讀器讀得到開或關
    assert ':disabled="safeMode.busy || safeMode.forced"' in block                              # 請求期間與緊急煞車時禁用
    for name in ("safeModeCopy.stateOn", "safeModeCopy.stateOff", "safeModeCopy.forcedNote", "safeModeCopy.boundNote", "safeModeCopy.returnHint"):
        assert name in block, name
    assert 'v-if="safeMode.forced"' in block and 'v-if="safeMode.error && !safeMode.modalOpen"' in block
    assert 'v-if="safeMode.returnPath"' in block and ':href="safeMode.returnPath"' in block


def test_the_modal_has_only_the_rules_checkbox_and_a_confirm_button_that_waits_for_it(app):
    html = page_for(app).get_data(as_text=True)
    modal = safe_mode_modal(html)
    inputs = re.findall(r"<input\b[^>]*>", modal)
    assert len(inputs) == 1                                                                     # 只有安全規範的勾選框
    assert 'type="checkbox"' in inputs[0] and 'name="confirm_rules"' in inputs[0] and 'v-model="safeMode.agreed"' in inputs[0]
    assert ':disabled="!safeMode.agreed || safeMode.busy"' in modal and '@click="confirmSafeMode"' in modal
    assert '@click="cancelSafeModeModal"' in modal
    assert '@close="cancelSafeModeModal"' in html[html.index('v-model="safeMode.modalOpen"'):html.index("<p>{{ safeModeCopy.modalIntro")]
    for forbidden in ("滿 18", "已滿", "年齡", "生日"):
        assert forbidden not in modal, forbidden                                                # 沒有年齡勾選或年齡說明
    assert "safeModeCopy.rulesPoints" in modal and "safeModeCopy.agreeLabel" in modal
    assert 'href="/terms"' in modal and 'href="/privacy"' in modal


def test_the_block_and_modal_use_no_bootstrap_scripts_and_no_raw_html(app):
    html = page_for(app).get_data(as_text=True)
    parts = safe_mode_block(html) + safe_mode_modal(html)
    assert "data-bs-" not in parts and "v-html" not in parts
    assert re.findall(r"data-bs-[\w-]+", html) == ["data-bs-theme"]


def test_a_member_awaiting_deletion_has_no_safe_mode_block_or_state(app):
    page = page_for(app, status=STATUS_DELETION_PENDING, deletion_requested_at=START, status_before_deletion="active")
    html = page.get_data(as_text=True)
    assert mode_of(page) is None
    for absent in ("safe-mode-title", "safe_mode_switch", "toggleSafeMode", "confirm_rules", "safeMode."):
        assert absent not in html, absent


# ---- 初始狀態 ----------------------------------------------------------------------------------

def test_the_initial_state_of_a_new_member_is_only_enumerations_and_booleans(app):
    mode = mode_of(page_for(app))
    assert mode == {"effective": "safe", "confirmed": False, "forced": False, "returnPath": None}
    assert set(mode) == MODE_KEYS


def test_the_initial_state_never_holds_the_birthday_age_range_or_consent_timestamps(app):
    page = page_for(app, content_tier_preference="adult", **CONFIRMED)
    mode = mode_of(page)
    assert mode["effective"] == "adult" and mode["confirmed"] is True and set(mode) == MODE_KEYS
    assert "contentMode" in state_of(page) and mode_of(page)["effective"] == "adult"
    for absent in ("adult_confirmed_at", "adult_rules_version", "adult_confirm_method", "age_range", "birth_date", "age_confirmed_at"):
        assert absent not in json.dumps(mode), absent
    raw = page.get_data(as_text=True)
    assert birth_date_for_age(30).isoformat() not in raw and SAFE_RULES_VERSION not in raw


def test_a_member_who_agreed_to_an_older_version_is_shown_as_on_and_has_to_agree_again(app):
    stale = {**CONFIRMED, "adult_rules_version": "2020-01-01-old"}
    mode = mode_of(page_for(app, content_tier_preference="adult", **stale))
    assert mode["effective"] == "safe" and mode["confirmed"] is False                          # 偏好是關，但同意已失效，所以是開


def test_a_legacy_standard_preference_with_a_current_agreement_reads_as_off(app):
    mode = mode_of(page_for(app, content_tier_preference="standard", **CONFIRMED))
    assert mode["effective"] == "adult" and mode["confirmed"] is True


def test_the_emergency_brake_shows_the_switch_on_and_flags_it_but_keeps_the_saved_preference():
    forced = make_app(SAFE_MODE_FORCED="true")
    mode = mode_of(page_for(forced, content_tier_preference="adult", **CONFIRMED))
    assert mode == {"effective": "safe", "confirmed": True, "forced": True, "returnPath": None}
    with Session(forced.extensions["db_engine"]) as s:
        assert s.execute(select(User.content_tier_preference)).scalar_one() == "adult"       # 玩家保存的設定沒有被改動


def test_an_admin_who_has_not_confirmed_the_age_has_no_safe_mode_state(app):
    response = page_for(app, role="admin", age=None)
    assert response.status_code == 200 and mode_of(response) is None                           # 前端據此不顯示區塊


def test_an_admin_who_confirmed_the_age_gets_the_block_like_everyone_else(app):
    assert mode_of(page_for(app, role="admin"))["effective"] == "safe"                          # 管理員不豁免任何模式規則


# ---- 返回位置 ----------------------------------------------------------------------------------

@pytest.mark.parametrize("value", RETURN_CASES["valid"])
def test_a_valid_return_position_reaches_the_page_state(app, value):
    response = page_for(app, query={"return": value})
    assert response.status_code == 200 and mode_of(response)["returnPath"] == value


@pytest.mark.parametrize("value", [*RETURN_CASES["invalid"], "/game/start/" + "a" * 200])
def test_an_invalid_return_position_is_dropped_and_the_page_is_unchanged(app, value):
    response = page_for(app, query={"return": value})
    assert response.status_code == 200
    mode = mode_of(response)
    assert mode["returnPath"] is None and set(mode) == MODE_KEYS


def test_a_rejected_return_position_never_appears_anywhere_in_the_page(app):
    response = page_for(app, query={"return": "https://evil.example/"})
    assert "evil.example" not in response.get_data(as_text=True)


# ---- 腳本 --------------------------------------------------------------------------------------

def test_the_safe_mode_script_loads_before_the_page_script_and_only_from_the_site(app):
    html = page_for(app).get_data(as_text=True)
    sources = re.findall(r'<script[^>]+src="/static/(js/[^"?]+)', html)
    assert sources.index("js/auth/account-lifecycle.js") < sources.index("js/auth/account-safe-mode.js") < sources.index("js/auth/account.js")
    assert not re.findall(r'<script[^>]+src="https?://', html)


def test_the_script_puts_to_the_endpoint_the_server_serves_and_keeps_to_the_rules(app):
    source = (JS_DIR / "account-safe-mode.js").read_text(encoding="utf-8-sig")
    endpoint = re.search(r"var ENDPOINT = '([^']+)'", source).group(1)
    methods_by_rule = {}
    for rule in app.url_map.iter_rules():
        methods_by_rule.setdefault(rule.rule, set()).update(rule.methods)
    assert endpoint == "/api/account/content-mode" and {"GET", "PUT"} <= methods_by_rule[endpoint]
    account = (JS_DIR / "account.js").read_text(encoding="utf-8-sig")
    assert "axios.put(safeModeApi.ENDPOINT, body)" in account and "initial: state.contentMode" in account
    for text in (source, account):
        assert "v-html" not in text and "localStorage" not in text
