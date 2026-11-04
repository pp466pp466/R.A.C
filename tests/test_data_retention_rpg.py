"""data-retention 規格（rpg-data-and-script 新增）：RPG 存檔在最後活動後 12 個月清除；中斷與被擋回合的部分內容保存 14 天。

以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行。
"""
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import MemoryLog, RpgSession
from app.repositories.retention_repository import RetentionRepository
from app.repositories.session_repository import SessionRepository
from app.services.data_retention_service import SESSION_RETENTION, TURN_CONTENT_RETENTION, DataRetentionService
from tests.support.auth import START, FakeClock, add_user

SECOND = timedelta(seconds=1)
DAY = timedelta(days=1)


def service(session, **kwargs) -> DataRetentionService:
    return DataRetentionService(
        repository=RetentionRepository(session), clock=FakeClock(), commit=session.commit, rollback=session.rollback, **kwargs,
    )


def counts(report) -> dict[str, int]:
    return {r.category: r.matched for r in report.results}


def make_save(session, user_id, *, last_active, status="active") -> RpgSession:
    save = RpgSession(
        user_id=user_id, script_snapshot_json={"title": "劇本"}, player_name="旅人", character_age=18, last_active_at=last_active,
        created_at=last_active, status=status, notebook_text="記事簿的內容",
    )
    session.add(save)
    session.flush()
    return save


def make_turn(session, save, n, *, status, content="部分劇情", updated_at=START, turn_number=None):
    turn = MemoryLog(
        session_id=save.id, client_msg_id=f"m-{n}", seq=n, turn_status=status, llm_raw_response=content, turn_number=turn_number,
        user_action=f"行動{n}", created_at=updated_at, updated_at=updated_at,
    )
    session.add(turn)
    session.flush()
    return turn


def remaining_saves(session) -> set[int]:
    session.expire_all()
    return set(session.scalars(select(RpgSession.id)))


# ---- 存檔在最後活動後 12 個月清除 -----------------------------------------------------------------

def test_the_session_period_is_365_days():
    assert SESSION_RETENTION == timedelta(days=365)


def test_a_save_idle_for_more_than_365_days_is_deleted_with_its_turns_and_notebook(member_session):
    s = member_session
    user = add_user(s, 1)
    old = make_save(s, user.id, last_active=START - SESSION_RETENTION - SECOND)
    make_turn(s, old, 1, status="complete", turn_number=1)
    make_turn(s, old, 2, status="interrupted")
    s.commit()
    report = service(s).run()
    assert counts(report)["sessions"] == 1
    assert remaining_saves(s) == set() and s.scalar(select(func.count(MemoryLog.id))) == 0


def test_a_save_idle_for_364_days_is_kept(member_session):
    s = member_session
    save = make_save(s, add_user(s, 1).id, last_active=START - SESSION_RETENTION + DAY)
    s.commit()
    assert counts(service(s).run())["sessions"] == 0 and remaining_saves(s) == {save.id}


def test_only_the_last_activity_counts_not_the_creation_time(member_session):
    s = member_session
    save = make_save(s, add_user(s, 1).id, last_active=START - DAY)
    save.created_at = START - SESSION_RETENTION - 100 * DAY
    s.commit()
    assert counts(service(s).run())["sessions"] == 0 and remaining_saves(s) == {save.id}


def test_a_save_with_no_turns_is_measured_from_its_creation(member_session):
    s = member_session
    repo = SessionRepository(s)
    user = add_user(s, 1)
    old = repo.create(user.id, source_version_id=None, snapshot={}, snapshot_spec_version=1, player_name="旅人", character_age=18, content_tier="safe", now=START - SESSION_RETENTION - DAY)
    fresh = repo.create(user.id, source_version_id=None, snapshot={}, snapshot_spec_version=1, player_name="旅人", character_age=18, content_tier="safe", now=START - DAY)
    s.commit()
    kept, gone = fresh.id, old.id
    assert counts(service(s).run())["sessions"] == 1 and remaining_saves(s) == {kept} and gone != kept


def test_an_ended_save_is_deleted_too(member_session):
    s = member_session
    ended = make_save(s, add_user(s, 1).id, last_active=START - SESSION_RETENTION - DAY, status="ended")
    ended.ended_at = START - SESSION_RETENTION - DAY
    s.commit()
    assert counts(service(s).run())["sessions"] == 1 and remaining_saves(s) == set()


def test_recent_activity_through_a_new_turn_keeps_an_old_save(member_session):
    s = member_session
    repo = SessionRepository(s)
    user = add_user(s, 1)
    old = repo.create(user.id, source_version_id=None, snapshot={}, snapshot_spec_version=1, player_name="旅人", character_age=18, content_tier="safe", now=START - SESSION_RETENTION - 10 * DAY)
    repo.start_turn(user.id, old.public_id, client_msg_id="t-1", user_action="很久以後又回來玩", now=START - DAY)
    s.commit()
    assert counts(service(s).run())["sessions"] == 0 and remaining_saves(s) == {old.id}


def test_the_session_preview_counts_without_deleting(member_session, log_lines):
    s = member_session
    make_save(s, add_user(s, 1).id, last_active=START - SESSION_RETENTION - DAY)
    s.commit()
    report = service(s).run(dry_run=True)
    assert report.dry_run and counts(report)["sessions"] == 1
    assert s.scalar(select(func.count(RpgSession.id))) == 1
    assert [line for line in log_lines() if line.get("event", "").startswith("retention.")] == []


