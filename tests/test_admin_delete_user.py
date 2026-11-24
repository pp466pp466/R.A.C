"""admin-user-management 規格「刪除帳號」「刪除帳號只適用於一般玩家且一次一個」與 admin-audit-log 的 `user.delete`：
`AdminService.delete_user` 的行為（真的 Repository、真的密碼確認與抹除出口，只有時鐘是假的）。

以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行。
"""
import pytest
from sqlalchemy import func, select

from app.models import AdminAuditLog, ChatConversation, DeletionTombstone, Script, ScriptVersion, User
from app.models.script import VERSION_PUBLISHED
from app.models.user import STATUS_ACTIVE, STATUS_DELETION_PENDING, STATUS_SUSPENDED
from app.services.current_user import CurrentUser
from app.services.errors import AdminTargetForbidden, Conflict, NotFound, ValidationError
from tests.support.admin import GOOD_PASSWORD, WRONG_PASSWORD, AdminKit
from tests.support.auth import START, add_user
from tests.support.script_review import put_in_status


@pytest.fixture()
def kit(member_session):
    return AdminKit(member_session)


def actions(kit):
    return [row.action for row in kit.audit_rows()]


def count(kit, model) -> int:
    kit.session.expire_all()
    return kit.session.scalar(select(func.count()).select_from(model))


def exists(kit, user_id) -> bool:
    kit.session.expire_all()
    return kit.session.get(User, user_id) is not None


def delete(kit, admin, member, **overrides):
    values = dict(reason="違規帳號", password=GOOD_PASSWORD)
    values.update(overrides)
    return kit.service.delete_user(admin, member.public_id, **values)


# ---- 成功 -------------------------------------------------------------------------------------


def test_deleting_erases_the_account_at_once_and_leaves_one_audit_row_and_one_tombstone(kit, log_lines):
    admin = kit.admin()
    member = kit.member(2)
    member_id, public_id, email = member.id, member.public_id, member.email

    assert delete(kit, admin, member, reason="  反覆違規  ") is None

    kit.session.expire_all()
    assert kit.session.get(User, member_id) is None
    [entry] = kit.audit_rows()
    assert (entry.action, entry.target_user_public_id, entry.reason, entry.detail) == ("user.delete", public_id, "反覆違規", {"from_status": STATUS_ACTIVE})
    [tombstone] = kit.session.scalars(select(DeletionTombstone))
    assert (tombstone.user_id, tombstone.reason) == (member_id, "admin_deleted")
    events = [line for line in log_lines() if line.get("event") == "admin.action"]
    assert [(e["user_id"], e["reason"]) for e in events] == [(admin.id, "user.delete")]
    assert "反覆違規" not in log_lines.raw() and email not in log_lines.raw()


def test_the_audit_row_has_no_email_or_name_and_survives_the_deletion(kit):
    admin = kit.admin()
    member = kit.member(2, nickname="暱稱先生")
    email, name, public_id = member.email, member.display_name, member.public_id
    delete(kit, admin, member)
    [entry] = kit.audit_rows()
    assert email not in repr(entry) and name not in repr(entry) and "暱稱先生" not in repr(entry)
    assert entry.target_user_public_id == public_id                             # 只有對外識別碼；帳號已不存在，稽核仍在


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": STATUS_SUSPENDED},
        {"locked_at": START, "lock_reason": "rate_limited"},
        {"status": STATUS_DELETION_PENDING, "deletion_requested_at": None, "status_before_deletion": STATUS_ACTIVE},
    ],
    ids=["suspended", "locked", "deletion_pending"],
)
def test_any_state_of_a_player_account_can_be_deleted_including_deletion_pending(kit, overrides):
    admin = kit.admin()
    member = kit.member(2, **overrides)
    member_id, from_status = member.id, member.status
    delete(kit, admin, member)
    assert not exists(kit, member_id)
    assert kit.audit_rows()[0].detail == {"from_status": from_status}


