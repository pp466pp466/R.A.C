"""deletion-replay 規格：備份還原後依刪除紀錄（資料庫加日誌取出的檔案）重新抹除。

以 `member_session` 同時在 SQLite 與真實 SQL Server 上執行：復活的帳號被抹除並匿名化帳本、重複執行為 0、
編號被重新發出時不誤刪、管理員不抹除、檔案先整體驗證、新增刪除紀錄、預覽不改變任何資料。
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.models import ChatConversation, ChatMessage, DeletionTombstone, LlmCallLog, User
from app.models.base import utcnow
from app.repositories.deletion_tombstone_repository import DeletionTombstoneRepository
from app.repositories.user_repository import UserRepository
from app.services.account_erasure_service import AccountErasureService
from app.services.deletion_replay_service import (
    DeletionReplayService, ReplayFileError, ReplayRecord, parse_replay_file,
)
from tests.support.auth import START, add_user

DELETED_AT = START
LONG_AGO = DELETED_AT - timedelta(days=90)


def make_service(session, replayed=None):
    users, tombstones = UserRepository(session), DeletionTombstoneRepository(session)
    erasure = AccountErasureService(users=users, tombstones=tombstones, commit=session.commit, rollback=session.rollback)
    kwargs = {"on_replayed": replayed.append} if replayed is not None else {}
    return DeletionReplayService(users=users, tombstones=tombstones, erasure=erasure, **kwargs)


def tombstone(session, user_id: int, when=DELETED_AT, reason="user_requested"):
    DeletionTombstoneRepository(session).record(user_id=user_id, reason=reason, deleted_at=when)
    session.commit()


def count(session, model) -> int:
    return session.scalar(select(func.count(model.id)))


def conversation_with_ledger(session, user_id: int) -> tuple[int, int]:
    conv = ChatConversation(user_id=user_id, client_id="00000000-0000-4000-8000-000000000001", state_json={})
    session.add(conv)
    session.flush()
    session.add(ChatMessage(conversation_id=conv.id, client_msg_id="m1", seq=1, role="user", content="哈囉"))
    row = LlmCallLog(
        user_id=user_id, conversation_id=conv.id, kind="chat", provider="gemini", model="m", attempt=1,
        status="ok", counted=True, latency_ms=10, created_at=utcnow(),
    )
    session.add(row)
    session.commit()
    return conv.id, row.id


# ---- 還原後復活的帳號 -------------------------------------------------------------------


def test_a_restored_account_in_the_tombstones_is_erased_with_its_conversations_and_the_ledger_is_anonymized(member_session):
    s = member_session
    zombie = add_user(s, 1, created_at=LONG_AGO)
    survivor = add_user(s, 2, created_at=LONG_AGO)
    zombie_id, survivor_id = zombie.id, survivor.id
    _, ledger_id = conversation_with_ledger(s, zombie_id)
    tombstone(s, zombie_id)

    report = make_service(s).run()

    assert (report.erased, report.reissued, report.admin, report.absent) == (1, 0, 0, 0)
    s.expire_all()
    assert s.scalars(select(User.id)).all() == [survivor_id]
    assert count(s, ChatConversation) == 0 and count(s, ChatMessage) == 0
    row = s.get(LlmCallLog, ledger_id)
    assert row is not None and row.user_id is None and row.conversation_id is None, "帳本保留，使用者與對話欄位匿名化"


def test_running_it_twice_erases_nothing_the_second_time(member_session):
    s = member_session
    zombie = add_user(s, 1, created_at=LONG_AGO)
    tombstone(s, zombie.id)
    service = make_service(s)
    assert service.run().erased == 1
    second = service.run()
    assert (second.erased, second.absent) == (0, 1)


# ---- 編號被重新發出與管理員 -------------------------------------------------------------


def test_a_user_who_reused_a_deleted_id_after_the_restore_is_not_erased(member_session):
    s = member_session
    newcomer = add_user(s, 1, created_at=DELETED_AT + timedelta(days=3))
    tombstone(s, newcomer.id, DELETED_AT)
    report = make_service(s).run()
    assert (report.erased, report.reissued) == (0, 1)
    assert count(s, User) == 1


def test_an_admin_is_never_erased_even_when_a_record_points_at_it(member_session):
    s = member_session
    admin = add_user(s, 1, role="admin", created_at=LONG_AGO)
    tombstone(s, admin.id)
    report = make_service(s).run()
    assert (report.erased, report.admin) == (0, 1) and count(s, User) == 1


def test_only_the_latest_deletion_time_per_id_counts(member_session):
    """編號 7 先屬於 A（t1 刪除），再被 B（t2 建立）沿用並於 t3 刪除；還原點在 t2 與 t3 之間，B 復活了，應被抹除。"""
    s = member_session
    t1, t2, t3 = DELETED_AT, DELETED_AT + timedelta(days=10), DELETED_AT + timedelta(days=20)
    b = add_user(s, 1, created_at=t2)
    tombstone(s, b.id, t1)
    report = make_service(s).run([ReplayRecord(b.id, t3)])
    assert report.erased == 1 and count(s, User) == 0


# ---- 檔案 -------------------------------------------------------------------------------


def test_a_record_from_the_file_erases_the_user_and_adds_a_tombstone_with_the_original_time(member_session):
    s = member_session
    user = add_user(s, 1, created_at=LONG_AGO)
    user_id = user.id
    report = make_service(s).run(parse_replay_file(f"{user_id},{DELETED_AT.isoformat()}\n"))
    assert report.erased == 1
    (row,) = s.scalars(select(DeletionTombstone)).all()
    assert (row.user_id, row.reason, row.deleted_at) == (user_id, "restore_replay", DELETED_AT)


def test_a_record_that_already_has_a_tombstone_is_not_written_twice(member_session):
    s = member_session
    user = add_user(s, 1, created_at=LONG_AGO)
    tombstone(s, user.id, DELETED_AT)
    make_service(s).run([ReplayRecord(user.id, DELETED_AT)])
    assert count(s, DeletionTombstone) == 1


def test_the_file_format_parses_utc_z_naive_times_and_ignores_blank_lines():
    records = parse_replay_file("\n7,2026-03-01T10:00:00Z\n 8 , 2026-03-02T10:00:00+08:00\n\n9,2026-03-03T10:00:00\n")
    assert [r.user_id for r in records] == [7, 8, 9]
    assert records[0].deleted_at == datetime(2026, 3, 1, 10, tzinfo=timezone.utc)
    assert records[1].deleted_at == datetime(2026, 3, 2, 2, tzinfo=timezone.utc)
    assert records[2].deleted_at.tzinfo is not None, "沒有時區的時間視為 UTC"


@pytest.mark.parametrize("bad", ["abc,2026-03-01T10:00:00Z", "7", "7,not-a-time", "-3,2026-03-01T10:00:00Z", "0,2026-03-01T10:00:00Z", "7,2026-03-01T10:00:00Z,extra", "٣,2026-03-01T10:00:00Z"])
def test_a_malformed_line_is_reported_by_its_number_without_echoing_its_content(bad):
    with pytest.raises(ReplayFileError) as error:
        parse_replay_file("1,2026-03-01T10:00:00Z\n2,2026-03-01T10:00:00Z\n" + bad + "\n")
    assert error.value.line_number == 3 and "第 3 行" in str(error.value)
    assert bad not in str(error.value)


def test_nothing_is_erased_when_the_file_is_malformed_because_parsing_comes_first(member_session):
    s = member_session
    zombie = add_user(s, 1, created_at=LONG_AGO)
    tombstone(s, zombie.id)
    with pytest.raises(ReplayFileError):
        parse_replay_file("1,2026-03-01T10:00:00Z\nbroken line\n")
    assert count(s, User) == 1   # 解析失敗時根本沒有呼叫服務


# ---- 預覽與事件 -------------------------------------------------------------------------


def test_the_preview_counts_without_changing_anything_or_logging_events(member_session, log_lines):
    s = member_session
    zombie = add_user(s, 1, created_at=LONG_AGO)
    conversation_with_ledger(s, zombie.id)
    tombstone(s, zombie.id)
    replayed: list[int] = []
    report = make_service(s, replayed).run(dry_run=True)
    assert (report.dry_run, report.erased) == (True, 1)
    assert count(s, User) == 1 and count(s, ChatConversation) == 1 and replayed == []
    assert [line for line in log_lines() if line.get("event") == "account.deletion_replayed"] == []


def test_each_erased_account_logs_one_event_with_only_the_internal_id(member_session, log_lines):
    s = member_session
    first, second = add_user(s, 1, created_at=LONG_AGO), add_user(s, 2, created_at=LONG_AGO)
    ids = sorted([first.id, second.id])
    tombstone(s, ids[0])
    tombstone(s, ids[1])
    users, tombstones = UserRepository(s), DeletionTombstoneRepository(s)
    erasure = AccountErasureService(users=users, tombstones=tombstones, commit=s.commit, rollback=s.rollback)
    DeletionReplayService(users=users, tombstones=tombstones, erasure=erasure).run()   # 預設的 on_replayed 就是真正的日誌事件
    events = [line for line in log_lines() if line.get("event") == "account.deletion_replayed"]
    assert [e["user_id"] for e in events] == ids
    for event in events:
        assert set(event) <= {"time", "level", "logger", "message", "request_id", "event", "user_id"}
