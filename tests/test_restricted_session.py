"""access-control 規格「受限登入狀態預設拒絕」：帳號正在等待刪除的人，只能查詢自己、撤銷刪除、登出與開啟 `/account`。

做法不是逐一列出被擋的端點，而是**掃描 `app.url_map` 的每一條規則**：
1. 以未登入的請求找出「需要登入」的端點（回 401，或導向登入頁）；
2. 以受限登入狀態逐一請求這些端點，除了允許清單之外，一律必須被擋下（API 回 403 `account_deletion_pending`、頁面導向 `/account`）。
新增的端點不用改這個測試，也不必記得檢查：漏掉的人會在這裡被抓到，而不是在上線後。
"""
from __future__ import annotations

import pytest
from flask import jsonify

from app import create_app
from app.config import load_config
from app.models import Base
from app.models.user import STATUS_DELETION_PENDING
from app.security.session import current_user
from app.services.errors import Unauthenticated
from sqlalchemy.orm import Session
from tests.support.auth import START, FakeClock, add_user
from tests.support.idor import logged_in_client

# 允許受限登入狀態的端點（方法、規則）。登出本來就不需要登入，所以不在需要登入的端點之列。
ALLOWED = {
    ("GET", "/api/auth/me"),
    ("GET", "/account"),
    ("POST", "/api/account/cancel-deletion"),
}
REQUIRED_TO_BE_FOUND = {
    ("GET", "/api/auth/me"), ("GET", "/account"), ("GET", "/cs"), ("GET", "/admin"), ("GET", "/api/cs/conversations"),
    ("POST", "/api/cs/conversations"), ("POST", "/api/auth/logout-all"), ("POST", "/api/auth/change-password"),
    ("GET", "/api/admin/overview"), ("POST", "/api/auth/verify-age"), ("POST", "/api/account/delete"),
    ("POST", "/api/account/cancel-deletion"),
}
UUID = "00000000-0000-4000-8000-000000000000"


def make_app():
    app = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
    Base.metadata.create_all(app.extensions["db_engine"])
    return app


def restricted_client(app):
    with Session(app.extensions["db_engine"]) as s:
        user = add_user(s, 1, status=STATUS_DELETION_PENDING, deletion_requested_at=START, status_before_deletion="active")
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
    """未登入時的回應是 401，或導向登入頁。"""
    if response.status_code == 401:
        return True
    return response.status_code in (301, 302, 303, 307, 308) and (response.headers.get("Location") or "").startswith("/login")


def is_blocked_as_restricted(response) -> bool:
    if response.status_code == 403:
        return "account_deletion_pending" in response.get_data(as_text=True)
    return response.status_code in (301, 302, 303, 307, 308) and response.headers.get("Location") == "/account"


def leaks(app) -> tuple[list[str], set[tuple[str, str]]]:
    """回傳（放行了不該放行的端點的描述清單，掃描到的需要登入的端點集合）。"""
    anonymous = app.test_client()
    restricted = restricted_client(app)
    leaked: list[str] = []
    protected: set[tuple[str, str]] = set()
    for method, rule, url in routes(app):
        if not requires_login(send(anonymous, method, url)):
            continue
        protected.add((method, rule))
        if (method, rule) in ALLOWED:
            continue
        response = send(restricted, method, url)
        if not is_blocked_as_restricted(response):
            leaked.append(f"{method} {rule} → {response.status_code}")
    return leaked, protected


def test_every_endpoint_that_requires_login_refuses_the_restricted_session_except_the_allowlist():
    app = make_app()
    leaked, protected = leaks(app)
    assert leaked == [], "受限登入狀態可以呼叫這些需要登入的端點（必須預設拒絕，或明確列入允許清單）：" + "；".join(leaked)
    assert REQUIRED_TO_BE_FOUND <= protected


def test_the_scan_is_not_vacuous_it_sees_the_known_protected_endpoints():
    app = make_app()
    _, protected = leaks(app)
    assert len(protected) >= 15
    assert REQUIRED_TO_BE_FOUND <= protected


def test_the_allowed_endpoints_really_work_for_the_restricted_session():
    app = make_app()
    client = restricted_client(app)
    me = client.get("/api/auth/me")
    assert me.status_code == 200 and me.get_json()["user"]["status"] == "deletion_pending"
    assert "deletion_scheduled_at" in me.get_json()["user"]
    assert client.get("/account").status_code == 200
    assert client.post("/api/auth/logout").status_code == 200


def test_pages_redirect_the_restricted_session_to_the_account_page_and_apis_answer_403_with_the_code():
    app = make_app()
    client = restricted_client(app)
    page = client.get("/cs")
    assert page.status_code == 302 and page.headers["Location"] == "/account"
    admin_page = client.get("/admin")
    assert admin_page.status_code == 302 and admin_page.headers["Location"] == "/account"
    for url in ("/api/cs/conversations", "/api/admin/overview", "/api/cs/usage"):
        api = client.get(url)
        assert api.status_code == 403 and "account_deletion_pending" in api.get_data(as_text=True), url
    assert client.post("/api/account/delete", json={}).status_code == 403          # 已在待刪除，不能再申請


def test_the_scan_really_catches_an_endpoint_that_forgot_the_check():
    """自我驗證：暫時加一個自己檢查登入、卻沒有拒絕受限狀態的端點（用 `current_user()` 而不是 `require_login()`），掃描必須報出它。"""
    app = make_app()

    @app.get("/api/_probe_unprotected")
    def probe():
        if current_user() is None:
            raise Unauthenticated()
        return jsonify(ok=True)

    leaked, _ = leaks(app)
    assert leaked == ["GET /api/_probe_unprotected → 200"]