def test_the_conversations_and_all_scripts_go_with_the_account_whatever_the_retention_preference(kit):
    admin = kit.admin()
    member = kit.member(2, keep_published_scripts=True)
    kit.conversation(member, turns=("complaint_feedback",))
    put_in_status(kit.scripts, member, VERSION_PUBLISHED, title="上架中的劇本")
    kit.session.commit()
    assert count(kit, ChatConversation) == 1 and count(kit, Script) == 1

    delete(kit, admin, member)

    assert count(kit, ChatConversation) == 0
    assert count(kit, Script) == 0 and count(kit, ScriptVersion) == 0           # 偏好開啟也沒有保留，沒有「已註銷的作者」的版本


def test_other_players_are_not_touched(kit):
    admin = kit.admin()
    doomed, other = kit.member(2), kit.member(3)
    other_id = other.id
    delete(kit, admin, doomed)
    assert exists(kit, other_id) and kit.reload(other).status == STATUS_ACTIVE
    assert kit.session.get(User, admin.id) is not None                          # 管理員自己當然也還在


def test_the_same_email_can_register_again_as_a_brand_new_account(kit):
    admin = kit.admin()
    member = kit.member(2)
    email = member.email
    delete(kit, admin, member)
    again = add_user(kit.session, 2, age=30)                                     # 同一個 email（add_user 預設 userN@example.com）：沒有唯一鍵衝突
    kit.session.commit()
    assert again.email == email and exists(kit, again.id)


def test_the_deleted_users_old_session_no_longer_resolves(kit):
    admin = kit.admin()
    member = kit.member(2)
    member_id, version = member.id, member.token_version
    delete(kit, admin, member)
    assert kit.auth.resolve_session(member_id, version) is None


# ---- 目標 -------------------------------------------------------------------------------------


def test_another_admin_cannot_be_deleted(kit):
    admin = kit.admin()
    other_admin = add_user(kit.session, 5, role="admin", age=30)
    with pytest.raises(AdminTargetForbidden) as exc:
        delete(kit, admin, other_admin)
    assert exc.value.code == "admin_target_forbidden"
    assert kit.reload(other_admin) is not None and kit.audit_rows() == [] and count(kit, DeletionTombstone) == 0


def test_an_admin_cannot_delete_themselves(kit):
    admin_model = add_user(kit.session, 1, role="admin", age=30)
    admin = CurrentUser.from_model(admin_model)
    with pytest.raises(AdminTargetForbidden):
        delete(kit, admin, admin_model)
    assert kit.reload(admin_model) is not None and kit.audit_rows() == []


@pytest.mark.parametrize("public_id", ["00000000-0000-4000-8000-000000000000", "", None, 5])
def test_an_unknown_id_is_a_404_and_is_not_audited(kit, public_id):
    with pytest.raises(NotFound):
        kit.service.delete_user(kit.admin(), public_id, reason="違規", password=GOOD_PASSWORD)
    assert kit.audit_rows() == [] and count(kit, DeletionTombstone) == 0


def test_a_non_admin_cannot_delete_anyone(kit):
    kit.admin()
    caller = kit.current(kit.member(2))
    victim = kit.member(3)
    with pytest.raises(NotFound):
        delete(kit, caller, victim)
    assert kit.reload(victim) is not None and kit.audit_rows() == []


# ---- 原因與密碼 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("reason", [None, "", "   ", 5, "x" * 201])
def test_a_missing_or_too_long_reason_is_a_400_on_the_reason_field_and_changes_nothing(kit, reason):
    admin = kit.admin()
    member = kit.member(2)
    with pytest.raises(ValidationError) as exc:
        delete(kit, admin, member, reason=reason)
    assert set(exc.value.fields) == {"reason"}
    assert kit.reload(member) is not None and kit.audit_rows() == [] and count(kit, DeletionTombstone) == 0


def test_a_reason_of_exactly_200_characters_is_accepted(kit):
    admin = kit.admin()
    member = kit.member(2)
    delete(kit, admin, member, reason="因" * 200)
    assert kit.audit_rows()[0].reason == "因" * 200