def test_batches_larger_than_the_batch_size_are_all_processed(member_session):
    s = member_session
    user = add_user(s, 1)
    for _ in range(7):
        make_save(s, user.id, last_active=START - SESSION_RETENTION - DAY)
    s.commit()
    report = service(s, batch_size=3).run()
    assert counts(report)["sessions"] == 7 and remaining_saves(s) == set()


def test_running_it_twice_leaves_nothing_for_the_second_run(member_session):
    s = member_session
    save = make_save(s, add_user(s, 1).id, last_active=START - SESSION_RETENTION - DAY)
    make_turn(s, save, 1, status="blocked", updated_at=START - TURN_CONTENT_RETENTION - DAY)
    s.commit()
    service(s).run()
    second = service(s).run()
    assert counts(second)["sessions"] == 0 and counts(second)["turn_content"] == 0


# ---- 中斷與被擋回合的部分內容保存 14 天 --------------------------------------------------------------

def test_the_turn_content_period_is_14_days():
    assert TURN_CONTENT_RETENTION == timedelta(days=14)


@pytest.mark.parametrize("status", ["interrupted", "blocked"])
def test_partial_content_older_than_14_days_is_cleared_but_the_turn_record_stays(member_session, status):
    s = member_session
    save = make_save(s, add_user(s, 1).id, last_active=START)
    turn = make_turn(s, save, 1, status=status, content="部分劇情", updated_at=START - TURN_CONTENT_RETENTION - SECOND)
    s.commit()
    turn_id, seq, client_msg_id = turn.id, turn.seq, turn.client_msg_id
    assert counts(service(s).run())["turn_content"] == 1
    s.expire_all()
    kept = s.get(MemoryLog, turn_id)
    assert (kept.llm_raw_response, kept.turn_status, kept.seq, kept.client_msg_id, kept.user_action) == ("", status, seq, client_msg_id, "行動1")


def test_content_of_13_days_is_kept(member_session):
    s = member_session
    save = make_save(s, add_user(s, 1).id, last_active=START)
    turn = make_turn(s, save, 1, status="interrupted", updated_at=START - 13 * DAY)
    s.commit()
    assert counts(service(s).run())["turn_content"] == 0
    assert s.get(MemoryLog, turn.id).llm_raw_response == "部分劇情"


@pytest.mark.parametrize("status", ["complete", "streaming"])
def test_complete_and_streaming_turns_are_never_touched(member_session, status):
    s = member_session
    save = make_save(s, add_user(s, 1).id, last_active=START)
    turn = make_turn(s, save, 1, status=status, content="完整的劇情原文", updated_at=START - 30 * DAY, turn_number=1 if status == "complete" else None)
    s.commit()
    assert counts(service(s).run())["turn_content"] == 0
    assert s.get(MemoryLog, turn.id).llm_raw_response == "完整的劇情原文"


def test_a_turn_that_is_already_empty_is_not_counted_again(member_session):
    s = member_session
    save = make_save(s, add_user(s, 1).id, last_active=START)
    make_turn(s, save, 1, status="interrupted", content="", updated_at=START - 30 * DAY)
    s.commit()
    assert counts(service(s).run(dry_run=True))["turn_content"] == 0


def test_the_turn_content_preview_counts_without_changing_anything(member_session):
    s = member_session
    save = make_save(s, add_user(s, 1).id, last_active=START)
    turn = make_turn(s, save, 1, status="blocked", updated_at=START - 30 * DAY)
    s.commit()
    report = service(s).run(dry_run=True)
    assert counts(report)["turn_content"] == 1 and s.get(MemoryLog, turn.id).llm_raw_response == "部分劇情"


def test_a_resent_turn_gets_fresh_content_and_is_no_longer_stale(member_session):
    """重送中斷的回合會把它重設為生成中（內容清空、狀態 streaming），之後不再被視為過期的部分內容。"""
    s = member_session
    repo = SessionRepository(s)
    user = add_user(s, 1)
    save = repo.create(user.id, source_version_id=None, snapshot={}, snapshot_spec_version=1, player_name="旅人", character_age=18, content_tier="safe", now=START - 40 * DAY)
    repo.start_turn(user.id, save.public_id, client_msg_id="t-1", user_action="行動", now=START - 40 * DAY)
    repo.interrupt_turn(user.id, save.public_id, "t-1", partial_response="舊的半截")
    s.commit()
    s.execute(MemoryLog.__table__.update().values(updated_at=START - 30 * DAY))
    s.commit()
    repo.start_turn(user.id, save.public_id, client_msg_id="t-1", user_action="行動", now=START)
    s.commit()
    assert counts(service(s).run(dry_run=True))["turn_content"] == 0


def test_the_events_carry_only_the_category_and_the_count_never_the_content(member_session, log_lines):
    s = member_session
    save = make_save(s, add_user(s, 1).id, last_active=START - SESSION_RETENTION - DAY)
    make_turn(s, save, 1, status="interrupted", content="日誌不應出現的劇情片段", updated_at=START - 30 * DAY)
    s.commit()
    service(s).run()
    purged = [(line["reason"], line["count"]) for line in log_lines() if line.get("event") == "retention.purged"]
    assert ("turn_content", 1) in purged and ("sessions", 1) in purged
    assert "日誌不應出現" not in log_lines.raw() and "記事簿的內容" not in log_lines.raw()
