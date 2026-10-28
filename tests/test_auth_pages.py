"""auth-pages 與 member-registration 規格：會員頁面的存取規則、初始狀態、轉址安全、條款頁。"""
import json
import re
from html.parser import HTMLParser

import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base
from app.services.legal import CURRENT_TERMS_VERSION
from tests.architecture.test_frontend_rules import own_asset_violations
from tests.support.auth import GOOD_PASSWORD, add_user
from tests.support.config_env import AUTH_ENV
from tests.support.email_flow import FakeSender
from tests.support.google import FakeProvider

STATE_PATTERN = re.compile(r'<script type="application/json" id="initial-state">(.*?)</script>', re.S)
PAGES = ("/login", "/register", "/forgot-password", "/reset-password", "/verify-email", "/terms", "/privacy")


def make_app(*, verification=False, google=False):
    environ = {"APP_ENV": "testing"}
    if verification:
        environ.update({"EMAIL_VERIFICATION_REQUIRED": "true", **AUTH_ENV, "BASE_URL": "http://localhost:5000"})
    app = create_app(
        load_config(environ=environ),
        mail_sender=FakeSender() if verification else None,
        identity_provider=FakeProvider() if google else None,
    )
    Base.metadata.create_all(app.extensions["db_engine"])
    return app


def state_of(response) -> dict:
    match = STATE_PATTERN.search(response.get_data(as_text=True))
    assert match, "頁面沒有初始狀態區塊"
    return json.loads(match.group(1))


def login(client, email="user1@example.com", password=GOOD_PASSWORD):
    return client.post("/api/auth/login", json={"email": email, "password": password})


@pytest.fixture()
def app():
    return make_app()


@pytest.fixture()
def full_app():
    return make_app(verification=True, google=True)


def logged_in(app, **user_overrides):
    with Session(app.extensions["db_engine"]) as s:
        add_user(s, 1, **user_overrides)
    client = app.test_client()
    assert login(client).status_code == 200
    return client


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.refs, self.data_bs, self.root_attrs, self.meta = [], [], {}, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "html":
            self.root_attrs = attrs
        if tag == "meta":
            self.meta.append(attrs)
        for name in ("src", "href"):
            if attrs.get(name):
                self.refs.append((tag, attrs[name]))
        self.data_bs += [(k, v) for k, v in attrs.items() if k.startswith("data-bs-")]


# =============================================================================
# 所有頁面：共通規則
# =============================================================================

@pytest.mark.parametrize("path", PAGES)
def test_every_page_is_html_with_the_required_shell_and_loads_only_local_resources(full_app, path):
    response = full_app.test_client().get(path)
    assert response.status_code == 200 and response.mimetype == "text/html"
    page = _Collector()
    page.feed(response.get_data(as_text=True))
    assert page.root_attrs.get("lang") == "zh-Hant-TW" and page.root_attrs.get("data-bs-theme") == "dark"
    assert any(m.get("charset") == "UTF-8" for m in page.meta)
    assert any(m.get("name") == "csrf-token" and m.get("content") for m in page.meta)
    external = [ref for _, ref in page.refs if re.match(r"(https?:)?//", ref)]
    assert external == []
    assert page.data_bs == [("data-bs-theme", "dark")]
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("path", PAGES)
def test_every_local_script_and_stylesheet_a_page_references_really_exists(full_app, path):
    client = full_app.test_client()
    page = _Collector()
    page.feed(client.get(path).get_data(as_text=True))
    assets = [ref for tag, ref in page.refs if ref.startswith("/static/") and tag in ("script", "link")]
    assert assets, "頁面沒有引用任何本站資源"
    for ref in assets:
        assert client.get(ref).status_code == 200, ref


def test_vue_templates_are_not_processed_by_jinja(full_app):
    html = full_app.test_client().get("/login").get_data(as_text=True)
    assert "{{ form.state.submitting" in html and 'id="app"' in html           # Vue 的插值原樣送到瀏覽器


@pytest.mark.parametrize("path", ["/login", "/register", "/forgot-password", "/reset-password", "/verify-email", "/terms", "/privacy"])
def test_no_jinja_syntax_leaks_into_any_page(full_app, path):
    """{% raw %} 區塊內的 Jinja 語法不會被處理：任何頁面都不能把 {# #} 或 {% %} 原樣送給使用者。"""
    html = full_app.test_client().get(path).get_data(as_text=True)
    assert "{#" not in html and "#}" not in html and "{%" not in html and "%}" not in html


