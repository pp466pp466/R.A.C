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


# ---- 劇本審核分頁（rpg-script-publishing）---------------------------------------------------------------------

def _script_section(template: str) -> str:
    return template.split("<!-- ===== 劇本審核 ===== -->", 1)[1].split("<!-- ===== 被鎖定的帳號 ===== -->", 1)[0]


def test_the_console_has_the_script_review_tab_with_four_lists_loaded_from_the_script_endpoints():
    script, template = _frontend("script"), _frontend("template")
    assert "{ id: 'scripts', label: '劇本審核' }" in script
    for status in ("pending_review", "published", "reported", "removed"):                # 被檢舉（rpg-script-reports）與其他三個走同一個端點
        assert f"scripts_{status}: {{ path: '/scripts', key: 'versions', paged: true, params: {{ status: '{status}' }} }}" in script
    assert "tab === 'scripts'" in template
    for label in ("待審", "上架中", "被檢舉", "已下架"):
        assert f"label: '{label}'" in script


def test_the_pending_count_comes_from_the_pending_list_and_is_shown_on_the_tab_label_not_the_overview():
    script, template = _frontend("script"), _frontend("template")
    assert "ensureLoaded(PENDING_LIST)" in script.split("Vue.onMounted", 1)[1].split("return {", 1)[0]     # 進入頁面就讀一次待審清單
    assert "tabBadge(item.id)" in template.split("</nav>", 1)[0]
    assert "pending" not in template.split("<!-- ===== 總覽 ===== -->", 1)[1].split("<!-- ===== 被標記對話 ===== -->", 1)[0].lower()


def test_the_shell_carries_no_script_text_even_when_a_script_is_waiting_for_review(app):
    from tests.support.script_review import at, make_author, put_in_status
    from app.repositories.script_repository import ScriptRepository
    from app.models.script import VERSION_PENDING

    with Session(app.extensions["db_engine"]) as session:
        repo = ScriptRepository(session)
        author = make_author(session, 9)
        version = put_in_status(repo, author, VERSION_PENDING, now=at(), title="獨一無二的測試標題", summary="獨一無二的測試簡介")
        markers = (version.public_id, repo.get_script_of(version).public_id, author.email, author.public_id)
        session.commit()
    html = client_for(app, "admin").get("/admin").get_data(as_text=True)
    for marker in ("獨一無二的測試標題", "獨一無二的測試簡介", *markers):
        assert marker not in html


def test_script_text_the_note_and_the_titles_are_rendered_as_plain_text_never_as_markup():
    template, script = _frontend("template"), _frontend("script")
    section = _script_section(template)
    assert "{{ row.title }}" in section and "{{ row.summary }}" in section
    modal = template.split("<!-- 劇本全文", 1)[1].split("<!-- 退回", 1)[0]
    for expression in ("{{ scriptDoc.version.title }}", "{{ scriptDoc.version.decision_note }}", "{{ section.text }}", "{{ section.label }}"):
        assert expression in modal, expression
    assert "v-html" not in template and "innerHTML" not in script and "insertAdjacentHTML" not in script
    assert "admin-msg__text" in modal                                      # 與對話訊息相同的純文字樣式（保留換行）


def test_the_row_list_has_no_script_body_and_only_the_full_text_button_reads_it():
    template, script = _frontend("template"), _frontend("script")
    section = _script_section(template)
    assert "openScript(row.id)" in section and "看全文" in section
    assert "content" not in section.replace("content_tier", "")           # 清單沒有內文
    assert "approve" not in section and "askReject" not in section         # 核可與退回只在讀完全文的視窗裡
    assert "'/scripts/versions/' + encodeURIComponent(publicId)" in script


