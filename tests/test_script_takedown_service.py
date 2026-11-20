"""script-takedown 規格（作者部分）：`ScriptAuthoringService.remove／restore`——作者自行下架與恢復、各自的前置狀態與 409／403、
非擁有者與不存在的 404 完全相同、被暫停發佈者仍可操作、不寫管理員稽核只記日誌事件、版本摘要的新欄位與**鍵集合固定且不含審核備註**。

用真的 Repository 與真的 AccessPolicy（只有時鐘是假的），一般測試在 SQLite 與真實 SQL Server 各跑一次。
"""
import logging

import pytest
from sqlalchemy import select

from app.models import AdminAuditLog, ScriptVersion
from app.observability import script_events
from app.models.script import (
    REMOVER_ADMIN, REMOVER_AUTHOR, VERSION_DRAFT, VERSION_PENDING, VERSION_PUBLISHED, VERSION_REJECTED, VERSION_REMOVED,
    VERSION_SUPERSEDED,
)
from app.repositories.errors import LiveVersionExistsError
from app.repositories.script_repository import ScriptRepository
from app.services.current_user import CurrentUser
from app.services.errors import (
    AgeConfirmationRequired, LiveExists, NotDraft, NotFound, NotPending, NotPublished, NotRemoved, RemovalLocked,
)
from tests.support.auth import FakeClock, add_user
from tests.support.script_review import at, authoring_service, new_draft, put_in_status, script_public_id

ADMIN_NOTE = "只有管理員看得到的下架備註"
SUMMARY_KEYS = {
    "id", "version_no", "status", "title", "summary", "content_tier", "content_hash", "total_chars", "created_at", "updated_at",
    "published_at", "decision_category", "decided_at", "removed_by",
}


@pytest.fixture()
def clock():
    return FakeClock()


@pytest.fixture()
def service(member_session, clock):
    return authoring_service(member_session, clock)


@pytest.fixture()
def repo(member_session):
    return ScriptRepository(member_session)


@pytest.fixture()
def alice(member_session):
    return add_user(member_session, 1, nickname="雲遊者", nickname_code="AB01")


@pytest.fixture()
def bob(member_session):
    return add_user(member_session, 2, nickname="旅人", nickname_code="CD02")


def current(user) -> CurrentUser:
    return CurrentUser.from_model(user)


def row(session, version) -> ScriptVersion:
    session.expire_all()
    return session.get(ScriptVersion, version.id)


def version_in(session, repo, author, status, **kwargs):
    version = put_in_status(repo, author, status, **kwargs)
    session.commit()
    return version


# ---- 作者自行下架 --------------------------------------------------------------------------------------


def test_the_author_takes_down_a_published_version_and_it_leaves_the_public_list(member_session, service, repo, alice, clock):
    version = version_in(member_session, repo, alice, VERSION_PUBLISHED)
    assert [v.public_id for v, _ in repo.list_published_cards()] == [version.public_id]
    result = service.remove(current(alice), version.public_id)
    member_session.commit()
    stored = row(member_session, version)
    assert stored.status == VERSION_REMOVED and stored.removed_by == REMOVER_AUTHOR and stored.decided_at is not None
    assert stored.decision_category is None and stored.decision_note is None                  # 作者自行下架沒有類別與備註
    assert result["status"] == VERSION_REMOVED and result["removed_by"] == REMOVER_AUTHOR and result["decision_category"] is None
    assert repo.list_published_cards() == []


def test_taking_down_needs_no_review_no_password_and_writes_no_admin_audit(member_session, service, repo, alice):
    version = version_in(member_session, repo, alice, VERSION_PUBLISHED)
    service.remove(current(alice), version.public_id)
    member_session.commit()
    assert member_session.scalars(select(AdminAuditLog)).all() == []


def test_taking_down_records_an_event_with_the_remover_and_no_script_text(member_session, service, repo, alice, caplog):
    version = version_in(member_session, repo, alice, VERSION_PUBLISHED, title="不該出現在日誌的標題")
    caplog.set_level(logging.INFO, logger="app.script")
    service.remove(current(alice), version.public_id)
    events = [record for record in caplog.records if record.name == "app.script" and record.event == script_events.EVENT_REMOVED]
    assert len(events) == 1 and events[0].kind == "author" and events[0].reason is None
    assert "不該出現在日誌的標題" not in caplog.text and version.public_id not in caplog.text


