"""account-deletion 與 account-retention 規格：單一抹除出口（帳本匿名化的順序、條件不成立時整個交易回復、刪除紀錄與事件）。

以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行。
"""
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import ChatConversation, DeletionTombstone, LlmCallLog, User
from app.models.base import utcnow
from app.models.deletion_tombstone import REASON_INACTIVE_12_MONTHS, REASON_USER_REQUESTED
from app.models.user import STATUS_ACTIVE, STATUS_DELETION_PENDING
from app.repositories.deletion_tombstone_repository import DeletionTombstoneRepository
from app.repositories.user_repository import UserRepository
from app.services.account_erasure_service import AccountErasureService
from app.services.account_retention_service import INACTIVITY_PERIOD
from tests.support.auth import START, add_user

CUTOFF = START - timedelta(days=7)
SECRET_EMAIL = "erase.me@example.com"
SECRET_NAME = "待抹除姓名"


def make_service(session, **overrides) -> AccountErasureService:
    return AccountErasureService(
        users=overrides.pop("users", UserRepository(session)), tombstones=DeletionTombstoneRepository(session),
        commit=overrides.pop("commit", session.commit), rollback=overrides.pop("rollback", session.rollback), **overrides,
    )


def with_conversation_and_ledger(session, user: User) -> ChatConversation:
    conv = ChatConversation(user_id=user.id, client_id="00000000-0000-4000-8000-000000000001", state_json={})
    session.add(conv)
    session.flush()
    for _ in range(2):
        session.add(LlmCallLog(
            user_id=user.id, conversation_id=conv.id, kind="chat", provider="gemini", model="m", attempt=1, status="ok",
            counted=True, prompt_tokens=11, completion_tokens=7, latency_ms=250, created_at=utcnow(),
        ))
    session.commit()
    return conv


def ledger(session) -> list[tuple]:
    session.expire_all()
    return sorted(
        (r.user_id, r.conversation_id, r.prompt_tokens, r.completion_tokens, r.latency_ms) for r in session.scalars(select(LlmCallLog))
    )


def pending_due_user(session, n: int = 1, **overrides) -> User:
    return add_user(
        session, n, status=STATUS_DELETION_PENDING, deletion_requested_at=CUTOFF - timedelta(days=1),
        status_before_deletion=STATUS_ACTIVE, **overrides,
    )


# ---- 成功 -------------------------------------------------------------------------

def test_erasing_a_due_account_anonymises_the_ledger_and_keeps_its_statistics(member_session):
    s = member_session
    user = pending_due_user(s)
    with_conversation_and_ledger(s, user)

    assert make_service(s).erase_pending_due(user.id, cutoff=CUTOFF, now=START) is True

    assert s.scalar(select(func.count(User.id))) == 0 and s.scalar(select(func.count(ChatConversation.id))) == 0
    assert ledger(s) == [(None, None, 11, 7, 250)] * 2             # 紀錄與統計保留，使用者與對話編號都是空值


def test_the_tombstone_holds_only_the_internal_id_the_time_and_the_reason(member_session):
    s = member_session
    due_id = pending_due_user(s, 1).id
    stale_id = add_user(s, 2, last_login_at=START - INACTIVITY_PERIOD - timedelta(days=1)).id
    service = make_service(s)
    assert service.erase_pending_due(due_id, cutoff=CUTOFF, now=START)
    assert service.erase_inactive(stale_id, cutoff=START - INACTIVITY_PERIOD, now=START)

    rows = list(s.scalars(select(DeletionTombstone).order_by(DeletionTombstone.id)))
    assert [(r.user_id, r.reason, r.deleted_at) for r in rows] == [
        (due_id, REASON_USER_REQUESTED, START), (stale_id, REASON_INACTIVE_12_MONTHS, START),
    ]
    assert {c.name for c in DeletionTombstone.__table__.columns} == {"id", "user_id", "deleted_at", "reason"}