def test_approving_needs_no_password_and_rejecting_has_a_category_and_an_optional_private_note():
    template, script = _frontend("template"), _frontend("script")
    approve = script.split("function approveOpenScript()", 1)[1].split("/* 從全文視窗進入", 1)[0]
    assert "password" not in approve and "/approve'" in approve
    reject = template.split("<!-- 退回", 1)[1].split("<!-- 危險操作共用的確認視窗", 1)[0]
    assert "v-model=\"rejectState.category\"" in reject and "v-model=\"rejectState.note\"" in reject
    assert "password" not in reject and "只有管理員看得到" in reject and "categoryOptions" in reject
    assert "選填" in reject and "必填" in reject
    submit = script.split("function submitReject()", 1)[1].split("// ---- 啟動", 1)[0]
    assert "'/reject'" in submit and "password" not in submit


def test_removing_and_restoring_go_through_the_shared_password_confirmation_with_category_and_note():
    script, template = _frontend("script"), _frontend("template")
    remove = script.split("script_remove: {", 1)[1].split("script_restore: {", 1)[0]
    assert "needsCategory: true" in remove and "needsNote: true" in remove and "needsReason: false" in remove
    assert "'/scripts/versions/' + encodeURIComponent(id) + '/remove'" in remove
    restore = script.split("script_restore: {", 1)[1].split("var NO_CONFIRM", 1)[0]
    assert "needsCategory" not in restore and "'/scripts/versions/' + encodeURIComponent(id) + '/restore'" in restore
    confirm = template.split("<!-- 危險操作共用的確認視窗", 1)[1]
    assert "confirmSpec.needsCategory" in confirm and "confirmSpec.needsNote" in confirm and 'type="password"' in confirm
    submit = script.split("function submitConfirm()", 1)[1].split("// ---- 劇本審核", 1)[0]
    assert submit.index("confirmState.password = ''") < submit.index("request('post', spec.path(targetId)")    # 密碼送出前清空
    assert "body.category = confirmState.category" in submit and "body.note = confirmState.note" in submit


def test_the_note_is_cleared_with_the_password_when_the_confirmation_closes_and_never_stored():
    script = _frontend("script")
    closing = script.split("Vue.watch(confirmOpen", 1)[1].split("function userTarget", 1)[0]
    assert "confirmState.note = ''" in closing and "confirmState.password = ''" in closing
    reject_closing = script.split("Vue.watch(rejectOpen", 1)[1].split("function submitReject", 1)[0]
    assert "rejectState.note = ''" in reject_closing
    for storage in ("localStorage", "sessionStorage", "indexedDB"):
        assert storage + "." not in script and storage + "[" not in script        # 註解可以提到，程式碼不使用


def test_a_409_closes_the_window_shows_the_reason_and_reloads_the_lists():
    script = _frontend("script")
    for function in ("function approveOpenScript()", "function dismissOpenScript()", "function submitReject()", "function submitConfirm()"):
        body = script.split(function, 1)[1].split("\n      function ", 1)[0] if function != "function submitConfirm()" else \
            script.split(function, 1)[1].split("// ---- 劇本審核", 1)[0]
        assert "status === 409" in body and "refreshScriptLists()" in body, function
    refresh = script.split("function refreshScriptLists()", 1)[1].split("function removeScriptRow", 1)[0]
    assert "fetchList(PENDING_LIST, false)" in refresh                      # 標籤上的待審數量也要更新


def test_the_modals_are_driven_by_vue_state_and_use_no_bootstrap_javascript():
    template = _frontend("template")
    for model in ("scriptOpen", "rejectOpen", "confirmOpen"):
        assert f'v-model="{model}"' in template
    assert "data-bs-" not in template and "bootstrap.bundle" not in template


# ---- 被檢舉檢視與檢舉面板（rpg-script-reports；行為在 tests/js/admin.test.js 與 admin-format.test.js）----------------------------------------------

