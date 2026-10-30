"""data-retention 規格：五類資料的期限清除（邊界、帳號刪除紀錄不納入、近期紀錄與額度不受影響、一類失敗不阻止其餘、分批、重跑、事件）。

以 `member_session` 同時在 SQLite 與真實 SQL Server 上執行。
"""
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import (
    AdminAuditLog, AuthToken, ChatConversation, ChatMessage, DeletionTombstone, LlmCallLog, User,
)
from app.models.user import TOKEN_VERIFY_EMAIL
from app.repositories.retention_repository import RetentionRepository
from app.services.data_retention_service import (
    ADMIN_AUDIT_RETENTION, AUTH_TOKEN_RETENTION, CONVERSATION_RETENTION, ERROR_DETAIL_RETENTION, LEDGER_RETENTION,
    DataRetentionService,
)
from tests.support.auth import START, FakeClock, add_user

SECOND = timedelta(seconds=1)
CATEGORIES = ["conversations", "error_detail", "ledger", "auth_tokens", "admin_audit"]


def make_service(session, **kwargs) -> DataRetentionService:
    return DataRetentionService(
        repository=kwargs.pop("repository", RetentionRepository(session)), clock=FakeClock(), commit=session.commit,
        rollback=session.rollback, **kwargs,
    )


def counts(report) -> dict[str, int]:
    return {r.category: r.matched for r in report.results}


def conv(session, user_id, n, updated_at):
    c = ChatConversation(user_id=user_id, client_id=f"{n:036d}", state_json={}, created_at=updated_at, updated_at=updated_at)
    session.add(c)
    session.flush()
    session.add(ChatMessage(conversation_id=c.id, client_msg_id=f"m{n}", seq=1, role="user", content=f"機密訊息{n}"))
    session.flush()
    return c


def ledger(session, user_id, created_at, *, conversation_id=None, error_detail=None, counted=False):
    row = LlmCallLog(
        user_id=user_id, conversation_id=conversation_id, kind="chat", provider="gemini", model="m", attempt=1, status="ok",
        counted=counted, prompt_tokens=10, completion_tokens=5, latency_ms=100, created_at=created_at, error_detail=error_detail,
    )
    session.add(row)
    session.flush()
    return row


def seed_every_category(session) -> dict[str, tuple]:
    """每一類各放一筆「剛好過期」與一筆「差一秒才過期」。"""
    user = add_user(session, 1).id
    admin = add_user(session, 2, role="admin").id
    out = {}
    out["conversations"] = (conv(session, user, 1, START - CONVERSATION_RETENTION - SECOND).id, conv(session, user, 2, START - CONVERSATION_RETENTION + SECOND).id)
    out["ledger"] = (ledger(session, user, START - LEDGER_RETENTION - SECOND).id, ledger(session, user, START - LEDGER_RETENTION + SECOND).id)
    out["error_detail"] = (
        ledger(session, user, START - ERROR_DETAIL_RETENTION - SECOND, error_detail="過期原文").id,
        ledger(session, user, START - ERROR_DETAIL_RETENTION + SECOND, error_detail="未過期原文").id,
    )
    tokens = []
    for n, expires in enumerate((START - AUTH_TOKEN_RETENTION - SECOND, START - AUTH_TOKEN_RETENTION + SECOND), start=1):
        token = AuthToken(user_id=user, purpose=TOKEN_VERIFY_EMAIL, token_hash=f"{n:064d}", expires_at=expires)
        session.add(token)
        session.flush()
        tokens.append(token.id)
    out["auth_tokens"] = tuple(tokens)
    audits = []
    for created in (START - ADMIN_AUDIT_RETENTION - SECOND, START - ADMIN_AUDIT_RETENTION + SECOND):
        row = AdminAuditLog(admin_user_id=admin, action="user.lookup", detail_json={}, created_at=created)
        session.add(row)
        session.flush()
        audits.append(row.id)
    out["admin_audit"] = tuple(audits)
    session.commit()
    return out


# ---- 期限 -------------------------------------------------------------------------

def test_the_periods_are_the_documented_ones():
    assert CONVERSATION_RETENTION == LEDGER_RETENTION == timedelta(days=365)
    assert ERROR_DETAIL_RETENTION == timedelta(days=14) and AUTH_TOKEN_RETENTION == timedelta(days=7)
    assert ADMIN_AUDIT_RETENTION == timedelta(days=730)


def test_each_category_removes_only_what_is_past_its_own_period(member_session):
    s = member_session
    ids = seed_every_category(s)
    report = make_service(s).run()

    assert [r.category for r in report.results] == CATEGORIES and report.failed == []
    assert counts(report) == {"conversations": 1, "ledger": 1, "error_detail": 1, "auth_tokens": 1, "admin_audit": 1}
    s.expire_all()
    assert set(s.scalars(select(ChatConversation.id))) == {ids["conversations"][1]}
    assert s.scalar(select(func.count(ChatMessage.id))) == 1
    # 帳本：過期的 365 天那筆被刪；失敗原文那筆（建立只有 14 天多）保留但原文清空
    remaining = {r.id: r for r in s.scalars(select(LlmCallLog))}
    assert ids["ledger"][0] not in remaining and ids["ledger"][1] in remaining
    assert remaining[ids["error_detail"][0]].error_detail is None and remaining[ids["error_detail"][1]].error_detail == "未過期原文"
    assert set(s.scalars(select(AuthToken.id))) == {ids["auth_tokens"][1]}
    assert set(s.scalars(select(AdminAuditLog.id))) == {ids["admin_audit"][1]}


