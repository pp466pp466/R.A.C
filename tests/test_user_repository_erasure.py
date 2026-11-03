"""account-deletion 與 account-retention 規格：抹除用的 Repository 方法（寬限期到期的判斷、串聯刪除、帳本的匿名化）。

以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行。
"""
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import AuthToken, ChatConversation, ChatMessage, LlmCallLog, User, UserIdentity
from app.models.base import utcnow
from app.models.user import PROVIDER_GOOGLE, STATUS_ACTIVE, STATUS_DELETION_PENDING, TOKEN_RESET_PASSWORD
from app.repositories.user_repository import UserRepository
from tests.support.auth import START, add_user

GRACE = timedelta(days=7)
NOW = START
CUTOFF = NOW - GRACE            # 申請時間不晚於這個時間，就是寬限期已滿


def pending(session, n: int, *, requested_at, **overrides) -> User:
    return add_user(
        session, n, status=STATUS_DELETION_PENDING, deletion_requested_at=requested_at,
        status_before_deletion=STATUS_ACTIVE, **overrides,
    )


def ledger_row(session, user_id, conversation_id) -> LlmCallLog:
    row = LlmCallLog(
        user_id=user_id, conversation_id=conversation_id, kind="chat", provider="gemini", model="m", attempt=1,
        status="ok", counted=True, latency_ms=10, created_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return row


def conversation(session, user_id: int, n: int = 1) -> ChatConversation:
    conv = ChatConversation(user_id=user_id, client_id=f"00000000-0000-4000-8000-{n:012d}", state_json={})
    session.add(conv)
    session.flush()
    session.add(ChatMessage(conversation_id=conv.id, client_msg_id=f"m{n}", seq=1, role="user", content="哈囉"))
    session.flush()
    return conv


# ---- 到期的判斷 -------------------------------------------------------------------

def test_only_accounts_whose_grace_period_is_over_are_due(member_session):
    s = member_session
    exact = pending(s, 1, requested_at=CUTOFF)
    over = pending(s, 2, requested_at=CUTOFF - timedelta(days=30))
    one_second_short = pending(s, 3, requested_at=CUTOFF + timedelta(seconds=1))
    never_set = add_user(s, 4, status=STATUS_DELETION_PENDING)                          # 申請時間為空：永遠不視為到期
    active_with_stale_field = add_user(s, 5, deletion_requested_at=CUTOFF - timedelta(days=99))   # 狀態不是待刪除
    admin = add_user(s, 6, role="admin", status=STATUS_DELETION_PENDING, deletion_requested_at=CUTOFF - GRACE)
    users = UserRepository(s)

    assert users.find_pending_due_ids(CUTOFF) == [exact.id, over.id]
    assert users.count_pending_due(CUTOFF) == 2
    not_due = {one_second_short.id, never_set.id, active_with_stale_field.id, admin.id}
    assert not not_due & set(users.find_pending_due_ids(CUTOFF))


def test_find_pending_due_ids_is_ordered_and_limited_and_returns_only_ids(member_session):
    s = member_session
    ids = [pending(s, n, requested_at=CUTOFF - timedelta(days=n)).id for n in range(1, 6)]
    found = UserRepository(s).find_pending_due_ids(CUTOFF, limit=3)
    assert found == ids[:3]
    assert all(isinstance(i, int) for i in found)


# ---- 條件式刪除 -------------------------------------------------------------------

def test_delete_if_pending_due_rechecks_the_condition_at_deletion_time(member_session):
    s = member_session
    due = pending(s, 1, requested_at=CUTOFF - timedelta(days=1))
    cancelled = add_user(s, 2)                                   # 判斷之後剛好撤銷：狀態已回到 active
    not_yet = pending(s, 3, requested_at=CUTOFF + timedelta(hours=1))
    users = UserRepository(s)

    assert users.delete_if_pending_due(cancelled.id, CUTOFF) is False
    assert users.delete_if_pending_due(not_yet.id, CUTOFF) is False
    assert users.delete_if_pending_due(due.id, CUTOFF) is True
    assert users.delete_if_pending_due(due.id, CUTOFF) is False        # 已經不存在
    s.commit()
    s.expire_all()
    assert set(s.scalars(select(User.id))) == {cancelled.id, not_yet.id}


def test_deleting_cascades_to_identities_tokens_conversations_and_messages_and_anonymises_the_ledger(member_session):
    s = member_session
    gone = pending(s, 1, requested_at=CUTOFF - timedelta(days=1))
    keeper = add_user(s, 2)
    s.add(UserIdentity(user_id=gone.id, provider=PROVIDER_GOOGLE, provider_subject="sub-1"))
    s.add(AuthToken(user_id=gone.id, purpose=TOKEN_RESET_PASSWORD, token_hash="a" * 64, expires_at=NOW))
    gone_conv = conversation(s, gone.id, 1)
    keeper_conv = conversation(s, keeper.id, 2)
    ledger_row(s, gone.id, gone_conv.id)
    ledger_row(s, keeper.id, keeper_conv.id)
    s.commit()
    users = UserRepository(s)

    users.clear_ledger_conversation_refs(gone.id)
    assert users.delete_if_pending_due(gone.id, CUTOFF) is True
    s.commit()
    s.expire_all()

    assert set(s.scalars(select(User.id))) == {keeper.id}
    assert s.scalar(select(func.count(UserIdentity.id))) == 0
    assert s.scalar(select(func.count(AuthToken.id))) == 0
    assert set(s.scalars(select(ChatConversation.id))) == {keeper_conv.id}
    assert s.scalar(select(func.count(ChatMessage.id))) == 1
    rows = {(r.user_id, r.conversation_id) for r in s.scalars(select(LlmCallLog))}
    assert rows == {(None, None), (keeper.id, keeper_conv.id)}          # 帳本保留；被刪者的使用者與對話編號都是空值


# ---- 帳本的對話參照 ----------------------------------------------------------------

def test_clear_ledger_conversation_refs_touches_only_that_users_rows(member_session):
    s = member_session
    a, b = add_user(s, 1), add_user(s, 2)
    conv_a, conv_b = conversation(s, a.id, 1), conversation(s, b.id, 2)
    ledger_row(s, a.id, conv_a.id)
    ledger_row(s, a.id, conv_a.id)
    ledger_row(s, a.id, None)
    ledger_row(s, b.id, conv_b.id)
    ledger_row(s, None, None)

    assert UserRepository(s).clear_ledger_conversation_refs(a.id) == 2       # 本來就是空值的那筆不算
    s.commit()
    s.expire_all()

    by_user = {}
    for row in s.scalars(select(LlmCallLog)):
        by_user.setdefault(row.user_id, []).append(row.conversation_id)
    assert by_user[a.id] == [None, None, None]
    assert by_user[b.id] == [conv_b.id]
    assert s.scalar(select(func.count(LlmCallLog.id))) == 5                  # 沒有刪除任何帳本紀錄


def test_clear_ledger_conversation_refs_on_a_user_without_ledger_rows_returns_zero(member_session):
    user = add_user(member_session, 1)
    assert UserRepository(member_session).clear_ledger_conversation_refs(user.id) == 0
