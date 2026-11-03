"""cs-usage-limits、global-usage-budget、cs-safety-suspension 規格：用量限制在整條請求鏈上的行為。

走真實的 Flask 應用程式、`ChatService`、`UsageService` 與資料庫，供應商是假的；時鐘是可手動前進的假時鐘。
重點是「拒絕不留痕」（訊息、帳本、處理標記都不變、模型沒有被呼叫）與 HTTP 的呈現（狀態碼、代碼、文案、`Retry-After`）。
這個檔案**不**使用寬鬆限制的 fixture——它就是要測限制本身。
"""
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.llm.types import LLMBlockedError
from app.models import AdminAuditLog, ChatConversation, ChatMessage, LlmCallLog, LockStrike, User
from tests.support.llm import FakeLLMProvider, Step
from tests.test_cs_api import (
    BASE, client_for, count, make_app, new_conversation, reply_json, send, smart_provider, uid, user_of,
)

SECRET_TEXT = "只該出現在對話裡的私密內容QWERTY"


def clock_of(app):
    return app.extensions["clock"]


def seed_calls(app, name, n, *, counted=True, status="ok", kind="chat", prompt_tokens=None):
    """直接寫入 n 筆帳本（代表先前已發生的呼叫）。"""
    user_id = None if name is None else app.ids[name]
    with Session(app.extensions["db_engine"]) as session:
        for _ in range(n):
            session.add(LlmCallLog(
                user_id=user_id, conversation_id=None, kind=kind, provider="fake", model="m", attempt=1, status=status,
                counted=counted, prompt_tokens=prompt_tokens, latency_ms=10, created_at=clock_of(app)(),
            ))
        session.commit()


def processing_marker(app, public_id):
    with Session(app.extensions["db_engine"]) as session:
        return session.scalar(select(ChatConversation.processing_since).where(ChatConversation.public_id == public_id))


def snapshot(app):
    """資料庫裡跟「一次被拒絕的請求不該改變任何東西」有關的全部數字。"""
    return (count(app, ChatMessage), count(app, LlmCallLog), count(app, ChatConversation))


def error_of(response):
    return response.get_json()["error"]


@pytest.fixture()
def app():
    return make_app()


def sent(app, public_id, text="你好", name="alice", first=1):
    """連續送出 `count` 則；回傳最後一次的回應。"""
    return send(app, public_id, n=first, text=text, name=name)


# ---- 每分鐘訊息數 ---------------------------------------------------------------------------

def test_the_seventh_message_in_a_minute_is_a_429_that_stores_and_calls_nothing(app):
    public_id = new_conversation(app)
    for n in range(1, 7):
        assert send(app, public_id, n=n).status_code == 200
    provider_calls = len(app.extensions["llm_provider"].calls)
    before = snapshot(app)

    response = send(app, public_id, n=7, text=SECRET_TEXT)

    assert response.status_code == 429
    assert error_of(response) == {"code": "rate_limited", "message": "您傳送訊息的速度太快了，請稍候幾秒再試。"}
    assert int(response.headers["Retry-After"]) == 60                    # 六則都在同一個（假）時間點，最早那則 60 秒後離開視窗
    assert snapshot(app) == before and len(app.extensions["llm_provider"].calls) == provider_calls
    assert processing_marker(app, public_id) is None


def test_after_the_window_slides_the_same_message_goes_through(app):
    public_id = new_conversation(app)
    for n in range(1, 7):
        send(app, public_id, n=n)
    assert send(app, public_id, n=7).status_code == 429
    clock_of(app).advance(seconds=61)
    response = send(app, public_id, n=7)                                   # 同一個識別碼：上次被拒絕時沒有被儲存
    assert response.status_code == 200 and response.get_json()["user_message"]["content"] == "你好"


def test_resending_an_answered_message_is_never_limited_nor_counted_twice(app):
    public_id = new_conversation(app)
    for n in range(1, 7):
        send(app, public_id, n=n)
    before = snapshot(app)
    again = send(app, public_id, n=3)                                      # 已有回覆：回傳既有的，不受每分鐘限制
    assert again.status_code == 200 and snapshot(app) == before
    assert send(app, public_id, n=7).status_code == 429                    # 重送沒有多佔一個名額，但新的一則照樣被擋