def test_another_player_gets_the_same_404_as_for_a_missing_id_and_nothing_changes(member_session, service, repo, alice, bob):
    version = version_in(member_session, repo, alice, VERSION_PUBLISHED)
    with pytest.raises(NotFound) as other:
        service.remove(current(bob), version.public_id)
    with pytest.raises(NotFound) as missing:
        service.remove(current(bob), "00000000-0000-0000-0000-000000000000")
    assert (other.value.code, other.value.message) == (missing.value.code, missing.value.message)
    assert row(member_session, version).status == VERSION_PUBLISHED


@pytest.mark.parametrize("bad_id", [None, 5, ["x"], {"a": 1}])
def test_a_non_string_id_is_a_404_not_an_error(service, alice, bad_id):
    with pytest.raises(NotFound):
        service.remove(current(alice), bad_id)
    with pytest.raises(NotFound):
        service.restore(current(alice), bad_id)


@pytest.mark.parametrize("status", [VERSION_DRAFT, VERSION_PENDING, VERSION_REJECTED, VERSION_REMOVED, VERSION_SUPERSEDED])
def test_only_a_published_version_can_be_taken_down(member_session, service, repo, alice, status):
    version = version_in(member_session, repo, alice, status)
    with pytest.raises(NotPublished) as caught:
        service.remove(current(alice), version.public_id)
    assert caught.value.code == "not_published"
    assert row(member_session, version).status == status


def test_a_publish_suspended_author_can_still_take_down(member_session, service, repo, alice, clock):
    version = version_in(member_session, repo, alice, VERSION_PUBLISHED)
    alice.publish_suspended_at = clock()
    member_session.commit()
    service.remove(current(alice), version.public_id)
    member_session.commit()
    assert row(member_session, version).status == VERSION_REMOVED


def test_taking_down_does_not_touch_the_content_or_the_hash(member_session, service, repo, alice):
    version = version_in(member_session, repo, alice, VERSION_PUBLISHED)
    before = (version.content_hash, version.published_at, version.title)
    service.remove(current(alice), version.public_id)
    member_session.commit()
    stored = row(member_session, version)
    assert (stored.content_hash, stored.published_at, stored.title) == before


# ---- 作者恢復 ------------------------------------------------------------------------------------------


def test_the_author_restores_their_own_takedown_without_review_and_the_content_and_time_stay(member_session, service, repo, alice):
    version = version_in(member_session, repo, alice, VERSION_REMOVED, removed_by=REMOVER_AUTHOR)
    published_at, content_hash = row(member_session, version).published_at, version.content_hash
    result = service.restore(current(alice), version.public_id)
    member_session.commit()
    stored = row(member_session, version)
    assert stored.status == VERSION_PUBLISHED and stored.published_at == published_at and stored.content_hash == content_hash
    assert (stored.removed_by, stored.decision_category, stored.decision_note, stored.decided_at) == (None, None, None, None)
    assert result["status"] == VERSION_PUBLISHED and result["removed_by"] is None
    assert [v.public_id for v, _ in repo.list_published_cards()] == [version.public_id]


def test_restoring_records_an_event(member_session, service, repo, alice, caplog):
    version = version_in(member_session, repo, alice, VERSION_REMOVED, removed_by=REMOVER_AUTHOR)
    caplog.set_level(logging.INFO, logger="app.script")
    service.restore(current(alice), version.public_id)
    assert [r.kind for r in caplog.records if r.name == "app.script" and r.event == script_events.EVENT_RESTORED] == ["author"]


def test_an_admin_takedown_cannot_be_restored_by_the_author_and_stays_removed(member_session, service, repo, alice):
    version = version_in(member_session, repo, alice, VERSION_REMOVED, removed_by=REMOVER_ADMIN)
    before = row(member_session, version)
    snapshot = (before.status, before.removed_by, before.decision_category, before.decision_note, before.decided_at)
    with pytest.raises(RemovalLocked) as caught:
        service.restore(current(alice), version.public_id)
    assert caught.value.code == "removal_locked"
    after = row(member_session, version)
    assert (after.status, after.removed_by, after.decision_category, after.decision_note, after.decided_at) == snapshot


