"""cs-conversations、cs-chat-ui（頁面初始狀態）、access-control 規格：`/api/cs/*` 與 `/cs` 的 HTTP 行為。

整個應用程式以假的模型供應商組裝（沒有網路與金鑰），資料在記憶體 SQLite；登入以直接寫入 cookie 的方式進行，
CSRF 由測試 client 自動處理（專門測 CSRF 的測試改用 raw_client）。
"""
import json
import re
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base, ChatConversation, ChatMessage, LlmCallLog, User
from app.repositories.chat_repository import ChatRepository
from tests.support.auth import START, FakeClock, add_user
from tests.support.client import raw_client
from tests.support.idor import EndpointSpec, check_endpoint, logged_in_client
from tests.support.llm import FakeLLMProvider, Step

BASE = "/api/cs/conversations"

pytestmark = pytest.mark.usefixtures("generous_free_limits")      # 這個檔案測端點與流程，不是用量限制（見 conftest）


def uid(n: int) -> str:
    return str(uuid.UUID(int=n))


def reply_json(text="您好，請問有什麼可以協助您的嗎？", intent="greeting", confidence=0.9, entities=None, actions=None):
    return json.dumps({
        "intent": intent, "confidence": confidence,
        "entities": {"topic": None, "membership_tier": None, "account_issue": None, "feedback_type": None, **(entities or {})},
        "reply_text": text, "suggested_actions": actions or [],
    }, ensure_ascii=False)


def smart_provider():
    """對話呼叫回覆客服 JSON；摘要呼叫（指令含「累積摘要」）回覆摘要 JSON。"""
    def respond(request):
        if "累積摘要" in request.system_instruction:
            return {"summary": "先前的對話摘要。"}
        return json.loads(reply_json())

    return FakeLLMProvider(default=Step(respond))


def make_app(*, provider="default", environ=None, identity_provider=None):
    config = load_config(environ={"APP_ENV": "testing", **(environ or {})})
    kwargs = {} if provider is None else {"llm_provider": smart_provider() if provider == "default" else provider}
    if identity_provider is not None:
        kwargs["identity_provider"] = identity_provider
    app = create_app(config, clock=FakeClock(), **kwargs)
    Base.metadata.create_all(app.extensions["db_engine"])
    with Session(app.extensions["db_engine"]) as session:
        ids = {
            "alice": add_user(session, 1, age=30).id, "bob": add_user(session, 2, age=30).id,
            "admin": add_user(session, 3, age=30, role="admin").id,
            # 管理員沒有「待確認年齡」的登入狀態，只是沒確認前不能用會呼叫模型的功能（由 AccessPolicy 判斷）
            "fresh": add_user(session, 4, age=None, role="admin").id,
            # 玩家沒確認年齡時，除了年齡確認與登出什麼都不能做（受限登入狀態，在登入檢查就被擋下）
            "pending": add_user(session, 5, age=None).id,
        }
    app.ids = ids
    return app


@pytest.fixture()
def app():
    return make_app()


def user_of(app, name):
    with Session(app.extensions["db_engine"]) as session:
        return session.get(User, app.ids[name])


def client_for(app, name):
    return logged_in_client(app, user_of(app, name))


def new_conversation(app, name="alice", n=100):
    response = client_for(app, name).post(BASE, json={"client_id": uid(n)})
    assert response.status_code in (200, 201), response.get_data(as_text=True)
    return response.get_json()["conversation"]["id"]


def send(app, public_id, n=1, text="你好", name="alice"):
    return client_for(app, name).post(f"{BASE}/{public_id}/messages", json={"client_msg_id": uid(n), "content": text})


def count(app, model):
    with Session(app.extensions["db_engine"]) as session:
        return session.scalar(select(func.count()).select_from(model))


# ---- 未登入 ------------------------------------------------------------------------------


@pytest.mark.parametrize("method, path", [
    ("POST", BASE), ("GET", BASE), ("PATCH", f"{BASE}/{uid(1)}"), ("DELETE", f"{BASE}/{uid(1)}"),
    ("POST", f"{BASE}/{uid(1)}/messages"), ("GET", f"{BASE}/{uid(1)}/messages"),
])
def test_every_endpoint_requires_login(app, method, path):
    response = app.test_client().open(path, method=method, json={} if method in ("POST", "PATCH") else None)
    assert response.status_code == 401 and response.get_json()["error"]["code"] == "unauthorized"