def test_the_limit_is_per_user(app):
    mine, theirs = new_conversation(app, "alice", 100), new_conversation(app, "bob", 101)
    for n in range(1, 7):
        send(app, mine, n=n)
    assert send(app, mine, n=7).status_code == 429
    assert send(app, theirs, n=1, name="bob").status_code == 200


def test_plus_members_get_the_larger_per_minute_limit(app):
    with Session(app.extensions["db_engine"]) as session:
        session.get(type(user_of(app, "alice")), app.ids["alice"]).member_tier = "plus"
        session.commit()
    public_id = new_conversation(app)
    for n in range(1, 11):
        assert send(app, public_id, n=n).status_code == 200
    assert send(app, public_id, n=11).status_code == 429


# ---- 洗版：被每分鐘訊息數上限擋下會計次，達門檻鎖定帳號（account-lockout）---------------------------

def lock_state(app, name="alice"):
    with Session(app.extensions["db_engine"]) as session:
        user = session.get(User, app.ids[name])
        return user.locked_at, user.lock_reason, session.scalar(select(func.count()).select_from(LockStrike).where(LockStrike.user_id == user.id))


def test_a_rate_limited_request_leaves_a_strike_that_survives_the_429(app):
    public_id = new_conversation(app)
    for n in range(1, 7):
        send(app, public_id, n=n)
    assert send(app, public_id, n=7).status_code == 429
    assert lock_state(app) == (None, None, 1)                               # 回應是 4xx，計次仍然寫入了


def test_ten_rate_limited_requests_lock_the_account_and_everything_after_that_is_account_locked(app):
    public_id = new_conversation(app)
    for n in range(1, 7):
        send(app, public_id, n=n)
    for n in range(7, 16):
        assert send(app, public_id, n=n).status_code == 429
    assert lock_state(app) == (None, None, 9)                               # 第 9 次：還沒到門檻
    assert send(app, public_id, n=16).status_code == 429                    # 第 10 次仍是 429，但這一次鎖定了帳號
    locked_at, reason, strikes = lock_state(app)
    assert locked_at is not None and reason == "rate_limited" and strikes == 10

    after = send(app, public_id, n=17)
    assert after.status_code == 403 and error_of(after)["code"] == "account_locked"
    assert client_for(app, "alice").get(BASE).status_code == 403            # 連列表也不行：只剩申訴
    with Session(app.extensions["db_engine"]) as session:
        (audit,) = session.scalars(select(AdminAuditLog).where(AdminAuditLog.action == "account.lock"))
        assert audit.admin_user_id is None and audit.detail_json == {"reason": "rate_limited"}


def test_other_users_are_not_affected_by_someones_spam(app):
    mine, theirs = new_conversation(app, "alice", 100), new_conversation(app, "bob", 101)
    for n in range(1, 17):
        send(app, mine, n=n)
    assert lock_state(app)[0] is not None
    assert send(app, theirs, n=1, name="bob").status_code == 200
    assert lock_state(app, "bob") == (None, None, 0)


def test_strikes_older_than_fifteen_minutes_do_not_add_up_to_a_lock(app):
    public_id = new_conversation(app)
    for n in range(1, 7):
        send(app, public_id, n=n)
    for n in range(7, 13):                                                  # 6 次被擋
        assert send(app, public_id, n=n).status_code == 429
    clock_of(app).advance(minutes=16)
    for n in range(100, 106):                                               # 視窗滑過去後再 6 次（先把名額用完）
        send(app, public_id, n=n)
    for n in range(200, 205):
        send(app, public_id, n=n)
    assert lock_state(app)[0] is None


