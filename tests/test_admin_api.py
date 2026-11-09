"""admin-console 規格：只有管理員可用、非管理員一律 404（與不存在逐字相同）、no-store、CSRF；各端點的基本回應。

每個端點都以「未登入、一般使用者、vip 一般使用者、管理員」四種身分走過；非管理員連格式錯誤的請求本文都只得到 404。
"""
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import AdminAuditLog, Base, ChatConversation, LlmCallLog, User
from app.models.user import STATUS_ACTIVE, STATUS_SUSPENDED
from app.repositories.chat_repository import ChatRepository
from app.repositories.lock_appeal_repository import LockAppealRepository
from tests.support.admin import STATE
from tests.support.auth import GOOD_PASSWORD, START, FakeClock, add_user
from tests.support.client import raw_client
from tests.support.idor import logged_in_client

ENDPOINTS = (
    "overview", "audit", "lookup", "user", "suspend", "unsuspend", "tier", "feedback", "flagged", "read", "delete", "failures",
    "locked", "appeals", "unlock", "wallet",
)


class World:
    """一個應用程式，加上管理員、兩位一般使用者（甲有被標記的對話、乙已停權）與路徑的組裝。"""

    def __init__(self):
        self.app = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
        self.engine = self.app.extensions["db_engine"]
        Base.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            self.admin_id = add_user(session, 1, age=30, role="admin").id
            self.target_id = add_user(session, 2, age=30).id
            self.vip_id = add_user(session, 3, age=30, member_tier="vip").id
            self.suspended_id = add_user(session, 4, age=30, status=STATUS_SUSPENDED).id
            self.locked_id = add_user(session, 5, age=30, locked_at=START, lock_reason="rate_limited").id
            LockAppealRepository(session).create(self.locked_id, "請幫我解除鎖定", START)
            target = session.get(User, self.target_id)
            chats = ChatRepository(session)
            conv = chats.create_conversation(target.id, client_id="1" * 36, state=STATE, now=START)
            chats.append_message(target.id, conv, client_msg_id="a" * 36, role="user", content="使用者說的一句話", now=START)
            chats.append_message(target.id, conv, client_msg_id="b" * 36, role="assistant", content="客服的回覆", intent="complaint_feedback", now=START)
            session.add(LlmCallLog(
                user_id=target.id, conversation_id=conv.id, kind="chat", provider="f", model="m", attempt=1, status="blocked",
                counted=False, latency_ms=1, created_at=START,
            ))
            session.commit()
            self.target_public, self.target_email = target.public_id, target.email
            self.conversation_public = conv.public_id
            self.suspended_public = session.get(User, self.suspended_id).public_id
            self.locked_public = session.get(User, self.locked_id).public_id

    def client(self, who):
        if who == "anonymous":
            return self.app.test_client()
        user_id = {"admin": self.admin_id, "member": self.target_id, "vip": self.vip_id}[who]
        with Session(self.engine) as session:
            return logged_in_client(self.app, session.get(User, user_id))

    def request(self, name):
        """（方法, 路徑, 本文, 預期的成功狀態）。"""
        password = {"password": GOOD_PASSWORD}
        return {
            "overview": ("GET", "/api/admin/overview", None),
            "audit": ("GET", "/api/admin/audit", None),
            "lookup": ("POST", "/api/admin/users/lookup", {"email": self.target_email}),
            "user": ("GET", f"/api/admin/users/{self.target_public}", None),
            "suspend": ("POST", f"/api/admin/users/{self.target_public}/suspend", {**password, "reason": "違規"}),
            "unsuspend": ("POST", f"/api/admin/users/{self.suspended_public}/unsuspend", password),
            "tier": ("POST", f"/api/admin/users/{self.target_public}/tier", {**password, "tier": "plus"}),
            "feedback": ("GET", "/api/admin/feedback", None),
            "flagged": ("GET", "/api/admin/flagged", None),
            "read": ("GET", f"/api/admin/conversations/{self.conversation_public}", None),
            "delete": ("POST", f"/api/admin/conversations/{self.conversation_public}/delete", {**password, "reason": "違規"}),
            "failures": ("GET", "/api/admin/failures", None),
            "locked": ("GET", "/api/admin/locked", None),
            "appeals": ("GET", "/api/admin/appeals", None),
            "unlock": ("POST", f"/api/admin/users/{self.locked_public}/unlock", password),
            "wallet": ("POST", f"/api/admin/users/{self.target_public}/wallet/adjust", {**password, "bucket": "paid", "delta": 50, "reason": "活動補償"}),
        }[name]

    def send(self, who, name, **overrides):
        method, path, body = self.request(name)
        client = self.client(who)
        if body is not None:
            return client.open(path, method=method, json=overrides.get("json", body))
        return client.open(path, method=method)

    def user(self, user_id):
        with Session(self.engine) as session:
            row = session.get(User, user_id)
            return row.status, row.member_tier, row.token_version, row.locked_at

    def count(self, model):
        with Session(self.engine) as session:
            return session.scalar(select(func.count()).select_from(model))


