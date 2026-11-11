"""auth-pages 規格：帳號設定頁的匯出與刪除帳號區塊、等待刪除者看到的撤銷頁、管理員沒有刪除、登入頁的寬限期說明。

等待刪除者與管理員拿到的是不同的 HTML（多餘的區塊不在頁面裡，不只是用 v-if 藏起來）。真正的點擊流程在瀏覽器驗收。
"""
import json
import re
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import PROJECT_ROOT, load_config
from app.models import Base
from app.models.user import STATUS_DELETION_PENDING
from app.services.account_deletion_service import CONFIRM_TEXT
from tests.support.age import birth_date_for_age
from tests.support.auth import START, FakeClock, add_user
from tests.support.idor import logged_in_client

STATE_PATTERN = re.compile(r'<script type="application/json" id="initial-state">(.*?)</script>', re.S)
JS_DIR = PROJECT_ROOT / "app" / "static" / "js" / "auth"
NORMAL_ONLY = ("變更密碼", "匯出我的資料", "登出所有裝置", "年齡確認", "安全性", "會員等級")


@pytest.fixture()
def app():
    application = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


def page_for(app, **overrides):
    with Session(app.extensions["db_engine"]) as s:
        user = add_user(s, 1, age=30, **overrides)
        s.refresh(user)
        s.expunge(user)
    return logged_in_client(app, user).get("/account")


def state_of(response) -> dict:
    match = STATE_PATTERN.search(response.get_data(as_text=True))
    assert match, "頁面沒有初始狀態區塊"
    return json.loads(match.group(1))


# ---- 一般會員 ---------------------------------------------------------------------

def test_a_regular_member_sees_the_export_and_delete_sections_and_the_confirmation_text_comes_from_the_server(app):
    page = page_for(app)
    html = page.get_data(as_text=True)
    assert page.status_code == 200 and '<div id="app" v-cloak>' in html
    for needle in ("匯出我的資料", "下載 JSON", "下載 CSV（zip）", "刪除帳號", 'v-model="deleteOpen"', 'id="delete_password"', 'id="delete_confirm_text"'):
        assert needle in html, needle
    assert state_of(page)["deletion"] == {"confirmText": CONFIRM_TEXT} == {"confirmText": "刪除我的帳號"}
    assert "cancelDeletion" not in html                                # 撤銷按鈕只出現在等待刪除者的頁面


def test_the_delete_modal_explains_the_grace_period_the_scope_and_suggests_exporting_first(app):
    html = page_for(app).get_data(as_text=True)
    modal = html.split('v-model="deleteOpen"', 1)[1]
    for needle in ("7 天", "寬限期", "永久刪除", "無法復原", "匯出我的資料", "所有裝置都會登出"):
        assert needle in modal, needle


def test_the_scripts_load_the_pure_functions_before_the_page_script_and_only_from_the_site(app):
    html = page_for(app).get_data(as_text=True)
    lifecycle = html.index("js/auth/account-lifecycle.js")
    assert lifecycle < html.index("js/auth/account.js")
    assert "v-html" not in html


def test_the_account_state_still_never_holds_the_birthday(app):
    page = page_for(app)
    assert birth_date_for_age(30).isoformat() not in page.get_data(as_text=True)
    assert "deletion_scheduled_at" not in state_of(page)["user"]


# ---- 管理員 -----------------------------------------------------------------------

def test_an_admin_page_has_no_delete_section_in_the_html_at_all(app):
    page = page_for(app, role="admin")
    html = page.get_data(as_text=True)
    assert "匯出我的資料" in html and "變更密碼" in html
    for absent in ("刪除帳號", "delete_password", "delete_confirm_text", "deleteOpen", "account-danger"):
        assert absent not in html, absent


# ---- 等待刪除 ---------------------------------------------------------------------

def test_a_member_awaiting_deletion_sees_only_the_scheduled_time_cancel_and_logout(app):
    page = page_for(app, status=STATUS_DELETION_PENDING, deletion_requested_at=START, status_before_deletion="active")
    html = page.get_data(as_text=True)
    assert page.status_code == 200 and '<div id="app" v-cloak>' in html
    for needle in ("帳號正在等待刪除", "撤銷刪除", "scheduledText", "登出"):
        assert needle in html, needle
    for absent in NORMAL_ONLY + ("刪除帳號", "account_birth_date", "下載 JSON", "delete_password"):
        assert absent not in html, absent
    state = state_of(page)
    assert state["user"]["status"] == "deletion_pending" and state["user"]["deletion_scheduled_at"] == "2026-11-03T12:00:00Z"
    assert birth_date_for_age(30).isoformat() not in html


