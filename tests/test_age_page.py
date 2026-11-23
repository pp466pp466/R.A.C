"""post-login-age-confirmation 與 auth-pages 規格：年齡確認頁 `/age` 的存取規則與初始狀態。

頁面只負責外殼與初始狀態；補填的資料操作走 `/api/auth/verify-age`（見 `test_age_step_service.py`、`test_auth_api.py`）。
前端的互動（送出中、各種錯誤代碼、補填成功後導向 `next`）由 `tests/js/` 的狀態機測試涵蓋。
"""
from __future__ import annotations

import json
import re
from urllib.parse import quote

import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base
from app.models.user import STATUS_DELETION_PENDING
from app.security.redirects import DEFAULT_AFTER_LOGIN
from tests.support.auth import START, FakeClock, add_user
from tests.support.idor import logged_in_client

STATE_PATTERN = re.compile(r'<script type="application/json" id="initial-state">(.*?)</script>', re.S)


@pytest.fixture()
def app():
    application = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


def client_with(app, **overrides):
    with Session(app.extensions["db_engine"]) as s:
        return logged_in_client(app, add_user(s, 1, **overrides))


def state_of(response) -> dict:
    return json.loads(STATE_PATTERN.search(response.get_data(as_text=True)).group(1))


# ---- 誰能開 -------------------------------------------------------------------------------------------

def test_a_player_waiting_for_the_age_step_can_open_the_page(app):
    response = client_with(app, age=None).get("/age")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'lang="zh-Hant-TW"' in html and "年齡確認" in html


def test_the_age_page_says_the_service_is_for_adults_with_the_number_from_the_age_rules(app):
    """adult-only-signup：年齡確認頁提示「本服務僅限滿 18 歲者使用」。數字由 Vue 以年齡規則插值，範本不寫死；
    暱稱會公開、請勿使用真實姓名的提示照舊；舊的「僅供 N 歲以上使用」說法已不存在。"""
    response = client_with(app, age=None).get("/age")
    html = response.get_data(as_text=True)
    note = html.split('id="adults-only-note"', 1)[1].split("</p>", 1)[0]
    assert "本服務僅限滿 {{ ageRules.minRegisterAge }} 歲者使用" in note
    assert state_of(response)["ageRules"]["minRegisterAge"] == 18
    assert "暱稱會公開顯示於您創作的劇本，請勿使用真實姓名" in html
    assert "僅供 {{ ageRules.minRegisterAge }} 歲以上使用" not in html


def test_a_player_who_already_confirmed_is_sent_to_the_default_page(app):
    response = client_with(app, age=30).get("/age")
    assert response.status_code == 302 and response.headers["Location"] == DEFAULT_AFTER_LOGIN


def test_a_player_who_already_confirmed_is_sent_to_the_game_entry_not_the_chat(app):
    response = client_with(app, age=30).get("/age")
    assert response.status_code == 302 and response.headers["Location"] == "/game"


def test_the_age_step_without_a_destination_leads_to_the_game_entry_afterwards(app):
    """完成年齡確認之後沒有指定返回位置：前端依初始狀態的 `next` 導向 /game（rpg-entry-ui）。"""
    assert state_of(client_with(app, age=None).get("/age"))["next"] == "/game"


def test_an_admin_without_age_data_is_sent_to_the_default_page_because_the_admin_fills_it_in_the_account_settings(app):
    response = client_with(app, age=None, role="admin").get("/age")
    assert response.status_code == 302 and response.headers["Location"] == DEFAULT_AFTER_LOGIN


def test_an_anonymous_visitor_is_sent_to_the_login_page_and_the_destination_is_kept(app):
    response = app.test_client().get("/age?next=/account")
    assert response.status_code == 302
    assert response.headers["Location"] == "/login?next=" + quote("/age?next=/account", safe="")


@pytest.mark.parametrize(
    ("overrides", "page"),
    [
        (dict(age=None, locked_at=START, lock_reason="rate_limited"), "/appeal"),                       # 被鎖定優先
        (dict(age=30, status=STATUS_DELETION_PENDING, deletion_requested_at=START, status_before_deletion="active"), "/account"),
    ],
    ids=["locked", "deletion-pending"],
)
def test_the_other_restricted_states_are_sent_to_their_own_pages(app, overrides, page):
    response = client_with(app, **overrides).get("/age")
    assert response.status_code == 302 and response.headers["Location"] == page


