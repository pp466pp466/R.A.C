"""admin-content-review 規格：兩類可讀的對話（意見回饋、被標記）、資格、讀取、刪除與模型失敗的原文。"""
from dataclasses import fields
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.models import ChatConversation, ChatMessage, LlmCallLog, User
from app.repositories.admin_repository import (
    AdminRepository, FailureRow, FeedbackRow, FlaggedRow, ReviewableConversation,
)
from app.repositories.chat_repository import ChatRepository
from app.repositories.llm_call_log_repository import LlmCallLogRepository
from tests.support.auth import add_user

T0 = datetime(2026, 10, 29, 4, 0, tzinfo=timezone.utc)
STATE = {"member_tier": "free", "last_intent": None, "pending_entities": {}, "open_issue": None}
INTENTS = ("complaint_feedback", "human_handoff")
SECRET = "只該出現在讀取全文時的私密內容"


@pytest.fixture()
def repo(member_session):
    return AdminRepository(member_session)


@pytest.fixture()
def chats(member_session):
    return ChatRepository(member_session)


@pytest.fixture()
def ledger(member_session):
    return LlmCallLogRepository(member_session)


class World:
    def __init__(self, session, chats, ledger):
        self.session, self.chats, self.ledger, self.n = session, chats, ledger, 0

    def conversation(self, user, *, turns=(), at=T0):
        """對話與其中的輪：`turns` 是助理回覆的意圖（None 代表降級回覆）。"""
        self.n += 1
        conv = self.chats.create_conversation(user.id, client_id=f"{self.n:036d}", state=STATE, now=at)
        for index, intent in enumerate(turns):
            self.n += 1
            self.chats.append_message(user.id, conv, client_msg_id=f"{self.n:036d}", role="user", content=f"{SECRET}{index}", now=at)
            self.n += 1
            self.chats.append_message(
                user.id, conv, client_msg_id=f"{self.n:036d}", role="assistant", content=f"回覆{index}", intent=intent,
                msg_status="ok" if intent else "fallback", now=at + timedelta(seconds=1),
            )
        self.session.commit()
        return conv

    def block(self, user, conv, *, kind="chat", at=T0):
        self.ledger.add(
            user_id=user.id, conversation_id=conv.id if conv is not None else None, kind=kind, provider="f", model="m", attempt=1,
            status="blocked", counted=False, latency_ms=1, created_at=at,
        )
        self.session.commit()


@pytest.fixture()
def world(member_session, chats, ledger):
    return World(member_session, chats, ledger)


# ---- 意見回饋清單 ---------------------------------------------------------------------------

def test_the_feedback_list_has_conversations_with_a_feedback_or_handoff_reply_newest_first(repo, world, member_session):
    alice, bob = add_user(member_session, 1), add_user(member_session, 2)
    old = world.conversation(alice, turns=("complaint_feedback",), at=T0)
    new = world.conversation(bob, turns=("general_qa", "human_handoff", "complaint_feedback"), at=T0 + timedelta(hours=1))
    world.conversation(alice, turns=("general_qa", "account_help", "unknown"), at=T0 + timedelta(hours=2))      # 沒有回饋
    rows = repo.feedback_conversations(intents=INTENTS, limit=10, offset=0)
    assert [(r.conversation_public_id, r.user_public_id, r.count) for r in rows] == [
        (new.public_id, bob.public_id, 2), (old.public_id, alice.public_id, 1),
    ]
    assert rows[0].latest_at > rows[1].latest_at and all(r.latest_at.tzinfo is not None for r in rows)


def test_a_conversation_with_only_unknown_replies_is_not_feedback(repo, world, member_session):
    user = add_user(member_session, 1)
    world.conversation(user, turns=("unknown", "unknown", None))
    assert repo.feedback_conversations(intents=INTENTS, limit=10, offset=0) == []


def test_which_intents_count_is_decided_by_the_caller(repo, world, member_session):
    user = add_user(member_session, 1)
    world.conversation(user, turns=("human_handoff",))
    assert len(repo.feedback_conversations(intents=("human_handoff",), limit=10, offset=0)) == 1
    assert repo.feedback_conversations(intents=("complaint_feedback",), limit=10, offset=0) == []
    assert repo.feedback_conversations(intents=(), limit=10, offset=0) == []


