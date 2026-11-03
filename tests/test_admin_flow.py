"""admin-console 全流程：使用者被擋三次 → 管理員從警告找到他 → 讀對話 → 停權 → 解除 → 調整等級 → 刪除對話；
以及重新輸入密碼的失敗與鎖定。走真實的客服送出路徑、真實的後台 Service 與稽核，供應商是假的。

整段流程完成後掃描日誌：找不到任何 email、訊息文字、管理員填寫的原因與密碼。
"""
import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.llm.types import LLMBlockedError
from app.models import AdminAuditLog, ChatConversation, LlmCallLog, User
from app.services.google_oidc import GoogleProfile
from tests.support.auth import GOOD_PASSWORD, START
from tests.support.google import SUB, FakeProvider, admin_sign_in
from tests.support.llm import FakeLLMProvider, Step
from tests.test_cs_api import BASE, client_for, make_app, reply_json, send, uid, user_of

BLOCKED_TEXT = "會被安全過濾擋下的私密句子"
HANDOFF_TEXT = "我要找真人客服，我的訂單有問題"
REASON = "反覆觸發內容安全限制"


@pytest.fixture()
def app():
    provider = FakeLLMProvider(default=Step(json.loads(reply_json()))).script(
        Step(json.loads(reply_json(intent="human_handoff", text="目前沒有真人客服，請直接說出問題。"))),
        Step(LLMBlockedError("input")), Step(LLMBlockedError("input")), Step(LLMBlockedError("input")),
    )
    identity = FakeProvider()
    identity.profile = GoogleProfile(SUB, "user3@example.com", True, "管理員")             # test_cs_api 的 admin 是第 3 位使用者
    application = make_app(provider=provider, identity_provider=identity)
    with Session(application.extensions["db_engine"]) as session:
        # 與 `create-admin` 建立的管理員相同：信箱已驗證（否則 Google 的合併規則會把未驗證信箱帳號的密碼清掉）
        session.get(User, application.ids["admin"]).email_verified_at = START
        session.commit()
    return application


def count(app, model):
    with Session(app.extensions["db_engine"]) as session:
        return session.scalar(select(func.count()).select_from(model))


def admin_client(app):
    """真實的管理員登入：先 Google、再輸入密碼，兩者都通過才有登入 session。"""
    client = app.test_client()
    response = admin_sign_in(app, client, GOOD_PASSWORD)
    assert response.status_code == 200, response.get_data(as_text=True)
    return client


def bob_status(app):
    return user_of(app, "bob").status


def test_the_whole_moderation_flow(app, log_lines):
    # ---- 使用者的一天：alice 要求真人客服；bob 被擋三次 ----
    alice_conv = client_for(app, "alice").post(BASE, json={"client_id": uid(1001)}).get_json()["conversation"]["id"]
    assert send(app, alice_conv, n=1, text=HANDOFF_TEXT, name="alice").status_code == 200
    bob_conv = client_for(app, "bob").post(BASE, json={"client_id": uid(1002)}).get_json()["conversation"]["id"]
    for n in (1, 2, 3):
        response = send(app, bob_conv, n=n, text=f"{BLOCKED_TEXT}{n}", name="bob")
        assert response.status_code == 200 and response.get_json()["assistant_message"]["msg_status"] == "fallback"
    assert send(app, bob_conv, n=4, text="第四句", name="bob").status_code == 403           # 客服對話已暫停

    admin = admin_client(app)

    # ---- 警告：總覽 → 被標記清單 ----
    overview = admin.get("/api/admin/overview").get_json()["overview"]
    assert overview["warnings"] == {"blocked_calls": 3, "blocked_users": 1, "suspended_users": 1}
    [flagged] = admin.get("/api/admin/flagged").get_json()["conversations"]
    assert flagged["conversation"] == bob_conv and flagged["blocked_count"] == 3
    assert flagged["user_blocked_today"] == 3 and flagged["user_chat_suspended_until"] is not None
    assert BLOCKED_TEXT not in json.dumps(flagged, ensure_ascii=False)                       # 清單沒有內容
    [feedback] = admin.get("/api/admin/feedback").get_json()["conversations"]
    assert feedback["conversation"] == alice_conv                                              # 要求真人客服的對話出現在意見回饋

    # ---- 讀取（有稽核）→ 看使用者 ----
    read = admin.get(f"/api/admin/conversations/{bob_conv}").get_json()
    assert read["conversation"]["is_flagged"] is True
    assert [m["content"] for m in read["messages"] if m["role"] == "user"][0].startswith(BLOCKED_TEXT)
    bob_public = flagged["user"]
    profile = admin.get(f"/api/admin/users/{bob_public}").get_json()["user"]
    assert profile["status"] == "active" and profile["usage"]["suspended_until"] is not None
    assert not [word for word in ("birth", "age_range", "age_group", "adult", "minor") if word in json.dumps(profile)]

    # ---- 停權：既有登入失效、無法再登入 ----
    suspended = admin.post(f"/api/admin/users/{bob_public}/suspend", json={"password": GOOD_PASSWORD, "reason": REASON})
    assert suspended.status_code == 200 and suspended.get_json()["user"]["status"] == "suspended"
    assert client_for(app, "bob").get(BASE).status_code == 401                                 # 舊 cookie 的 token_version 已過期
    login = app.test_client().post("/api/auth/login", json={"email": "user2@example.com", "password": GOOD_PASSWORD})
    assert login.status_code == 403 and login.get_json()["error"]["code"] == "account_disabled"

    # ---- 解除 → 可以再登入；調整等級 → 額度立即改變 ----
    assert admin.post(f"/api/admin/users/{bob_public}/unsuspend", json={"password": GOOD_PASSWORD}).get_json()["user"]["status"] == "active"
    relogin = app.test_client().post("/api/auth/login", json={"email": "user2@example.com", "password": GOOD_PASSWORD})
    assert relogin.status_code == 200
    changed = admin.post(f"/api/admin/users/{bob_public}/tier", json={"password": GOOD_PASSWORD, "tier": "plus"})
    assert changed.get_json()["user"]["usage"]["daily"]["limit"] == 100
    assert client_for(app, "bob").get("/api/cs/usage").get_json()["usage"]["per_minute_limit"] == 10

    # ---- 刪除違規的對話：對話與訊息消失、帳本保留、離開清單 ----
    ledger_before = count(app, LlmCallLog)
    deleted = admin.post(f"/api/admin/conversations/{bob_conv}/delete", json={"password": GOOD_PASSWORD, "reason": REASON})
    assert deleted.get_json() == {"ok": True}
    assert count(app, LlmCallLog) == ledger_before and count(app, ChatConversation) == 1
    assert admin.get("/api/admin/flagged").get_json()["conversations"] == []
    assert admin.get(f"/api/admin/conversations/{bob_conv}").status_code == 404
    assert client_for(app, "bob").get(f"{BASE}/{bob_conv}/messages").status_code == 404

    # ---- 稽核：由新到舊，內容正確、沒有個資 ----
    entries = admin.get("/api/admin/audit").get_json()["entries"]
    assert [e["action"] for e in entries] == [
        "conversation.delete", "user.change_tier", "user.unsuspend", "user.suspend", "user.lookup", "conversation.read",
    ]
    by_action = {e["action"]: e for e in entries}
    assert by_action["user.suspend"]["reason"] == REASON and by_action["user.suspend"]["detail"] == {"from_status": "active"}
    assert by_action["user.change_tier"]["detail"] == {"from_tier": "free", "to_tier": "plus"}
    assert by_action["conversation.delete"]["target_conversation"] == bob_conv and by_action["conversation.read"]["target_user"] == bob_public
    assert all(e["admin"] == user_of(app, "admin").display_name for e in entries)
    audit_text = json.dumps(entries, ensure_ascii=False)
    for secret in ("user2@example.com", "user3@example.com", BLOCKED_TEXT, HANDOFF_TEXT, GOOD_PASSWORD):
        assert secret not in audit_text

    # ---- 日誌：整段流程沒有 email、訊息文字、原因與密碼 ----
    raw = log_lines.raw()
    for secret in ("user1@example.com", "user2@example.com", "user3@example.com", BLOCKED_TEXT, HANDOFF_TEXT, REASON, GOOD_PASSWORD):
        assert secret not in raw, secret
    events = [(line["event"], line.get("reason")) for line in log_lines() if str(line.get("event", "")).startswith("admin.")]
    assert ("admin.action", "user.suspend") in events and ("admin.action", "conversation.delete") in events


