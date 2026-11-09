"""wallet-api 規格：`/api/wallet`、`/api/wallet/checkin`、`/api/wallet/entries`——查詢與簽到預覽、兩步驟的簽到確認、
記錄的顯示與分頁、登入與受限狀態、CSRF、快取標頭，以及「玩家無法經由 HTTP 花費或增加餘額」。

用測試應用程式（記憶體 SQLite）與直接寫入的登入狀態；Service 層的細節在 `test_wallet_service_core.py` 等檔案。
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base, User, Wallet, WalletEntry
from app.repositories.wallet_repository import WalletRepository
from tests.support.auth import FakeClock, add_user, sign_in_as
from tests.support.client import raw_client


@pytest.fixture()
def app():
    application = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


def db(app) -> Session:
    return Session(app.extensions["db_engine"])


def player(app, n: int = 1, **overrides):
    with db(app) as s:
        user = add_user(s, n, password=None, **overrides)
        uid, tv = user.id, user.token_version
    client = app.test_client()
    sign_in_as(client, uid, tv)
    return client


def user_id(app, n: int = 1) -> int:
    with db(app) as s:
        return s.scalar(select(User.id).where(User.email == f"user{n}@example.com"))


def wallet(response) -> dict:
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["wallet"]


def seed_free(app, n: int, amount: int, client=None):
    """把某位玩家的免費餘額調到指定的值（直接寫資料庫，模擬先前的簽到與花費）。先讓玩家存取一次錢包，
    使註冊贈幣已經發過（否則贈幣會在之後第一次存取時才疊上去）。"""
    if client is not None:
        client.get("/api/wallet")
    uid = user_id(app, n)
    with db(app) as s:
        repo = WalletRepository(s)
        state = repo.state(uid) or repo.create_if_missing(uid, FakeClock()())
        from app.repositories.wallet_repository import EntryDraft

        repo.apply(
            uid, expected_version=state.version, new_free=amount, new_paid=state.paid_balance,
            draft=EntryDraft("admin_adjust", f"seed:{n}:{amount}:{state.version}", amount - state.free_balance, 0), now=FakeClock()(),
        )
        s.commit()


# ---- 查詢錢包 ------------------------------------------------------------------------------------------


def test_a_new_player_sees_200_free_coins_nothing_paid_and_a_claimable_checkin(app):
    body = wallet(player(app).get("/api/wallet"))
    assert (body["free_balance"], body["paid_balance"], body["total"]) == (200, 0, 200)
    assert body["free_cap"] == 1000 and body["turn_cost"] == {"standard": 30}
    assert body["checkin"]["claimable"] is True and body["checkin"]["grant"] == 200
    assert (body["checkin"]["would_gain"], body["checkin"]["would_lose"]) == (200, 0)
    assert body["checkin"]["next_reset_at"] == "2026-10-27T16:00:00Z"               # 假時鐘是台北 20:00 → 當日 24:00


def test_after_checking_in_the_wallet_says_so_and_names_the_next_reset(app):
    client = player(app)
    assert wallet(client.post("/api/wallet/checkin", json={}))["free_balance"] == 400
    body = wallet(client.get("/api/wallet"))
    assert body["checkin"]["claimable"] is False and (body["checkin"]["would_gain"], body["checkin"]["would_lose"]) == (0, 0)
    assert body["checkin"]["next_reset_at"].endswith("Z")


def test_a_balance_close_to_the_cap_previews_the_forfeit(app):
    client = player(app)
    seed_free(app, 1, 900, client)
    body = wallet(client.get("/api/wallet"))
    assert (body["checkin"]["would_gain"], body["checkin"]["would_lose"]) == (100, 100)


def test_the_response_has_no_internal_numbers_or_ledger_details(app):
    client = player(app)
    text = client.get("/api/wallet").get_data(as_text=True)
    body = client.get("/api/wallet").get_json()
    assert set(body["wallet"]) == {"free_balance", "paid_balance", "total", "free_cap", "turn_cost", "checkin"}
    for forbidden in ("user_id", "\"id\"", "idempotency", "version", "seq", "signup:"):
        assert forbidden not in text


# ---- 簽到 --------------------------------------------------------------------------------------------


def test_an_ordinary_checkin_succeeds_directly(app):
    client = player(app)
    body = wallet(client.post("/api/wallet/checkin", json={}))
    assert body["free_balance"] == 400


def test_the_checkin_body_may_be_empty(app):
    client = player(app)
    response = client.post("/api/wallet/checkin")
    assert wallet(response)["free_balance"] == 400


def test_a_checkin_that_would_forfeit_coins_returns_409_with_the_preview_and_changes_nothing(app):
    client = player(app)
    seed_free(app, 1, 900, client)
    response = client.post("/api/wallet/checkin", json={})
    assert response.status_code == 409
    body = response.get_json()
    assert body["error"]["code"] == "checkin_overflow" and body["checkin"] == {"would_gain": 100, "would_lose": 100}
    assert wallet(client.get("/api/wallet"))["free_balance"] == 900
    assert wallet(client.get("/api/wallet"))["checkin"]["claimable"] is True


def test_confirming_the_overflow_claims_up_to_the_cap(app):
    client = player(app)
    seed_free(app, 1, 900, client)
    assert wallet(client.post("/api/wallet/checkin", json={"confirm_overflow": True}))["free_balance"] == 1000


def test_a_second_checkin_the_same_day_is_409_already_checked_in(app):
    client = player(app)
    client.post("/api/wallet/checkin", json={})
    response = client.post("/api/wallet/checkin", json={})
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "already_checked_in"
    assert wallet(client.get("/api/wallet"))["free_balance"] == 400


@pytest.mark.parametrize("bad", ["yes", "true", 1, 0, None, [], {}])
def test_a_confirmation_that_is_not_a_boolean_is_a_400_and_changes_nothing(app, bad):
    client = player(app)
    response = client.post("/api/wallet/checkin", json={"confirm_overflow": bad})
    assert response.status_code == 400 and "confirm_overflow" in response.get_json()["error"]["fields"]
    assert wallet(client.get("/api/wallet"))["free_balance"] == 200


def test_a_non_json_body_is_refused(app):
    client = player(app)
    response = client.post("/api/wallet/checkin", data="confirm_overflow=true", content_type="text/plain")
    assert response.status_code == 415
    assert wallet(client.get("/api/wallet"))["free_balance"] == 200


# ---- 記錄 --------------------------------------------------------------------------------------------


def test_the_history_lists_the_signup_gift_and_the_checkin_newest_first_with_chinese_labels(app):
    client = player(app)
    client.post("/api/wallet/checkin", json={})
    body = client.get("/api/wallet/entries").get_json()
    assert [(e["label"], e["free_delta"]) for e in body["entries"]] == [("每日簽到", 200), ("註冊贈送", 200)]
    assert body["next_before"] is None and all(e["pending"] is False for e in body["entries"])


def test_held_turns_show_as_processing_and_refunded_ones_do_not_show_at_all(app):
    client = player(app)
    uid = user_id(app)
    with db(app) as s:
        from tests.support.wallet import WalletKit

        kit = WalletKit(s)
        current = kit.service  # noqa: F841 — 只為了組裝
        from app.services.current_user import CurrentUser

        me = CurrentUser.from_model(s.get(User, uid))
        kit.service.hold(me, key="turn:done")
        kit.service.settle(me, "turn:done")
        kit.service.hold(me, key="turn:failed")
        kit.service.refund(me, "turn:failed")
        kit.service.hold(me, key="turn:open")
        s.commit()
    body = client.get("/api/wallet/entries").get_json()
    assert [(e["label"], e["pending"]) for e in body["entries"]] == [("處理中", True), ("冒險回合", False), ("註冊贈送", False)]


def test_the_history_pages_by_the_players_own_cursor(app):
    client = player(app)
    uid = user_id(app)
    with db(app) as s:
        from tests.support.wallet import WalletKit

        kit = WalletKit(s)
        kit.service.view(__import__("app.services.current_user", fromlist=["CurrentUser"]).CurrentUser.from_model(s.get(User, uid)))
        for _ in range(44):
            kit.service.adjust(uid, bucket="free", delta=1)
        s.commit()
    first = client.get("/api/wallet/entries").get_json()
    assert len(first["entries"]) == 20 and first["next_before"] is not None
    second = client.get(f"/api/wallet/entries?before={first['next_before']}").get_json()
    third = client.get(f"/api/wallet/entries?before={second['next_before']}&limit=100").get_json()
    assert (len(second["entries"]), len(third["entries"]), third["next_before"]) == (20, 5, None)


def test_the_history_never_includes_internal_ids_keys_or_other_players(app):
    mine, theirs = player(app, 1), player(app, 2)
    mine.post("/api/wallet/checkin", json={})
    theirs.post("/api/wallet/checkin", json={})
    body = mine.get("/api/wallet/entries").get_json()
    assert len(body["entries"]) == 2
    for entry in body["entries"]:
        assert set(entry) == {"kind", "label", "pending", "free_delta", "paid_delta", "created_at", "seq"}
    assert "signup:" not in str(body) and "checkin:" not in str(body)


@pytest.mark.parametrize("query", ["before=abc", "limit=abc", "before=1.5"])
def test_malformed_query_values_are_a_400(app, query):
    response = player(app).get(f"/api/wallet/entries?{query}")
    assert response.status_code == 400


# ---- 登入與受限狀態 ----------------------------------------------------------------------------------------

ENDPOINTS = [("GET", "/api/wallet"), ("POST", "/api/wallet/checkin"), ("GET", "/api/wallet/entries")]


@pytest.mark.parametrize(("method", "path"), ENDPOINTS)
def test_every_endpoint_needs_a_login(app, method, path):
    assert app.test_client().open(path, method=method, json={}).status_code == 401


@pytest.mark.parametrize(("method", "path"), ENDPOINTS)
def test_a_player_who_has_not_confirmed_the_age_gets_the_age_gate_code_and_no_coins(app, method, path):
    client = player(app, age=None)
    response = client.open(path, method=method, json={})
    assert response.status_code == 403 and response.get_json()["error"]["code"] == "age_confirmation_required"
    with db(app) as s:
        assert s.scalar(select(Wallet.id)) is None and s.scalar(select(WalletEntry.id)) is None


@pytest.mark.parametrize(("method", "path"), ENDPOINTS)
def test_a_locked_account_gets_account_locked(app, method, path):
    client = player(app, locked_at=datetime(2026, 10, 26, tzinfo=timezone.utc))
    response = client.open(path, method=method, json={})
    assert response.status_code == 403 and response.get_json()["error"]["code"] == "account_locked"


@pytest.mark.parametrize(("method", "path"), ENDPOINTS)
def test_an_account_waiting_for_deletion_is_refused(app, method, path):
    client = player(app, status="deletion_pending", deletion_requested_at=datetime(2026, 10, 26, tzinfo=timezone.utc))
    assert client.open(path, method=method, json={}).status_code == 403


def test_the_checkin_without_a_csrf_token_is_refused_and_changes_nothing(app):
    client = player(app)
    raw = raw_client(app)
    with client.session_transaction() as source, raw.session_transaction() as target:
        target.update(dict(source))
    response = raw.post("/api/wallet/checkin", json={})
    assert response.status_code in (400, 403)
    assert wallet(client.get("/api/wallet"))["free_balance"] == 200


@pytest.mark.parametrize(("method", "path"), ENDPOINTS)
def test_responses_forbid_caching(app, method, path):
    client = player(app)
    assert client.open(path, method=method, json={}).headers["Cache-Control"] == "no-store"
    assert app.test_client().open(path, method=method, json={}).headers["Cache-Control"] == "no-store"       # 錯誤回應也是


# ---- 玩家無法經由 HTTP 花費或增加餘額 -------------------------------------------------------------------------


def test_the_only_player_route_that_changes_the_balance_is_the_checkin(app):
    routes = {
        (method, rule.rule) for rule in app.url_map.iter_rules() if rule.rule.startswith("/api/wallet")
        for method in rule.methods - {"HEAD", "OPTIONS"}
    }
    assert routes == {("GET", "/api/wallet"), ("POST", "/api/wallet/checkin"), ("GET", "/api/wallet/entries")}


@pytest.mark.parametrize("path", ["/api/wallet/hold", "/api/wallet/spend", "/api/wallet/refund", "/api/wallet/settle", "/api/wallet/adjust"])
def test_there_is_no_endpoint_to_spend_refund_or_adjust(app, path):
    assert player(app).post(path, json={"amount": 1000}).status_code in (404, 405)