def test_user_messages_with_a_feedback_looking_text_do_not_count_only_assistant_intents_do(repo, world, member_session):
    user = add_user(member_session, 1)
    conv = world.conversation(user, turns=())
    world.chats.append_message(user.id, conv, client_msg_id="9" * 36, role="user", content="complaint_feedback", intent="complaint_feedback", now=T0)
    member_session.commit()
    assert repo.feedback_conversations(intents=INTENTS, limit=10, offset=0) == []


def test_the_feedback_list_pages_and_has_no_content_fields(repo, world, member_session):
    user = add_user(member_session, 1)
    made = [world.conversation(user, turns=("complaint_feedback",), at=T0 + timedelta(minutes=m)) for m in range(5)]
    ids = [c.public_id for c in reversed(made)]
    assert [r.conversation_public_id for r in repo.feedback_conversations(intents=INTENTS, limit=2, offset=0)] == ids[:2]
    assert [r.conversation_public_id for r in repo.feedback_conversations(intents=INTENTS, limit=2, offset=4)] == ids[4:]
    assert {f.name for f in fields(FeedbackRow)} == {"conversation_public_id", "user_public_id", "count", "latest_at"}
    assert SECRET not in repr(repo.feedback_conversations(intents=INTENTS, limit=10, offset=0))


# ---- 被標記清單 -----------------------------------------------------------------------------

def test_the_flagged_list_has_conversations_with_blocked_chat_calls_newest_first(repo, world, member_session):
    alice, bob = add_user(member_session, 1), add_user(member_session, 2)
    first = world.conversation(alice, turns=(None,))
    second = world.conversation(bob, turns=(None,))
    world.block(alice, first, at=T0)
    world.block(alice, first, at=T0 + timedelta(minutes=5))
    world.block(bob, second, at=T0 + timedelta(minutes=9))
    rows = repo.flagged_conversations(limit=10, offset=0)
    assert [(r.conversation_public_id, r.user_public_id, r.blocked_count) for r in rows] == [
        (second.public_id, bob.public_id, 1), (first.public_id, alice.public_id, 2),
    ]
    assert rows[1].latest_at == T0 + timedelta(minutes=5) and rows[0].user_id == bob.id


def test_blocked_summary_calls_do_not_flag_a_conversation(repo, world, member_session):
    user = add_user(member_session, 1)
    conv = world.conversation(user, turns=("general_qa",))
    world.block(user, conv, kind="summary")
    assert repo.flagged_conversations(limit=10, offset=0) == []


def test_a_blocked_call_without_a_conversation_flags_nothing(repo, world, member_session):
    user = add_user(member_session, 1)
    world.block(user, None)
    assert repo.flagged_conversations(limit=10, offset=0) == []


def test_the_flagged_list_pages_and_carries_no_message_content(repo, world, member_session):
    user = add_user(member_session, 1)
    for m in range(3):
        conv = world.conversation(user, turns=(None,))
        world.block(user, conv, at=T0 + timedelta(minutes=m))
    assert len(repo.flagged_conversations(limit=2, offset=0)) == 2 and len(repo.flagged_conversations(limit=2, offset=2)) == 1
    assert SECRET not in repr(repo.flagged_conversations(limit=10, offset=0))
    assert {f.name for f in fields(FlaggedRow)} == {"conversation_public_id", "user_id", "user_public_id", "blocked_count", "latest_at"}


# ---- 資格 -----------------------------------------------------------------------------------

def test_a_feedback_conversation_is_reviewable_and_says_why(repo, world, member_session):
    user = add_user(member_session, 1)
    conv = world.conversation(user, turns=("complaint_feedback",))
    found = repo.find_reviewable(conv.public_id, intents=INTENTS)
    assert isinstance(found, ReviewableConversation)
    assert (found.id, found.user_public_id, found.is_feedback, found.is_flagged) == (conv.id, user.public_id, True, False)