# =============================================================================
# 登入與註冊頁
# =============================================================================

def test_login_page_state_has_a_validated_next_and_the_feature_flags(full_app):
    state = state_of(full_app.test_client().get("/login?next=/account%3Ftab%3D1"))
    assert state["next"] == "/account?tab=1"
    assert state["googleEnabled"] is True and state["verificationRequired"] is True
    assert state["termsVersion"] == CURRENT_TERMS_VERSION and state["notice"] is None


def test_the_flags_follow_the_configuration(app):
    state = state_of(app.test_client().get("/login"))
    assert state["googleEnabled"] is False and state["verificationRequired"] is False


@pytest.mark.parametrize("bad", ["https://evil.example/", "//evil.example", "javascript:alert(1)", "/\\evil", "/%2f/evil"])
def test_an_unsafe_next_never_reaches_the_page_state(app, bad):
    for path in ("/login", "/register"):
        assert state_of(app.test_client().get(f"{path}?next={bad}"))["next"] == "/account"


@pytest.mark.parametrize(
    ("code", "fragment"),
    [("google_state", "Google 登入的驗證"), ("google_unavailable", "暫時無法使用"), ("google_rejected", "沒有完成"),
     ("terms_required", "勾選同意"), ("age_required", "請先確認您已年滿 18 歲。"), ("account_disabled", "已停用")],
)
def test_known_error_codes_become_chinese_notices(full_app, code, fragment):
    notice = state_of(full_app.test_client().get(f"/login?error={code}"))["notice"]
    assert fragment in notice


def test_the_age_notice_is_distinct_from_the_terms_notice(full_app):
    """年齡與條款是各自獨立的確認：缺哪一項就顯示哪一項的說明。"""
    notices = {code: state_of(full_app.test_client().get(f"/login?error={code}"))["notice"] for code in ("age_required", "terms_required")}
    assert notices["age_required"] != notices["terms_required"]
    assert "條款" not in notices["age_required"] and "18" not in notices["terms_required"]


@pytest.mark.parametrize("attack", ["<script>alert(1)</script>", "google_state%22%3E%3Cimg%20src=x%3E", "nope", "age_required%22%3E", "AGE_REQUIRED", ""])
def test_unknown_error_values_are_ignored_and_never_echoed(app, attack):
    response = app.test_client().get(f"/login?error={attack}")
    assert state_of(response)["notice"] is None
    assert "alert(1)" not in response.get_data(as_text=True) and "<img src=x" not in response.get_data(as_text=True)


@pytest.mark.parametrize("path", ["/login", "/register"])
def test_a_logged_in_member_is_sent_to_the_account_page(app, path):
    client = logged_in(app)
    response = client.get(path)
    assert response.status_code == 302 and response.headers["Location"] == "/account"


def test_the_register_page_links_to_the_terms_and_the_privacy_policy(app):
    html = app.test_client().get("/register").get_data(as_text=True)
    assert 'href="/terms"' in html and 'href="/privacy"' in html
    assert 'type="checkbox"' in html and 'autocomplete="new-password"' in html and 'autocomplete="email"' in html


def test_the_register_page_has_a_separate_adult_checkbox_with_its_own_error_slot(app):
    html = app.test_client().get("/register").get_data(as_text=True)
    assert 'id="accept_terms"' in html and 'id="confirm_adult"' in html          # 兩個各自獨立的勾選
    assert 'for="confirm_adult"' in html and "我已年滿 18 歲" in html
    assert 'v-model="form.values.confirm_adult"' in html and "form.fieldErrors.confirm_adult" in html
    terms_label = html.split('for="accept_terms"', 1)[1].split("</label>", 1)[0]
    assert "18" not in terms_label                                                 # 年齡不是併在條款那一個勾選裡
    assert "google.fieldErrors.confirm_adult" in html                             # Google 入口缺年齡時的提示位置