# ---- 請求格式與 CSRF -----------------------------------------------------------------------


def test_a_form_body_is_415_and_a_bad_json_body_is_400(app):
    client = client_for(app, "alice")
    assert client.post(BASE, data={"client_id": uid(1)}).status_code == 415
    assert client.post(BASE, data="{not json", content_type="application/json").status_code == 400
    assert client.post(BASE, data="[1, 2]", content_type="application/json").status_code == 400


def test_a_body_over_64_kb_is_413(app):
    response = client_for(app, "alice").post(BASE, data=json.dumps({"client_id": "a" * (64 * 1024 + 10)}), content_type="application/json")
    assert response.status_code == 413


@pytest.mark.parametrize("method, path", [("POST", BASE), ("PATCH", f"{BASE}/{uid(1)}"), ("DELETE", f"{BASE}/{uid(1)}"), ("POST", f"{BASE}/{uid(1)}/messages")])
def test_a_write_without_a_csrf_token_is_403_and_changes_nothing(app, method, path):
    user = user_of(app, "alice")
    client = raw_client(app)
    with client.session_transaction() as session:
        session["uid"], session["tv"] = user.id, user.token_version
    response = client.open(path, method=method, json={"client_id": uid(7), "title": "x", "state_version": 1, "client_msg_id": uid(8), "content": "嗨"})
    assert response.status_code == 403
    assert count(app, ChatConversation) == 0 and count(app, ChatMessage) == 0


# ---- 建立與列出 ------------------------------------------------------------------------------


def test_creating_returns_201_then_200_for_the_same_client_id_with_the_same_conversation(app):
    client = client_for(app, "alice")
    first = client.post(BASE, json={"client_id": uid(1)})
    again = client.post(BASE, json={"client_id": uid(1)})
    assert (first.status_code, again.status_code) == (201, 200)
    assert first.get_json() == again.get_json() and count(app, ChatConversation) == 1
    conversation = first.get_json()["conversation"]
    assert set(conversation) == {"id", "title", "state_version", "updated_at", "created_at"}
    assert conversation["title"] == "新對話" and conversation["state_version"] == 1
    assert re.fullmatch(r"[0-9a-f-]{36}", conversation["id"]) and "user_id" not in conversation


@pytest.mark.parametrize("body", [{}, {"client_id": "abc"}, {"client_id": 5}, {"client_id": None}])
def test_a_bad_client_id_is_400_naming_the_field(app, body):
    response = client_for(app, "alice").post(BASE, json=body)
    assert response.status_code == 400 and "client_id" in response.get_json()["error"]["fields"]
    assert count(app, ChatConversation) == 0


def test_a_member_who_has_not_confirmed_their_age_gets_403_with_the_dedicated_code(app):
    response = client_for(app, "fresh").post(BASE, json={"client_id": uid(1)})
    assert response.status_code == 403 and response.get_json()["error"]["code"] == "age_confirmation_required"
    assert count(app, ChatConversation) == 0


def test_a_player_without_the_age_confirmation_is_stopped_at_the_login_gate_everywhere_under_api_cs(app):
    client = client_for(app, "pending")
    for response in (client.post(BASE, json={"client_id": uid(1)}), client.get(BASE)):
        assert response.status_code == 403 and response.get_json()["error"]["code"] == "age_confirmation_required"
    page = client.get("/cs")
    assert page.status_code == 302 and page.headers["Location"] == "/age"
    assert count(app, ChatConversation) == 0


def test_listing_shows_only_my_conversations(app):
    new_conversation(app, "alice", 1)
    new_conversation(app, "alice", 2)
    new_conversation(app, "bob", 3)
    body = client_for(app, "alice").get(BASE).get_json()
    assert len(body["conversations"]) == 2 and set(body) == {"conversations"}
    assert len(client_for(app, "bob").get(BASE).get_json()["conversations"]) == 1


def test_listing_pages(app):
    for n in range(1, 6):
        new_conversation(app, "alice", n)
    client = client_for(app, "alice")
    first = client.get(f"{BASE}?limit=2&offset=0").get_json()["conversations"]
    second = client.get(f"{BASE}?limit=2&offset=2").get_json()["conversations"]
    third = client.get(f"{BASE}?limit=2&offset=4").get_json()["conversations"]
    ids = [c["id"] for c in first + second + third]
    assert [len(first), len(second), len(third)] == [2, 2, 1] and len(set(ids)) == 5


