"""script-store 規格：劇本與版本的資料行為（身分、狀態轉移、不可變、唯一性、取代、筆名鎖定、標籤、讀取範圍、刪除）。

一般測試以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行；並行測試另用 `concurrent_engine`，
以真實的執行緒與獨立連線證明「最多一個成功」。
"""
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Base, RpgSession, Script, ScriptVersion, ScriptVersionTag, User
from app.models.base import utcnow
from app.models.script import (
    OFFICIAL_AUTHOR_NAME, VERSION_DRAFT, VERSION_PENDING, VERSION_PUBLISHED, VERSION_REJECTED, VERSION_STATUSES, VERSION_SUPERSEDED,
)
from app.repositories.errors import (
    AuthorNameLockedError, InvalidVersionTransitionError, OpenVersionExistsError, VersionNotDraftError,
)
from app.repositories.script_repository import ScriptRepository
from tests.support.script_data import valid, validated

NOW = utcnow()


def make_user(session, n: int = 1) -> User:
    user = User(email=f"u{n}@example.com", email_canonical=f"u{n}@example.com", display_name=f"使用者{n}", status="active")
    session.add(user)
    session.flush()
    return user


@pytest.fixture()
def repo(member_session):
    return ScriptRepository(member_session)


def new_script(repo, user, **changes) -> ScriptVersion:
    result = validated(**changes)
    return repo.create_script(
        user.id, author_name=f"筆名{user.id}", author_code="a1b2c3", content=result.content, content_hash=result.content_hash,
        total_chars=result.total_chars,
    )


def new_draft(repo, user, script_public_id, **changes) -> ScriptVersion:
    result = validated(**changes)
    return repo.create_draft_version(
        user.id, script_public_id, content=result.content, content_hash=result.content_hash, total_chars=result.total_chars
    )


def publish(repo, user, version, now=NOW) -> ScriptVersion:
    repo.submit_for_review(user.id, version.public_id)
    return repo.approve_version(version.public_id, now=now)


def script_public_id(repo, version) -> str:
    return repo.get_script_of(version).public_id


def status_of(session, version) -> str:
    session.expire_all()
    return session.get(ScriptVersion, version.id).status


# ---- 劇本的身分、擁有者與內建劇本 ----------------------------------------------------------

def test_a_new_script_has_a_uuid_public_id_an_owner_and_a_first_draft(repo, member_session):
    alice = make_user(member_session)
    first, second = new_script(repo, alice), new_script(repo, alice)
    scripts = [repo.get_script_of(first), repo.get_script_of(second)]
    assert all(re.fullmatch(r"[0-9a-f-]{36}", s.public_id) for s in scripts) and scripts[0].public_id != scripts[1].public_id
    assert scripts[0].creator_id == alice.id and scripts[0].seed_key is None
    assert (first.version_no, first.status, first.open_script_id, first.live_script_id) == (1, VERSION_DRAFT, scripts[0].id, None)
    assert scripts[0].author_name == f"筆名{alice.id}" and scripts[0].author_code == "a1b2c3" and scripts[0].author_locked_at is None


def test_two_player_scripts_without_seed_keys_can_coexist(repo, member_session):
    alice, bob = make_user(member_session, 1), make_user(member_session, 2)
    new_script(repo, alice)
    new_script(repo, bob)
    member_session.commit()
    assert member_session.scalar(select(func.count()).select_from(Script).where(Script.seed_key.is_(None))) == 2


def test_a_builtin_script_has_no_author_a_seed_key_and_is_published_without_review(repo, member_session):
    result = validated()
    version, created = repo.publish_builtin("xianxia-test", content=result.content, content_hash=result.content_hash, total_chars=result.total_chars, now=NOW)
    script = repo.get_script_of(version)
    assert created is True
    assert (script.creator_id, script.seed_key, script.author_name, script.author_locked_at) == (None, "xianxia-test", OFFICIAL_AUTHOR_NAME, NOW)
    assert (version.version_no, version.status, version.live_script_id, version.open_script_id, version.published_at) == (1, VERSION_PUBLISHED, script.id, None, NOW)