def test_a_flagged_conversation_is_reviewable_and_says_why(repo, world, member_session):
    user = add_user(member_session, 1)
    conv = world.conversation(user, turns=("general_qa",))
    world.block(user, conv)
    found = repo.find_reviewable(conv.public_id, intents=INTENTS)
    assert (found.is_feedback, found.is_flagged) == (False, True)


def test_a_conversation_can_be_both(repo, world, member_session):
    user = add_user(member_session, 1)
    conv = world.conversation(user, turns=("human_handoff",))
    world.block(user, conv)
    found = repo.find_reviewable(conv.public_id, intents=INTENTS)
    assert (found.is_feedback, found.is_flagged) == (True, True)


@pytest.mark.parametrize("turns", [(), ("general_qa",), ("unknown", "unknown"), ("greeting", None)])
def test_an_ordinary_conversation_is_not_reviewable(repo, world, member_session, turns):
    user = add_user(member_session, 1)
    conv = world.conversation(user, turns=turns)
    assert repo.find_reviewable(conv.public_id, intents=INTENTS) is None


@pytest.mark.parametrize("public_id", ["00000000-0000-4000-8000-000000000000", "", "x" * 36, "%"])
def test_an_unknown_id_is_not_reviewable(repo, public_id):
    assert repo.find_reviewable(public_id, intents=INTENTS) is None


def test_the_list_and_the_eligibility_rule_agree_for_every_conversation(repo, world, member_session):
    """清單裡有的才讀得到，反之亦然：資格與清單用的是同一組條件。"""
    user = add_user(member_session, 1)
    convs = [
        world.conversation(user, turns=("complaint_feedback",)),
        world.conversation(user, turns=("general_qa",)),
        world.conversation(user, turns=("unknown",)),
        world.conversation(user, turns=("human_handoff", "general_qa")),
    ]
    world.block(user, convs[2])
    listed = {r.conversation_public_id for r in repo.feedback_conversations(intents=INTENTS, limit=50, offset=0)}
    listed |= {r.conversation_public_id for r in repo.flagged_conversations(limit=50, offset=0)}
    reviewable = {c.public_id for c in convs if repo.find_reviewable(c.public_id, intents=INTENTS)}
    assert listed == reviewable == {convs[0].public_id, convs[2].public_id, convs[3].public_id}


# ---- 讀取全文 -------------------------------------------------------------------------------

def test_messages_come_back_in_sequence_order_with_their_fields(repo, world, member_session):
    user = add_user(member_session, 1)
    conv = world.conversation(user, turns=("complaint_feedback", None))
    rows, more = repo.read_messages(conv.id, limit=200)
    assert [(m.seq, m.role) for m in rows] == [(1, "user"), (2, "assistant"), (3, "user"), (4, "assistant")]
    assert rows[0].content == f"{SECRET}0" and rows[3].msg_status == "fallback" and not more
    assert all(m.created_at.tzinfo is not None for m in rows)


def test_reading_stops_at_the_limit_and_says_there_is_more(repo, world, member_session):
    user = add_user(member_session, 1)
    conv = world.conversation(user, turns=("general_qa",) * 5)               # 10 則
    rows, more = repo.read_messages(conv.id, limit=4)
    assert [m.seq for m in rows] == [1, 2, 3, 4] and more is True
    rows, more = repo.read_messages(conv.id, limit=10)
    assert len(rows) == 10 and more is False


def test_reading_returns_only_that_conversations_messages(repo, world, member_session):
    user = add_user(member_session, 1)
    one, two = world.conversation(user, turns=("general_qa",)), world.conversation(user, turns=("general_qa", "general_qa"))
    assert len(repo.read_messages(one.id, limit=50)[0]) == 2 and len(repo.read_messages(two.id, limit=50)[0]) == 4


# ---- 刪除 -----------------------------------------------------------------------------------

