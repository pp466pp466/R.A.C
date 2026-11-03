"""admin-user-management 規格：後台對使用者的查詢與條件式改變（停權、解除、調整等級）。"""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy.orm import Session

from app.models import User
from app.models.user import (
    MEMBER_TIERS, STATUS_ACTIVE, STATUS_DELETION_PENDING, STATUS_SUSPENDED, USER_STATUSES,
)
from app.repositories.admin_repository import AdminRepository
from app.services.normalization import canonical_email
from tests.support.auth import add_user

SUSPENDABLE = (STATUS_ACTIVE,)


@pytest.fixture()
def repo(member_session):
    return AdminRepository(member_session)


def reload(session, user):
    session.expire_all()
    return session.get(User, user.id)


# ---- 查詢 -----------------------------------------------------------------------------------

def test_a_user_is_found_by_the_canonical_form_of_their_email(repo, member_session):
    user = add_user(member_session, 1)
    assert repo.find_user_by_canonical_email(user.email_canonical).id == user.id


def test_gmail_variants_reach_the_same_user_through_the_canonical_form(repo, member_session):
    user = add_user(member_session, 1, email="Some.One+tag@gmail.com", email_canonical=canonical_email("some.one@gmail.com"))
    assert repo.find_user_by_canonical_email(canonical_email("someone+other@gmail.com")).id == user.id


@pytest.mark.parametrize("probe", ["user", "user1@example.co", "ser1@example.com", "", "%", "user1@example.com%"])
def test_there_is_no_partial_matching(repo, member_session, probe):
    add_user(member_session, 1)
    assert repo.find_user_by_canonical_email(probe) is None


def test_a_user_is_found_by_the_public_id_and_unknown_ids_find_nothing(repo, member_session):
    user = add_user(member_session, 1)
    assert repo.find_user_by_public_id(user.public_id).id == user.id
    assert repo.find_user_by_public_id("00000000-0000-4000-8000-000000000000") is None
    assert repo.find_user_by_public_id("") is None


def test_the_repository_cannot_list_users():
    public = {name for name in dir(AdminRepository) if not name.startswith("_")}
    assert not {name for name in public if "list" in name or "all_users" in name or "search" in name} & {
        "list_users", "all_users", "search_users", "list", "search"
    }


# ---- 停權 -----------------------------------------------------------------------------------

@pytest.mark.parametrize("status", SUSPENDABLE)
def test_suspending_changes_the_status_and_bumps_the_token_version(repo, member_session, status):
    user = add_user(member_session, 1, status=status)
    version = user.token_version
    assert repo.suspend_user(user, allowed_from=SUSPENDABLE) is True
    row = reload(member_session, user)
    assert (row.status, row.token_version) == (STATUS_SUSPENDED, version + 1)


@pytest.mark.parametrize("status", [s for s in USER_STATUSES if s not in SUSPENDABLE])
def test_suspending_a_user_in_another_state_writes_nothing(repo, member_session, status):
    user = add_user(member_session, 1, status=status)
    version = user.token_version
    assert repo.suspend_user(user, allowed_from=SUSPENDABLE) is False
    row = reload(member_session, user)
    assert (row.status, row.token_version) == (status, version)         # 沒有寫入，登入狀態也沒有被無端撤銷


def test_suspending_does_not_touch_other_users(repo, member_session):
    target, bystander = add_user(member_session, 1), add_user(member_session, 2)
    repo.suspend_user(target, allowed_from=SUSPENDABLE)
    other = reload(member_session, bystander)
    assert (other.status, other.token_version) == (STATUS_ACTIVE, bystander.token_version)


def test_unsaved_changes_to_the_object_are_flushed_before_the_conditional_update(repo, member_session):
    user = add_user(member_session, 1)
    user.display_name = "改過的名稱"
    repo.suspend_user(user, allowed_from=SUSPENDABLE)
    assert reload(member_session, user).display_name == "改過的名稱"


# ---- 解除停權 -------------------------------------------------------------------------------

def test_unsuspending_restores_active(repo, member_session):
    user = add_user(member_session, 1, status=STATUS_SUSPENDED)
    assert repo.unsuspend_user(user) is True
    assert reload(member_session, user).status == STATUS_ACTIVE


@pytest.mark.parametrize("status", [s for s in USER_STATUSES if s != STATUS_SUSPENDED])
def test_only_a_suspended_user_can_be_unsuspended(repo, member_session, status):
    user = add_user(member_session, 1, status=status)
    assert repo.unsuspend_user(user) is False
    assert reload(member_session, user).status == status


def test_unsuspending_does_not_change_the_token_version(repo, member_session):
    user = add_user(member_session, 1, status=STATUS_SUSPENDED)
    version = user.token_version
    repo.unsuspend_user(user)
    assert reload(member_session, user).token_version == version       # 停權時已撤銷既有登入；解除不需要再改


# ---- 調整等級 -------------------------------------------------------------------------------

@pytest.mark.parametrize(("before", "after"), [(a, b) for a in MEMBER_TIERS for b in MEMBER_TIERS if a != b])
def test_changing_to_a_different_tier_writes_it(repo, member_session, before, after):
    user = add_user(member_session, 1, member_tier=before)
    assert repo.change_tier(user, tier=after) is True
    assert reload(member_session, user).member_tier == after


@pytest.mark.parametrize("tier", MEMBER_TIERS)
def test_changing_to_the_same_tier_writes_nothing(repo, member_session, tier):
    user = add_user(member_session, 1, member_tier=tier)
    assert repo.change_tier(user, tier=tier) is False


@pytest.mark.parametrize("bad", ["gold", "", "FREE", None])
def test_an_unknown_tier_is_refused(repo, member_session, bad):
    user = add_user(member_session, 1)
    with pytest.raises(ValueError):
        repo.change_tier(user, tier=bad)
    assert reload(member_session, user).member_tier == "free"


# ---- 並行：兩位管理員同時操作只有一個成功 --------------------------------------------------------

def test_two_admins_suspending_the_same_user_at_once_only_one_succeeds_and_the_token_version_moves_once(concurrent_engine):
    with Session(concurrent_engine) as setup:
        target = add_user(setup, 1)
        target_id, version = target.id, target.token_version
    gate = threading.Barrier(2)

    def suspend() -> bool:
        with Session(concurrent_engine) as session:
            user = session.get(User, target_id)
            gate.wait(10)
            changed = AdminRepository(session).suspend_user(user, allowed_from=SUSPENDABLE)
            session.commit()
            return changed

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: suspend(), range(2)))
    assert sorted(results) == [False, True]
    with Session(concurrent_engine) as check:
        row = check.get(User, target_id)
        assert (row.status, row.token_version) == (STATUS_SUSPENDED, version + 1)


def test_two_admins_changing_the_tier_to_the_same_value_at_once_only_one_writes(concurrent_engine):
    with Session(concurrent_engine) as setup:
        target_id = add_user(setup, 1).id
    gate = threading.Barrier(2)

    def change() -> bool:
        with Session(concurrent_engine) as session:
            user = session.get(User, target_id)
            gate.wait(10)
            changed = AdminRepository(session).change_tier(user, tier="plus")
            session.commit()
            return changed

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: change(), range(2)))
    assert sorted(results) == [False, True]