def test_publishing_the_same_seed_key_again_adds_a_version_and_supersedes_instead_of_creating_a_second_script(repo, member_session):
    first = validated()
    repo.publish_builtin("k", content=first.content, content_hash=first.content_hash, total_chars=first.total_chars, now=NOW)
    changed = validated(title="青雲問道二")
    version, created = repo.publish_builtin("k", content=changed.content, content_hash=changed.content_hash, total_chars=changed.total_chars, now=NOW)
    member_session.commit()
    assert created is False and version.version_no == 2 and version.status == VERSION_PUBLISHED
    assert member_session.scalar(select(func.count()).select_from(Script).where(Script.seed_key == "k")) == 1
    script, live = repo.get_builtin("k")
    assert live.id == version.id
    assert [v.status for v in member_session.scalars(select(ScriptVersion).where(ScriptVersion.script_id == script.id).order_by(ScriptVersion.version_no))] == [VERSION_SUPERSEDED, VERSION_PUBLISHED]
    assert repo.get_builtin("missing") is None


# ---- 版本的狀態與轉移 -----------------------------------------------------------------------

def test_version_numbers_run_one_two_three_within_a_script(repo, member_session):
    alice = make_user(member_session)
    first = new_script(repo, alice)
    pid = script_public_id(repo, first)
    publish(repo, alice, first)
    second = new_draft(repo, alice, pid)
    publish(repo, alice, second)
    third = new_draft(repo, alice, pid)
    assert [first.version_no, second.version_no, third.version_no] == [1, 2, 3]


def test_a_draft_is_submitted_and_approved(repo, member_session):
    alice = make_user(member_session)
    version = new_script(repo, alice)
    assert repo.submit_for_review(alice.id, version.public_id).status == VERSION_PENDING
    approved = repo.approve_version(version.public_id, now=NOW)
    assert (approved.status, approved.published_at, approved.live_script_id, approved.open_script_id) == (VERSION_PUBLISHED, NOW, approved.script_id, None)


def test_a_rejected_version_cannot_become_published_and_keeps_its_content(repo, member_session):
    alice = make_user(member_session)
    version = new_script(repo, alice)
    repo.submit_for_review(alice.id, version.public_id)
    assert repo.reject_version(version.public_id).status == VERSION_REJECTED
    with pytest.raises(InvalidVersionTransitionError):
        repo.approve_version(version.public_id, now=NOW)
    assert status_of(member_session, version) == VERSION_REJECTED
    assert member_session.get(ScriptVersion, version.id).title == "青雲問道"
    # 退回之後不再佔用「進行中」的位置，作者可以開新的草稿
    assert new_draft(repo, alice, script_public_id(repo, version)).version_no == 2


# 每個狀態嘗試每一種轉移：只有固定集合內的成功，其餘一律拒絕且狀態、輔助欄位都不變
def put_in_status(repo, session, user, status):
    version = new_script(repo, user)
    pid = script_public_id(repo, version)
    if status == VERSION_DRAFT:
        return version
    repo.submit_for_review(user.id, version.public_id)
    if status == VERSION_PENDING:
        return version
    if status == VERSION_REJECTED:
        repo.reject_version(version.public_id)
        return version
    repo.approve_version(version.public_id, now=NOW)
    if status == VERSION_PUBLISHED:
        return version
    successor = new_draft(repo, user, pid)
    publish(repo, user, successor)
    return version                                                    # 第 1 版：superseded


TRANSITIONS = {
    "submit": (VERSION_DRAFT, VERSION_PENDING),
    "withdraw": (VERSION_PENDING, VERSION_DRAFT),
    "approve": (VERSION_PENDING, VERSION_PUBLISHED),
    "reject": (VERSION_PENDING, VERSION_REJECTED),
}


def attempt(repo, user, version, name):
    if name == "submit":
        return repo.submit_for_review(user.id, version.public_id)
    if name == "withdraw":
        return repo.withdraw(user.id, version.public_id)
    if name == "approve":
        return repo.approve_version(version.public_id, now=NOW)
    return repo.reject_version(version.public_id)


@pytest.mark.parametrize("name", sorted(TRANSITIONS))
@pytest.mark.parametrize("status", VERSION_STATUSES)
def test_only_the_fixed_set_of_transitions_is_allowed_and_a_refusal_changes_nothing(member_session, status, name):
    repo = ScriptRepository(member_session)
    alice = make_user(member_session)
    version = put_in_status(repo, member_session, alice, status)
    member_session.commit()
    source, target = TRANSITIONS[name]
    before = (status_of(member_session, version), version.live_script_id, version.open_script_id)
    assert before[0] == status
    if status == source:
        assert attempt(repo, alice, version, name).status == target
    else:
        with pytest.raises(InvalidVersionTransitionError):
            attempt(repo, alice, version, name)
        member_session.expire_all()
        assert (version.status, version.live_script_id, version.open_script_id) == before