@pytest.fixture()
def world():
    return World()


def not_found_body(world):
    return world.client("member").get("/api/admin/this-endpoint-does-not-exist")


# ---- 身分矩陣 -------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ENDPOINTS)
def test_an_anonymous_visitor_gets_401_json(world, name):
    response = world.send("anonymous", name)
    assert response.status_code == 401 and response.get_json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize("who", ["member", "vip"])
@pytest.mark.parametrize("name", ENDPOINTS)
def test_a_logged_in_non_admin_gets_the_same_404_as_a_missing_endpoint_and_nothing_changes(world, who, name):
    before = (world.user(world.target_id), world.user(world.suspended_id), world.user(world.locked_id), world.count(ChatConversation), world.count(AdminAuditLog))
    response = world.send(who, name)
    missing = not_found_body(world)
    assert response.status_code == 404
    assert response.get_json() == missing.get_json()
    assert (world.user(world.target_id), world.user(world.suspended_id), world.user(world.locked_id), world.count(ChatConversation), world.count(AdminAuditLog)) == before


@pytest.mark.parametrize("name", [n for n in ENDPOINTS if n in ("lookup", "suspend", "unsuspend", "tier", "delete", "unlock", "wallet")])
@pytest.mark.parametrize("body", [{"data": "not json"}, None])
def test_a_non_admin_with_a_malformed_body_still_only_sees_404_never_415_or_400(world, name, body):
    method, path, _ = world.request(name)
    client = world.client("member")
    response = client.open(path, method=method, data=body["data"] if body else "{not json", content_type="application/x-www-form-urlencoded" if body else "application/json")
    assert response.status_code == 404


def test_a_non_admin_with_bad_paging_parameters_still_sees_404(world):
    assert world.client("member").get("/api/admin/audit?limit=abc").status_code == 404
    assert world.client("member").get("/api/admin/feedback?limit=9999").status_code == 404


@pytest.mark.parametrize("name", ENDPOINTS)
def test_the_admin_succeeds_on_every_endpoint(world, name):
    response = world.send("admin", name)
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.mimetype == "application/json"


def test_the_response_shapes(world):
    admin = world.client("admin")
    assert set(admin.get("/api/admin/overview").get_json()["overview"]) == {"users", "usage", "warnings", "ages"}
    assert [c["conversation"] for c in admin.get("/api/admin/flagged").get_json()["conversations"]] == [world.conversation_public]
    assert [c["conversation"] for c in admin.get("/api/admin/feedback").get_json()["conversations"]] == [world.conversation_public]
    assert admin.get(f"/api/admin/conversations/{world.conversation_public}").get_json()["messages"][0]["content"] == "使用者說的一句話"
    assert admin.get("/api/admin/failures").get_json() == {"failures": []}
    assert admin.post("/api/admin/users/lookup", json={"email": world.target_email}).get_json()["user"]["public_id"] == world.target_public


def test_a_suspended_admin_is_treated_as_logged_out(world):
    with Session(world.engine) as session:
        admin = session.get(User, world.admin_id)
        client = logged_in_client(world.app, admin)
        admin.status = STATUS_SUSPENDED
        session.commit()
    assert client.get("/api/admin/overview").status_code == 401


