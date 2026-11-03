"""auth-pages 與 member-registration 規格：會員頁面的存取規則、初始狀態、轉址安全、條款頁。"""
import json
import re
from html.parser import HTMLParser

import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base, User
from app.security.redirects import DEFAULT_AFTER_LOGIN
from app.services.age_policy import DEFAULT_AGE_POLICY, AgePolicy
from app.services.legal import CURRENT_TERMS_VERSION
from tests.architecture.test_frontend_rules import own_asset_violations
from tests.support.age import birth_date_for_age
from tests.support.auth import GOOD_PASSWORD, FakeClock, add_user, sign_in_as
from tests.support.config_env import AUTH_ENV
from tests.support.email_flow import FakeSender
from tests.support.google import FakeProvider
from tests.support.idor import logged_in_client

STATE_PATTERN = re.compile(r'<script type="application/json" id="initial-state">(.*?)</script>', re.S)
PAGES = ("/login", "/reset-password", "/terms", "/privacy")          # /reset-password 需要寄信設定（full_app 有）


def make_app(*, mail=False, google=False, age_policy: AgePolicy | None = None):
    """`mail=True`：有寄信設定（管理員的重設密碼頁才存在）。"""
    environ = {"APP_ENV": "testing"}
    if mail:
        environ.update({"ALERT_EMAIL_TO": "ops@example.com", **AUTH_ENV, "BASE_URL": "http://localhost:5000"})
    app = create_app(
        load_config(environ=environ),
        mail_sender=FakeSender() if mail else None,
        identity_provider=FakeProvider() if google else None,
        clock=FakeClock(),                     # 固定的「今天」：生日欄位的範圍與年齡都不受真實日期影響
        age_policy=age_policy,
    )
    Base.metadata.create_all(app.extensions["db_engine"])
    return app


def state_of(response) -> dict:
    match = STATE_PATTERN.search(response.get_data(as_text=True))
    assert match, "頁面沒有初始狀態區塊"
    return json.loads(match.group(1))


def login(client, email="user1@example.com"):
    """代表「通過 Google 登入」：以該信箱目前的 (uid, token_version) 寫入登入 cookie（玩家沒有密碼登入）。"""
    with Session(client.application.extensions["db_engine"]) as s:
        user = s.query(User).filter(User.email == email).one()
        sign_in_as(client, user.id, user.token_version)


@pytest.fixture()
def app():
    return make_app()


@pytest.fixture()
def full_app():
    return make_app(mail=True, google=True)


def logged_in(app, **user_overrides):
    with Session(app.extensions["db_engine"]) as s:
        add_user(s, 1, **user_overrides)
    client = app.test_client()
    login(client)
    return client


def logged_in_admin_without_age(app, **user_overrides):
    """尚未補填年齡的管理員（玩家沒補填就不能開帳號頁，會被導向年齡確認頁；補填區塊只留給管理員）。"""
    with Session(app.extensions["db_engine"]) as s:
        return logged_in_client(app, add_user(s, 1, age=None, role="admin", **user_overrides))


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
    assert "{{ google.state.submitting" in html and 'id="app"' in html         # Vue 的插值原樣送到瀏覽器


@pytest.mark.parametrize("path", PAGES)
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
    assert state["googleEnabled"] is True and "verificationRequired" not in state
    assert state["termsVersion"] == CURRENT_TERMS_VERSION and state["notice"] is None


def test_the_flags_follow_the_configuration(app):
    state = state_of(app.test_client().get("/login"))
    assert state["googleEnabled"] is False and "verificationRequired" not in state


@pytest.mark.parametrize("bad", ["https://evil.example/", "//evil.example", "javascript:alert(1)", "/\\evil", "/%2f/evil"])
def test_an_unsafe_next_never_reaches_the_page_state(app, bad):
    assert state_of(app.test_client().get(f"/login?next={bad}"))["next"] == DEFAULT_AFTER_LOGIN


@pytest.mark.parametrize(
    ("code", "fragment"),
    [("google_state", "Google 登入的驗證"), ("google_unavailable", "暫時無法使用"), ("google_rejected", "沒有完成"),
     ("terms_required", "勾選同意"), ("account_disabled", "已停用")],
)
def test_known_error_codes_become_chinese_notices(full_app, code, fragment):
    notice = state_of(full_app.test_client().get(f"/login?error={code}"))["notice"]
    assert fragment in notice