@pytest.mark.parametrize("status", VERSION_STATUSES)
def test_the_helper_columns_hold_if_and_only_if_the_status_is_in_their_set(member_session, status):
    repo = ScriptRepository(member_session)
    alice = make_user(member_session)
    version = put_in_status(repo, member_session, alice, status)
    member_session.commit()
    member_session.expire_all()
    for row in member_session.scalars(select(ScriptVersion)):
        assert (row.live_script_id == row.script_id) == (row.status == VERSION_PUBLISHED), (row.id, row.status)
        assert (row.open_script_id == row.script_id) == (row.status in (VERSION_DRAFT, VERSION_PENDING)), (row.id, row.status)
        assert row.live_script_id in (None, row.script_id) and row.open_script_id in (None, row.script_id)


def test_a_withdrawn_version_can_be_edited_again_and_a_pending_one_cannot(repo, member_session):
    alice = make_user(member_session)
    version = new_script(repo, alice)
    repo.submit_for_review(alice.id, version.public_id)
    changed = validated(title="改過的標題")
    with pytest.raises(VersionNotDraftError):
        repo.update_draft(alice.id, version.public_id, content=changed.content, content_hash=changed.content_hash, total_chars=changed.total_chars)
    assert status_of(member_session, version) == VERSION_PENDING and member_session.get(ScriptVersion, version.id).title == "青雲問道"
    repo.withdraw(alice.id, version.public_id)
    edited = repo.update_draft(alice.id, version.public_id, content=changed.content, content_hash=changed.content_hash, total_chars=changed.total_chars)
    assert edited.title == "改過的標題" and edited.content_hash == changed.content_hash


# ---- 非草稿的內容不可變 -----------------------------------------------------------------------

def test_the_owner_can_edit_a_draft_including_its_tags(repo, member_session):
    alice = make_user(member_session)
    version = new_script(repo, alice)
    changed = validated(title="新標題", tags=["科幻"], background="新的背景", characters=[
        {"name": "甲", "personality": "冷靜", "age": 3, "appearance": "嬰兒"},
    ])
    edited = repo.update_draft(alice.id, version.public_id, content=changed.content, content_hash=changed.content_hash, total_chars=changed.total_chars)
    assert (edited.title, edited.background, edited.characters_json[0]["age"]) == ("新標題", "新的背景", 3)
    assert repo.tags_of(edited) == ["科幻"] and edited.total_chars == changed.total_chars


@pytest.mark.parametrize("status", [VERSION_PENDING, VERSION_PUBLISHED, VERSION_REJECTED, VERSION_SUPERSEDED])
def test_content_and_tags_outside_the_draft_state_are_immutable(member_session, status):
    repo = ScriptRepository(member_session)
    alice = make_user(member_session)
    version = put_in_status(repo, member_session, alice, status)
    member_session.commit()
    before = (repo.content_of(version), version.content_hash)
    changed = validated(title="偷改的標題", background="偷改的背景", tags=["偷改"])
    with pytest.raises(VersionNotDraftError):
        repo.update_draft(alice.id, version.public_id, content=changed.content, content_hash=changed.content_hash, total_chars=changed.total_chars)
    member_session.expire_all()
    assert (repo.content_of(member_session.get(ScriptVersion, version.id)), version.content_hash) == before


def test_a_non_owner_cannot_edit_and_gets_the_same_result_as_for_a_missing_version(repo, member_session):
    alice, bob = make_user(member_session, 1), make_user(member_session, 2)
    version = new_script(repo, alice)
    changed = validated(title="別人改的")
    assert repo.update_draft(bob.id, version.public_id, content=changed.content, content_hash=changed.content_hash, total_chars=1) is None
    assert repo.update_draft(bob.id, "00000000-0000-4000-8000-000000000000", content=changed.content, content_hash=changed.content_hash, total_chars=1) is None
    assert repo.submit_for_review(bob.id, version.public_id) is None and repo.withdraw(bob.id, version.public_id) is None
    assert member_session.get(ScriptVersion, version.id).title == "青雲問道" and status_of(member_session, version) == VERSION_DRAFT