# ---- 回應標頭與 CSRF ------------------------------------------------------------------------

@pytest.mark.parametrize("who", ["anonymous", "member", "admin"])
@pytest.mark.parametrize("name", ENDPOINTS)
def test_every_response_is_no_store_even_errors(world, who, name):
    assert world.send(who, name).headers.get("Cache-Control") == "no-store"


def test_a_missing_admin_path_is_no_store_too(world):
    assert not_found_body(world).headers.get("Cache-Control") == "no-store"


@pytest.mark.parametrize("name", ["lookup", "suspend", "unsuspend", "tier", "delete", "unlock", "wallet"])
def test_a_write_without_a_csrf_token_is_403_and_changes_nothing(world, name):
    method, path, body = world.request(name)
    client = raw_client(world.app)
    with client.session_transaction() as session:
        session["uid"], session["tv"] = world.admin_id, 1
    before = (world.user(world.target_id), world.user(world.suspended_id), world.user(world.locked_id), world.count(ChatConversation), world.count(AdminAuditLog))
    assert client.open(path, method=method, json=body).status_code == 403
    assert (world.user(world.target_id), world.user(world.suspended_id), world.user(world.locked_id), world.count(ChatConversation), world.count(AdminAuditLog)) == before


# ---- 不存在的端點 ----------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/admin/users"),                       # 沒有列出全部使用者的端點
        ("GET", "/api/admin/users/search"),
        ("POST", "/api/admin/users"),
        ("PUT", "/api/admin/audit"),                       # 稽核沒有修改的路徑
        ("PATCH", "/api/admin/audit"),
        ("DELETE", "/api/admin/audit"),
        ("DELETE", "/api/admin/conversations/x"),          # 沒有刪除單則訊息、也沒有以 DELETE 刪對話的路徑
        ("GET", "/api/admin/messages"),
    ],
)
def test_there_are_no_endpoints_for_listing_users_changing_the_audit_or_deleting_single_messages(world, method, path):
    assert world.client("admin").open(path, method=method, json={} if method != "GET" else None).status_code in (404, 405)


def test_the_registered_admin_routes_are_exactly_the_eighteen_documented_ones(world):
    routes = {(rule.rule, method) for rule in world.app.url_map.iter_rules() if rule.rule.startswith("/api/admin") for method in rule.methods - {"HEAD", "OPTIONS"}}
    assert routes == {
        ("/api/admin/overview", "GET"), ("/api/admin/audit", "GET"), ("/api/admin/users/lookup", "POST"),
        ("/api/admin/users/<public_id>", "GET"), ("/api/admin/users/<public_id>/suspend", "POST"),
        ("/api/admin/users/<public_id>/unsuspend", "POST"), ("/api/admin/users/<public_id>/tier", "POST"),
        ("/api/admin/feedback", "GET"), ("/api/admin/flagged", "GET"), ("/api/admin/conversations/<public_id>", "GET"),
        ("/api/admin/conversations/<public_id>/delete", "POST"), ("/api/admin/failures", "GET"),
        ("/api/admin/locked", "GET"), ("/api/admin/appeals", "GET"), ("/api/admin/users/<public_id>/unlock", "POST"),
        ("/api/admin/publish-suspended", "GET"), ("/api/admin/users/<public_id>/lift-publish-suspension", "POST"),      # rpg-script-authoring
        ("/api/admin/users/<public_id>/wallet/adjust", "POST"),                                                          # game-wallet
    }


# ---- 請求格式與錯誤 -------------------------------------------------------------------------

def test_the_admin_gets_400_json_for_a_form_body_and_for_bad_json(world):
    admin = world.client("admin")
    path = f"/api/admin/users/{world.target_public}/suspend"
    assert admin.post(path, data={"reason": "x"}).status_code == 415
    assert admin.post(path, data="{not json", content_type="application/json").status_code == 400
    assert admin.post(path, data="[1]", content_type="application/json").status_code == 400
    assert world.user(world.target_id)[0] == STATUS_ACTIVE