# ---- 初始狀態 -----------------------------------------------------------------------------------------

def test_the_state_carries_the_age_rules_and_a_validated_next_and_nothing_personal(app):
    client = client_with(app, age=None)
    response = client.get("/age?next=/account%3Ftab%3D1")
    state = state_of(response)
    assert state["next"] == "/account?tab=1"
    rules = state["ageRules"]
    assert rules["minRegisterAge"] == 18 and rules["adultAge"] == 18 and rules["ranges"]
    assert rules["birthDateMin"] == "1900-01-01" and rules["birthDateMax"] == "2026-10-27"          # 假時鐘的台北日期
    assert "user" not in state                                                                       # 頁面狀態不含個人資料
    html = response.get_data(as_text=True)
    assert "user1@example.com" not in html and "使用者1" not in html


@pytest.mark.parametrize("bad", ["https://evil.example/", "//evil.example", "javascript:alert(1)", "/\\evil", "/%2f/evil", ""])
def test_an_unsafe_next_is_replaced_by_the_default_page(app, bad):
    assert state_of(client_with(app, age=None).get(f"/age?next={bad}"))["next"] == DEFAULT_AFTER_LOGIN


def test_the_page_is_not_cached_by_shared_caches(app):
    response = client_with(app, age=None).get("/age")
    cache = response.headers.get("Cache-Control", "")
    assert "public" not in cache


# ---- 頁面外殼與腳本（互動的判斷邏輯在 tests/js/auth-flows.test.js）------------------------------------------

def _asset(kind: str, name: str) -> str:
    from app.config import PROJECT_ROOT

    base = PROJECT_ROOT / "app" / ("templates" if kind == "template" else "static/js")
    return (base / name).read_text(encoding="utf-8")


def test_the_page_has_a_labelled_birthday_and_a_range_select_fed_by_the_age_rules(app):
    html = client_with(app, age=None).get("/age").get_data(as_text=True)
    assert 'for="birth_date"' in html and 'id="birth_date"' in html and 'type="date"' in html and 'autocomplete="bday"' in html
    assert ':min="ageRules.birthDateMin"' in html and ':max="ageRules.birthDateMax"' in html
    assert 'for="age_range"' in html and "<select" in html and 'v-for="range in ageRules.ranges"' in html
    assert "form.fieldErrors.birth_date" in html and "form.fieldErrors.age_range" in html
    assert "{{ ageRules.minRegisterAge }}" in html                                       # 年齡數字從初始狀態取，不寫死
    assert "confirm_adult" not in html and "我已年滿" not in html


def test_the_page_asks_for_a_nickname_in_the_same_step_with_the_public_display_hint(app):
    """玩家的暱稱與生日、年齡區間是同一個步驟：欄位必填、有公開顯示的提示、錯誤顯示在欄位旁、純文字渲染。"""
    html = client_with(app, age=None).get("/age").get_data(as_text=True)
    assert 'for="nickname"' in html and 'id="nickname"' in html and 'name="nickname"' in html and "required" in html
    assert 'v-model="form.values.nickname"' in html and "form.fieldErrors.nickname" in html
    assert "暱稱會公開顯示於您創作的劇本，請勿使用真實姓名" in html
    assert "v-html" not in html
    script = _asset("static", "auth/age.js")
    assert "nickname: ''" in script and "nicknameRequired: true" in script
    flows = _asset("static", "auth/auth-flows.js")
    assert "nickname" in flows.split("function ageBody", 1)[1].split("function ageOutcome", 1)[0]


def test_the_submit_button_is_disabled_and_busy_while_sending_and_there_is_a_way_out(app):
    html = client_with(app, age=None).get("/age").get_data(as_text=True)
    assert ':disabled="form.state.submitting"' in html and ":aria-busy=" in html
    assert '@click="logout"' in html                                                    # 填不下去時可以登出
    assert 'href="/terms"' in html