def test_once_the_grace_period_is_over_the_page_explains_it_and_offers_no_cancel_button(app):
    page = page_for(app, status=STATUS_DELETION_PENDING, deletion_requested_at=START - timedelta(days=7), status_before_deletion="active")
    html = page.get_data(as_text=True)
    assert "帳號即將刪除" in html and "已過" in html and "無法再撤銷" in html
    assert "cancelDeletion" not in html and "撤銷刪除" not in html and "登出" in html           # 只剩登出


def test_a_page_opened_just_inside_the_grace_period_still_has_the_cancel_button(app):
    page = page_for(app, status=STATUS_DELETION_PENDING, deletion_requested_at=START - timedelta(days=7) + timedelta(seconds=1), status_before_deletion="active")
    html = page.get_data(as_text=True)
    assert "cancelDeletion" in html and "帳號正在等待刪除" in html and "帳號即將刪除" not in html


# ---- 腳本 -------------------------------------------------------------------------

def test_the_account_script_validates_before_sending_and_always_restores_the_ui():
    script = (JS_DIR / "account.js").read_text(encoding="utf-8")
    delete_body = script.split("function deleteAccount()", 1)[1].split("function cancelDeletion", 1)[0]
    assert delete_body.index("lifecycle.canSubmitDeletion(options)") < delete_body.index("axios.post('/api/account/delete'")
    assert "return Promise.resolve(false)" in delete_body and "deleteForm.values.password = ''" in delete_body
    assert "leave('/login?notice=deletion_requested')" in delete_body

    export_body = script.split("function downloadExport(format)", 1)[1].split("window.addEventListener('pagehide'", 1)[0]
    assert "responseType: 'blob'" in export_body and "lifecycle.EXPORT_TIMEOUT_MS" in export_body
    assert "AbortController" in export_body and "lifecycle.unwrapBlobError" in export_body
    assert ".finally(" in export_body and "exportingFormat.value = ''" in export_body       # 無論成功、失敗或中斷都恢復
    assert "exportController.abort()" in script and "'pagehide'" in script                  # 離開頁面時中止
    assert "v-html" not in script


def test_the_pure_script_hardcodes_no_second_copy_of_the_confirmation_text():
    assert CONFIRM_TEXT not in (JS_DIR / "account-lifecycle.js").read_text(encoding="utf-8")
    assert CONFIRM_TEXT not in (JS_DIR / "account.js").read_text(encoding="utf-8")


# ---- 登入頁的寬限期說明 ----------------------------------------------------------------

def test_the_login_page_explains_the_grace_period_only_for_the_known_notice_code(app):
    client = app.test_client()
    shown = state_of(client.get("/login?notice=deletion_requested"))
    assert "7 天" in shown["info"] and "撤銷" in shown["info"] and shown["notice"] is None
    for query in ("", "?notice=unknown", "?notice=<script>alert(1)</script>"):
        response = client.get("/login" + query)
        assert state_of(response)["info"] is None
        assert "alert(1)" not in response.get_data(as_text=True)
    assert 'v-if="info"' in client.get("/login").get_data(as_text=True)


# ---- 故事偏好（story-preference 規格的「帳號頁的故事偏好設定」）----------------------------------------------------

def story_block(html: str) -> str:
    start = html.index('aria-labelledby="story-title"')
    return html[start:html.index('aria-labelledby="security-title"', start)]


def test_the_story_preference_block_has_three_radios_a_hint_and_a_save_button_after_the_nickname_section(app):
    html = page_for(app).get_data(as_text=True)
    assert html.index('aria-labelledby="nickname-title"') < html.index('aria-labelledby="story-title"') < html.index('aria-labelledby="security-title"')
    block = story_block(html)
    radios = re.findall(r"<input\b[^>]*>", block)
    assert [re.search(r'value="(\w+)"', tag).group(1) for tag in radios] == ["all", "male", "female"]
    assert all('type="radio"' in tag and 'v-model="storyForm.values.story_preference"' in tag for tag in radios)
    for needle in ("故事偏好", "全讀者", "男頻", "女頻", "儲存偏好", "只會影響劇本清單的排序", "不會限制你可以看或可以玩的劇本"):
        assert needle in block, needle
    assert '@submit.prevent="saveStoryPreference"' in block
    assert ':disabled="storyForm.state.submitting"' in block and "storyForm.state.submitting ? 'true' : 'false'" in block     # 忙碌時禁用並標示
    assert 'v-if="storyForm.fieldErrors.story_preference"' in block                                                           # 欄位錯誤顯示在欄位旁


