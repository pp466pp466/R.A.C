"""data-retention 規格：期限清除用的資料存取（每一類的邊界、對話清除時帳本的處理、失敗原文只清空不刪列、批次上限）。

以 `member_session` 同時在 SQLite 與真實 SQL Server 上執行。邊界一律是「嚴格早於」：剛好等於期限的紀錄保留。
"""
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import (
    AdminAuditLog, AuthToken, ChatConversation, ChatMessage, DeletionTombstone, LlmCallLog, User,
)
from app.models.user import TOKEN_VERIFY_EMAIL
from app.repositories.retention_repository import RetentionRepository
from tests.support.auth import START, add_user

CUTOFF = START - timedelta(days=365)
SECOND = timedelta(seconds=1)


def conversation(session, user_id: int, n: int, *, updated_at, created_at=None) -> ChatConversation:
    conv = ChatConversation(
        user_id=user_id, client_id=f"{n:036d}", state_json={}, created_at=created_at or updated_at, updated_at=updated_at
    )
    session.add(conv)
    session.flush()
    session.add(ChatMessage(conversation_id=conv.id, client_msg_id=f"m{n}", seq=1, role="user", content=f"訊息{n}"))
    session.flush()
    return conv


def ledger(session, user_id, conversation_id=None, *, created_at, error_detail=None) -> LlmCallLog:
    row = LlmCallLog(
        user_id=user_id, conversation_id=conversation_id, kind="chat", provider="gemini", model="m", attempt=1,
        status="ok" if error_detail is None else "validation_failed", counted=error_detail is None, prompt_tokens=10,
        completion_tokens=5, latency_ms=100, created_at=created_at, error_detail=error_detail,
    )
    session.add(row)
    session.flush()
    return row


# ---- 對話與訊息 -------------------------------------------------------------------

def test_conversations_are_found_by_last_activity_with_a_strict_boundary(member_session):
    s = member_session
    user = add_user(s, 1).id
    old = conversation(s, user, 1, updated_at=CUTOFF - SECOND)
    exactly = conversation(s, user, 2, updated_at=CUTOFF)
    recent = conversation(s, user, 3, updated_at=CUTOFF + SECOND)
    # 建立很久以前，但最近有新訊息：看的是最後活動，不是建立時間
    active_old = conversation(s, user, 4, updated_at=START, created_at=CUTOFF - timedelta(days=400))
    repo = RetentionRepository(s)

    assert repo.find_conversations(CUTOFF, 100) == [old.id]
    assert repo.count_conversations(CUTOFF) == 1
    assert {exactly.id, recent.id, active_old.id}.isdisjoint(repo.find_conversations(CUTOFF, 100))


def test_purging_conversations_deletes_messages_and_unlinks_the_ledger_but_keeps_its_rows(member_session):
    s = member_session
    user = add_user(s, 1).id
    gone = conversation(s, user, 1, updated_at=CUTOFF - SECOND)
    kept = conversation(s, user, 2, updated_at=START)
    ledger(s, user, gone.id, created_at=START)
    ledger(s, user, gone.id, created_at=START)
    ledger(s, user, kept.id, created_at=START)
    s.commit()
    gone_id, kept_id = gone.id, kept.id
    repo = RetentionRepository(s)

    assert repo.purge_conversations([gone_id]) == 1
    s.commit()
    s.expire_all()

    assert set(s.scalars(select(ChatConversation.id))) == {kept_id}
    assert s.scalar(select(func.count(ChatMessage.id))) == 1
    assert sorted((r.user_id, r.conversation_id) for r in s.scalars(select(LlmCallLog)) if r.conversation_id is None) == [(user, None)] * 2
    assert s.scalar(select(func.count(LlmCallLog.id))) == 3 and s.scalar(select(LlmCallLog.id).where(LlmCallLog.conversation_id == kept_id)) is not None
    assert s.get(User, user) is not None                            # 使用者本身不受影響


# ---- 用量帳本與失敗原文 ----------------------------------------------------------------

def test_ledger_rows_are_found_by_creation_time_with_a_strict_boundary(member_session):
    s = member_session
    user = add_user(s, 1).id
    old = ledger(s, user, created_at=CUTOFF - SECOND)
    ledger(s, user, created_at=CUTOFF)
    ledger(s, user, created_at=START)
    repo = RetentionRepository(s)
    assert repo.find_ledger(CUTOFF, 100) == [old.id] and repo.count_ledger(CUTOFF) == 1
    assert repo.purge_ledger([old.id]) == 1
    assert s.scalar(select(func.count(LlmCallLog.id))) == 2


