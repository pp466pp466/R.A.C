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


def test_a_player_who_already_confirmed_is_sent_to_the_default_page(app):
    response = client_with(app, age=30).get("/age")
    assert response.status_code == 302 and response.headers["Location"] == DEFAULT_AFTER_LOGIN


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
    assert rules["minRegisterAge"] == 13 and rules["adultAge"] == 18 and rules["ranges"]
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


def test_the_submit_button_is_disabled_and_busy_while_sending_and_there_is_a_way_out(app):
    html = client_with(app, age=None).get("/age").get_data(as_text=True)
    assert ':disabled="form.state.submitting"' in html and ":aria-busy=" in html
    assert '@click="logout"' in html                                                    # 填不下去時可以登出
    assert 'href="/terms"' in html


def test_when_the_account_was_erased_only_the_message_and_a_link_to_the_login_page_remain(app):
    html = client_with(app, age=None).get("/age").get_data(as_text=True)
    final = html.split('<div v-if="finished">', 1)[1].split("</div>", 1)[0]
    assert "{{ finished.message }}" in final and ':href="finished.to"' in final and "回到登入頁" in final
    assert 'v-else @submit.prevent="submit"' in html                                    # 表單只在流程還沒結束時存在


def test_the_script_validates_sends_and_follows_the_flow_module_and_keeps_nothing_in_the_browser():
    script = _asset("script", "auth/age.js")
    assert "Auth.validateAge(form.values)" in script and "Auth.ageBody(form.values)" in script
    assert "axios.post('/api/auth/verify-age'" in script and "Auth.safeDestination(state.next" in script
    assert "Auth.ageOutcome(info)" in script and "form.run(" in script                    # 處理中、錯誤與 finally 的恢復都由 useForm 負責
    for storage in ("localStorage", "sessionStorage", "indexedDB", "document.cookie"):
        assert storage not in script, storage                                           # 生日不寫進任何瀏覽器儲存體
    template = _asset("template", "auth/age.html")
    assert template.index("auth-flows.js") < template.index("age.js")                   # 判斷邏輯先載入
