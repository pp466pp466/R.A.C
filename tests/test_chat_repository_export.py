"""personal-data-export 規格：匯出用的對話讀取（只回傳擁有者的資料、順序、含沒有任何訊息的對話）。

訊息沿用既有的 `messages_after` 分頁讀取（序號游標、帶擁有者條件），這裡只驗證匯出新增的 `export_conversations`。
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.repositories.chat_repository import ChatRepository
from tests.support.auth import add_user

T0 = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)
STATE = {"member_tier": "free", "last_intent": None, "pending_entities": {}, "open_issue": None}


def uuid_like(n: int) -> str:
    return f"{n:036d}"


@pytest.fixture()
def repo(member_session):
    return ChatRepository(member_session)


def test_only_the_owners_conversations_are_returned_oldest_first(member_session, repo):
    alice, bob = add_user(member_session, 1).id, add_user(member_session, 2).id
    second = repo.create_conversation(alice, client_id=uuid_like(2), state=STATE, now=T0 + timedelta(minutes=5))
    first = repo.create_conversation(alice, client_id=uuid_like(1), state=STATE, now=T0)
    repo.create_conversation(bob, client_id=uuid_like(3), state=STATE, now=T0)

    found = repo.export_conversations(alice)
    assert [c.id for c in found] == [first.id, second.id]
    assert all(c.user_id == alice for c in found)


def test_a_conversation_without_messages_is_still_exported(member_session, repo):
    alice = add_user(member_session, 1).id
    empty = repo.create_conversation(alice, client_id=uuid_like(1), state=STATE, now=T0)
    assert [c.id for c in repo.export_conversations(alice)] == [empty.id]
    assert repo.messages_after(alice, empty, after_seq=0, limit=500) == ([], False)


def test_a_user_with_no_conversations_gets_an_empty_list(member_session, repo):
    assert repo.export_conversations(add_user(member_session, 1).id) == []


def test_messages_are_paged_by_sequence_and_only_for_the_owner(member_session, repo):
    alice, bob = add_user(member_session, 1).id, add_user(member_session, 2).id
    conv = repo.create_conversation(alice, client_id=uuid_like(1), state=STATE, now=T0)
    for n in range(1, 8):
        repo.append_message(alice, conv, client_msg_id=uuid_like(100 + n), role="user", content=f"第{n}則", now=T0 + timedelta(seconds=n))

    seen, after, pages = [], 0, 0
    while True:
        page, more = repo.messages_after(alice, conv, after_seq=after, limit=3)
        seen += [m.seq for m in page]
        pages += 1
        if not more:
            break
        after = page[-1].seq
    assert seen == [1, 2, 3, 4, 5, 6, 7] and pages == 3
    assert repo.messages_after(bob, conv, after_seq=0, limit=500) == ([], False)       # 別人的對話讀不到任何訊息