def test_running_it_twice_leaves_nothing_for_the_second_run(member_session):
    s = member_session
    seed_every_category(s)
    make_service(s).run()
    second = make_service(s).run()
    assert counts(second) == dict.fromkeys(CATEGORIES, 0)


def test_the_dry_run_counts_every_category_and_changes_nothing(member_session, log_lines):
    s = member_session
    seed_every_category(s)
    before = [s.scalar(select(func.count(model.id))) for model in (ChatConversation, LlmCallLog, AuthToken, AdminAuditLog)]
    report = make_service(s).run(dry_run=True)
    s.expire_all()
    after = [s.scalar(select(func.count(model.id))) for model in (ChatConversation, LlmCallLog, AuthToken, AdminAuditLog)]
    assert report.dry_run and counts(report) == {"conversations": 1, "ledger": 1, "error_detail": 1, "auth_tokens": 1, "admin_audit": 1}
    assert before == after and s.scalar(select(LlmCallLog.error_detail).where(LlmCallLog.error_detail == "過期原文")) == "過期原文"
    assert [line for line in log_lines() if line.get("event", "").startswith("retention.")] == []


# ---- 不該被動到的 -------------------------------------------------------------------

def test_deletion_tombstones_are_kept_forever(member_session):
    s = member_session
    s.add(DeletionTombstone(user_id=7, reason="user_requested", deleted_at=START - timedelta(days=2000)))
    s.commit()
    make_service(s).run()
    assert s.scalar(select(func.count(DeletionTombstone.id))) == 1


def test_recent_records_and_todays_usage_are_unaffected(member_session):
    """近期紀錄全數保留，今日額度與全站今日用量前後相同（它們只讀最近幾天的帳本）。"""
    s = member_session
    user_id = add_user(s, 1).id
    for n in range(4):
        ledger(s, user_id, START - timedelta(hours=n), counted=True)
    ledger(s, user_id, START - LEDGER_RETENTION - timedelta(days=30), counted=True)          # 一年多以前的，會被清掉
    s.commit()

    from app.repositories.llm_call_log_repository import LlmCallLogRepository

    ledger_repo = LlmCallLogRepository(s)
    start, end = START - timedelta(days=1), START + timedelta(days=1)

    def snapshot():
        return ledger_repo.count_counted_chat_calls(user_id, start=start, end=end), ledger_repo.usage_totals(start=start, end=end)

    before = snapshot()
    report = make_service(s).run()
    assert counts(report)["ledger"] == 1 and snapshot() == before        # 額度與全站今日用量讀的就是這兩個查詢，前後相同
    assert s.scalar(select(func.count(LlmCallLog.id))) == 4


def test_the_user_and_other_tables_are_untouched(member_session):
    s = member_session
    seed_every_category(s)
    make_service(s).run()
    assert s.scalar(select(func.count(User.id))) == 2


# ---- 失敗與分批 ----------------------------------------------------------------------

def test_a_failing_category_does_not_stop_the_others_and_is_named_in_the_report(member_session, log_lines):
    s = member_session
    seed_every_category(s)

    class LedgerBreaks(RetentionRepository):
        def purge_ledger(self, ids):
            raise RuntimeError("資料庫暫時失敗：含機密文字 sekret-detail")

    report = make_service(s, repository=LedgerBreaks(s)).run()

    assert report.failed == ["ledger"]
    assert counts(report) == {"conversations": 1, "ledger": 0, "error_detail": 1, "auth_tokens": 1, "admin_audit": 1}
    s.expire_all()
    assert s.scalar(select(func.count(ChatConversation.id))) == 1 and s.scalar(select(func.count(AuthToken.id))) == 1
    failures = [line for line in log_lines() if line.get("event") == "retention.purge_failed"]
    assert [line["reason"] for line in failures] == ["ledger"]
    assert "sekret-detail" not in log_lines.raw()                    # 不記錄例外內容


def test_more_records_than_one_batch_are_all_removed_with_a_commit_per_batch(member_session):
    s = member_session
    user_id = add_user(s, 1).id
    for _ in range(11):
        ledger(s, user_id, START - LEDGER_RETENTION - timedelta(days=2))
    s.commit()
    commits = []
    service = DataRetentionService(
        repository=RetentionRepository(s), clock=FakeClock(), commit=lambda: (commits.append(1), s.commit()), rollback=s.rollback, batch_size=4,
    )
    report = service.run()
    assert counts(report)["ledger"] == 11 and s.scalar(select(func.count(LlmCallLog.id))) == 0
    assert len(commits) >= 3                                         # 11 筆、每批 4 筆：至少三批各自提交


def test_a_batch_where_nothing_is_processed_ends_the_run_instead_of_looping(member_session):
    s = member_session
    user_id = add_user(s, 1).id
    ledger(s, user_id, START - LEDGER_RETENTION - timedelta(days=2))
    s.commit()

    class ProcessesNothing(RetentionRepository):
        def purge_ledger(self, ids):
            return 0

    report = make_service(s, repository=ProcessesNothing(s)).run()
    assert counts(report)["ledger"] == 0 and report.failed == []


# ---- 事件與輸出不含內容 -----------------------------------------------------------------

def test_events_carry_only_the_category_and_the_count(member_session, log_lines):
    s = member_session
    seed_every_category(s)
    make_service(s).run()
    purged = [(line["reason"], line["count"]) for line in log_lines() if line.get("event") == "retention.purged"]
    assert purged == [("conversations", 1), ("error_detail", 1), ("ledger", 1), ("auth_tokens", 1), ("admin_audit", 1)]
    for secret in ("機密訊息", "過期原文", "user1@example.com", "使用者1"):
        assert secret not in log_lines.raw()
