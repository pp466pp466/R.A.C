"""access-control 規格「受限登入狀態預設拒絕」：等待刪除、待確認年齡、被鎖定的帳號，各自只能做一小組事情。

- 等待刪除：查詢自己、撤銷刪除、登出、開啟 `/account`。
- 待確認年齡（玩家 Google 首次登入後、補填生日之前）：查詢自己、補填年齡、登出、開啟 `/age`。
- 被鎖定（玩家）：查詢自己、送出與查看自己的申訴、登出、開啟 `/appeal`。

做法不是逐一列出被擋的端點，而是**掃描 `app.url_map` 的每一條規則**：
1. 以未登入的請求找出「需要登入」的端點（回 401，或導向登入頁）；
2. 以每一種受限登入狀態逐一請求這些端點，除了該狀態的允許清單之外，一律必須被擋下
   （API 回 403 與該狀態的代碼，頁面導向該狀態的頁面）。
新增的端點不用改這個測試，也不必記得檢查：漏掉的人會在這裡被抓到，而不是在上線後。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from flask import jsonify
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base
from app.models.user import STATUS_DELETION_PENDING
from app.security.session import current_user
from app.services.errors import Unauthenticated
from tests.support.auth import START, FakeClock, add_user
from tests.support.idor import logged_in_client


@dataclass(frozen=True)
class State:
    name: str
    code: str                                   # 被擋下時 API 回的錯誤代碼
    page: str                                   # 被擋下時頁面導向的位置
    overrides: dict                             # 建立該狀態的使用者所需的欄位
    allowed: frozenset[tuple[str, str]]         # 允許該狀態的端點（方法、規則）；登出本來就不需要登入，不在其中
    # 允許清單中「由後續任務才會新增」的端點：掃描時還找不到沒關係，等任務完成後從這裡移除，就變成必須存在
    not_built_yet: frozenset[tuple[str, str]] = field(default_factory=frozenset)


DELETION_PENDING = State(
    name="deletion_pending", code="account_deletion_pending", page="/account",
    overrides=dict(age=30, status=STATUS_DELETION_PENDING, deletion_requested_at=START, status_before_deletion="active"),
    allowed=frozenset({("GET", "/api/auth/me"), ("GET", "/account"), ("POST", "/api/account/cancel-deletion")}),
)
AGE_PENDING = State(
    name="age_pending", code="age_confirmation_required", page="/age",
    overrides=dict(age=None),
    allowed=frozenset({("GET", "/api/auth/me"), ("POST", "/api/auth/verify-age"), ("GET", "/age")}),
)
LOCKED = State(
    name="locked", code="account_locked", page="/appeal",
    overrides=dict(age=30, locked_at=START, lock_reason="rate_limited"),
    allowed=frozenset({
        ("GET", "/api/auth/me"), ("GET", "/appeal"), ("POST", "/api/appeals"), ("GET", "/api/appeals/mine"),
    }),
)
STATES = (DELETION_PENDING, AGE_PENDING, LOCKED)

REQUIRED_TO_BE_FOUND = {
    ("GET", "/api/auth/me"), ("GET", "/account"), ("GET", "/cs"), ("GET", "/admin"), ("GET", "/api/cs/conversations"),
    ("POST", "/api/cs/conversations"), ("POST", "/api/auth/logout-all"), ("POST", "/api/auth/change-password"),
    ("GET", "/api/admin/overview"), ("POST", "/api/auth/verify-age"), ("POST", "/api/account/delete"),
    ("POST", "/api/account/cancel-deletion"),
    ("GET", "/api/sessions/catalog"), ("GET", "/api/sessions"),     # RC-3a 的兩個入口清單：不寫進允許清單，三種受限狀態都必須被擋
    ("GET", "/game"), ("GET", "/game/<session_public_id>"), ("GET", "/game/start/<version_public_id>"),     # RC-3a 的三個頁面：同樣不在允許清單內
}
# 登入之前的端點：管理員的密碼驗證步驟只認簽章的中間狀態 cookie，完全不看登入 session（也不被登入 session 接受），
# 沒有中間狀態就是 401。它們不是「需要登入的端點」，不在掃描的範圍；專屬測試見 test_admin_two_step_login.py。
PRE_LOGIN = {("POST", "/api/auth/admin-password"), ("POST", "/api/auth/admin-password/forgot")}
UUID = "00000000-0000-4000-8000-000000000000"


def make_app():
    app = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
    Base.metadata.create_all(app.extensions["db_engine"])
    return app


def restricted_client(app, state: State, *, n: int = 1, **extra):
    with Session(app.extensions["db_engine"]) as s:
        user = add_user(s, n, **{**state.overrides, **extra})
        s.refresh(user)
        return logged_in_client(app, user)


def concrete_url(rule) -> str:
    """把規則裡的路徑參數換成看起來合法的值（整數轉換器用 1，其餘用 UUID 字串）。"""
    values = {}
    for argument in rule.arguments:
        converter = type(rule._converters[argument]).__name__
        values[argument] = 1 if converter == "IntegerConverter" else ("a.js" if converter == "PathConverter" else UUID)
    return rule.build(values, append_unknown=False)[1]


def routes(app):
    for rule in app.url_map.iter_rules():
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            yield method, rule.rule, concrete_url(rule)


def send(client, method: str, url: str):
    kwargs = {"json": {}} if method in ("POST", "PUT", "PATCH", "DELETE") else {}
    return client.open(url, method=method, **kwargs)


def requires_login(response) -> bool:
    """未登入時的回應是 401，或帶著 `next` 導向登入頁（`/register` 之類單純導向 `/login` 的公開頁面不算）。"""
    if response.status_code == 401:
        return True
    return response.status_code in (301, 302, 303, 307, 308) and (response.headers.get("Location") or "").startswith("/login?next=")


def is_blocked_as_restricted(response, state: State) -> bool:
    if response.status_code == 403:
        return state.code in response.get_data(as_text=True)
    return response.status_code in (301, 302, 303, 307, 308) and response.headers.get("Location") == state.page


def leaks(app, state: State) -> tuple[list[str], set[tuple[str, str]]]:
    """回傳（放行了不該放行的端點的描述清單，掃描到的需要登入的端點集合）。"""
    anonymous = app.test_client()
    restricted = restricted_client(app, state)
    leaked: list[str] = []
    protected: set[tuple[str, str]] = set()
    for method, rule, url in routes(app):
        if (method, rule) in PRE_LOGIN or not requires_login(send(anonymous, method, url)):
            continue
        protected.add((method, rule))
        if (method, rule) in state.allowed:
            continue
        response = send(restricted, method, url)
        if not is_blocked_as_restricted(response, state):
            leaked.append(f"{method} {rule} → {response.status_code}")
    return leaked, protected


# ---- 掃描：每一種受限狀態都預設拒絕 -----------------------------------------------------------------

@pytest.mark.parametrize("state", STATES, ids=lambda s: s.name)
def test_every_endpoint_that_requires_login_refuses_the_restricted_session_except_the_allowlist(state):
    app = make_app()
    leaked, protected = leaks(app, state)
    assert leaked == [], f"[{state.name}] 可以呼叫這些需要登入的端點（必須預設拒絕，或明確列入允許清單）：" + "；".join(leaked)
    assert REQUIRED_TO_BE_FOUND <= protected
    assert (state.allowed - state.not_built_yet) <= protected, "允許清單中有端點不存在或不需要登入，清單已過時"


@pytest.mark.parametrize("state", STATES, ids=lambda s: s.name)
def test_the_scan_is_not_vacuous_it_sees_the_known_protected_endpoints(state):
    app = make_app()
    _, protected = leaks(app, state)
    assert len(protected) >= 15
    assert REQUIRED_TO_BE_FOUND <= protected


@pytest.mark.parametrize("state", STATES, ids=lambda s: s.name)
def test_the_scan_really_catches_an_endpoint_that_forgot_the_check(state):
    """自我驗證：暫時加一個自己檢查登入、卻沒有拒絕受限狀態的端點（用 `current_user()` 而不是 `require_login()`），掃描必須報出它。"""
    app = make_app()

    @app.get("/api/_probe_unprotected")
    def probe():
        if current_user() is None:
            raise Unauthenticated()
        return jsonify(ok=True)

    leaked, _ = leaks(app, state)
    assert leaked == ["GET /api/_probe_unprotected → 200"]


# ---- 各狀態的行為 ------------------------------------------------------------------------------------

@pytest.mark.parametrize("state", STATES, ids=lambda s: s.name)
def test_me_reports_the_restriction_and_logout_always_works(state):
    app = make_app()
    client = restricted_client(app, state)
    me = client.get("/api/auth/me")
    assert me.status_code == 200 and me.get_json()["user"]["restriction"] == state.name
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401


def test_the_deletion_pending_allowed_endpoints_really_work():
    app = make_app()
    client = restricted_client(app, DELETION_PENDING)
    me = client.get("/api/auth/me")
    assert me.status_code == 200 and me.get_json()["user"]["status"] == "deletion_pending"
    assert "deletion_scheduled_at" in me.get_json()["user"]
    assert client.get("/account").status_code == 200


@pytest.mark.parametrize("state", STATES, ids=lambda s: s.name)
def test_pages_redirect_to_the_states_own_page_and_apis_answer_403_with_the_code(state):
    app = make_app()
    client = restricted_client(app, state)
    for page_url in ("/cs", "/admin"):
        page = client.get(page_url)
        assert page.status_code == 302 and page.headers["Location"] == state.page, page_url
    for url in ("/api/cs/conversations", "/api/admin/overview", "/api/cs/usage"):
        api = client.get(url)
        assert api.status_code == 403 and state.code in api.get_data(as_text=True), url
        assert api.get_json()["error"]["code"] == state.code, url
    assert client.post("/api/account/delete", json={}).status_code == 403


def test_the_age_pending_player_can_complete_the_age_step_and_is_then_released_without_logging_in_again():
    app = make_app()
    client = restricted_client(app, AGE_PENDING)
    assert client.get("/api/cs/conversations").status_code == 403
    from tests.support.age import age_fields

    assert client.post("/api/auth/verify-age", json=age_fields(30)).status_code == 200
    assert client.get("/api/auth/me").get_json()["user"]["restriction"] is None
    assert client.get("/api/cs/conversations").status_code == 200
    assert client.get("/cs").status_code == 200


# ---- 優先順序與管理員 ---------------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (dict(age=None, locked_at=START, lock_reason="rate_limited"), "account_locked"),                    # 鎖定優先於待確認年齡
        (dict(age=30, locked_at=START, lock_reason="rate_limited", status=STATUS_DELETION_PENDING,
              deletion_requested_at=START, status_before_deletion="active"), "account_locked"),              # 鎖定優先於等待刪除
        (dict(age=None, status=STATUS_DELETION_PENDING, deletion_requested_at=START,
              status_before_deletion="active"), "age_confirmation_required"),                                # 待確認年齡優先於等待刪除
    ],
    ids=["locked-over-age-pending", "locked-over-deletion-pending", "age-pending-over-deletion-pending"],
)
def test_when_several_restrictions_apply_the_most_specific_one_wins(overrides, expected):
    app = make_app()
    with Session(app.extensions["db_engine"]) as s:
        user = add_user(s, 1, **overrides)
        s.refresh(user)
        client = logged_in_client(app, user)
    response = client.get("/api/cs/conversations")
    assert response.status_code == 403 and response.get_json()["error"]["code"] == expected


@pytest.mark.parametrize("overrides", [dict(age=None), dict(age=None, locked_at=START, lock_reason="rate_limited")], ids=["no-age", "no-age-and-lock-flag"])
def test_an_admin_never_enters_the_age_pending_or_locked_state(overrides):
    """管理員（由部署者以指令建立、沒有年齡資料）可登入與管理帳號；未確認前只是不能用會呼叫模型的功能（由 AccessPolicy 判斷）。"""
    app = make_app()
    client = restricted_client(app, AGE_PENDING, role="admin", **overrides)
    assert client.get("/api/auth/me").get_json()["user"]["restriction"] is None
    assert client.get("/account").status_code == 200
    assert client.get("/api/admin/overview").status_code == 200
    assert client.get("/api/cs/conversations").status_code == 200
