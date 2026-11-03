"""admin-audit-log 規格：稽核紀錄的內容、固定的動作、不記錄敏感內容、不可修改、管理員被刪除後保留。"""
import inspect
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.models import AdminAuditLog, User
from app.models.admin_audit_log import AUDIT_ACTIONS, MAX_REASON_CHARS
from app.repositories.admin_audit_repository import AdminAuditRepository, AuditEntry
from tests.support.auth import add_user

NOW = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)
TARGET = "11111111-1111-4111-8111-111111111111"
CONVERSATION = "22222222-2222-4222-8222-222222222222"


@pytest.fixture()
def repo(member_session):
    return AdminAuditRepository(member_session)


@pytest.fixture()
def admin(member_session):
    return add_user(member_session, 1, role="admin")


def count(session):
    return session.scalar(select(func.count()).select_from(AdminAuditLog))


def test_a_row_keeps_every_documented_field(repo, member_session, admin):
    repo.add(
        admin_user_id=admin.id, action="user.change_tier", now=NOW, target_user_public_id=TARGET,
        target_conversation_public_id=CONVERSATION, reason="使用者來信要求升級", detail={"from_tier": "free", "to_tier": "plus", "count": 3},
    )
    member_session.commit()
    member_session.expire_all()
    stored = member_session.scalars(select(AdminAuditLog)).one()
    assert (stored.admin_user_id, stored.action, stored.target_user_public_id, stored.target_conversation_public_id) == (
        admin.id, "user.change_tier", TARGET, CONVERSATION,
    )
    assert stored.reason == "使用者來信要求升級" and stored.created_at == NOW
    assert stored.detail_json == {"from_tier": "free", "to_tier": "plus", "count": 3}


def test_the_documented_actions_are_exactly_the_twelve_in_the_specification():
    assert set(AUDIT_ACTIONS) == {
        "user.lookup", "user.suspend", "user.unsuspend", "user.change_tier",
        "conversation.read", "conversation.delete", "failures.read", "reauth.failed",
        "account.lock", "appeal.submit", "appeal.read", "account.unlock",
    }


@pytest.mark.parametrize("action", AUDIT_ACTIONS)
def test_every_listed_action_can_be_written(repo, member_session, admin, action):
    repo.add(admin_user_id=admin.id, action=action, now=NOW)
    assert count(member_session) == 1


@pytest.mark.parametrize("action", ["user.delete_everything", "", "USER.SUSPEND", "user.suspend ", None])
def test_an_action_outside_the_list_is_refused_and_nothing_is_written(repo, member_session, admin, action):
    with pytest.raises(ValueError):
        repo.add(admin_user_id=admin.id, action=action, now=NOW)
    assert count(member_session) == 0


@pytest.mark.parametrize(("length", "accepted"), [(0, True), (1, True), (MAX_REASON_CHARS, True), (MAX_REASON_CHARS + 1, False)])
def test_the_reason_is_limited_to_200_characters(repo, member_session, admin, length, accepted):
    if accepted:
        repo.add(admin_user_id=admin.id, action="user.suspend", now=NOW, reason="因" * length)
        assert count(member_session) == 1
    else:
        with pytest.raises(ValueError):
            repo.add(admin_user_id=admin.id, action="user.suspend", now=NOW, reason="因" * length)
        assert count(member_session) == 0


@pytest.mark.parametrize(
    "detail",
    [
        {"note": "使用者說了一些話"},                   # 中文與空白不是列舉值
        {"note": "someone@example.com"},                # email 不是列舉值（有 @）
        {"note": "has a space"},
        {"note": "x" * 41},                             # 太長
        {"nested": {"a": 1}},                           # 巢狀
        {"items": [1, 2]},
        {"flag": True},                                 # 布林不是整數
        {"ratio": 0.5},
        {"Bad Key": "x"},
        {"": "x"},
    ],
)
def test_details_that_could_carry_free_text_are_refused(repo, member_session, admin, detail):
    with pytest.raises(ValueError):
        repo.add(admin_user_id=admin.id, action="user.lookup", now=NOW, detail=detail)
    assert count(member_session) == 0