def test_the_retired_age_error_code_is_now_just_an_unknown_value(full_app):
    """年齡確認改在登入之後的年齡確認頁，登入頁不再有「缺少年齡」的說明：舊代碼和任何未知的值一樣被忽略。"""
    assert state_of(full_app.test_client().get("/login?error=age_required"))["notice"] is None


@pytest.mark.parametrize("attack", ["<script>alert(1)</script>", "google_state%22%3E%3Cimg%20src=x%3E", "nope", "age_required%22%3E", "AGE_REQUIRED", ""])
def test_unknown_error_values_are_ignored_and_never_echoed(app, attack):
    response = app.test_client().get(f"/login?error={attack}")
    assert state_of(response)["notice"] is None
    assert "alert(1)" not in response.get_data(as_text=True) and "<img src=x" not in response.get_data(as_text=True)


def test_a_logged_in_member_is_sent_to_the_default_page_from_the_login_page(app):
    client = logged_in(app)
    response = client.get("/login")
    assert response.status_code == 302 and response.headers["Location"] == DEFAULT_AFTER_LOGIN


def test_the_page_state_carries_the_age_rules_from_the_policy_and_the_dates_of_today(app):
    rules = state_of(app.test_client().get("/login"))["ageRules"]
    assert rules["minRegisterAge"] == 13 and rules["adultAge"] == 18
    assert [r["code"] for r in rules["ranges"]] == [r.code for r in DEFAULT_AGE_POLICY.ranges]
    assert all(set(r) == {"code", "label"} and r["label"] for r in rules["ranges"])
    assert rules["birthDateMin"] == "1900-01-01" and rules["birthDateMax"] == "2026-10-27"      # 假時鐘的台北日期


def test_a_custom_policy_changes_the_state_the_options_and_the_wording_without_touching_the_pages():
    strict = make_app(age_policy=AgePolicy(min_register_age=18, min_use_age=18, version="test"))
    rules = state_of(strict.test_client().get("/login"))["ageRules"]
    assert rules["minRegisterAge"] == 18
    terms = strict.test_client().get("/terms").get_data(as_text=True)
    assert "18 歲以上" in terms and "13 歲" not in terms and "至 17 歲" not in terms


def test_no_template_or_script_hardcodes_an_age_number():
    """年齡數字只能來自 AgePolicy：範本與會員頁面的腳本裡不得出現「13 歲」「18 歲」這類寫死的數字。"""
    from app.config import PROJECT_ROOT

    pattern = re.compile(r"(?<![\d{])(13|17|18)\s*(歲|至)")
    offenders = []
    files = list((PROJECT_ROOT / "app" / "templates").rglob("*.html")) + list((PROJECT_ROOT / "app" / "static" / "js" / "auth").glob("*.js"))
    for path in files:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.name}:{number}: {line.strip()[:60]}")
    assert offenders == [], offenders


def test_the_register_page_and_script_no_longer_exist():
    from app.config import PROJECT_ROOT

    assert not (PROJECT_ROOT / "app" / "templates" / "auth" / "register.html").exists()
    assert not (PROJECT_ROOT / "app" / "static" / "js" / "auth" / "register.js").exists()


def test_the_login_page_has_only_the_google_button_and_the_terms_checkbox(full_app):
    html = full_app.test_client().get("/login").get_data(as_text=True)
    # 登入與註冊合一：沒有電子郵件、密碼、顯示名稱、生日與年齡區間，也沒有註冊與忘記密碼的連結
    for forbidden in ('type="email"', 'type="password"', 'type="date"', "<select", 'id="birth', 'id="age_range"', 'for="email"',
                      'for="password"', 'href="/register"', 'href="/forgot-password"', "confirm_adult"):
        assert forbidden not in html, forbidden
    assert 'id="accept-terms"' in html and 'type="checkbox"' in html
    assert 'href="/terms"' in html and 'href="/privacy"' in html                       # 勾選框旁有條款與隱私權政策的連結
    assert "使用 Google 登入" in html and "第一次使用時必須勾選" in html