@pytest.mark.parametrize("query, field", [("limit=0", "limit"), ("limit=101", "limit"), ("limit=abc", "limit"), ("offset=-1", "offset"), ("offset=x", "offset")])
def test_bad_paging_parameters_are_400_naming_the_field(app, query, field):
    response = client_for(app, "alice").get(f"{BASE}?{query}")
    assert response.status_code == 400 and field in response.get_json()["error"]["fields"]


def test_listing_still_works_for_a_member_who_can_no_longer_chat(app):
    assert client_for(app, "fresh").get(BASE).status_code == 200            # 列表不過閘門（頁面載入時需要）


# ---- 送出訊息 ----------------------------------------------------------------------------------


def test_sending_returns_both_messages_and_the_conversation_without_any_internal_field(app):
    public_id = new_conversation(app)
    action = {"type": "quick_reply", "label": "忘記密碼", "text": "我忘記密碼了"}
    app.extensions["llm_provider"].script(Step(reply_json("您好！歡迎使用客服。", intent="account_help", entities={"account_issue": "forgot_password"}, actions=[action])))
    response = send(app, public_id, 1, "你好")
    assert response.status_code == 200
    body = response.get_json()
    assert set(body) == {"user_message", "assistant_message", "conversation"}
    assert (body["user_message"]["seq"], body["user_message"]["role"], body["user_message"]["content"]) == (1, "user", "你好")
    assert (body["assistant_message"]["seq"], body["assistant_message"]["role"]) == (2, "assistant")
    assert body["assistant_message"]["content"] == "您好！歡迎使用客服。" and body["assistant_message"]["suggested_actions"] == [action]
    assert body["assistant_message"]["msg_status"] == "ok" and body["conversation"]["title"] == "你好"
    text = response.get_data(as_text=True)
    for forbidden in ("intent", "entities", "confidence", "account_help", "forgot_password", "summary", "state_json", "user_id"):
        assert forbidden not in text


def test_resending_returns_the_same_result_and_calls_the_model_once(app):
    public_id = new_conversation(app)
    first = send(app, public_id, 1, "你好")
    again = send(app, public_id, 1, "你好")
    assert first.get_json() == again.get_json() and len(app.extensions["llm_provider"].calls) == 1
    assert count(app, ChatMessage) == 2


def test_the_same_id_with_other_content_is_409_message_id_reused(app):
    public_id = new_conversation(app)
    send(app, public_id, 1, "你好")
    response = send(app, public_id, 1, "完全不同")
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "message_id_reused"
    assert count(app, ChatMessage) == 2


def test_a_busy_conversation_is_409_with_no_writes_and_no_call(app):
    public_id = new_conversation(app)
    with Session(app.extensions["db_engine"]) as session:
        repo = ChatRepository(session)
        conversation = repo.get_conversation(app.ids["alice"], public_id)
        assert repo.acquire_processing(app.ids["alice"], conversation, now=START - timedelta(seconds=30), stale_before=START - timedelta(seconds=90))
        session.commit()
    response = send(app, public_id, 1, "你好")
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "conversation_busy"
    assert count(app, ChatMessage) == 0 and app.extensions["llm_provider"].calls == []


@pytest.mark.parametrize("content", ["", "   ", "\n\t", None, 5])
def test_a_blank_message_is_400_with_no_writes_and_no_call(app, content):
    public_id = new_conversation(app)
    response = client_for(app, "alice").post(f"{BASE}/{public_id}/messages", json={"client_msg_id": uid(1), "content": content})
    assert response.status_code == 400 and "content" in response.get_json()["error"]["fields"]
    assert count(app, ChatMessage) == 0 and app.extensions["llm_provider"].calls == []


def test_a_message_of_1001_characters_is_400_and_1000_is_accepted(app):
    public_id = new_conversation(app)
    assert send(app, public_id, 1, "字" * 1001).status_code == 400
    assert send(app, public_id, 2, "字" * 1000).status_code == 200


def test_a_degraded_reply_is_200_and_marked(app):
    public_id = new_conversation(app)
    app.extensions["llm_provider"].script(Step("不是 JSON"), Step("還是不是"))
    body = send(app, public_id, 1, "你好").get_json()
    assert body["assistant_message"]["msg_status"] == "fallback" and body["assistant_message"]["content"].startswith("抱歉")
    assert body["assistant_message"]["suggested_actions"] == []