def test_a_wrong_password_deletes_nothing_but_the_failed_attempt_is_audited_and_kept(kit, log_lines):
    admin = kit.admin()
    member = kit.member(2)
    with pytest.raises(ValidationError) as exc:
        delete(kit, admin, member, password=WRONG_PASSWORD)
    assert set(exc.value.fields) == {"password"}
    assert kit.reload(member) is not None and count(kit, DeletionTombstone) == 0
    [entry] = kit.audit_rows()
    assert (entry.action, entry.target_user_public_id, entry.detail) == ("reauth.failed", member.public_id, {"attempted": "user.delete"})
    assert kit.persisted.calls >= 2
    events = [line for line in log_lines() if line.get("event") == "admin.reauth_failed"]
    assert [(e["user_id"], e["reason"]) for e in events] == [(admin.id, "user.delete")]
    assert WRONG_PASSWORD not in log_lines.raw()


@pytest.mark.parametrize("password", [None, "", 12345])
def test_a_missing_password_is_a_400_on_the_password_field_and_nothing_is_deleted(kit, password):
    admin = kit.admin()
    member = kit.member(2)
    with pytest.raises(ValidationError) as exc:
        delete(kit, admin, member, password=password)
    assert set(exc.value.fields) == {"password"}
    assert kit.reload(member) is not None and actions(kit) == ["reauth.failed"]


def test_an_admin_without_a_password_gets_no_password_and_nothing_is_deleted(kit):
    google_admin = CurrentUser.from_model(add_user(kit.session, 7, role="admin", age=30, password=None))
    member = kit.member(2)
    with pytest.raises(ValidationError) as exc:
        delete(kit, google_admin, member, password="anything")
    assert exc.value.code == "no_password" and kit.audit_rows() == []
    assert kit.reload(member) is not None


def test_five_wrong_passwords_lock_the_admin_and_then_even_the_right_one_is_refused(kit):
    admin = kit.admin()
    member = kit.member(2)
    for _ in range(5):
        with pytest.raises(ValidationError):
            delete(kit, admin, member, password=WRONG_PASSWORD)
    with pytest.raises(ValidationError):
        delete(kit, admin, member)
    assert kit.reload(member) is not None and count(kit, DeletionTombstone) == 0


# ---- 原子性 -----------------------------------------------------------------------------------


def test_when_the_target_becomes_an_admin_after_the_check_nothing_is_deleted_and_no_audit_row_remains(kit, monkeypatch):
    """檢查之後、抹除之前目標變成管理員：條件式刪除不成立 → 整個交易回復（稽核列一併消失）→ 409 `state_changed`。"""
    admin = kit.admin()
    member = kit.member(2)
    member_id = member.id
    real_confirm = kit.service._confirm

    def confirm_then_promote(*args, **kwargs):
        real_confirm(*args, **kwargs)
        member.role = "admin"                                                   # 別的請求剛把它升為管理員（已提交）
        kit.session.commit()

    monkeypatch.setattr(kit.service, "_confirm", confirm_then_promote)

    with pytest.raises(Conflict) as exc:
        delete(kit, admin, member)

    assert exc.value.code == "state_changed"
    kit.session.expire_all()
    assert kit.session.get(User, member_id) is not None
    assert count(kit, AdminAuditLog) == 0                                       # 先寫的 user.delete 稽核隨交易回復
    assert count(kit, DeletionTombstone) == 0


def test_when_the_erasure_raises_the_audit_row_written_before_it_does_not_survive_the_rollback(kit, monkeypatch, log_lines):
    admin = kit.admin()
    member = kit.member(2)
    member_id = member.id

    def explode(user_id, *, now):
        raise RuntimeError("抹除失敗")

    monkeypatch.setattr(kit.service._erasure, "erase_by_admin", explode)

    with pytest.raises(RuntimeError):
        delete(kit, admin, member)
    kit.session.rollback()                                                      # 請求結束時框架會回復尚未提交的交易

    kit.session.expire_all()
    assert kit.session.get(User, member_id) is not None
    assert count(kit, AdminAuditLog) == 0 and count(kit, DeletionTombstone) == 0
    assert [line for line in log_lines() if line.get("event") == "admin.action"] == []      # 沒有成功，就沒有成功的日誌