def test_the_google_button_stays_clickable_when_the_terms_are_not_ticked_and_only_the_flag_and_next_are_sent(full_app):
    from app.config import PROJECT_ROOT

    html = full_app.test_client().get("/login").get_data(as_text=True)
    button = html.split('@click="signInWithGoogle"', 1)[0].rsplit("<button", 1)[1]
    assert ":disabled=\"google.state.submitting\"" in button and "acceptTerms" not in button       # 只在送出中禁用，與勾選與否無關
    script = (PROJECT_ROOT / "app" / "static" / "js" / "auth" / "login.js").read_text(encoding="utf-8")
    assert "Auth.googleStartBody(google.values.acceptTerms, state.next)" in script
    assert "birth" not in script and "age_range" not in script and "password" not in script and "email" not in script
    flows = (PROJECT_ROOT / "app" / "static" / "js" / "auth" / "auth-flows.js").read_text(encoding="utf-8")
    body = flows.split("function googleStartBody", 1)[1].split("}", 1)[0]
    assert "accept_terms" in body and "next" in body and "birth" not in body


def test_the_account_page_shows_the_age_row_and_the_form_for_an_admin_without_age_data(full_app):
    client = logged_in_admin_without_age(full_app)                                    # 管理員由指令建立，沒有年齡資料
    page = client.get("/account")
    html = page.get_data(as_text=True)
    assert "<dt>年齡確認</dt>" in html and "尚未完成" in html and "已完成" in html
    assert 'v-if="isAdmin && !ageConfirmed"' in html and 'id="account_birth_date"' in html and 'id="account_age_range"' in html
    assert "confirm_adult" not in html and "我已年滿" not in html
    user = state_of(page)["user"]
    assert user["age_confirmed"] is False and user["age_group"] is None


@pytest.mark.parametrize(("age", "group"), [(15, "minor"), (30, "adult")])
def test_the_account_state_and_page_never_hold_the_birthday_or_the_range(full_app, age, group):
    client = logged_in(full_app, age=age)
    page = client.get("/account")
    user = state_of(page)["user"]
    assert user["age_confirmed"] is True and user["age_group"] == group
    assert not {"birth_date", "age_range", "age_confirmed_at", "age_policy_version", "is_adult_at_confirmation"} & set(user)
    assert birth_date_for_age(age).isoformat() not in page.get_data(as_text=True)


def test_a_minor_sees_the_safe_mode_notice_block_driven_by_the_age_group(full_app):
    html = logged_in(full_app, age=15).get("/account").get_data(as_text=True)
    assert 'v-if="ageGroup === \'minor\'"' in html and "目前為安全模式" in html
    assert "{{ ageRules.adultAge }}" in html                                           # 數字來自年齡規則


def test_verifying_the_age_on_the_account_page_flow_updates_the_state(full_app):
    client = logged_in_admin_without_age(full_app)
    body = {"birth_date": birth_date_for_age(15).isoformat(), "age_range": "13_17"}
    assert client.post("/api/auth/verify-age", json=body).status_code == 200
    user = state_of(client.get("/account"))["user"]
    assert user["age_confirmed"] is True and user["age_group"] == "minor"


def test_the_account_script_skips_the_request_when_a_field_is_missing_and_updates_the_state_on_success():
    from app.config import PROJECT_ROOT

    script = (PROJECT_ROOT / "app" / "static" / "js" / "auth" / "account.js").read_text(encoding="utf-8")
    body = script.split("function verifyAge()", 1)[1].split("function resetPasswordForm", 1)[0]
    assert body.index("if (!ageForm.values.birth_date)") < body.index("axios.post('/api/auth/verify-age'")
    assert body.index("if (!ageForm.values.age_range)") < body.index("axios.post('/api/auth/verify-age'")
    assert "return Promise.resolve(false)" in body                                    # 沒填：不送出請求
    assert "UI.toast(" in body and "ageConfirmed.value = true" in body and "ageGroup.value" in body
    assert "age_mismatch" in body and "age_not_eligible" in body                       # 對不起來：清空欄位，可立刻重填
    assert "confirm-adult" not in script and "confirm_adult" not in script