def test_when_the_account_was_erased_only_the_message_and_a_link_to_the_login_page_remain(app):
    html = client_with(app, age=None).get("/age").get_data(as_text=True)
    final = html.split('<div v-if="finished">', 1)[1].split("</div>", 1)[0]
    assert "{{ finished.message }}" in final and ':href="finished.to"' in final and "回到登入頁" in final
    assert 'v-else @submit.prevent="onPrimary"' in html                                 # 表單只在流程還沒結束時存在


def test_the_script_validates_sends_and_follows_the_flow_module_and_keeps_nothing_in_the_browser():
    script = _asset("script", "auth/age.js")
    assert "Auth.validateAge(form.values, { nicknameRequired: true })" in script
    assert "Onboarding.createOnboarding(" in script and "flow.submit(form.values)" in script          # 一次送出的順序在 onboarding.js（Node 測試）
    assert "axios.post(url, body)" in script and "axios.put(url, body)" in script and "Auth.safeDestination(state.next" in script
    assert "Auth.ageOutcome(info)" in script and "form.run(" in script                    # 處理中、錯誤與 finally 的恢復都由 useForm 負責
    for storage in ("localStorage", "sessionStorage", "indexedDB", "document.cookie"):
        assert storage not in script, storage                                           # 生日不寫進任何瀏覽器儲存體
    template = _asset("template", "auth/age.html")
    assert template.index("js/auth/auth-flows.js") < template.index("js/auth/onboarding.js") < template.index("js/auth/age.js")      # 判斷邏輯先載入


def test_the_fallback_after_a_successful_age_check_is_the_default_landing_page_not_the_chat():
    """伺服器一定會給 `next`（預設就是 `DEFAULT_AFTER_LOGIN`），腳本的備援只在初始狀態壞掉時才用；它曾停在改版之前的 `/cs`（客服），
    與「登入後的預設頁面是 /game」不一致。備援要跟常數一致，之後預設頁面再改時這裡會提醒。"""
    script = _asset("script", "auth/age.js")
    assert f"Auth.safeDestination(state.next, '{DEFAULT_AFTER_LOGIN}')" in script
    assert "'/cs'" not in script


# ---- 分步精靈（onboarding-wizard 規格）-----------------------------------------------------------------------

def _age_html(app) -> str:
    return client_with(app, age=None).get("/age").get_data(as_text=True)


def test_the_page_is_a_three_step_wizard_with_a_progress_bar_a_step_label_and_back_and_next_buttons(app):
    html = _age_html(app)
    assert 'class="onb-progress"' in html and 'v-for="(filled, position) in progress.segments"' in html and ':aria-label="progress.label"' in html
    assert 'aria-live="polite"' in html and "{{ progress.label }}" in html                 # 「第 N 步，共 M 步」給輔助科技讀
    for step in ("progress.step === 'welcome'", "progress.step === 'age'"):
        assert step in html
    assert '<section v-else class="onb-step"' in html                                      # 第 3 步（故事偏好）
    assert 'v-if="!progress.isFirst"' in html and '@click="goBack"' in html and "上一步" in html
    assert "progress.primaryLabel" in html and "送出中…" in html and ':disabled="!gate.ok || form.state.submitting"' in html
    assert "{{ gate.reason }}" in html and 'id="onb-gate"' in html                         # 不可前進時以文字說明缺什麼
    assert html.count('ref="heading"') == 3 and html.count('tabindex="-1"') == 3           # 每一步的標題都能接手焦點


def test_step_one_has_the_nickname_and_the_guideline_row_and_step_two_the_age_fields_and_step_three_the_preference(app):
    html = _age_html(app)
    welcome = html.split("progress.step === 'welcome'", 1)[1].split("progress.step === 'age'", 1)[0]
    assert 'id="nickname"' in welcome and "使用守則" in welcome and "已確認使用守則" in welcome and 'aria-haspopup="dialog"' in welcome
    assert 'id="birth_date"' not in welcome and 'id="age_range"' not in welcome
    age = html.split("progress.step === 'age'", 1)[1].split('<section v-else class="onb-step"', 1)[0]
    assert 'id="birth_date"' in age and 'id="age_range"' in age and 'id="adults-only-note"' in age and 'id="nickname"' not in age
    preference = html.split('<section v-else class="onb-step"', 1)[1].split("</section>", 1)[0]
    assert 'type="radio"' in preference and 'name="story_preference"' in preference and 'v-model="form.values.storyPreference"' in preference
    script = _asset("static", "auth/age.js")
    for value, label in (("all", "全讀者"), ("male", "男頻"), ("female", "女頻")):
        assert "{ value: '" + value + "', label: '" + label + "' }" in script
    assert "storyPreference: Onboarding.DEFAULT_PREFERENCE" in script                    # 預設全讀者