def test_the_age_gate_refuses_a_send_before_anything_is_written(app):
    public_id = new_conversation(app)
    with Session(app.extensions["db_engine"]) as session:                    # alice 的生日被清除，等同尚未完成年齡確認
        alice = session.get(User, app.ids["alice"])
        alice.birth_date = None
        session.commit()
    response = send(app, public_id, 1, "你好")
    assert response.status_code == 403 and response.get_json()["error"]["code"] == "age_confirmation_required"
    assert count(app, ChatMessage) == 0 and app.extensions["llm_provider"].calls == []


def test_without_a_model_provider_sending_is_503_and_writes_nothing_while_reading_works():
    app = make_app(provider=None)
    public_id = new_conversation(app)
    response = send(app, public_id, 1, "你好")
    assert response.status_code == 503 and response.get_json()["error"] == {
        "code": "service_unavailable", "message": "服務暫時無法使用，請稍後再試。",
    }
    assert count(app, ChatMessage) == 0 and count(app, LlmCallLog) == 0
    assert client_for(app, "alice").get(BASE).status_code == 200
    assert client_for(app, "alice").get(f"{BASE}/{public_id}/messages").get_json() == {"messages": [], "has_more": False}


# ---- 拉取訊息 ----------------------------------------------------------------------------------


def test_messages_are_pulled_incrementally_by_sequence(app):
    public_id = new_conversation(app)
    send(app, public_id, 1, "第一句")
    send(app, public_id, 2, "第二句")
    client = client_for(app, "alice")
    everything = client.get(f"{BASE}/{public_id}/messages").get_json()
    assert [m["seq"] for m in everything["messages"]] == [1, 2, 3, 4] and everything["has_more"] is False
    assert [m["seq"] for m in client.get(f"{BASE}/{public_id}/messages?since_seq=2").get_json()["messages"]] == [3, 4]
    assert client.get(f"{BASE}/{public_id}/messages?since_seq=4").get_json() == {"messages": [], "has_more": False}
    assert set(everything["messages"][1]) == {"seq", "client_msg_id", "role", "content", "msg_status", "suggested_actions", "created_at"}


def test_has_more_is_reported_when_the_limit_is_smaller_than_what_is_left(app):
    public_id = new_conversation(app)
    send(app, public_id, 1, "第一句")
    page = client_for(app, "alice").get(f"{BASE}/{public_id}/messages?limit=1").get_json()
    assert len(page["messages"]) == 1 and page["has_more"] is True


@pytest.mark.parametrize("query, field", [("since_seq=-1", "since_seq"), ("since_seq=x", "since_seq"), ("limit=0", "limit"), ("limit=201", "limit")])
def test_bad_pull_parameters_are_400(app, query, field):
    public_id = new_conversation(app)
    response = client_for(app, "alice").get(f"{BASE}/{public_id}/messages?{query}")
    assert response.status_code == 400 and field in response.get_json()["error"]["fields"]


# ---- 改名與刪除 --------------------------------------------------------------------------------


def test_renaming_with_the_current_version_succeeds_and_a_stale_version_is_409_with_the_latest_conversation(app):
    public_id = new_conversation(app)
    client = client_for(app, "alice")
    renamed = client.patch(f"{BASE}/{public_id}", json={"title": "我的標題", "state_version": 1})
    assert renamed.status_code == 200 and renamed.get_json()["conversation"]["state_version"] == 2
    stale = client.patch(f"{BASE}/{public_id}", json={"title": "另一個", "state_version": 1})
    body = stale.get_json()
    assert stale.status_code == 409 and body["error"]["code"] == "version_conflict"
    assert body["conversation"]["title"] == "我的標題" and body["conversation"]["state_version"] == 2


@pytest.mark.parametrize("body, field", [({"title": "", "state_version": 1}, "title"), ({"title": "x" * 101, "state_version": 1}, "title"), ({"title": "ok", "state_version": "1"}, "state_version"), ({"title": "ok"}, "state_version")])
def test_a_bad_rename_is_400_naming_the_field(app, body, field):
    public_id = new_conversation(app)
    response = client_for(app, "alice").patch(f"{BASE}/{public_id}", json=body)
    assert response.status_code == 400 and field in response.get_json()["error"]["fields"]


def test_deleting_removes_the_conversation_and_its_messages(app):
    public_id = new_conversation(app)
    send(app, public_id, 1, "你好")
    client = client_for(app, "alice")
    assert client.delete(f"{BASE}/{public_id}").get_json() == {"ok": True}
    assert count(app, ChatConversation) == 0 and count(app, ChatMessage) == 0
    assert client.get(f"{BASE}/{public_id}/messages").status_code == 404