# ---- 每個劇本至多一個上架版本與一個進行中的版本 ------------------------------------------------------

def test_a_second_open_version_is_refused_as_a_conflict_and_leaves_only_the_first(repo, member_session):
    alice = make_user(member_session)
    first = new_script(repo, alice)
    with pytest.raises(OpenVersionExistsError):
        new_draft(repo, alice, script_public_id(repo, first))
    repo.submit_for_review(alice.id, first.public_id)
    with pytest.raises(OpenVersionExistsError):                       # 待審也算進行中
        new_draft(repo, alice, script_public_id(repo, first))
    member_session.commit()
    assert member_session.scalar(select(func.count()).select_from(ScriptVersion)) == 1


def test_a_published_version_and_a_new_draft_coexist_and_the_live_one_stays_live(repo, member_session):
    alice = make_user(member_session)
    first = new_script(repo, alice)
    publish(repo, alice, first)
    second = new_draft(repo, alice, script_public_id(repo, first))
    member_session.commit()
    assert status_of(member_session, first) == VERSION_PUBLISHED and status_of(member_session, second) == VERSION_DRAFT
    assert repo.get_published_by_script(script_public_id(repo, first)).id == first.id


def test_creating_a_draft_for_someone_elses_script_returns_nothing(repo, member_session):
    alice, bob = make_user(member_session, 1), make_user(member_session, 2)
    first = new_script(repo, alice)
    publish(repo, alice, first)
    assert new_draft(repo, bob, script_public_id(repo, first)) is None
    assert new_draft(repo, bob, "00000000-0000-4000-8000-000000000000") is None


def run_in_threads(engine, worker, count: int = 2):
    barrier = Barrier(count)

    def task(index: int):
        with Session(engine) as s:
            barrier.wait(timeout=30)
            outcome = worker(s, index)
            s.commit()
            return outcome

    with ThreadPoolExecutor(max_workers=count) as pool:
        return [f.result(timeout=60) for f in [pool.submit(task, i) for i in range(count)]]


def test_two_requests_creating_a_new_version_at_the_same_time_leave_at_most_one(concurrent_engine):
    with Session(concurrent_engine) as s:
        repo = ScriptRepository(s)
        alice = make_user(s)
        version = new_script(repo, alice)
        publish(repo, alice, version)
        pid, owner = script_public_id(repo, version), alice.id
        s.commit()

    def worker(s: Session, _index: int) -> str:
        result = validated()
        try:
            created = ScriptRepository(s).create_draft_version(
                owner, pid, content=result.content, content_hash=result.content_hash, total_chars=result.total_chars
            )
            return "created" if created is not None else "none"
        except OpenVersionExistsError:
            return "conflict"

    outcomes = run_in_threads(concurrent_engine, worker)
    assert sorted(outcomes) == ["conflict", "created"], outcomes
    with Session(concurrent_engine) as s:
        rows = list(s.scalars(select(ScriptVersion).order_by(ScriptVersion.version_no)))
        assert [(r.version_no, r.status) for r in rows] == [(1, VERSION_PUBLISHED), (2, VERSION_DRAFT)]


# ---- 上架新版本時取代舊版 ----------------------------------------------------------------------

def test_approving_a_new_version_supersedes_the_old_one_and_keeps_its_content(repo, member_session):
    alice = make_user(member_session)
    first = new_script(repo, alice)
    publish(repo, alice, first)
    second = new_draft(repo, alice, script_public_id(repo, first), title="第二版標題")
    publish(repo, alice, second, now=NOW + timedelta(days=1))
    member_session.commit()
    member_session.expire_all()
    assert (status_of(member_session, first), status_of(member_session, second)) == (VERSION_SUPERSEDED, VERSION_PUBLISHED)
    assert (first.live_script_id, second.live_script_id) == (None, second.script_id)
    assert repo.content_of(first)["title"] == "青雲問道" and repo.tags_of(first) == ["仙俠", "成長"]