def test_the_guideline_modal_is_a_vue_controlled_plain_text_dialog_that_only_the_know_button_confirms(app):
    html = _age_html(app)
    modal = html.split("<ui-modal", 1)[1].split("</ui-modal>", 1)[0]
    assert 'v-model="guideOpen"' in modal and 'title="使用守則"' in modal and 'confirm-text="我已知悉"' in modal and 'cancel-text="關閉"' in modal
    assert '@confirm="confirmGuide"' in html and "@cancel" not in html and "@close" not in html      # 只有「我已知悉」才算確認
    script = _asset("static", "auth/age.js")
    assert "function confirmGuide() { acknowledged.value = true; }" in script
    assert "acknowledged" not in script.split("flow.submit(", 1)[1].split(")", 1)[0]       # 確認狀態不放進送出的內容
    assert 'href="/terms"' in modal and 'href="/privacy"' in modal


def test_the_guidelines_cover_the_required_points_and_every_one_is_also_stated_in_the_terms_or_the_privacy_policy(app):
    html = _age_html(app)
    modal = html.split("<ui-modal", 1)[1].split("</ui-modal>", 1)[0]
    assert "本服務僅限滿 {{ ageRules.minRegisterAge }} 歲者使用" in modal                    # 年齡數字不寫死
    for phrase in ("暱稱會公開顯示於您創作的劇本，請勿使用真實姓名", "請勿輸入真實個資", "經 AI 處理", "模型供應商留存或審閱", "未成年角色的性內容",
                   "性暴力", "非自願的性情節", "以真人名義的性內容", "故事內容皆為虛構", "不會另外留下紀錄"):
        assert phrase in modal, phrase
    legal = app.test_client()
    terms = legal.get("/terms").get_data(as_text=True)
    privacy = legal.get("/privacy").get_data(as_text=True)
    for phrase in ("未成年", "性暴力", "非自願", "真人", "虛構"):
        assert phrase in terms, phrase                                                     # 守則只是白話摘要，不與條款矛盾
    for phrase in ("供應商", "審閱", "留存"):
        assert phrase in privacy, phrase


def test_the_wizard_template_and_scripts_use_no_unsafe_dom_no_bootstrap_scripts_no_browser_storage_and_only_local_resources(app):
    html = _age_html(app)
    for forbidden in ("v-html", "bootstrap.bundle", "bootstrap.min.js"):
        assert forbidden not in html, forbidden
    assert "data-bs-" not in html.replace('data-bs-theme="dark"', "")              # 唯一的例外是深色模式開關
    assert "http://" not in html and "https://" not in html
    for script_name in ("auth/age.js", "auth/onboarding.js"):
        source = _asset("static", script_name)
        code = "\n".join(line for line in source.splitlines() if not line.strip().startswith(("*", "/*", "//")))
        for banned in ("localStorage", "sessionStorage", "indexedDB", "document.cookie", "innerHTML", "v-html"):
            assert banned not in code, (script_name, banned)                              # 生日是個資：精靈不寫任何瀏覽器儲存


def test_the_wizard_styles_respect_reduced_motion_and_never_use_pure_white():
    from app.config import PROJECT_ROOT

    css = (PROJECT_ROOT / "app" / "static" / "css" / "auth.css").read_text(encoding="utf-8")
    wizard = css.split("登入後設定的分步精靈", 1)[1]
    assert "prefers-reduced-motion: no-preference" in wizard                              # 動畫只在沒有要求減少動態時才套用
    for color in ("#fff", "#FFF", "#ffffff", "#FFFFFF", "white"):
        assert color not in wizard, color


def test_the_wizard_scripts_are_served_from_the_local_static_folder(app):
    client = client_with(app, age=None)
    for name in ("auth/onboarding.js", "auth/age.js"):
        assert client.get(f"/static/js/{name}").status_code == 200