def test_other_usage_rejections_are_not_counted_as_spam(app):
    public_id = new_conversation(app)
    seed_calls(app, "alice", 30)                                            # 今天的輪數已用完
    response = send(app, public_id, n=1)
    assert response.status_code == 429 and error_of(response)["code"] == "daily_quota_exceeded"
    assert lock_state(app) == (None, None, 0)


# ---- 每日輪數 -------------------------------------------------------------------------------

def test_the_thirty_first_turn_of_the_day_is_a_429_with_the_time_until_the_reset(app):
    public_id = new_conversation(app)
    seed_calls(app, "alice", 30)
    before = snapshot(app)
    response = send(app, public_id, n=1, text=SECRET_TEXT)
    assert response.status_code == 429
    assert error_of(response) == {"code": "daily_quota_exceeded", "message": "您今日的對話額度已用完，將於明日 00:00（台灣時間）重置。"}
    assert int(response.headers["Retry-After"]) == 4 * 3600                # 假時鐘是台灣 20:00，距離 00:00 四小時
    assert snapshot(app) == before and processing_marker(app, public_id) is None


def test_replaying_an_answered_message_still_works_after_the_quota_is_used_up(app):
    public_id = new_conversation(app)
    assert send(app, public_id, n=1).status_code == 200
    seed_calls(app, "alice", 30)
    again = send(app, public_id, n=1)
    assert again.status_code == 200 and again.get_json()["assistant_message"]["content"]
    assert send(app, public_id, n=2).status_code == 429


def test_the_next_taipei_day_starts_a_fresh_quota(app):
    public_id = new_conversation(app)
    seed_calls(app, "alice", 30)
    assert send(app, public_id, n=1).status_code == 429
    clock_of(app).advance(hours=4, seconds=1)                              # 台灣 00:00:01
    assert send(app, public_id, n=1).status_code == 200


def test_degraded_replies_do_not_use_the_quota(app):
    public_id = new_conversation(app)
    seed_calls(app, "alice", 29)
    seed_calls(app, "alice", 20, counted=False, status="timeout")
    assert send(app, public_id, n=1).status_code == 200                    # 第 30 輪
    assert send(app, public_id, n=2).status_code == 429                    # 第 31 輪


def test_the_administrator_is_not_exempt(app):
    public_id = new_conversation(app, "admin", 102)
    seed_calls(app, "admin", 30)
    response = send(app, public_id, n=1, name="admin")
    assert response.status_code == 429 and error_of(response)["code"] == "daily_quota_exceeded"


# ---- 對話數上限 -----------------------------------------------------------------------------

def test_the_twenty_first_conversation_is_a_409_and_a_resend_of_an_existing_one_is_not(app):
    client = client_for(app, "alice")
    for n in range(1, 21):
        assert client.post(BASE, json={"client_id": uid(n)}).status_code == 201
    response = client.post(BASE, json={"client_id": uid(21)})
    assert response.status_code == 409
    assert error_of(response) == {"code": "conversation_limit_reached", "message": "對話數已達上限，請先刪除不需要的對話。"}
    assert count(app, ChatConversation) == 20
    assert client.post(BASE, json={"client_id": uid(1)}).status_code == 200      # 冪等重送：不受上限影響


def test_deleting_a_conversation_frees_a_slot(app):
    client = client_for(app, "alice")
    ids = [client.post(BASE, json={"client_id": uid(n)}).get_json()["conversation"]["id"] for n in range(1, 21)]
    assert client.post(BASE, json={"client_id": uid(99)}).status_code == 409
    assert client.delete(f"{BASE}/{ids[0]}").status_code == 200
    assert client.post(BASE, json={"client_id": uid(99)}).status_code == 201


# ---- 全站用量 -------------------------------------------------------------------------------

def test_when_the_site_token_limit_is_reached_every_send_is_a_503_that_reveals_no_number():
    app = make_app(environ={"USAGE_DAILY_TOKEN_LIMIT": "1000"})
    public_id = new_conversation(app)
    seed_calls(app, None, 1, counted=False, prompt_tokens=1000)
    before = snapshot(app)
    response = send(app, public_id, n=1, text=SECRET_TEXT)
    assert response.status_code == 503
    assert error_of(response) == {"code": "system_busy", "message": "目前使用人數較多，請稍後再試。"}
    assert response.headers["Retry-After"] == "60"
    assert snapshot(app) == before and processing_marker(app, public_id) is None