def test_validation_errors_name_the_field(world):
    admin = world.client("admin")
    response = admin.post(f"/api/admin/users/{world.target_public}/suspend", json={"password": GOOD_PASSWORD})
    assert response.status_code == 400 and set(response.get_json()["error"]["fields"]) == {"reason"}
    response = admin.post(f"/api/admin/users/{world.target_public}/tier", json={"password": GOOD_PASSWORD, "tier": "gold"})
    assert response.status_code == 400 and set(response.get_json()["error"]["fields"]) == {"tier"}
    response = admin.get("/api/admin/audit?limit=abc")
    assert response.status_code == 400 and set(response.get_json()["error"]["fields"]) == {"limit"}
    assert admin.get("/api/admin/audit?limit=101").status_code == 400


def test_a_wrong_password_is_400_on_the_password_field_and_is_kept_in_the_audit_despite_the_error_status(world):
    admin = world.client("admin")
    response = admin.post(f"/api/admin/users/{world.target_public}/suspend", json={"password": "wrong-password-xyz", "reason": "違規"})
    assert response.status_code == 400 and set(response.get_json()["error"]["fields"]) == {"password"}
    assert world.user(world.target_id)[0] == STATUS_ACTIVE
    entries = admin.get("/api/admin/audit").get_json()["entries"]
    assert [e["action"] for e in entries] == ["reauth.failed"] and entries[0]["detail"] == {"attempted": "user.suspend"}


def test_the_specific_error_codes_reach_the_client(world):
    admin = world.client("admin")
    other_admin = world.target_public
    assert admin.post(f"/api/admin/users/{world.target_public}/unsuspend", json={"password": GOOD_PASSWORD}).get_json()["error"]["code"] == "not_suspended"
    assert admin.post(f"/api/admin/users/{world.suspended_public}/suspend", json={"password": GOOD_PASSWORD, "reason": "x"}).get_json()["error"]["code"] == "already_suspended"
    assert admin.post(f"/api/admin/users/{other_admin}/tier", json={"password": GOOD_PASSWORD, "tier": "free"}).get_json()["error"]["code"] == "tier_unchanged"
    with Session(world.engine) as session:
        admin_public = session.get(User, world.admin_id).public_id
    response = admin.post(f"/api/admin/users/{admin_public}/suspend", json={"password": GOOD_PASSWORD, "reason": "自己"})
    assert response.status_code == 403 and response.get_json()["error"]["code"] == "admin_target_forbidden"


def test_a_lookup_miss_is_404_and_still_leaves_the_audit_row(world):
    admin = world.client("admin")
    assert admin.post("/api/admin/users/lookup", json={"email": "nobody@example.com"}).status_code == 404
    entries = admin.get("/api/admin/audit").get_json()["entries"]
    assert [(e["action"], e["detail"]) for e in entries] == [("user.lookup", {"via": "email", "found": 0})]


def test_no_response_contains_a_password_hash_a_token_version_or_age_data(world):
    admin = world.client("admin")
    texts = [
        admin.get("/api/admin/overview").get_data(as_text=True),
        admin.get(f"/api/admin/users/{world.target_public}").get_data(as_text=True),
        admin.post("/api/admin/users/lookup", json={"email": world.target_email}).get_data(as_text=True),
        admin.get("/api/admin/flagged").get_data(as_text=True),
        admin.get("/api/admin/feedback").get_data(as_text=True),
        admin.get(f"/api/admin/conversations/{world.conversation_public}").get_data(as_text=True),
    ]
    joined = " ".join(texts)
    for word in ("argon2", "password_hash", "token_version", "birth", "age_range", "age_group"):
        assert word not in joined


