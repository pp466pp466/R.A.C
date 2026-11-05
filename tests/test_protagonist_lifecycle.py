"""玩家主角的生命週期（rpg-protagonist 規格「主角內容是個資」）：抹除、期限清除與管理員守門。

主角資料在 `sessions.protagonist_json`，所以不需要任何新的刪除路徑：存檔消失它就消失。這裡證明這一點，而不是假設。
以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行。
"""
from datetime import timedelta

from sqlalchemy import func, select

from app.models import Base, RpgSession, User
from app.repositories.deletion_tombstone_repository import DeletionTombstoneRepository
from app.repositories.retention_repository import RetentionRepository
from app.repositories.session_repository import SessionRepository
from app.repositories.user_repository import UserRepository
from app.services.account_erasure_service import AccountErasureService
from app.services.data_retention_service import SESSION_RETENTION, DataRetentionService
from tests.support.auth import START, FakeClock, add_user
from tests.support.protagonist import protagonist

DAY = timedelta(days=1)
SECRET_NAME = "主角姓名LIFECYCLE-NAME"
SECRET_FACT = "聯繫文字LIFECYCLE-FACT"
ANCHOR = {"kind": "location", "ref": "古劍山莊", "text": SECRET_FACT}


def make_save(session, user, *, now=START, name=SECRET_NAME):
    return SessionRepository(session).create(
        user.id, source_version_id=None, snapshot={"title": "劇本"}, snapshot_spec_version=1,
        protagonist=protagonist(name, 19, anchor=ANCHOR, hidden_trait="LIFECYCLE-HIDDEN"), content_tier="safe", now=now,
    )


def everything_in_the_database(session) -> str:
    session.expire_all()
    parts = []
    for table in Base.metadata.tables.values():
        parts += [str(row) for row in session.execute(select(table)).all()]
    return "\n".join(parts)


# ---- 抹除 ---------------------------------------------------------------------------------------


def test_erasing_a_player_removes_the_protagonist_with_the_save(member_session):
    s = member_session
    user = add_user(s, 1, last_login_at=START - 400 * DAY, created_at=START - 500 * DAY)
    make_save(s, user)
    s.commit()
    assert "LIFECYCLE-NAME" in everything_in_the_database(s)
    service = AccountErasureService(
        users=UserRepository(s), tombstones=DeletionTombstoneRepository(s), commit=s.commit, rollback=s.rollback,
    )
    assert service.erase_inactive(user.id, cutoff=START - 365 * DAY, now=START) is True
    text = everything_in_the_database(s)
    for secret in ("LIFECYCLE-NAME", "LIFECYCLE-FACT", "LIFECYCLE-HIDDEN"):
        assert secret not in text
    assert s.scalar(select(func.count()).select_from(RpgSession)) == 0


def test_erasing_one_player_leaves_another_players_protagonist_alone(member_session):
    s = member_session
    gone = add_user(s, 1, last_login_at=START - 400 * DAY, created_at=START - 500 * DAY)
    kept = add_user(s, 2)
    make_save(s, gone, name="被抹除者的主角")
    make_save(s, kept, name="留下來的主角")
    s.commit()
    service = AccountErasureService(
        users=UserRepository(s), tombstones=DeletionTombstoneRepository(s), commit=s.commit, rollback=s.rollback,
    )
    assert service.erase_inactive(gone.id, cutoff=START - 365 * DAY, now=START) is True
    s.expire_all()
    [save] = list(s.scalars(select(RpgSession)))
    assert save.user_id == kept.id and save.protagonist_json["character"]["name"] == "留下來的主角" and s.get(User, kept.id) is not None


# ---- 期限清除 -----------------------------------------------------------------------------------


def retention(session):
    return DataRetentionService(repository=RetentionRepository(session), clock=FakeClock(), commit=session.commit, rollback=session.rollback)


def test_a_save_past_its_retention_period_is_purged_with_its_protagonist(member_session):
    s = member_session
    user = add_user(s, 1)
    make_save(s, user, now=START - SESSION_RETENTION - DAY)
    s.commit()
    report = retention(s).run()
    assert {r.category: r.matched for r in report.results}["sessions"] == 1
    assert "LIFECYCLE-NAME" not in everything_in_the_database(s)


def test_a_recent_save_keeps_its_protagonist(member_session):
    s = member_session
    user = add_user(s, 1)
    make_save(s, user, now=START - DAY)
    s.commit()
    assert {r.category: r.matched for r in retention(s).run().results}["sessions"] == 0
    assert "LIFECYCLE-NAME" in everything_in_the_database(s)


# ---- 管理員預設看不到 ------------------------------------------------------------------------------


def test_an_admin_has_no_path_to_a_players_protagonist(member_session):
    s = member_session
    admin = add_user(s, 1, role="admin")
    player = add_user(s, 2)
    save = make_save(s, player)
    s.commit()
    repo = SessionRepository(s)
    assert repo.get_owned(admin.id, save.public_id) is None and repo.list_owned(admin.id) == []
    assert list(repo.export_sessions(admin.id)) == []


def test_the_admin_repositories_and_service_never_mention_the_protagonist():
    from app.config import PROJECT_ROOT

    for relative in ("app/repositories/admin_repository.py", "app/repositories/admin_audit_repository.py", "app/services/admin_service.py"):
        source = (PROJECT_ROOT / relative).read_text(encoding="utf-8-sig")
        for name in ("protagonist", "RpgSession", "sessions"):
            assert name not in source, (relative, name)