# ---- 權限：擁有者條件 ----------------------------------------------------------------------------


def make_resource(app, owner):
    with Session(app.extensions["db_engine"]) as session:
        from app.services.cs_context import initial_state
        repo = ChatRepository(session)
        conversation = repo.create_conversation(owner.id, client_id=str(uuid.uuid4()), state=initial_state("free"), now=START)
        session.commit()
        return conversation.public_id


@pytest.mark.parametrize("spec", [
    EndpointSpec("PATCH", BASE + "/{id}", create_resource=make_resource, json={"title": "新標題", "state_version": 1}, description="改名"),
    EndpointSpec("DELETE", BASE + "/{id}", create_resource=make_resource, description="刪除"),
    EndpointSpec("POST", BASE + "/{id}/messages", create_resource=make_resource, json={"client_msg_id": uid(1), "content": "你好"}, description="送訊息"),
    EndpointSpec("GET", BASE + "/{id}/messages", create_resource=make_resource, description="拉訊息"),
], ids=lambda spec: spec.description)
def test_someone_elses_conversation_is_a_byte_identical_404_to_a_missing_one(app, spec):
    check_endpoint(app, spec, owner=user_of(app, "alice"), intruder=user_of(app, "bob"))


def test_an_intruders_write_attempts_changed_nothing(app):
    public_id = make_resource(app, user_of(app, "alice"))
    bob = client_for(app, "bob")
    assert bob.patch(f"{BASE}/{public_id}", json={"title": "被改了", "state_version": 1}).status_code == 404
    assert bob.post(f"{BASE}/{public_id}/messages", json={"client_msg_id": uid(1), "content": "偷偷送"}).status_code == 404
    assert bob.delete(f"{BASE}/{public_id}").status_code == 404
    with Session(app.extensions["db_engine"]) as session:
        conversation = session.scalar(select(ChatConversation))
        assert (conversation.title, conversation.state_version) == ("新對話", 1)
    assert count(app, ChatMessage) == 0 and app.extensions["llm_provider"].calls == []


def test_an_admin_reading_or_writing_someone_elses_conversation_gets_404_like_anyone_else(app):
    public_id = new_conversation(app)
    send(app, public_id, 1, "私人內容")
    admin = client_for(app, "admin")
    missing = admin.get(f"{BASE}/{uuid.uuid4()}/messages")
    for response in (
        admin.get(f"{BASE}/{public_id}/messages"), admin.patch(f"{BASE}/{public_id}", json={"title": "x", "state_version": 1}),
        admin.delete(f"{BASE}/{public_id}"), admin.post(f"{BASE}/{public_id}/messages", json={"client_msg_id": uid(9), "content": "x"}),
    ):
        assert response.status_code == 404 and response.get_json() == missing.get_json()
    assert "私人內容" not in admin.get(BASE).get_data(as_text=True)


# ---- 快取 --------------------------------------------------------------------------------------


def test_every_response_under_api_cs_is_no_store_including_errors(app):
    public_id = new_conversation(app)
    anonymous = app.test_client()
    client = client_for(app, "alice")
    responses = [
        client.get(BASE), client.post(BASE, json={"client_id": uid(5)}), client.get(f"{BASE}/{public_id}/messages"),
        send(app, public_id, 1), client.get(f"{BASE}/{uuid.uuid4()}/messages"), client.post(BASE, json={}),
        anonymous.get(BASE), client_for(app, "fresh").post(BASE, json={"client_id": uid(6)}),
        send(app, public_id, 1, "不同內容"),
    ]
    assert {r.status_code for r in responses} >= {200, 201, 400, 401, 403, 404, 409}
    assert all(r.headers["Cache-Control"] == "no-store" for r in responses)


# ---- 摘要的接線：第 11 輪的回應之後，以自己的 Session 折疊第 1 輪 ----------------------------------------