def test_the_story_preference_block_is_shown_only_once_the_age_is_confirmed_like_the_nickname_block(app):
    html = page_for(app).get_data(as_text=True)
    assert re.search(r'<section v-if="ageConfirmed" class="account-section" aria-labelledby="story-title">', html)


@pytest.mark.parametrize("value", ["all", "male", "female"])
def test_the_initial_state_carries_the_current_preference_and_still_has_no_birthday_or_age_range(app, value):
    page = page_for(app, story_preference=value)
    user = state_of(page)["user"]
    assert user["story_preference"] == value                                                  # 前端據此選取對應的單選
    assert not {"birth_date", "age_range", "age_confirmed_at"} & set(user)                    # 生日與年齡區間不進頁面狀態
    assert birth_date_for_age(30).isoformat() not in page.get_data(as_text=True)


def test_a_member_without_a_stored_choice_gets_all_readers_in_the_initial_state(app):
    assert state_of(page_for(app))["user"]["story_preference"] == "all"                       # 預設值（新帳號與遷移前的帳號）


def test_a_member_awaiting_deletion_has_no_story_preference_block_at_all(app):
    page = page_for(app, status=STATUS_DELETION_PENDING, deletion_requested_at=START, status_before_deletion="active")
    html = page.get_data(as_text=True)
    for absent in ("story-title", "故事偏好", "saveStoryPreference", "storyForm", 'name="story_preference"', 'type="radio"'):
        assert absent not in html, absent                         # 初始狀態 JSON 裡的使用者資料有 story_preference 鍵，那是狀態，不是區塊


def test_the_account_script_saves_the_preference_with_put_through_the_form_helper_and_keeps_the_selection_on_failure():
    script = (JS_DIR / "account.js").read_text(encoding="utf-8-sig")
    body = script.split("function saveStoryPreference()", 1)[1].split("// ---- 匯出我的資料", 1)[0]
    assert "storyForm.run(" in body                                                         # 送出中禁用、finally 恢復都在 Auth.useForm
    assert "axios.put('/api/account/story-preference', { story_preference: storyForm.values.story_preference })" in body
    assert "UI.toast('故事偏好已儲存。')" in body and "{ type: 'error' }" in body
    on_error = body.split("onError: function (info)", 1)[1]
    assert "storyForm.values" not in on_error                                               # 失敗時不改動玩家目前的選取
    assert "localStorage" not in script and "v-html" not in script


def test_account_page_loads_every_script_that_account_js_depends_on(app):
    """帳號頁腳本用到的每個 `Auth.xxx` 都必須由同一頁載入的其他腳本提供。

    曾經漏載 `auth-flows.js`，暱稱儲存時 `Auth.validateNickname is not a function`，按鈕沒有任何反應；
    Node 測試只測純函式檔案，不會發現頁面漏載，所以由這裡以頁面實際載入的腳本清單檢查。
    """
    html = page_for(app).get_data(as_text=True)
    sources = re.findall(r'<script[^>]+src="/static/(js/[^"?]+)', html)
    assert "js/auth/auth-flows.js" in sources and "js/auth/account.js" in sources
    assert sources.index("js/auth/auth-flows.js") < sources.index("js/auth/account.js")      # 先載入被依賴的檔案

    used = set(re.findall(r"\bAuth\.([A-Za-z]+)", (JS_DIR / "account.js").read_text(encoding="utf-8-sig")))
    provided = "\n".join(
        (PROJECT_ROOT / "app" / "static" / source).read_text(encoding="utf-8-sig")
        for source in sources if source != "js/auth/account.js"
    )
    missing = sorted(name for name in used if not re.search(rf"\b{name}\b\s*[:=(]", provided))
    assert not missing, f"account.js 用到但頁面沒有載入的 Auth 函式：{missing}"