@pytest.mark.parametrize("status", [VERSION_DRAFT, VERSION_PENDING, VERSION_PUBLISHED, VERSION_REJECTED, VERSION_SUPERSEDED])
def test_only_a_removed_version_can_be_restored(member_session, service, repo, alice, status):
    version = version_in(member_session, repo, alice, status)
    with pytest.raises(NotRemoved) as caught:
        service.restore(current(alice), version.public_id)
    assert caught.value.code == "not_removed"
    assert row(member_session, version).status == status


def test_after_a_newer_version_goes_live_the_old_takedown_is_superseded_and_cannot_come_back(member_session, service, repo, alice):
    first = version_in(member_session, repo, alice, VERSION_REMOVED, removed_by=REMOVER_AUTHOR)
    second = new_draft(repo, alice, script_public_id(repo, first))
    repo.submit_for_review(alice.id, second.public_id, now=at(5))
    repo.approve_version(second.public_id, now=at(6))
    member_session.commit()
    assert row(member_session, first).status == VERSION_SUPERSEDED
    with pytest.raises(NotRemoved):
        service.restore(current(alice), first.public_id)
    assert row(member_session, second).status == VERSION_PUBLISHED


def test_a_live_version_found_at_restore_time_is_a_409_and_the_takedown_stays(member_session, service, repo, alice, monkeypatch):
    version = version_in(member_session, repo, alice, VERSION_REMOVED, removed_by=REMOVER_AUTHOR)

    def another_version_went_live(*args, **kwargs):
        raise LiveVersionExistsError

    monkeypatch.setattr(repo.__class__, "restore_own", another_version_went_live)
    with pytest.raises(LiveExists) as caught:
        service.restore(current(alice), version.public_id)
    assert caught.value.code == "live_exists"
    assert row(member_session, version).status == VERSION_REMOVED


def test_another_player_cannot_restore_and_gets_a_404(member_session, service, repo, alice, bob):
    version = version_in(member_session, repo, alice, VERSION_REMOVED, removed_by=REMOVER_AUTHOR)
    with pytest.raises(NotFound):
        service.restore(current(bob), version.public_id)
    assert row(member_session, version).status == VERSION_REMOVED


def test_a_publish_suspended_author_can_restore_too(member_session, service, repo, alice, clock):
    version = version_in(member_session, repo, alice, VERSION_REMOVED, removed_by=REMOVER_AUTHOR)
    alice.publish_suspended_at = clock()
    member_session.commit()
    service.restore(current(alice), version.public_id)
    member_session.commit()
    assert row(member_session, version).status == VERSION_PUBLISHED


# ---- 3.4 盤點：其餘依版本狀態分支的作者端方法怎麼對待 `removed` ---------------------------------------------


@pytest.mark.parametrize("removed_by", [REMOVER_AUTHOR, REMOVER_ADMIN])
def test_a_removed_version_is_frozen_it_cannot_be_edited_deleted_or_withdrawn(member_session, service, repo, alice, removed_by):
    version = version_in(member_session, repo, alice, VERSION_REMOVED, removed_by=removed_by)
    before = row(member_session, version)
    snapshot = (before.status, before.title, before.content_hash, before.removed_by)
    with pytest.raises(NotDraft):
        service.update_draft(current(alice), version.public_id, repo.content_of(before), before.content_hash)
    with pytest.raises(NotDraft):
        service.delete_draft(current(alice), version.public_id)
    with pytest.raises(NotPending):
        service.withdraw(current(alice), version.public_id)
    after = row(member_session, version)
    assert (after.status, after.title, after.content_hash, after.removed_by) == snapshot


@pytest.mark.parametrize("removed_by", [REMOVER_AUTHOR, REMOVER_ADMIN])
def test_a_new_draft_can_be_made_from_a_removed_latest_version_and_the_removed_one_does_not_change(member_session, service, repo, alice, removed_by):
    version = version_in(member_session, repo, alice, VERSION_REMOVED, removed_by=removed_by)
    script_id = script_public_id(repo, version)
    draft = service.new_version(current(alice), script_id)
    member_session.commit()
    assert draft["status"] == VERSION_DRAFT and draft["version_no"] == version.version_no + 1
    assert draft["content_hash"] == version.content_hash and draft["decision_category"] is None and draft["removed_by"] is None
    old = row(member_session, version)
    assert (old.status, old.removed_by) == (VERSION_REMOVED, removed_by)