def test_when_the_site_call_limit_is_reached_every_send_is_a_503():
    app = make_app(environ={"USAGE_DAILY_CALL_LIMIT": "3"})
    public_id = new_conversation(app)
    seed_calls(app, None, 3, counted=False)
    assert send(app, public_id, n=1).status_code == 503


def test_the_site_budget_does_not_block_reading_or_the_usage_view():
    app = make_app(environ={"USAGE_DAILY_CALL_LIMIT": "1"})
    public_id = new_conversation(app)
    seed_calls(app, None, 1)
    client = client_for(app, "alice")
    assert client.get(f"{BASE}/{public_id}/messages").status_code == 200
    assert client.get(BASE).status_code == 200
    assert client.get("/api/cs/usage").status_code == 200


def test_a_real_send_adds_to_the_site_total_until_the_limit_stops_the_next_one():
    app = make_app(environ={"USAGE_DAILY_CALL_LIMIT": "2"})
    public_id = new_conversation(app)
    assert send(app, public_id, n=1).status_code == 200                    # 對話呼叫 1 次
    assert send(app, public_id, n=2).status_code == 200                    # 累計 1，仍低於 2
    assert send(app, public_id, n=3).status_code == 503                    # 累計 2，達上限


# ---- 安全過濾累計暫停 -----------------------------------------------------------------------

def blocked_app():
    steps = [Step(LLMBlockedError("input")) for _ in range(3)]
    return make_app(provider=FakeLLMProvider(default=Step(json.loads(reply_json()))).script(*steps))


def test_three_blocked_messages_suspend_the_fourth_without_calling_the_model(log_lines):
    app = blocked_app()
    public_id = new_conversation(app)
    for n in (1, 2, 3):
        response = send(app, public_id, n=n)
        assert response.status_code == 200 and response.get_json()["assistant_message"]["msg_status"] == "fallback"
        clock_of(app).advance(seconds=20)
    calls_before, before = len(app.extensions["llm_provider"].calls), snapshot(app)

    response = send(app, public_id, n=4, text=SECRET_TEXT)

    assert response.status_code == 403
    body = response.get_json()
    assert body["error"]["code"] == "chat_suspended" and "（台灣時間）恢復" in body["error"]["message"]
    assert body["suspended_until"].startswith("2026-10-27T13:00")          # 最近一次被擋在 12:00:40（UTC）之後再加 1 小時
    assert len(app.extensions["llm_provider"].calls) == calls_before and snapshot(app) == before
    events = [line for line in log_lines() if line.get("event") in ("cs.chat_suspended", "usage.denied")]
    assert [(e["event"], e.get("reason")) for e in events] == [("cs.chat_suspended", "safety_blocks"), ("usage.denied", "suspended")]


def test_the_suspension_ends_after_one_hour(app):
    public_id = new_conversation(app)
    seed_calls(app, "alice", 3, counted=False, status="blocked")
    assert send(app, public_id, n=1).status_code == 403
    clock_of(app).advance(minutes=59)
    assert send(app, public_id, n=1).status_code == 403
    clock_of(app).advance(minutes=1, seconds=1)
    assert send(app, public_id, n=1).status_code == 200


def test_a_suspended_user_can_still_read_and_delete_history_and_see_the_suspension(app):
    public_id = new_conversation(app)
    assert send(app, public_id, n=1).status_code == 200
    seed_calls(app, "alice", 3, counted=False, status="blocked")
    client = client_for(app, "alice")
    assert client.get(f"{BASE}/{public_id}/messages").status_code == 200
    assert client.get(BASE).status_code == 200
    usage = client.get("/api/cs/usage").get_json()["usage"]
    assert usage["suspended_until"] is not None
    assert client.delete(f"{BASE}/{public_id}").status_code == 200