def test_a_failure_in_the_middle_of_publishing_leaves_both_versions_as_they_were(repo, member_session, monkeypatch):
    alice = make_user(member_session)
    first = new_script(repo, alice)
    publish(repo, alice, first)
    second = new_draft(repo, alice, script_public_id(repo, first))
    repo.submit_for_review(alice.id, second.public_id)
    member_session.commit()

    def boom(*_args, **_kwargs):
        raise RuntimeError("交易在中途失敗")

    monkeypatch.setattr(ScriptRepository, "_lock_author_name", boom)
    with pytest.raises(RuntimeError):
        repo.approve_version(second.public_id, now=NOW)
    member_session.expire_all()
    assert (status_of(member_session, first), status_of(member_session, second)) == (VERSION_PUBLISHED, VERSION_PENDING)
    assert (first.live_script_id, second.open_script_id) == (first.script_id, second.script_id)


def test_approving_a_version_that_is_not_pending_does_not_disturb_the_live_one(repo, member_session):
    alice = make_user(member_session)
    first = new_script(repo, alice)
    publish(repo, alice, first)
    second = new_draft(repo, alice, script_public_id(repo, first))        # 草稿，沒有送審
    with pytest.raises(InvalidVersionTransitionError):
        repo.approve_version(second.public_id, now=NOW)
    assert status_of(member_session, first) == VERSION_PUBLISHED and status_of(member_session, second) == VERSION_DRAFT
    assert repo.approve_version("00000000-0000-4000-8000-000000000000", now=NOW) is None


# ---- 筆名快照在第一次發佈時鎖定 -------------------------------------------------------------------

def test_the_pen_name_can_be_refreshed_before_the_first_publication_and_is_locked_by_it(repo, member_session):
    alice = make_user(member_session)
    version = new_script(repo, alice)
    pid = script_public_id(repo, version)
    renamed = repo.update_author_name(alice.id, pid, name="新暱稱", code="zzzzzz")
    assert (renamed.author_name, renamed.author_code, renamed.author_locked_at) == ("新暱稱", "zzzzzz", None)
    publish(repo, alice, version)
    locked = repo.get_owned_script(alice.id, pid)
    assert locked.author_locked_at == NOW
    with pytest.raises(AuthorNameLockedError):
        repo.update_author_name(alice.id, pid, name="想改的暱稱", code="yyyyyy")
    member_session.expire_all()
    assert (locked.author_name, locked.author_code) == ("新暱稱", "zzzzzz")


def test_a_later_version_keeps_the_original_pen_name_and_lock_time(repo, member_session):
    alice = make_user(member_session)
    first = new_script(repo, alice)
    pid = script_public_id(repo, first)
    publish(repo, alice, first)
    second = new_draft(repo, alice, pid)
    publish(repo, alice, second, now=NOW + timedelta(days=5))
    script = repo.get_owned_script(alice.id, pid)
    member_session.refresh(script)
    assert (script.author_name, script.author_locked_at) == (f"筆名{alice.id}", NOW)


def test_only_the_owner_can_update_a_pen_name(repo, member_session):
    alice, bob = make_user(member_session, 1), make_user(member_session, 2)
    pid = script_public_id(repo, new_script(repo, alice))
    assert repo.update_author_name(bob.id, pid, name="冒名", code="000000") is None
    assert repo.get_owned_script(alice.id, pid).author_name == f"筆名{alice.id}"


# ---- 標籤 -----------------------------------------------------------------------------------

def test_tags_are_queryable_independent_rows_and_ignore_case(repo, member_session):
    alice = make_user(member_session)
    xianxia = publish(repo, alice, new_script(repo, alice, tags=["仙俠", "Fantasy"]))
    scifi = publish(repo, alice, new_script(repo, alice, tags=["科幻"]))
    member_session.commit()
    assert [v.id for v in repo.list_published_by_tag("仙俠", "adult")] == [xianxia.id]
    assert [v.id for v in repo.list_published_by_tag("科幻", "adult")] == [scifi.id]
    assert [v.id for v in repo.list_published_by_tag("fantasy", "adult")] == [xianxia.id]
    assert [v.id for v in repo.list_published_by_tag("FANTASY", "adult")] == [xianxia.id]
    assert repo.list_published_by_tag("不存在", "adult") == []


def test_one_version_keeps_a_normalised_tag_only_once(repo, member_session):
    alice = make_user(member_session)
    version = new_script(repo, alice, tags=["仙俠", "仙俠", "Fantasy", "fantasy"])
    member_session.commit()
    assert repo.tags_of(version) == ["仙俠", "Fantasy"]
    assert member_session.scalar(select(func.count()).select_from(ScriptVersionTag).where(ScriptVersionTag.version_id == version.id)) == 2