def test_failed_reauthentication_is_kept_audited_and_locks_the_admin_after_five(app, log_lines):
    admin = admin_client(app)
    target = user_of(app, "bob").public_id
    for _ in range(5):
        response = admin.post(f"/api/admin/users/{target}/suspend", json={"password": "wrong-password-xyz", "reason": REASON})
        assert response.status_code == 400 and set(response.get_json()["error"]["fields"]) == {"password"}
    assert bob_status(app) == "active"

    # 鎖定：即使現在密碼正確也被拒絕
    refused = admin.post(f"/api/admin/users/{target}/suspend", json={"password": GOOD_PASSWORD, "reason": REASON})
    assert refused.status_code == 400 and bob_status(app) == "active"
    # 鎖定與登入共用：管理員用正確的密碼登入也被擋（通用的登入失敗訊息）
    assert app.test_client().post("/api/auth/login", json={"email": "user3@example.com", "password": GOOD_PASSWORD}).status_code == 401

    with Session(app.extensions["db_engine"]) as session:
        rows = list(session.scalars(select(AdminAuditLog).order_by(AdminAuditLog.id)))
    assert [r.action for r in rows] == ["reauth.failed"] * 6                                   # 五次錯誤加一次鎖定中被拒絕，都有留下
    assert all(r.detail_json == {"attempted": "user.suspend"} and r.target_user_public_id == target for r in rows)
    assert "wrong-password-xyz" not in log_lines.raw() and GOOD_PASSWORD not in log_lines.raw()
    warnings = [line for line in log_lines() if line.get("event") == "admin.reauth_failed"]
    assert len(warnings) == 6 and all(w["reason"] == "user.suspend" for w in warnings)


def test_a_non_admin_never_reaches_any_of_it_and_the_attempt_is_logged_without_personal_data(app, log_lines):
    bob = client_for(app, "bob")
    victim = user_of(app, "alice").public_id
    assert bob.post(f"/api/admin/users/{victim}/suspend", json={"password": GOOD_PASSWORD, "reason": REASON}).status_code == 404
    assert bob.get("/api/admin/overview").status_code == 404
    assert user_of(app, "alice").status == "active" and count(app, AdminAuditLog) == 0
    denied = [line for line in log_lines() if line.get("event") == "admin.access_denied"]
    assert len(denied) == 2 and all(set(d) >= {"user_id", "reason"} and d["reason"] == "not_admin" for d in denied)
    assert "user3@example.com" not in log_lines.raw() and REASON not in log_lines.raw()


def test_an_admin_who_never_completed_the_age_check_can_still_use_the_console(app):
    """後台不呼叫模型：年齡閘門只擋會呼叫 Gemini 的功能。"""
    with Session(app.extensions["db_engine"]) as session:
        admin = user_of(app, "admin")
        row = session.get(type(admin), admin.id)
        row.birth_date = row.age_range = row.age_confirmed_at = None
        session.commit()
    assert admin_client(app).get("/api/admin/overview").status_code == 200
