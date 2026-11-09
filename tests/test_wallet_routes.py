"""wallet-api 規格「玩家無法經由 HTTP 花費或增加餘額」的路由盤點與原始碼盤點。

兩道守門：①列出應用程式註冊的全部路由，玩家端（不在 `/api/admin/` 底下）會寫入的錢包路由只有 `POST /api/wallet/checkin`；
②掃描全部 Controller 的原始碼，除了簽到與管理員的調整，沒有人呼叫錢包的預扣、結算、退回、調整或贈幣。附反向驗證。
"""
import ast

import pytest

from app import create_app
from app.config import PROJECT_ROOT, load_config
from tests.support.auth import FakeClock

CONTROLLERS = PROJECT_ROOT / "app" / "controllers"
MUTATING_METHODS = {"hold", "settle", "refund", "adjust", "ensure_signup_grant"}
WRITE_VERBS = {"POST", "PUT", "PATCH", "DELETE"}


@pytest.fixture()
def app():
    return create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())


def wallet_write_routes(application) -> set[tuple[str, str]]:
    return {
        (method, rule.rule) for rule in application.url_map.iter_rules() if "wallet" in rule.rule.lower()
        for method in rule.methods & WRITE_VERBS
    }


def test_the_only_player_route_that_changes_the_balance_is_the_checkin(app):
    player_routes = {route for route in wallet_write_routes(app) if not route[1].startswith("/api/admin/")}
    assert player_routes == {("POST", "/api/wallet/checkin")}


def test_the_only_administrator_route_that_changes_a_balance_is_the_adjustment(app):
    admin_routes = {route for route in wallet_write_routes(app) if route[1].startswith("/api/admin/")}
    assert admin_routes <= {("POST", "/api/admin/users/<public_id>/wallet/adjust")}


def test_no_route_can_write_a_wallet_setting(app):
    for rule in app.url_map.iter_rules():
        if "coin" in rule.rule.lower() or "setting" in rule.rule.lower():
            assert not (rule.methods & WRITE_VERBS), rule.rule


def mutating_calls_in(source: str) -> list[str]:
    problems = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in MUTATING_METHODS:
            if "wallet" in ast.unparse(node.func.value).lower() or node.func.attr in {"hold", "settle", "ensure_signup_grant"}:
                problems.append(f"第 {node.lineno} 行：{ast.unparse(node.func)}")
    return problems


def test_no_controller_calls_hold_settle_refund_adjust_or_the_grant_directly():
    found = {}
    for path in sorted(CONTROLLERS.glob("*.py")):
        problems = mutating_calls_in(path.read_text(encoding="utf-8-sig"))
        if problems:
            found[path.name] = problems
    assert found == {}, "Controller 不得直接預扣、結算、退回、調整或發放贈幣（管理員調整經由 AdminService）"


def test_the_source_scan_really_flags_each_kind_of_call():
    assert mutating_calls_in("def f():\n    get_wallet_service().hold(u, key='k')\n")
    assert mutating_calls_in("def f(wallet):\n    wallet.refund(u, 'k')\n")
    assert mutating_calls_in("def f():\n    get_wallet_service().adjust(1, bucket='free', delta=5)\n")
    assert mutating_calls_in("def f(svc):\n    svc.settle(u, 'k')\n")
    assert mutating_calls_in("def f(svc):\n    svc.ensure_signup_grant(u)\n")
    assert mutating_calls_in("def f():\n    return get_wallet_service().view(user)\n") == []
    assert mutating_calls_in("def f():\n    return get_wallet_service().checkin(user, confirm_overflow=True)\n") == []


def test_the_route_scan_would_notice_an_added_spend_route(app):
    app.add_url_rule("/api/wallet/spend", "wallet_spend_test", lambda: "x", methods=["POST"])
    assert ("POST", "/api/wallet/spend") in {route for route in wallet_write_routes(app) if not route[1].startswith("/api/admin/")}