def test_after_the_eleventh_round_the_first_is_folded_into_the_summary_after_the_response(app):
    public_id = new_conversation(app)
    for n in range(1, 11):
        send(app, public_id, n, f"問題{n}")
    with Session(app.extensions["db_engine"]) as session:
        assert session.scalar(select(ChatConversation.summary)) is None            # 10 輪以內不摘要
    send(app, public_id, 11, "問題11")
    with Session(app.extensions["db_engine"]) as session:
        conversation = session.scalar(select(ChatConversation))
        assert (conversation.summary, conversation.summary_upto_seq) == ("先前的對話摘要。", 2)
        kinds = list(session.scalars(select(LlmCallLog.kind).order_by(LlmCallLog.id)))
        assert kinds.count("summary") == 1 and kinds.count("chat") == 11
        assert session.scalar(select(func.count()).select_from(LlmCallLog).where(LlmCallLog.kind == "summary", LlmCallLog.counted == True)) == 0   # noqa: E712


def test_the_next_request_after_a_failed_summary_catches_up(app):
    public_id = new_conversation(app)
    provider = app.extensions["llm_provider"]
    for n in range(1, 11):
        send(app, public_id, n, f"問題{n}")
    default = provider._default
    provider._default = Step(lambda request: (_ for _ in ()).throw(RuntimeError("摘要壞了")) if "累積摘要" in request.system_instruction else json.loads(reply_json()))
    assert send(app, public_id, 11, "問題11").status_code == 200                  # 摘要失敗不影響回覆（延後動作的錯誤只記錄）
    with Session(app.extensions["db_engine"]) as session:
        assert session.scalar(select(ChatConversation.summary_upto_seq)) == 0
    provider._default = default
    send(app, public_id, 12, "問題12")                                             # 下一次回覆後補做：12 輪 → 折疊 2 輪
    with Session(app.extensions["db_engine"]) as session:
        assert session.scalar(select(ChatConversation.summary_upto_seq)) == 4


def test_a_degraded_reply_does_not_schedule_a_summary(app):
    public_id = new_conversation(app)
    for n in range(1, 11):
        send(app, public_id, n, f"問題{n}")
    app.extensions["llm_provider"].script(Step("壞"), Step("還是壞"))
    send(app, public_id, 11, "問題11")
    with Session(app.extensions["db_engine"]) as session:
        assert session.scalar(select(LlmCallLog.kind).where(LlmCallLog.kind == "summary")) is None


# ---- /cs 頁面 ---------------------------------------------------------------------------------


def initial_state_of(response):
    match = re.search(r'<script type="application/json" id="initial-state">(.*?)</script>', response.get_data(as_text=True), re.S)
    assert match, "頁面沒有初始狀態"
    return json.loads(match.group(1))


def test_the_page_redirects_anonymous_visitors_to_login_with_the_next_address(app):
    response = app.test_client().get("/cs")
    assert response.status_code == 302 and response.headers["Location"] == "/login?next=%2Fcs"


def test_the_page_carries_the_initial_state_the_front_end_needs(app):
    response = client_for(app, "alice").get("/cs")
    assert response.status_code == 200 and response.mimetype == "text/html" and response.headers["Cache-Control"] == "no-store"
    state = initial_state_of(response)
    user = user_of(app, "alice")
    assert state["chat_gate"] is None and state["service_available"] is True
    assert state["request_timeout_ms"] == 35000 and state["max_message_chars"] == 1000
    assert state["user"] == {"public_id": user.public_id, "display_name": user.display_name, "member_tier": "free"}
    text = response.get_data(as_text=True)
    assert 'lang="zh-Hant-TW"' in text and "user1@example.com" not in text


def test_the_timeout_follows_the_servers_total_budget_plus_five_seconds():
    app = make_app(environ={"LLM_REQUEST_BUDGET_SECONDS": "40", "LLM_CALL_TIMEOUT_SECONDS": "25"})
    assert initial_state_of(client_for(app, "alice").get("/cs"))["request_timeout_ms"] == 45000


def test_the_page_reports_the_gate_result_computed_by_the_policy(app):
    assert initial_state_of(client_for(app, "fresh").get("/cs"))["chat_gate"] == "age_confirmation_required"


def test_the_page_says_when_no_model_provider_is_available():
    app = make_app(provider=None)
    assert initial_state_of(client_for(app, "alice").get("/cs"))["service_available"] is False


def test_the_initial_state_cannot_break_out_of_its_script_block(app):
    with Session(app.extensions["db_engine"]) as session:
        alice = session.get(User, app.ids["alice"])
        alice.display_name = "</script><script>alert(1)</script>"
        session.commit()
    text = client_for(app, "alice").get("/cs").get_data(as_text=True)
    assert "</script><script>alert(1)" not in text and "\\u003c/script\\u003e" in text
