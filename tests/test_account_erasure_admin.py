"""admin-delete-account 規格（account-deletion「管理員刪除帳號走同一個抹除出口」、script-anonymous-retention「管理員刪除不保留」）：
`AccountErasureService.erase_by_admin` 的整合行為。

- 立即抹除：使用中、停權、待刪除（不必等寬限期）的玩家都刪得掉；
- 刪除紀錄的原因是 `admin_deleted`，只有內部編號、時間與原因；
- 劇本一律全刪，不讀偏好（偏好開啟也全刪）；別人用該作者劇本開的存檔保留、來源參照清為空值；用量帳本匿名化；
- 目標是管理員（或剛好變成管理員）時整個交易回復：帳號、劇本、刪除紀錄與同一個交易內的其他寫入都維持原狀。

以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行。
"""
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import ChatConversation, DeletionTombstone, LlmCallLog, RpgSession, Script, ScriptVersion, User
from app.models.admin_audit_log import ACTION_USER_DELETE, AdminAuditLog
from app.models.base import utcnow
from app.models.deletion_tombstone import REASON_ADMIN_DELETED
from app.models.script import VERSION_DRAFT, VERSION_PUBLISHED, VERSION_REMOVED
from app.models.user import STATUS_ACTIVE, STATUS_DELETION_PENDING, STATUS_SUSPENDED
from app.repositories.admin_audit_repository import AdminAuditRepository
from app.repositories.deletion_tombstone_repository import DeletionTombstoneRepository
from app.repositories.script_repository import ScriptRepository
from app.repositories.session_repository import SessionRepository
from app.repositories.user_repository import UserRepository
from app.services.account_erasure_service import AccountErasureService
from tests.support.auth import START, add_user
from tests.support.protagonist import protagonist
from tests.support.script_review import make_author, put_in_status


def make_service(session) -> AccountErasureService:
    return AccountErasureService(
        users=UserRepository(session), scripts=ScriptRepository(session), tombstones=DeletionTombstoneRepository(session),
        commit=session.commit, rollback=session.rollback,
    )


def author_with_scripts(session, n: int = 1, *, keep: bool = True, **overrides) -> User:
    """一位作者：有上架、下架與草稿三個劇本；匿名保留偏好為 `keep`。**已提交**。"""
    user = add_user(session, n, keep_published_scripts=keep, **overrides)
    scripts = ScriptRepository(session)
    put_in_status(scripts, user, VERSION_PUBLISHED, title="上架中的劇本")
    put_in_status(scripts, user, VERSION_REMOVED, title="下架的劇本")
    put_in_status(scripts, user, VERSION_DRAFT, title="草稿劇本")
    session.commit()
    return user


def count(session, model) -> int:
    session.expire_all()
    return session.scalar(select(func.count()).select_from(model))


# ---- 立即抹除與刪除紀錄 -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"status": STATUS_SUSPENDED},
        {"status": STATUS_DELETION_PENDING, "deletion_requested_at": START, "status_before_deletion": STATUS_ACTIVE},   # 才剛申請：不必等 7 天
    ],
    ids=["active", "suspended", "deletion_pending_just_requested"],
)
def test_the_account_is_erased_at_once_whatever_its_state(member_session, overrides):
    s = member_session
    user_id = add_user(s, 1, **overrides).id
    other_id = add_user(s, 2).id
    s.commit()

    assert make_service(s).erase_by_admin(user_id, now=START) is True

    s.expire_all()
    assert s.get(User, user_id) is None and s.get(User, other_id) is not None


def test_the_tombstone_has_the_admin_deleted_reason_and_only_the_id_the_time_and_the_reason(member_session):
    s = member_session
    user_id = add_user(s, 1).id
    s.commit()
    assert make_service(s).erase_by_admin(user_id, now=START) is True

    rows = list(s.scalars(select(DeletionTombstone)))
    assert [(row.user_id, row.reason, row.deleted_at) for row in rows] == [(user_id, REASON_ADMIN_DELETED, START)]
    assert REASON_ADMIN_DELETED == "admin_deleted"
    assert {column.name for column in DeletionTombstone.__table__.columns} == {"id", "user_id", "deleted_at", "reason"}


# ---- 劇本一律全刪 -----------------------------------------------------------------------------


@pytest.mark.parametrize("keep", [True, False], ids=["preference-on", "preference-off"])
def test_every_script_is_deleted_whatever_the_retention_preference(member_session, keep):
    s = member_session
    user = author_with_scripts(s, keep=keep)
    assert make_service(s).erase_by_admin(user.id, now=START) is True
    assert count(s, Script) == 0 and count(s, ScriptVersion) == 0                # 上架中的也沒有保留，沒有「已註銷的作者」的版本