def test_the_reported_view_lists_the_server_order_with_a_count_chip_and_a_red_line_marker_and_never_sorts_in_the_browser():
    script, template = _frontend("script"), _frontend("template")
    section = _script_section(template)
    assert "{{ fmt.reportCountText(row) }}" in section and "'admin-chip--alert': row.red_line_reported" in section
    assert ":empty-text=\"scriptEmptyText\"" in section                                  # 空清單的說明依檢視
    assert "scriptView === 'reported'" in section and "不會自動下架" in section and "不需要密碼" in section
    for sorting in (".sort(", ".reverse(", "orderBy"):
        assert sorting not in script, sorting                                              # 排序由後端決定，前端不重排


def test_the_script_window_reads_the_reports_after_the_full_text_and_only_for_a_published_version_with_open_reports():
    script = _frontend("script")
    opening = script.split("function openScript(publicId)", 1)[1].split("Vue.watch(scriptOpen", 1)[0]
    assert opening.index("scriptDoc.version = data.version") < opening.index("loadScriptReports(publicId, token)")    # 先全文、再檢舉
    assert "fmt.scriptActions(data.version).dismiss" in opening                                                       # 上架中且有未結案檢舉才讀
    loading = script.split("function loadScriptReports(publicId, token)", 1)[1].split("function reloadScriptReports", 1)[0]
    assert "'/reports'" in loading and "scriptReports.error = failureText(error)" in loading and ".finally(" in loading   # 失敗只在面板顯示
    assert "clearScriptReports();" in script.split("Vue.watch(scriptOpen", 1)[1].split("function approveOpenScript", 1)[0]  # 關閉視窗就丟掉內容


def test_dismissing_needs_no_password_and_is_blocked_until_the_reports_were_read():
    script, template = _frontend("script"), _frontend("template")
    dismiss = script.split("function dismissOpenScript()", 1)[1].split("/* 從全文視窗進入", 1)[0]
    assert "password" not in dismiss and "'/reports/dismiss'" in dismiss and "{ data: {} }" in dismiss
    assert "scriptReports.loading || scriptReports.error" in dismiss                        # 沒讀到檢舉就不能結案
    modal = template.split("<!-- 劇本全文", 1)[1].split("<!-- 退回", 1)[0]
    assert '@click="dismissOpenScript"' in modal and "不成立（結案）" in modal
    assert 'v-if="scriptDocActions.dismiss"' in modal and "!!scriptReports.error" in modal


def test_the_report_panel_shows_reasons_and_free_text_as_plain_text_and_has_no_reporter():
    template, script = _frontend("template"), _frontend("script")
    panel = template.split('<div v-if="scriptReports.shown"', 1)[1].split("scriptDoc.actionError", 1)[0]
    for expression in ("{{ report.reasonsText }}", "{{ report.text }}", "{{ report.createdText }}", "{{ report.statusText }}", "{{ openReportCount }}", "{{ scriptReports.error }}"):
        assert expression in panel, expression
    assert "admin-msg__text" in panel                                                       # 與對話訊息相同的純文字樣式（保留換行）
    for forbidden in ("v-html", "innerHTML", "reporter", "檢舉人身分"):
        assert forbidden not in panel, forbidden
    assert "reporter" not in script                                                          # 回應沒有檢舉人，前端也沒有任何讀取它的地方


def test_removing_a_reported_script_drops_it_from_the_reported_list_and_approving_marks_that_list_stale():
    script = _frontend("script")
    submit = script.split("function submitConfirm()", 1)[1].split("// ---- 劇本審核", 1)[0]
    assert "removeScriptRow('scripts_reported', targetId)" in submit
    approve = script.split("function approveOpenScript()", 1)[1].split("/* 結案為", 1)[0]
    assert "'scripts_reported'" in approve
    refresh = script.split("function refreshScriptLists()", 1)[1].split("function removeScriptRow", 1)[0]
    assert "'scripts_reported'" in refresh


def test_every_report_panel_class_has_a_style_rule():
    from app.config import PROJECT_ROOT

    css = (PROJECT_ROOT / "app" / "static" / "css" / "admin.css").read_text(encoding="utf-8")
    template = _frontend("template")
    for name in ("admin-reports", "admin-msg--flag"):
        assert name in template and f".{name}" in css, name