def test_deleting_removes_the_conversation_and_messages_but_keeps_the_ledger(repo, world, member_session):
    user = add_user(member_session, 1)
    conv = world.conversation(user, turns=("complaint_feedback",))
    other = world.conversation(user, turns=("general_qa",))
    world.block(user, conv)
    ledger_before = member_session.scalar(select(func.count()).select_from(LlmCallLog))
    conv_id, other_id = conv.id, other.id                       # 刪除會讓 Session 內的物件失效（與使用者自己刪除對話相同）
    assert repo.delete_conversation(conv_id) is True
    assert member_session.scalar(select(func.count()).select_from(LlmCallLog)) == ledger_before
    assert member_session.scalar(select(func.count()).select_from(ChatMessage).where(ChatMessage.conversation_id == conv_id)) == 0
    assert member_session.scalar(select(func.count()).select_from(ChatConversation).where(ChatConversation.id == conv_id)) == 0
    assert member_session.scalar(select(func.count()).select_from(ChatConversation).where(ChatConversation.id == other_id)) == 1
    assert member_session.scalar(select(func.count()).select_from(LlmCallLog).where(LlmCallLog.conversation_id == conv_id)) == 0
    assert repo.flagged_conversations(limit=10, offset=0) == []


def test_deleting_does_not_leave_the_ledger_pointing_at_a_reused_id(repo, world, member_session):
    user = add_user(member_session, 1)
    conv = world.conversation(user, turns=(None,))
    world.block(user, conv)
    user_id = user.id
    repo.delete_conversation(conv.id)
    member_session.commit()
    newcomer = world.conversation(member_session.get(User, user_id), turns=("general_qa",))
    assert repo.find_reviewable(newcomer.public_id, intents=INTENTS) is None          # 沒有被舊的被擋紀錄「繼承」


def test_deleting_a_conversation_that_is_gone_reports_false(repo, world, member_session):
    user = add_user(member_session, 1)
    conv = world.conversation(user, turns=("complaint_feedback",))
    conv_id = conv.id
    assert repo.delete_conversation(conv_id) is True
    assert repo.delete_conversation(conv_id) is False


# ---- 模型失敗的原文 -------------------------------------------------------------------------

def failure(ledger, session, *, at, status="validation_failed", detail="壞掉的模型輸出", kind="chat", model="m"):
    ledger.add(
        user_id=None, conversation_id=None, kind=kind, provider="f", model=model, attempt=1, status=status, counted=False,
        latency_ms=1, created_at=at, error_detail=detail,
    )
    session.commit()


def test_recent_failures_are_newest_first_and_limited_by_the_window_and_the_count(repo, ledger, member_session):
    now = T0 + timedelta(days=30)
    since = now - timedelta(days=14)
    failure(ledger, member_session, at=since - timedelta(seconds=1), detail="太舊")
    failure(ledger, member_session, at=since, detail="剛好在視窗內")
    failure(ledger, member_session, at=now - timedelta(days=1), detail="昨天", kind="summary", model="m2")
    failure(ledger, member_session, at=now, detail="現在")
    rows = repo.recent_failures(since=since, limit=100)
    assert [r.error_detail for r in rows] == ["現在", "昨天", "剛好在視窗內"]
    assert (rows[1].kind, rows[1].model) == ("summary", "m2")
    assert [r.error_detail for r in repo.recent_failures(since=since, limit=2)] == ["現在", "昨天"]


def test_only_validation_failures_with_a_saved_text_are_listed(repo, ledger, member_session):
    failure(ledger, member_session, at=T0, status="timeout", detail="逾時沒有原文")
    failure(ledger, member_session, at=T0, status="blocked", detail="被擋")
    failure(ledger, member_session, at=T0, status="ok", detail="成功")
    ledger.add(user_id=None, conversation_id=None, kind="chat", provider="f", model="m", attempt=1, status="validation_failed",
               counted=False, latency_ms=1, created_at=T0)                                 # 沒有保存原文
    member_session.commit()
    assert repo.recent_failures(since=T0 - timedelta(days=1), limit=10) == []


def test_a_failure_row_has_no_user_or_conversation_identity():
    assert {f.name for f in fields(FailureRow)} == {"created_at", "kind", "model", "error_detail"}