def test_details_with_enumerated_values_and_integers_are_accepted(repo, member_session, admin):
    repo.add(admin_user_id=admin.id, action="user.lookup", now=NOW, detail={"via": "email"})
    repo.add(admin_user_id=admin.id, action="failures.read", now=NOW, detail={"count": 0})
    repo.add(admin_user_id=admin.id, action="user.suspend", now=NOW, detail={"from_status": "pending_verification"})
    assert count(member_session) == 3


def test_a_missing_detail_is_stored_as_an_empty_object(repo, member_session, admin):
    repo.add(admin_user_id=admin.id, action="conversation.read", now=NOW)
    member_session.commit()
    member_session.expire_all()
    assert member_session.scalars(select(AdminAuditLog)).one().detail_json == {}


# ---- 檢視 -----------------------------------------------------------------------------------

def test_the_list_is_newest_first_and_breaks_ties_by_id(repo, member_session, admin):
    first = repo.add(admin_user_id=admin.id, action="user.lookup", now=NOW)
    second = repo.add(admin_user_id=admin.id, action="user.suspend", now=NOW + timedelta(minutes=1))
    third = repo.add(admin_user_id=admin.id, action="user.unsuspend", now=NOW + timedelta(minutes=1))
    entries = repo.list(limit=10, offset=0)
    assert [e.id for e in entries] == [third.id, second.id, first.id]
    assert all(isinstance(e, AuditEntry) for e in entries)


def test_the_list_pages(repo, admin):
    for minute in range(5):
        repo.add(admin_user_id=admin.id, action="user.lookup", now=NOW + timedelta(minutes=minute), detail={"count": minute})
    assert [e.detail["count"] for e in repo.list(limit=2, offset=0)] == [4, 3]
    assert [e.detail["count"] for e in repo.list(limit=2, offset=2)] == [2, 1]
    assert [e.detail["count"] for e in repo.list(limit=2, offset=4)] == [0]
    assert repo.list(limit=2, offset=5) == []


def test_an_entry_shows_the_admins_display_name_and_never_the_email_or_internal_ids(repo, member_session, admin):
    repo.add(admin_user_id=admin.id, action="user.suspend", now=NOW, target_user_public_id=TARGET, reason="違規")
    [entry] = repo.list(limit=10, offset=0)
    assert entry.admin_display_name == admin.display_name
    assert admin.email not in repr(entry)
    assert set(vars(entry)) == {                      # 欄位集合固定：沒有 email、管理員的內部編號或任何個資欄位
        "id", "created_at", "action", "admin_display_name", "target_user_public_id", "target_conversation_public_id", "reason", "detail",
    }


def test_deleting_the_admin_keeps_the_rows_and_clears_the_reference(repo, member_session, admin):
    repo.add(admin_user_id=admin.id, action="user.suspend", now=NOW, target_user_public_id=TARGET)
    member_session.commit()
    member_session.delete(member_session.get(User, admin.id))
    member_session.commit()
    member_session.expire_all()
    stored = member_session.scalars(select(AdminAuditLog)).one()
    assert stored.admin_user_id is None and stored.target_user_public_id == TARGET
    [entry] = repo.list(limit=10, offset=0)
    assert entry.admin_display_name is None and entry.action == "user.suspend"


def test_the_target_survives_the_target_being_deleted(repo, member_session, admin):
    victim = add_user(member_session, 2)
    repo.add(admin_user_id=admin.id, action="user.suspend", now=NOW, target_user_public_id=victim.public_id)
    member_session.commit()
    member_session.delete(member_session.get(User, victim.id))
    member_session.commit()
    member_session.expire_all()
    assert member_session.scalars(select(AdminAuditLog)).one().target_user_public_id == victim.public_id


def test_an_audit_row_may_have_no_admin(repo, member_session):
    repo.add(admin_user_id=None, action="reauth.failed", now=NOW)
    assert count(member_session) == 1


# ---- 不可修改 -------------------------------------------------------------------------------

def test_the_repository_offers_only_adding_and_reading():
    public = {name for name, member in inspect.getmembers(AdminAuditRepository, inspect.isfunction) if not name.startswith("_")}
    assert public == {"add", "list"}


def test_the_model_defines_no_behaviour_of_its_own():
    """模型只是資料：沒有任何自訂的方法（更新、刪除、清除……）。"""
    own_methods = [name for name, value in vars(AdminAuditLog).items() if callable(value) and not name.startswith("_")]
    assert own_methods == []