def test_a_reused_integer_id_never_points_an_anonymised_ledger_row_at_someone_elses_conversation(member_session):
    """帳本的 conversation_id 沒有外鍵；SQLite 在刪除最大編號後會重用它，所以必須先清掉（SQL Server 的 IDENTITY 不重用）。"""
    s = member_session
    user = pending_due_user(s)
    old_conversation_id = with_conversation_and_ledger(s, user).id
    assert make_service(s).erase_pending_due(user.id, cutoff=CUTOFF, now=START)

    newcomer = add_user(s, 2)
    new_conversation = ChatConversation(user_id=newcomer.id, client_id="00000000-0000-4000-8000-000000000002", state_json={})
    s.add(new_conversation)
    s.commit()
    if new_conversation.id == old_conversation_id:                 # SQLite 重用了編號：這正是要防的情形
        assert all(row[1] is None for row in ledger(s))
    assert all(row[1] != new_conversation.id for row in ledger(s))


# ---- 條件不成立 -------------------------------------------------------------------------

def test_when_the_condition_no_longer_holds_nothing_changes_not_even_the_ledger(member_session):
    s = member_session
    cancelled = add_user(s, 1)                                    # 判斷之後剛好撤銷：已回到 active
    conv = with_conversation_and_ledger(s, cancelled)
    before = ledger(s)
    events: list[int] = []

    assert make_service(s).erase_pending_due(cancelled.id, cutoff=CUTOFF, now=START, on_erased=events.append) is False

    s.expire_all()
    assert s.get(User, cancelled.id) is not None and s.get(ChatConversation, conv.id) is not None
    assert ledger(s) == before and all(row[1] == conv.id for row in before)       # 帳本對話參照完全不變（交易已回復）
    assert s.scalar(select(func.count(DeletionTombstone.id))) == 0
    assert events == []                                           # 沒有抹除就沒有事件


def test_an_active_user_who_logged_in_during_the_run_keeps_the_conversation_links(member_session):
    s = member_session
    user = add_user(s, 1, last_login_at=START)                    # 不是閒置帳號
    conv = with_conversation_and_ledger(s, user)
    assert make_service(s).erase_inactive(user.id, cutoff=START - INACTIVITY_PERIOD, now=START) is False
    assert all(row[1] == conv.id for row in ledger(s))


# ---- 失敗與事件 -------------------------------------------------------------------------

def test_an_exception_rolls_back_and_propagates_without_committing():
    calls: list[str] = []

    class Users:
        def clear_ledger_conversation_refs(self, user_id):
            calls.append("clear")

    class Tombstones:
        def record(self, **kwargs):
            calls.append("record")

    def boom(user_id):
        raise RuntimeError("資料庫暫時失敗")

    service = AccountErasureService(
        users=Users(), tombstones=Tombstones(), commit=lambda: calls.append("commit"), rollback=lambda: calls.append("rollback")
    )
    with pytest.raises(RuntimeError):
        service.erase(1, delete=boom, reason=REASON_USER_REQUESTED, now=START)
    assert calls == ["clear", "rollback"]


def test_the_event_is_emitted_only_after_the_commit_and_carries_the_id_only():
    order: list[str] = []

    class Users:
        def clear_ledger_conversation_refs(self, user_id):
            order.append("clear")

    class Tombstones:
        def record(self, **kwargs):
            order.append("record")

    service = AccountErasureService(users=Users(), tombstones=Tombstones(), commit=lambda: order.append("commit"))
    assert service.erase(9, delete=lambda uid: True, reason=REASON_USER_REQUESTED, now=START, on_erased=lambda uid: order.append(f"event:{uid}"))
    assert order == ["clear", "record", "commit", "event:9"]


def test_inactive_erasure_logs_the_existing_event_and_no_personal_data(member_session, log_lines):
    s = member_session
    stale = add_user(s, 1, email=SECRET_EMAIL, email_canonical=SECRET_EMAIL, display_name=SECRET_NAME, last_login_at=START - INACTIVITY_PERIOD - timedelta(days=1))
    stale_id = stale.id
    with_conversation_and_ledger(s, stale)
    assert make_service(s).erase_inactive(stale_id, cutoff=START - INACTIVITY_PERIOD, now=START)
    events = [line for line in log_lines() if line.get("event") == "account.inactive_purged"]
    assert [line["user_id"] for line in events] == [stale_id]
    assert SECRET_EMAIL not in log_lines.raw() and SECRET_NAME not in log_lines.raw()