def test_unpublished_versions_are_not_found_by_tag(repo, member_session):
    alice = make_user(member_session)
    new_script(repo, alice, tags=["草稿標籤"])
    assert repo.list_published_by_tag("草稿標籤", "adult") == []


# ---- 內容的還原 -----------------------------------------------------------------------------

def test_the_stored_content_comes_back_exactly_as_validated_with_the_same_hash(repo, member_session):
    from app.services.script_spec import hash_content

    alice = make_user(member_session)
    result = validated(
        ending_mode="custom", ending_text="壽終正寢。", ending_age=90, content_tier="standard",
        milestones=[{"title": "收徒大典", "note": "年度盛事", "trigger": {"kind": "year", "value": 1215}}],
    )
    version = repo.create_script(alice.id, author_name="甲", author_code="aaaaaa", content=result.content, content_hash=result.content_hash, total_chars=result.total_chars)
    member_session.commit()
    member_session.expire_all()
    rebuilt = repo.content_of(member_session.get(ScriptVersion, version.id))
    assert rebuilt == result.content and hash_content(rebuilt) == result.content_hash == version.content_hash


def test_content_without_a_calendar_or_optional_fields_round_trips_too(repo, member_session):
    from app.services.script_spec import hash_content

    alice = make_user(member_session)
    result = validated(calendar=..., opening=..., rules=..., tags=..., locations=..., characters=[])
    version = repo.create_script(alice.id, author_name="甲", author_code="aaaaaa", content=result.content, content_hash=result.content_hash, total_chars=result.total_chars)
    member_session.commit()
    member_session.expire_all()
    assert hash_content(repo.content_of(member_session.get(ScriptVersion, version.id))) == result.content_hash


# ---- 讀取範圍與擁有者隔離 -----------------------------------------------------------------------

def test_the_owner_reads_their_own_draft_and_a_non_owner_gets_the_same_nothing_as_for_a_missing_one(repo, member_session):
    alice, bob = make_user(member_session, 1), make_user(member_session, 2)
    draft = new_script(repo, alice)
    assert repo.get_owned_version(alice.id, draft.public_id).id == draft.id
    assert repo.get_owned_version(bob.id, draft.public_id) is None
    assert repo.get_owned_version(bob.id, "00000000-0000-4000-8000-000000000000") is None
    assert repo.get_owned_script(bob.id, repo.get_script_of(draft).public_id) is None


def test_public_reads_return_only_the_live_version(repo, member_session):
    alice, bob = make_user(member_session, 1), make_user(member_session, 2)
    first = new_script(repo, alice)
    pid = script_public_id(repo, first)
    publish(repo, alice, first)
    second = new_draft(repo, alice, pid, title="第二版草稿")
    member_session.commit()
    assert repo.get_published_by_script(pid).id == first.id
    assert repo.get_published(second.public_id) is None                    # 草稿不公開
    assert [v.id for v in repo.list_published("adult")] == [first.id]
    publish(repo, alice, second)
    assert repo.get_published(first.public_id) is None                    # 被取代的舊版不公開
    assert repo.get_published(second.public_id).id == second.id
    assert bob.id != alice.id and repo.get_startable(bob.id, first.public_id) is None


def test_listing_filters_by_the_content_mode_ceiling_and_orders_newest_first(repo, member_session):
    alice = make_user(member_session)
    safe = publish(repo, alice, new_script(repo, alice, content_tier="safe"), now=NOW)
    standard = publish(repo, alice, new_script(repo, alice, content_tier="standard"), now=NOW + timedelta(seconds=1))
    adult = publish(repo, alice, new_script(repo, alice, content_tier="adult"), now=NOW + timedelta(seconds=2))
    member_session.commit()
    assert [v.id for v in repo.list_published("safe")] == [safe.id]
    assert [v.id for v in repo.list_published("standard")] == [standard.id, safe.id]
    assert [v.id for v in repo.list_published("adult")] == [adult.id, standard.id, safe.id]
    assert [v.id for v in repo.list_published("mature")] == [safe.id]       # 無法判定的上限視為最保守
    assert [v.id for v in repo.list_published("adult", limit=1, offset=1)] == [standard.id]
    assert [v.id for v in repo.list_published_by_tag("仙俠", "standard")] == [standard.id, safe.id]