def test_another_user_is_not_affected_by_someone_elses_suspension(app):
    mine, theirs = new_conversation(app, "alice", 100), new_conversation(app, "bob", 101)
    seed_calls(app, "alice", 3, counted=False, status="blocked")
    assert send(app, mine, n=1).status_code == 403
    assert send(app, theirs, n=1, name="bob").status_code == 200


# ---- 用量檢視 -------------------------------------------------------------------------------

def test_the_usage_endpoint_requires_login(app):
    response = app.test_client().get("/api/cs/usage")
    assert response.status_code == 401 and error_of(response)["code"] == "unauthorized"


def test_the_usage_endpoint_describes_the_callers_own_usage(app):
    public_id = new_conversation(app)
    assert send(app, public_id, n=1).status_code == 200
    seed_calls(app, "bob", 9)                                              # 別人的用量不該出現
    seed_calls(app, None, 1, counted=False, prompt_tokens=123456)          # 全站用量不該出現
    response = client_for(app, "alice").get("/api/cs/usage")
    assert response.status_code == 200
    usage = response.get_json()["usage"]
    assert usage == {
        "tier": "free",
        "daily": {"used": 1, "limit": 30, "resets_at": "2026-10-27T16:00:00+00:00"},
        "per_minute_limit": 6,
        "conversations": {"count": 1, "limit": 20},
        "suspended_until": None,
    }
    assert "123456" not in response.get_data(as_text=True)


def test_the_usage_endpoint_is_open_to_a_user_who_has_not_confirmed_their_age(app):
    response = client_for(app, "fresh").get("/api/cs/usage")
    assert response.status_code == 200 and response.get_json()["usage"]["daily"]["used"] == 0


# ---- 日誌 -----------------------------------------------------------------------------------

def test_a_denied_message_is_logged_without_its_text(app, log_lines):
    public_id = new_conversation(app)
    seed_calls(app, "alice", 30)
    send(app, public_id, n=1, text=SECRET_TEXT)
    assert SECRET_TEXT not in log_lines.raw()
    [event] = [line for line in log_lines() if line.get("event") == "usage.denied"]
    assert (event["reason"], event["user_id"]) == ("daily_quota", app.ids["alice"])


# ---- 一般使用不受影響：從頭走完一天 ---------------------------------------------------------------

def test_a_free_member_gets_thirty_real_turns_then_is_stopped_and_gets_a_fresh_day(app):
    """完全不預先寫入帳本：用真實的送出路徑累積 30 輪（每則間隔 11 秒，低於每分鐘 6 則的上限），第 31 輪被擋，
    台灣時間的隔天恢復。證明「計入額度」的帳本標記、日界與限制在整條路徑上是一致的。"""
    public_id = new_conversation(app)
    for n in range(1, 31):
        response = send(app, public_id, n=n, text=f"第 {n} 則")
        assert response.status_code == 200, (n, response.get_data(as_text=True))
        assert response.get_json()["assistant_message"]["msg_status"] == "ok"
        clock_of(app).advance(seconds=11)

    usage = client_for(app, "alice").get("/api/cs/usage").get_json()["usage"]
    assert usage["daily"]["used"] == 30 and usage["daily"]["limit"] == 30

    blocked = send(app, public_id, n=31, text="第 31 則")
    assert blocked.status_code == 429 and error_of(blocked)["code"] == "daily_quota_exceeded"

    clock_of(app).advance(hours=4)                                          # 台灣 00:05 之後
    assert send(app, public_id, n=31, text="第 31 則").status_code == 200
    assert client_for(app, "alice").get("/api/cs/usage").get_json()["usage"]["daily"]["used"] == 1


def test_a_normal_day_of_chatting_is_never_rate_limited(app):
    """每則間隔 11 秒（一分鐘 5 則多一點），連續聊天不會碰到每分鐘 6 則的上限。"""
    public_id = new_conversation(app)
    for n in range(1, 16):
        assert send(app, public_id, n=n).status_code == 200
        clock_of(app).advance(seconds=11)