def test_error_detail_is_cleared_but_the_ledger_row_stays(member_session):
    s = member_session
    user = add_user(s, 1).id
    fourteen_days = timedelta(days=14)
    cutoff = START - fourteen_days
    stale = ledger(s, user, created_at=cutoff - SECOND, error_detail="壞掉的 JSON { 原文")
    boundary = ledger(s, user, created_at=cutoff, error_detail="剛好 14 天")
    fresh = ledger(s, user, created_at=START - timedelta(days=13), error_detail="13 天前")
    without = ledger(s, user, created_at=cutoff - timedelta(days=30))                   # 本來就沒有原文：不必處理
    repo = RetentionRepository(s)

    assert repo.find_error_detail(cutoff, 100) == [stale.id] and repo.count_error_detail(cutoff) == 1
    assert repo.purge_error_detail([stale.id]) == 1
    s.commit()
    s.expire_all()
    rows = {r.id: r for r in s.scalars(select(LlmCallLog))}
    assert rows[stale.id].error_detail is None and rows[stale.id].prompt_tokens == 10 and rows[stale.id].status == "validation_failed"
    assert rows[boundary.id].error_detail == "剛好 14 天" and rows[fresh.id].error_detail == "13 天前"
    assert without.id in rows and repo.find_error_detail(cutoff, 100) == []             # 重跑不再選到它


# ---- 驗證權杖 ---------------------------------------------------------------------

def test_tokens_are_found_by_expiry_whether_or_not_they_were_used(member_session):
    s = member_session
    user = add_user(s, 1).id
    seven_days = timedelta(days=7)
    cutoff = START - seven_days

    def token(n: int, expires_at, used_at=None) -> AuthToken:
        row = AuthToken(user_id=user, purpose=TOKEN_VERIFY_EMAIL, token_hash=f"{n:064d}", expires_at=expires_at, used_at=used_at)
        s.add(row)
        s.flush()
        return row

    expired_unused = token(1, cutoff - SECOND)
    expired_used = token(2, cutoff - timedelta(days=3), used_at=cutoff - timedelta(days=4))
    boundary = token(3, cutoff)
    just_expired = token(4, START - SECOND)
    still_valid = token(5, START + timedelta(hours=1))
    repo = RetentionRepository(s)

    assert repo.find_auth_tokens(cutoff, 100) == [expired_unused.id, expired_used.id] and repo.count_auth_tokens(cutoff) == 2
    assert repo.purge_auth_tokens([expired_unused.id, expired_used.id]) == 2
    assert set(s.scalars(select(AuthToken.id))) == {boundary.id, just_expired.id, still_valid.id}


# ---- 管理稽核 ---------------------------------------------------------------------

def test_audit_rows_are_found_by_creation_time_with_a_strict_boundary(member_session):
    s = member_session
    admin = add_user(s, 1, role="admin").id
    cutoff = START - timedelta(days=730)

    def audit(created_at) -> AdminAuditLog:
        row = AdminAuditLog(admin_user_id=admin, action="user.lookup", detail_json={}, created_at=created_at)
        s.add(row)
        s.flush()
        return row

    old, boundary, recent = audit(cutoff - SECOND), audit(cutoff), audit(START - timedelta(days=729))
    repo = RetentionRepository(s)
    assert repo.find_admin_audit(cutoff, 100) == [old.id] and repo.count_admin_audit(cutoff) == 1
    assert repo.purge_admin_audit([old.id]) == 1
    assert set(s.scalars(select(AdminAuditLog.id))) == {boundary.id, recent.id}


# ---- 共通 -------------------------------------------------------------------------

def test_find_is_ordered_limited_and_returns_plain_integers(member_session):
    s = member_session
    user = add_user(s, 1).id
    rows = [ledger(s, user, created_at=CUTOFF - timedelta(days=n)) for n in range(1, 8)]
    found = RetentionRepository(s).find_ledger(CUTOFF, 3)
    assert found == [r.id for r in rows][:3] and all(type(i) is int for i in found)


@pytest.mark.parametrize("method", ["purge_conversations", "purge_ledger", "purge_error_detail", "purge_auth_tokens", "purge_admin_audit"])
def test_purging_an_empty_batch_does_nothing(member_session, method):
    assert getattr(RetentionRepository(member_session), method)([]) == 0


def test_deletion_tombstones_are_never_touched(member_session):
    s = member_session
    s.add(DeletionTombstone(user_id=99, reason="user_requested", deleted_at=START - timedelta(days=900)))
    s.commit()
    repo = RetentionRepository(s)
    for cutoff in (START, START + timedelta(days=3650)):
        repo.purge_ledger(repo.find_ledger(cutoff, 100))
        repo.purge_admin_audit(repo.find_admin_audit(cutoff, 100))
    s.commit()
    assert s.scalar(select(func.count(DeletionTombstone.id))) == 1