def test_the_register_script_sends_both_confirmations_and_checks_both_before_leaving_for_google():
    from app.config import PROJECT_ROOT

    script = (PROJECT_ROOT / "app" / "static" / "js" / "auth" / "register.js").read_text(encoding="utf-8")
    assert "confirm_adult: form.values.confirm_adult" in script
    assert "accept_terms: true, confirm_adult: true" in script
    assert "請先確認您已年滿 18 歲。" in script and "請先勾選同意服務條款與隱私權政策。" in script


def test_the_login_page_google_entry_has_both_checkboxes_and_the_script_checks_both(full_app):
    from app.config import PROJECT_ROOT

    html = full_app.test_client().get("/login").get_data(as_text=True)
    assert 'id="google-terms"' in html and 'id="google-adult"' in html
    assert 'v-model="google.values.confirmAdult"' in html and "google.fieldErrors.confirm_adult" in html
    script = (PROJECT_ROOT / "app" / "static" / "js" / "auth" / "login.js").read_text(encoding="utf-8")
    assert "google.values.acceptTerms" in script and "google.values.confirmAdult" in script
    assert "accept_terms: true, confirm_adult: true" in script
    assert "請先確認您已年滿 18 歲。" in script


def test_the_account_page_shows_the_age_row_and_the_confirmation_form_for_both_states(full_app):
    """頁面外殼同時帶有兩種狀態的內容，由 `ageConfirmed` 決定顯示哪一個；初始值取自伺服器的快照。"""
    client = logged_in(full_app)
    page = client.get("/account")
    html = page.get_data(as_text=True)
    assert "<dt>年齡確認</dt>" in html and "已確認年滿 18 歲" in html and "尚未確認" in html
    assert 'v-if="!ageConfirmed"' in html and 'id="account_confirm_adult"' in html
    assert "不蒐集生日、年齡或證件" in html
    assert state_of(page)["user"]["age_confirmed"] is False                         # 這位測試使用者沒有確認紀錄

    assert client.post("/api/auth/confirm-adult", json={"confirm_adult": True}).status_code == 200
    assert state_of(client.get("/account"))["user"]["age_confirmed"] is True


def test_the_account_script_skips_the_request_when_the_box_is_unchecked_and_reports_success_with_a_toast():
    from app.config import PROJECT_ROOT

    script = (PROJECT_ROOT / "app" / "static" / "js" / "auth" / "account.js").read_text(encoding="utf-8")
    body = script.split("function confirmAdult()", 1)[1].split("function resetPasswordForm", 1)[0]
    assert body.index("if (!ageForm.values.confirm_adult)") < body.index("axios.post('/api/auth/confirm-adult'")
    assert "return Promise.resolve(false)" in body                                    # 沒勾選：不送出請求
    assert "UI.toast(" in body and "ageConfirmed.value = true" in body


@pytest.mark.parametrize("path", ["/login", "/register", "/forgot-password", "/reset-password", "/verify-email"])
def test_every_vue_mount_point_is_cloaked_until_vue_has_mounted(full_app, path):
    """Vue 掛載之前，頁面內的範本（{{ }}）不能被使用者看見。"""
    assert '<div id="app" v-cloak>' in full_app.test_client().get(path).get_data(as_text=True)


def test_the_account_page_is_cloaked_too_and_the_css_hides_cloaked_elements(full_app):
    from app.config import PROJECT_ROOT

    client = logged_in(full_app)
    assert '<div id="app" v-cloak>' in client.get("/account").get_data(as_text=True)
    css = (PROJECT_ROOT / "app" / "static" / "css" / "auth.css").read_text(encoding="utf-8")
    assert "[v-cloak]" in css and "display: none" in css


def test_the_change_password_form_can_be_submitted_with_enter(full_app):
    """表單有兩個欄位時，必須有一顆（隱藏的）送出按鈕，按 Enter 才會送出；且說明文字不能以原樣出現在畫面上。"""
    html = logged_in(full_app).get("/account").get_data(as_text=True)
    form = html.split('<form @submit.prevent="changePassword"', 1)[1].split("</form>", 1)[0]
    assert '<button type="submit" hidden' in form
    assert "{#" not in html and "#}" not in html            # raw 區塊內的 Jinja 註解不會被處理，不能留在輸出裡