def test_the_state_changing_endpoints_work_end_to_end(world):
    admin = world.client("admin")
    assert admin.post(f"/api/admin/users/{world.target_public}/tier", json={"password": GOOD_PASSWORD, "tier": "vip"}).get_json()["user"]["tier"] == "vip"
    suspended = admin.post(f"/api/admin/users/{world.target_public}/suspend", json={"password": GOOD_PASSWORD, "reason": "違規"})
    assert suspended.get_json()["user"]["status"] == STATUS_SUSPENDED and world.user(world.target_id)[0] == STATUS_SUSPENDED
    restored = admin.post(f"/api/admin/users/{world.target_public}/unsuspend", json={"password": GOOD_PASSWORD})
    assert restored.get_json()["user"]["status"] == STATUS_ACTIVE
    assert admin.post(f"/api/admin/conversations/{world.conversation_public}/delete", json={"password": GOOD_PASSWORD, "reason": "違規"}).get_json() == {"ok": True}
    assert world.count(ChatConversation) == 0
    actions = [e["action"] for e in admin.get("/api/admin/audit").get_json()["entries"]]
    assert actions == ["conversation.delete", "user.unsuspend", "user.suspend", "user.change_tier"]


# ---- 調整遊戲幣（game-wallet）------------------------------------------------------------------------------


def wallet_url(world) -> str:
    return f"/api/admin/users/{world.target_public}/wallet/adjust"


def wallet_body(**overrides) -> dict:
    return {"password": GOOD_PASSWORD, "bucket": "paid", "delta": 500, "reason": "活動補償", **overrides}


def test_adjusting_the_wallet_returns_the_user_view_with_the_new_balances_and_writes_the_audit_row(world):
    response = world.client("admin").post(wallet_url(world), json=wallet_body())
    assert response.status_code == 200
    assert response.get_json()["user"]["wallet"] == {"free_balance": 0, "paid_balance": 500}
    with Session(world.engine) as session:
        row = session.scalar(select(AdminAuditLog).where(AdminAuditLog.action == "wallet.adjust"))
        assert (row.reason, row.detail_json) == ("活動補償", {"bucket": "paid", "delta": 500, "balance_after": 500})
        assert row.target_user_public_id == world.target_public and row.admin_user_id == world.admin_id


@pytest.mark.parametrize(
    "overrides, field",
    [({"bucket": "gold"}, "bucket"), ({"delta": 0}, "delta"), ({"delta": "5"}, "delta"), ({"reason": ""}, "reason"), ({"password": ""}, "password")],
)
def test_invalid_wallet_fields_are_400_naming_the_field_and_change_nothing(world, overrides, field):
    response = world.client("admin").post(wallet_url(world), json=wallet_body(**overrides))
    assert response.status_code == 400 and field in response.get_json()["error"]["fields"]
    with Session(world.engine) as session:
        assert session.scalar(select(func.count()).select_from(AdminAuditLog).where(AdminAuditLog.action == "wallet.adjust")) == 0


def test_taking_more_than_the_balance_is_409_insufficient_coins_with_the_balance_and_the_amount(world):
    response = world.client("admin").post(wallet_url(world), json=wallet_body(bucket="free", delta=-30))
    assert response.status_code == 409
    body = response.get_json()
    assert body["error"]["code"] == "insufficient_coins" and (body["balance"], body["needed"]) == (0, 30)


def test_a_wrong_password_is_400_and_leaves_a_reauth_failure(world):
    response = world.client("admin").post(wallet_url(world), json=wallet_body(password="definitely-not-the-password"))
    assert response.status_code == 400
    with Session(world.engine) as session:
        failure = session.scalar(select(AdminAuditLog).where(AdminAuditLog.action == "reauth.failed"))
        assert failure.detail_json == {"attempted": "wallet.adjust"}


def test_an_administrator_cannot_be_adjusted_through_the_api(world):
    with Session(world.engine) as session:
        admin_public = session.get(User, world.admin_id).public_id
    response = world.client("admin").post(f"/api/admin/users/{admin_public}/wallet/adjust", json=wallet_body())
    assert response.status_code == 403


def test_the_user_view_shows_the_balances_after_an_adjustment_and_nothing_more(world):
    admin = world.client("admin")
    admin.post(wallet_url(world), json=wallet_body(bucket="free", delta=120))
    view = admin.get(f"/api/admin/users/{world.target_public}").get_json()["user"]
    assert view["wallet"] == {"free_balance": 120, "paid_balance": 0}
    assert "entries" not in json.dumps(view) and "idempotency" not in json.dumps(view)