def test_when_the_new_version_is_approved_the_old_takedown_becomes_superseded_and_stays_out_of_the_catalog(member_session, service, repo, alice):
    version = version_in(member_session, repo, alice, VERSION_REMOVED, removed_by=REMOVER_ADMIN)
    draft = service.new_version(current(alice), script_public_id(repo, version))
    repo.submit_for_review(alice.id, draft["id"], now=at(7))
    repo.approve_version(draft["id"], now=at(8))
    member_session.commit()
    assert row(member_session, version).status == VERSION_SUPERSEDED
    assert [v.public_id for v, _ in repo.list_published_cards()] == [draft["id"]]


def test_a_removed_version_is_invisible_to_every_public_read(member_session, repo, alice):
    version = version_in(member_session, repo, alice, VERSION_REMOVED, removed_by=REMOVER_AUTHOR)
    assert repo.get_published(version.public_id) is None
    assert repo.get_published_by_script(script_public_id(repo, version)) is None
    assert repo.list_published(limit=50) == [] and repo.list_published_cards() == []


# ---- 閘門（鎖定、等待刪除等受限登入狀態由 `require_login` 在 HTTP 層擋下，見 test_script_api_takedown）-------------


def test_a_player_without_age_confirmation_gets_the_age_gate_on_both(member_session, service, repo, alice):
    version = version_in(member_session, repo, alice, VERSION_PUBLISHED)
    alice.age_confirmed_at = alice.birth_date = alice.age_range = None
    member_session.commit()
    for call in (service.remove, service.restore):
        with pytest.raises(AgeConfirmationRequired):
            call(current(alice), version.public_id)
    assert row(member_session, version).status == VERSION_PUBLISHED


# ---- 版本摘要：新欄位、鍵集合固定、不含備註 ------------------------------------------------------------------


def test_every_version_summary_has_exactly_the_documented_keys(member_session, service, repo, alice):
    for status in (VERSION_DRAFT, VERSION_PENDING, VERSION_PUBLISHED, VERSION_REJECTED, VERSION_REMOVED):
        version_in(member_session, repo, alice, status)
    scripts = service.list_mine(current(alice))
    assert len(scripts) == 5
    for script in scripts:
        for summary in script["versions"]:
            assert set(summary) == SUMMARY_KEYS


def test_a_rejected_version_shows_the_category_and_time_but_never_the_note(member_session, service, repo, alice):
    version = version_in(member_session, repo, alice, VERSION_REJECTED)
    summary = service.get_version(current(alice), version.public_id)
    assert summary["decision_category"] == "other_policy" and summary["decided_at"] is not None and summary["removed_by"] is None
    assert "管理員的備註" not in repr(service.list_mine(current(alice))) and "管理員的備註" not in repr(summary)


def test_an_admin_takedown_shows_the_remover_and_category_but_never_the_note(member_session, service, repo, alice):
    version = version_in(member_session, repo, alice, VERSION_REMOVED, removed_by=REMOVER_ADMIN)
    summary = service.get_version(current(alice), version.public_id)
    assert summary["status"] == VERSION_REMOVED and summary["removed_by"] == REMOVER_ADMIN
    assert summary["decision_category"] == "other_policy" and summary["decided_at"] is not None
    assert "下架的備註" not in repr(service.list_mine(current(alice))) + repr(summary)


def test_an_author_takedown_has_no_category(member_session, service, repo, alice):
    version = version_in(member_session, repo, alice, VERSION_REMOVED, removed_by=REMOVER_AUTHOR)
    summary = service.get_version(current(alice), version.public_id)
    assert summary["removed_by"] == REMOVER_AUTHOR and summary["decision_category"] is None and summary["decided_at"] is not None


def test_a_version_that_was_never_decided_has_empty_decision_fields(member_session, service, repo, alice):
    version = version_in(member_session, repo, alice, VERSION_PUBLISHED)
    summary = service.get_version(current(alice), version.public_id)
    assert (summary["decision_category"], summary["decided_at"], summary["removed_by"]) == (None, None, None)


def test_a_restored_version_has_its_decision_fields_cleared_in_the_summary(member_session, service, repo, alice):
    version = version_in(member_session, repo, alice, VERSION_REMOVED, removed_by=REMOVER_AUTHOR)
    summary = service.restore(current(alice), version.public_id)
    assert (summary["decision_category"], summary["decided_at"], summary["removed_by"]) == (None, None, None)