def test_the_login_page_has_autofill_attributes_and_labels(app):
    html = app.test_client().get("/login").get_data(as_text=True)
    assert 'autocomplete="current-password"' in html and 'for="email"' in html and 'for="password"' in html


def test_forms_restore_their_state_in_a_finally_block_and_never_use_unsafe_dom_apis():
    from app.config import PROJECT_ROOT

    form_js = (PROJECT_ROOT / "app" / "static" / "js" / "auth" / "auth-form.js").read_text(encoding="utf-8")
    assert ".finally(" in form_js and "state.submitting = false" in form_js
    scripts = list((PROJECT_ROOT / "app" / "static" / "js" / "auth").glob("*.js"))
    assert len(scripts) >= 8
    for script in scripts:          # 使用專案既有的掃描規則（會先去除註解，說明文字可以提到被禁用的寫法）
        assert own_asset_violations(script.read_text(encoding="utf-8")) == [], script.name


# =============================================================================
# 信箱驗證關閉時：相關頁面 404；開啟時存在，且不含權杖
# =============================================================================

@pytest.mark.parametrize("path", ["/forgot-password", "/reset-password", "/verify-email"])
def test_email_flow_pages_are_404_when_verification_is_off_and_never_linked(app, path):
    response = app.test_client().get(path)
    assert response.status_code == 404 and "找不到" in response.get_data(as_text=True)


def test_the_forgot_password_entry_on_the_login_page_is_controlled_by_the_flag(app, full_app):
    """登入頁的「忘記密碼」連結由旗標控制：驗證關閉時旗標為 false，Vue 不會渲染它。"""
    assert 'v-if="verificationRequired" href="/forgot-password"' in app.test_client().get("/login").get_data(as_text=True)
    assert state_of(app.test_client().get("/login"))["verificationRequired"] is False
    assert state_of(full_app.test_client().get("/login"))["verificationRequired"] is True


@pytest.mark.parametrize("path", ["/forgot-password", "/reset-password", "/verify-email"])
def test_email_flow_pages_exist_when_verification_is_on(full_app, path):
    assert full_app.test_client().get(path).status_code == 200


@pytest.mark.parametrize("path", ["/reset-password", "/verify-email"])
def test_the_confirmation_pages_never_contain_a_token_even_if_one_is_sent_to_the_server(full_app, path):
    """權杖在網址片段，伺服器根本收不到；就算有人把它放進查詢字串，頁面也不會回顯。"""
    response = full_app.test_client().get(f"{path}?token=SECRET-TOKEN-VALUE-123")
    assert "SECRET-TOKEN-VALUE-123" not in response.get_data(as_text=True)
    assert "token" not in json.dumps(state_of(response)).lower()


def test_the_confirmation_pages_do_not_submit_anything_on_load():
    from app.config import PROJECT_ROOT

    for name in ("verify-email.js", "reset-password.js"):
        text = (PROJECT_ROOT / "app" / "static" / "js" / "auth" / name).read_text(encoding="utf-8")
        assert "readFragmentToken(window)" in text                      # 權杖來自片段，讀取後即移除
        body_before_setup = text.split("setup:")[0]
        assert "axios.post" not in body_before_setup                    # 只有在使用者按下按鈕的函式裡才送出


# =============================================================================
# 帳號設定頁
# =============================================================================

def test_the_account_page_requires_a_login_and_keeps_the_destination(app):
    response = app.test_client().get("/account")
    assert response.status_code == 302 and response.headers["Location"] == "/login?next=%2Faccount"


def test_the_account_page_state_holds_the_members_own_profile_only(app):
    client = logged_in(app, member_tier="plus")
    state = state_of(client.get("/account"))
    user = state["user"]
    assert (user["email"], user["display_name"], user["member_tier"]) == ("user1@example.com", "使用者1", "plus")
    assert set(user) == {
        "public_id", "email", "display_name", "member_tier", "role", "status", "email_verified", "has_password",
        "age_confirmed", "google_linked",
    }
    assert user["age_confirmed"] is False
    assert "password_hash" not in json.dumps(state) and "token_version" not in json.dumps(state)


