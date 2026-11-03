"""lockout-appeals 規格的頁面部分：`/appeal` 的外殼、字數提示、申訴內容的純文字顯示與腳本的接線。

存取規則與初始狀態見 `test_appeal_api.py`；互動的判斷邏輯（字數、各種錯誤代碼）見 `tests/js/auth-flows.test.js`。
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base
from tests.support.auth import START, FakeClock, add_user
from tests.support.idor import logged_in_client


@pytest.fixture()
def app():
    application = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


def locked_page(app) -> str:
    with Session(app.extensions["db_engine"]) as s:
        client = logged_in_client(app, add_user(s, 1, locked_at=START, lock_reason="rate_limited"))
    return client.get("/appeal").get_data(as_text=True)


def asset(kind: str, name: str) -> str:
    from app.config import PROJECT_ROOT

    base = PROJECT_ROOT / "app" / ("templates" if kind == "template" else "static/js")
    return (base / name).read_text(encoding="utf-8")


def test_the_page_explains_the_situation_and_has_a_labelled_textarea_with_a_live_counter(app):
    html = locked_page(app)
    assert "目前被鎖定" in html and "只有管理員看得到" in html
    assert 'for="appeal-content"' in html and "<textarea" in html and 'id="appeal-content"' in html
    assert 'id="appeal-counter"' in html and "{{ counter.text }}" in html and "{{ limits.hourlyLimit }}" in html
    assert "maxlength" not in html                                                    # 超過時計數變色，而不是默默截斷使用者的文字


def test_my_appeals_and_the_pending_notice_are_shown_as_plain_text(app):
    html = locked_page(app)
    assert "{{ item.content }}" in html and "{{ statusLabel(item.status) }}" in html
    assert "v-html" not in html
    assert "appeal-item__text" in html                                                # 保留換行的純文字樣式
    assert 'v-if="hasPending"' in html and 'v-else @submit.prevent="submit"' in html     # 有待處理的申訴時不再顯示表單


def test_the_submit_button_is_disabled_while_sending_and_there_is_a_way_out(app):
    html = locked_page(app)
    assert ':disabled="form.state.submitting"' in html and ":aria-busy=" in html
    assert '@click="logout"' in html and '@click="refresh"' in html


def test_the_script_loads_my_appeals_validates_sends_and_keeps_nothing_in_the_browser():
    script = asset("script", "auth/appeal.js")
    assert "axios.get('/api/appeals/mine')" in script and "Vue.onMounted(load)" in script
    assert "Auth.validateAppeal(form.values.content, limits.maxChars)" in script
    assert "axios.post('/api/appeals', { content: form.values.content })" in script
    assert "Auth.appealOutcome(info)" in script and "form.run(" in script
    for storage in ("localStorage", "sessionStorage", "indexedDB", "document.cookie"):
        assert storage not in script, storage                                         # 申訴內容是個資，不寫進任何瀏覽器儲存體
    template = asset("template", "auth/appeal.html")
    assert template.index("auth-flows.js") < template.index("appeal.js")


def test_the_time_is_shown_in_taipei_time():
    assert "timeZone: 'Asia/Taipei'" in asset("script", "auth/appeal.js")