def test_a_script_can_be_started_from_a_published_version_or_from_the_players_own_version_only(repo, member_session):
    alice, bob = make_user(member_session, 1), make_user(member_session, 2)
    live = publish(repo, alice, new_script(repo, alice))
    draft = new_script(repo, alice)
    mine = new_script(repo, bob)
    assert repo.get_startable(bob.id, live.public_id).id == live.id          # 別人上架的
    assert repo.get_startable(bob.id, mine.public_id).id == mine.id          # 自己的草稿
    assert repo.get_startable(bob.id, draft.public_id) is None               # 別人的草稿
    assert repo.get_startable(alice.id, draft.public_id).id == draft.id
    assert repo.get_startable(bob.id, "00000000-0000-4000-8000-000000000000") is None


def test_listing_the_owners_scripts_shows_only_their_own(repo, member_session):
    alice, bob = make_user(member_session, 1), make_user(member_session, 2)
    new_script(repo, alice)
    new_script(repo, alice)
    new_script(repo, bob)
    assert len(repo.list_owned(alice.id)) == 2 and len(repo.list_owned(bob.id)) == 1
    assert all(script.creator_id == alice.id for script, _version in repo.list_owned(alice.id))


# ---- 只有草稿可以刪除 -----------------------------------------------------------------------

def test_deleting_the_only_draft_deletes_the_script_with_it(repo, member_session):
    alice = make_user(member_session)
    version = new_script(repo, alice)
    script_id, version_id = version.script_id, version.id
    assert repo.delete_draft(alice.id, version.public_id) is True
    member_session.commit()
    assert member_session.get(ScriptVersion, version_id) is None and member_session.get(Script, script_id) is None
    assert member_session.scalar(select(func.count()).select_from(ScriptVersionTag)) == 0


def test_deleting_a_later_draft_keeps_the_script_and_its_other_versions(repo, member_session):
    alice = make_user(member_session)
    first = new_script(repo, alice)
    pid = script_public_id(repo, first)
    publish(repo, alice, first)
    second = new_draft(repo, alice, pid)
    assert repo.delete_draft(alice.id, second.public_id) is True
    member_session.commit()
    assert repo.get_published_by_script(pid).id == first.id and repo.get_owned_script(alice.id, pid) is not None
    assert new_draft(repo, alice, pid).version_no == 2                       # 版本編號取下一個未被占用的，不重用已刪除的草稿位置之外


@pytest.mark.parametrize("status", [VERSION_PENDING, VERSION_PUBLISHED, VERSION_REJECTED, VERSION_SUPERSEDED])
def test_versions_outside_the_draft_state_cannot_be_deleted_by_the_owner(member_session, status):
    repo = ScriptRepository(member_session)
    alice = make_user(member_session)
    version = put_in_status(repo, member_session, alice, status)
    member_session.commit()
    with pytest.raises(VersionNotDraftError):
        repo.delete_draft(alice.id, version.public_id)
    assert member_session.get(ScriptVersion, version.id) is not None and repo.tags_of(version) == ["仙俠", "成長"]


def test_a_non_owner_deleting_gets_nothing_and_nothing_is_deleted(repo, member_session):
    alice, bob = make_user(member_session, 1), make_user(member_session, 2)
    version = new_script(repo, alice)
    assert repo.delete_draft(bob.id, version.public_id) is None
    assert repo.delete_draft(bob.id, "00000000-0000-4000-8000-000000000000") is None
    assert member_session.get(ScriptVersion, version.id) is not None


def test_deleting_a_draft_keeps_other_players_saves_and_clears_their_reference(repo, member_session):
    alice, bob = make_user(member_session, 1), make_user(member_session, 2)
    draft = new_script(repo, alice)
    save = RpgSession(user_id=bob.id, source_version_id=draft.id, script_snapshot_json={"title": "複本"}, player_name="旅人", character_age=18)
    member_session.add(save)
    member_session.commit()
    assert repo.delete_draft(alice.id, draft.public_id) is True
    member_session.commit()
    member_session.expire_all()
    kept = member_session.get(RpgSession, save.id)
    assert kept is not None and kept.source_version_id is None and kept.script_snapshot_json == {"title": "複本"}