def test_the_tier_labels_are_chinese_and_the_page_uses_them():
    from app.config import PROJECT_ROOT

    script = (PROJECT_ROOT / "app" / "static" / "js" / "auth" / "account.js").read_text(encoding="utf-8")
    assert "free: '一般'" in script and "plus: '進階'" in script and "vip: '尊榮'" in script
    template = (PROJECT_ROOT / "app" / "templates" / "auth" / "account.html").read_text(encoding="utf-8")
    assert "{{ tierLabel }}" in template


def test_the_account_page_shows_the_verification_prompt_for_an_unverified_member_only_when_verification_is_on(full_app):
    client = logged_in(full_app)
    state = state_of(client.get("/account"))
    assert state["verificationRequired"] is True and state["user"]["email_verified"] is False
    html = client.get("/account").get_data(as_text=True)
    assert "重新寄送驗證信" in html


def test_dangerous_actions_are_confirmed_with_the_custom_modal_and_use_no_native_dialogs():
    from app.config import PROJECT_ROOT

    template = (PROJECT_ROOT / "app" / "templates" / "auth" / "account.html").read_text(encoding="utf-8")
    assert '<ui-modal v-model="logoutAllOpen"' in template and '<ui-modal v-model="changePasswordOpen"' in template
    script = (PROJECT_ROOT / "app" / "static" / "js" / "auth" / "account.js").read_text(encoding="utf-8")
    assert "window.confirm" not in script and "alert(" not in script


def test_user_supplied_text_cannot_break_out_of_the_state_block(app):
    hostile = '</script><script>alert("xss")</script>&"\''
    with Session(app.extensions["db_engine"]) as s:
        add_user(s, 1, display_name=hostile[:50])
    client = app.test_client()
    login(client)
    response = client.get("/account")
    html = response.get_data(as_text=True)
    assert '</script><script>alert("xss")' not in html
    assert state_of(response)["user"]["display_name"] == hostile[:50]         # 解析後仍是原本的文字


# =============================================================================
# 服務條款與隱私權政策
# =============================================================================

@pytest.mark.parametrize("path", ["/terms", "/privacy"])
def test_the_legal_pages_are_public_chinese_and_clearly_marked_as_drafts(app, path):
    response = app.test_client().get(path)
    html = response.get_data(as_text=True)
    assert response.status_code == 200 and "草稿" in html and "尚待法務覆核" in html
    assert CURRENT_TERMS_VERSION in html


def test_the_privacy_policy_discloses_gemini_and_every_retention_period(app):
    html = app.test_client().get("/privacy").get_data(as_text=True)
    assert "Gemini" in html and "傳送至 Google" in html
    for period in ("12 個月", "14 天", "30 天", "24 個月", "7 天"):
        assert period in html, f"隱私權政策缺少保存期限：{period}"
    for right in ("查詢", "複製", "更正", "刪除"):
        assert right in html


def test_the_terms_describe_the_service_as_ai_only(app):
    html = app.test_client().get("/terms").get_data(as_text=True)
    assert "沒有真人客服" in html and "一組電子郵件只能對應一個帳號" in html


def test_the_terms_limit_the_service_to_adults_and_say_what_is_recorded(app):
    response = app.test_client().get("/terms")
    html = response.get_data(as_text=True)
    assert "僅供年滿 18 歲者使用" in html and "另外勾選確認" in html
    assert "不蒐集生日、年齡或證件" in html and "帳號設定" in html
    assert "草稿" in html and CURRENT_TERMS_VERSION in html                          # 仍是草稿，版本照舊顯示
    headings = re.findall(r"<h2>(\d+)\. ", html)
    assert headings == [str(n) for n in range(1, len(headings) + 1)]                # 新增一節後編號仍連續


def test_the_privacy_policy_lists_the_age_record_and_its_retention_without_claiming_to_collect_more(app):
    html = app.test_client().get("/privacy").get_data(as_text=True)
    assert "年齡確認紀錄" in html and "不蒐集生日、年齡或證件" in html
    retention = html.split("<table>", 1)[1].split("</table>", 1)[0]
    assert "年齡確認紀錄" in retention and "帳號刪除時一併刪除" in retention
    assert "草稿" in html and CURRENT_TERMS_VERSION in html
    headings = re.findall(r"<h2>(\d+)\. ", html)
    assert headings == [str(n) for n in range(1, len(headings) + 1)]