def test_every_password_field_states_and_enforces_the_8_to_128_range(full_app):
    """AGENTS.md 第 10 節：8 至 128 字。頁面上的說明文字與瀏覽器的 minlength 都要跟著規則走。"""
    logged = logged_in(full_app)
    pages = {
        "/reset-password": full_app.test_client().get("/reset-password"),
        "/account": logged.get("/account"),
    }
    for path, response in pages.items():
        html = response.get_data(as_text=True)
        assert "8 至 128 字" in html and 'minlength="8"' in html and 'maxlength="128"' in html, path
        assert "10 至 128" not in html and 'minlength="10"' not in html, path


@pytest.mark.parametrize("path", ["/login", "/reset-password"])
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


def test_the_admin_password_page_has_autofill_attributes_and_labels(app):
    from app.config import PROJECT_ROOT

    html = (PROJECT_ROOT / "app" / "templates" / "auth" / "login_password.html").read_text(encoding="utf-8")
    assert 'autocomplete="current-password"' in html and 'for="password"' in html and 'type="password"' in html


def test_forms_restore_their_state_in_a_finally_block_and_never_use_unsafe_dom_apis():
    from app.config import PROJECT_ROOT

    form_js = (PROJECT_ROOT / "app" / "static" / "js" / "auth" / "auth-form.js").read_text(encoding="utf-8")
    assert ".finally(" in form_js and "state.submitting = false" in form_js
    scripts = list((PROJECT_ROOT / "app" / "static" / "js" / "auth").glob("*.js"))
    assert len(scripts) >= 8
    for script in scripts:          # 使用專案既有的掃描規則（會先去除註解，說明文字可以提到被禁用的寫法）
        assert own_asset_violations(script.read_text(encoding="utf-8")) == [], script.name


# =============================================================================
# 玩家端的忘記密碼與信箱驗證頁已移除；管理員的重設密碼頁只在有寄信設定時存在，且不含權杖
# =============================================================================

@pytest.mark.parametrize("path", ["/forgot-password", "/verify-email"])
@pytest.mark.parametrize("which", ["app", "full_app"])
def test_the_removed_player_pages_are_404_whatever_the_configuration(request, which, path):
    response = request.getfixturevalue(which).test_client().get(path)
    assert response.status_code == 404 and "找不到" in response.get_data(as_text=True)


def test_the_reset_page_needs_the_mail_settings_and_the_login_page_has_no_forgot_password_link(app, full_app):
    assert app.test_client().get("/reset-password").status_code == 404
    assert full_app.test_client().get("/reset-password").status_code == 200
    for client in (app.test_client(), full_app.test_client()):
        html = client.get("/login").get_data(as_text=True)
        assert 'href="/forgot-password"' not in html and 'href="/verify-email"' not in html


def test_the_reset_page_never_contains_a_token_even_if_one_is_sent_to_the_server(full_app):
    """權杖在網址片段，伺服器根本收不到；就算有人把它放進查詢字串，頁面也不會回顯。"""
    response = full_app.test_client().get("/reset-password?token=SECRET-TOKEN-VALUE-123")
    assert "SECRET-TOKEN-VALUE-123" not in response.get_data(as_text=True)
    assert "token" not in json.dumps(state_of(response)).lower()


def test_the_reset_page_does_not_submit_anything_on_load():
    from app.config import PROJECT_ROOT

    text = (PROJECT_ROOT / "app" / "static" / "js" / "auth" / "reset-password.js").read_text(encoding="utf-8")
    assert "readFragmentToken(window)" in text                      # 權杖來自片段，讀取後即移除
    body_before_setup = text.split("setup:")[0]
    assert "axios.post" not in body_before_setup                    # 只有在使用者按下按鈕的函式裡才送出
    assert not (PROJECT_ROOT / "app" / "static" / "js" / "auth" / "verify-email.js").exists()
    assert not (PROJECT_ROOT / "app" / "static" / "js" / "auth" / "forgot-password.js").exists()


# =============================================================================
# 帳號設定頁
# =============================================================================

def test_the_account_page_requires_a_login_and_keeps_the_destination(app):
    response = app.test_client().get("/account")
    assert response.status_code == 302 and response.headers["Location"] == "/login?next=%2Faccount"


