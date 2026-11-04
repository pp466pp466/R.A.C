"""admin-console 規格：`/admin` 只有管理員、非管理員 404（與不存在的頁面相同）、no-store、初始狀態只有管理員的顯示名稱。"""
import json
import re

import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base, User
from app.models.user import STATUS_SUSPENDED
from tests.support.auth import FakeClock, add_user
from tests.support.idor import logged_in_client


def state_of(response):
    match = re.search(r'<script type="application/json" id="initial-state">(.*?)</script>', response.get_data(as_text=True), re.S)
    assert match, "頁面沒有初始狀態"
    return json.loads(match.group(1))


def without_tokens(html: str) -> str:
    """頁面內嵌每個工作階段不同的 CSRF 權杖；比較兩頁是否相同時先拿掉它。"""
    return re.sub(r'(<meta name="csrf-token" content=")[^"]*(")', r"\1\2", html)


@pytest.fixture()
def app():
    app = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
    Base.metadata.create_all(app.extensions["db_engine"])
    with Session(app.extensions["db_engine"]) as session:
        app.ids = {
            "admin": add_user(session, 1, age=30, role="admin", display_name="站長小明").id,
            "member": add_user(session, 2, age=30).id,
            "vip": add_user(session, 3, age=30, member_tier="vip").id,
            "suspended_admin": add_user(session, 4, age=30, role="admin", status=STATUS_SUSPENDED).id,
            "no_age_admin": add_user(session, 5, role="admin").id,
        }
    return app


def client_for(app, name):
    with Session(app.extensions["db_engine"]) as session:
        return logged_in_client(app, session.get(User, app.ids[name]))


def test_an_anonymous_visitor_is_sent_to_the_login_page_and_comes_back_to_admin(app):
    response = app.test_client().get("/admin")
    assert response.status_code == 302 and response.headers["Location"] == "/login?next=%2Fadmin"


@pytest.mark.parametrize("who", ["member", "vip"])
def test_a_logged_in_non_admin_gets_the_same_404_page_as_a_page_that_does_not_exist(app, who):
    client = client_for(app, who)
    page, missing = client.get("/admin"), client.get("/this-page-does-not-exist")
    assert page.status_code == 404 and page.mimetype == "text/html"
    assert without_tokens(page.get_data(as_text=True)) == without_tokens(missing.get_data(as_text=True))
    assert "管理" not in page.get_data(as_text=True) and "admin" not in page.get_data(as_text=True).replace("admin-", "")


def test_a_suspended_admin_is_sent_to_login_like_a_logged_out_visitor(app):
    assert client_for(app, "suspended_admin").get("/admin").status_code == 302


def test_the_admin_gets_the_shell_with_no_store_and_a_minimal_initial_state(app):
    response = client_for(app, "admin").get("/admin")
    assert response.status_code == 200 and response.mimetype == "text/html"
    assert response.headers["Cache-Control"] == "no-store"
    assert state_of(response) == {"admin": {"display_name": "站長小明"}}
    html = response.get_data(as_text=True)
    assert 'id="admin-app"' in html and 'lang="zh-Hant-TW"' in html and 'charset="UTF-8"' in html


def test_the_page_carries_no_data_only_a_shell(app):
    """沒有統計、沒有任何使用者的資料：所有資料都經由 /api/admin/* 取得。"""
    with Session(app.extensions["db_engine"]) as session:
        member = session.get(User, app.ids["member"])
        secrets = (member.email, member.display_name, member.public_id)
    html = client_for(app, "admin").get("/admin").get_data(as_text=True)
    for secret in secrets:
        assert secret not in html


def test_an_admin_who_has_not_completed_the_age_check_can_open_the_console(app):
    assert client_for(app, "no_age_admin").get("/admin").status_code == 200


def test_the_404_for_a_non_admin_is_also_no_store(app):
    assert client_for(app, "member").get("/admin").headers["Cache-Control"] == "no-store"


def test_a_post_to_the_page_is_not_allowed(app):
    assert client_for(app, "admin").post("/admin", json={}).status_code == 405


# ---- 被鎖定的帳號與申訴（google-first-login）-------------------------------------------------------------

def _frontend(name: str) -> str:
    from app.config import PROJECT_ROOT

    paths = {
        "template": PROJECT_ROOT / "app" / "templates" / "admin" / "console.html",
        "script": PROJECT_ROOT / "app" / "static" / "js" / "admin" / "admin.js",
    }
    return paths[name].read_text(encoding="utf-8")


def test_the_console_has_the_locked_accounts_and_appeals_tabs_and_the_unlock_confirmation():
    script, template = _frontend("script"), _frontend("template")
    assert "{ id: 'locked', label: '被鎖定的帳號' }" in script and "{ id: 'appeals', label: '申訴' }" in script
    assert "locked: { path: '/locked', key: 'accounts', paged: true }" in script
    assert "appeals: { path: '/appeals', key: 'appeals', paged: true }" in script
    assert "unlock: {" in script and "'/users/' + encodeURIComponent(id) + '/unlock'" in script
    assert "tab === 'locked'" in template and "tab === 'appeals'" in template and "askUnlock(" in template


def test_the_unlock_goes_through_the_shared_password_confirmation_that_clears_the_password():
    script = _frontend("script")
    spec = script.split("unlock: {", 1)[1].split("remove: {", 1)[0]
    assert "needsReason: false" in spec and "needsTier: false" in spec        # 只需要密碼
    submit = script.split("function submitConfirm()", 1)[1].split("// ---- 啟動", 1)[0]
    assert submit.index("confirmState.password = ''") < submit.index("request('post', spec.path(targetId)")    # 送出的當下立即清空


def test_after_an_unlock_the_account_and_its_appeals_leave_both_lists():
    script = _frontend("script")
    body = script.split("function removeUnlockedFromLists", 1)[1].split("function applyConfirmError", 1)[0]
    assert "lists.locked.items" in body and "lists.appeals.items" in body and "user_public_id" in body


def test_appeal_text_is_rendered_as_plain_text_never_as_markup():
    template = _frontend("template")
    section = template.split("<!-- ===== 申訴 ===== -->", 1)[1].split("<!-- ===== 使用者 ===== -->", 1)[0]
    assert "{{ row.content }}" in section and "v-html" not in template and "innerHTML" not in _frontend("script")
    assert "admin-appeal__text" in section                                           # 與對話訊息相同的純文字樣式（保留換行）


def test_the_appeals_view_says_what_it_does_and_does_not_show():
    template = _frontend("template")
    section = template.split("<!-- ===== 申訴 ===== -->", 1)[1].split("<!-- ===== 使用者 ===== -->", 1)[0]
    assert "只有目前被鎖定或被暫停發佈的帳號" in section and "不提供該帳號的任何對話" in section