def test_other_players_saves_survive_with_the_source_reference_cleared(member_session):
    s = member_session
    author = author_with_scripts(s, keep=True)
    reader = make_author(s, 9)
    scripts, saves = ScriptRepository(s), SessionRepository(s)
    live = s.scalars(select(ScriptVersion).where(ScriptVersion.status == VERSION_PUBLISHED)).one()
    save = saves.create(
        reader.id, source_version_id=live.id, snapshot=scripts.content_of(live), snapshot_spec_version=1,
        protagonist=protagonist("讀者", 18), content_tier="safe", now=START,
    )
    s.commit()
    save_id = save.id

    assert make_service(s).erase_by_admin(author.id, now=START) is True

    s.expire_all()
    kept = s.get(RpgSession, save_id)
    assert kept is not None and kept.source_version_id is None                   # 存檔有自己的劇本複本，來源參照清為空值


def test_the_ledger_is_anonymised_and_the_conversations_are_deleted(member_session):
    s = member_session
    user = add_user(s, 1)
    conversation = ChatConversation(user_id=user.id, client_id="00000000-0000-4000-8000-000000000001", state_json={})
    s.add(conversation)
    s.flush()
    s.add(LlmCallLog(
        user_id=user.id, conversation_id=conversation.id, kind="chat", provider="gemini", model="m", attempt=1, status="ok",
        counted=True, prompt_tokens=11, completion_tokens=7, latency_ms=250, created_at=utcnow(),
    ))
    s.commit()

    assert make_service(s).erase_by_admin(user.id, now=START) is True

    s.expire_all()
    assert count(s, ChatConversation) == 0
    assert [(row.user_id, row.conversation_id, row.prompt_tokens) for row in s.scalars(select(LlmCallLog))] == [(None, None, 11)]


# ---- 管理員不可刪，條件不成立時整個交易回復 ------------------------------------------------------


def test_an_admin_is_not_deleted_and_nothing_is_left_behind(member_session):
    s = member_session
    admin = add_user(s, 1, role="admin")
    s.commit()
    admin_id = admin.id

    assert make_service(s).erase_by_admin(admin_id, now=START) is False

    s.expire_all()
    assert s.get(User, admin_id) is not None
    assert count(s, DeletionTombstone) == 0


def test_when_the_condition_fails_the_audit_row_written_before_it_is_rolled_back_with_everything_else(member_session):
    """稽核先於抹除寫入同一個 Session：條件不成立（目標剛好變成管理員）時，稽核列與劇本的清理都隨交易一起回復。"""
    s = member_session
    author = author_with_scripts(s, 1, keep=True)
    admin = add_user(s, 2, role="admin")
    s.commit()
    author_id, admin_id = author.id, admin.id
    author.role = "admin"                                                          # 檢查之後、刪除之前變成管理員（未提交的變更也屬於這個交易）
    s.flush()
    audit = AdminAuditRepository(s)
    audit.add(admin_user_id=admin_id, action=ACTION_USER_DELETE, now=START, reason="測試", detail={"from_status": "active"})
    assert count(s, AdminAuditLog) == 1                                            # 先確認稽核列確實寫進了這個交易

    assert make_service(s).erase_by_admin(author_id, now=START) is False

    s.expire_all()
    assert s.get(User, author_id) is not None
    assert count(s, DeletionTombstone) == 0
    assert count(s, AdminAuditLog) == 0                                            # 稽核列隨交易一起消失
    assert count(s, ScriptVersion) == 3                                            # 劇本的清理（無動作外鍵與參照）也回復，三個版本都還在


def test_a_successful_erasure_keeps_the_audit_row_written_before_it(member_session):
    s = member_session
    user = add_user(s, 1)
    admin = add_user(s, 2, role="admin")
    s.commit()
    user_public_id, admin_id, user_id = user.public_id, admin.id, user.id
    AdminAuditRepository(s).add(
        admin_user_id=admin_id, action=ACTION_USER_DELETE, now=START, target_user_public_id=user_public_id,
        reason="測試", detail={"from_status": "active"},
    )

    assert make_service(s).erase_by_admin(user_id, now=START) is True

    s.expire_all()
    rows = list(s.scalars(select(AdminAuditLog)))
    assert [(row.action, row.target_user_public_id, row.detail_json) for row in rows] == [
        (ACTION_USER_DELETE, user_public_id, {"from_status": "active"}),
    ]
    assert s.get(User, user_id) is None                                            # 稽核在，帳號不在（稽核的目標只有對外識別碼，沒有外鍵）