def test_the_account_page_state_holds_the_members_own_profile_only(app):
    client = logged_in_admin_without_age(app, member_tier="plus")
    state = state_of(client.get("/account"))
    user = state["user"]
    assert (user["email"], user["display_name"], user["member_tier"]) == ("user1@example.com", "使用者1", "plus")
    assert set(user) == {
        "public_id", "email", "display_name", "member_tier", "role", "status", "email_verified", "has_password",
        "age_confirmed", "age_group", "google_linked", "restriction",
    }
    assert user["restriction"] is None                                                # 管理員不會是待確認年齡的受限狀態
    assert user["age_confirmed"] is False and user["age_group"] is None
    assert "password_hash" not in json.dumps(state) and "token_version" not in json.dumps(state)


def test_the_tier_labels_are_chinese_and_the_page_uses_them():
    from app.config import PROJECT_ROOT

    script = (PROJECT_ROOT / "app" / "static" / "js" / "auth" / "account.js").read_text(encoding="utf-8")
    assert "free: '一般'" in script and "plus: '進階'" in script and "vip: '尊榮'" in script
    template = (PROJECT_ROOT / "app" / "templates" / "auth" / "account.html").read_text(encoding="utf-8")
    assert "{{ tierLabel }}" in template


def test_the_account_page_has_no_verification_prompt_or_resend_button_even_for_a_member_without_a_verification_time(full_app):
    client = logged_in(full_app)
    state = state_of(client.get("/account"))
    assert "verificationRequired" not in state and state["user"]["email_verified"] is False
    html = client.get("/account").get_data(as_text=True)
    assert "重新寄送驗證信" not in html and "resend" not in html and "尚未驗證，驗證完成前" not in html


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


def test_the_terms_set_the_minimum_age_and_the_teen_safe_mode_and_say_how_age_is_declared(app):
    html = app.test_client().get("/terms").get_data(as_text=True)
    assert "13 歲以上" in html and "未滿 13 歲者不得使用" in html
    assert "13 至 17 歲" in html and "只能使用安全模式" in html and "滿 18 歲之後會自動解除" in html
    assert "年齡區間" in html and "互相對照" in html and "自行聲明" in html and "年齡確認" in html
    assert "另外勾選確認" not in html and "僅供年滿 18 歲者使用" not in html            # 舊的勾選版說法已不存在
    assert "草稿" in html and CURRENT_TERMS_VERSION in html                          # 仍是草稿，版本照舊顯示
    headings = re.findall(r"<h2>(\d+)\. ", html)
    assert headings == [str(n) for n in range(1, len(headings) + 1)]                # 章節編號連續


def test_the_privacy_policy_discloses_the_birthday_its_purposes_and_its_retention(app):
    html = app.test_client().get("/privacy").get_data(as_text=True)
    assert "生日與年齡區間" in html and "不蒐集證件" in html
    assert "發現未滿 13 歲者" in html and "立即刪除" in html and "不會保存" in html
    assert "不蒐集生日、年齡或證件" not in html and "年齡確認紀錄" not in html           # 舊的說法已不存在
    for purpose in ("年齡確認", "內容分級", "去識別的統計方式了解用戶群"):
        assert purpose in html, purpose
    assert "不會顯示給其他使用者或管理員" in html and "不會傳送至 Google 的 Gemini 服務" in html
    assert "日後若新增生日祝賀等其他用途，我們會先更新本政策" in html
    retention = html.split("<table>", 1)[1].split("</table>", 1)[0]
    assert "含生日與年齡區間" in retention and "連續 12 個月未登入即直接刪除" in retention
    assert "匯出我的資料" in html and "您的帳號資料（含生日與年齡區間）" in html and "刪除您的信箱、名稱、密碼雜湊、生日與年齡區間" in html
    assert "草稿" in html and CURRENT_TERMS_VERSION in html
    headings = re.findall(r"<h2>(\d+)\. ", html)
    assert headings == [str(n) for n in range(1, len(headings) + 1)]


def test_the_legal_pages_follow_the_policy_numbers():
    strict = make_app(age_policy=AgePolicy(min_register_age=18, min_use_age=18, adult_age=18, version="test"))
    privacy = strict.test_client().get("/privacy").get_data(as_text=True)
    assert "發現未滿 18 歲者" in privacy and "未滿 13 歲" not in privacy
