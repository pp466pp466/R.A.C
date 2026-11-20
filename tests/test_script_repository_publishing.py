"""rpg-script-publishing 的資料行為（script-store 與 script-takedown 規格）：版本狀態轉移全表（含 `removed`）、核可取代下架的舊版、
下架與恢復的權限（誰下架誰恢復）與衝突、並行恢復與核可、決定欄位的寫入與清除、內容雜湊不變、開存檔與「最新的非進行中版本」對下架的處理。

一般測試以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行；並行測試用 `concurrent_engine`，
以真實的執行緒與獨立連線證明「最多一個成功」。
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import ScriptVersion, User
from app.models.base import utcnow
from app.models.script import (
    DECISION_CATEGORIES, MAX_DECISION_NOTE_CHARS, REMOVER_ADMIN, REMOVER_AUTHOR, REVIEW_CATEGORIES, VERSION_DRAFT, VERSION_PENDING,
    VERSION_PUBLISHED, VERSION_REJECTED, VERSION_REMOVED, VERSION_STATUSES, VERSION_SUPERSEDED,
)
from app.repositories import script_repository
from app.repositories.errors import (
    BuiltinNotRemovableError, InvalidVersionTransitionError, LiveVersionExistsError, RemovalLockedError, RemovedByAuthorError,
)
from app.repositories.script_repository import ScriptRepository
from tests.support.script_data import validated

NOW = utcnow()
LATER = NOW + timedelta(hours=3)
DECISION_FIELDS = ("removed_by", "decision_category", "decision_note", "decided_at")


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
    repo.submit_for_review(user.id, version.public_id, now=now)
    return repo.approve_version(version.public_id, now=now)


def script_public_id(repo, version) -> str:
    return repo.get_script_of(version).public_id


def refreshed(session, version) -> ScriptVersion:
    session.expire_all()
    return session.get(ScriptVersion, version.id)


def snapshot(session, version) -> tuple:
    """版本的狀態、兩個輔助欄位與全部中繼資料：失敗的操作之後必須與操作前完全相同。"""
    row = refreshed(session, version)
    return (
        row.status, row.live_script_id, row.open_script_id, row.published_at, row.submitted_at, row.removed_by, row.decision_category,
        row.decision_note, row.decided_at, row.content_hash,
    )


def put_in_status(repo, user, status, *, removed_by=REMOVER_AUTHOR) -> ScriptVersion:
    """把一個新劇本的第 1 版放進指定的狀態；`removed` 可指定下架者。"""
    version = new_script(repo, user)
    if status == VERSION_DRAFT:
        return version
    repo.submit_for_review(user.id, version.public_id, now=NOW)
    if status == VERSION_PENDING:
        return version
    if status == VERSION_REJECTED:
        repo.reject_version(version.public_id, "other_policy", "備註", NOW)
        return version
    repo.approve_version(version.public_id, now=NOW)
    if status == VERSION_PUBLISHED:
        return version
    if status == VERSION_REMOVED:
        if removed_by == REMOVER_ADMIN:
            repo.remove_by_admin(version.public_id, "other_policy", "管理員備註", NOW)
        else:
            repo.remove_own(user.id, version.public_id, NOW)
        return version
    successor = new_draft(repo, user, script_public_id(repo, version))
    publish(repo, user, successor, now=LATER)
    return version                                                    # 第 1 版：superseded


# ---- 轉移全表：只有固定集合內的成功，其餘一律拒絕且什麼都不改 ----------------------------------------------

# 操作 → 允許的 (來源狀態, 下架者) → 目標狀態；其餘的組合各有明確的例外
OPERATIONS = ("submit", "withdraw", "approve", "reject", "remove_own", "remove_admin", "restore_own", "restore_admin")
EXPECTED_TARGET = {
    "submit": (VERSION_DRAFT, VERSION_PENDING),
    "withdraw": (VERSION_PENDING, VERSION_DRAFT),
    "approve": (VERSION_PENDING, VERSION_PUBLISHED),
    "reject": (VERSION_PENDING, VERSION_REJECTED),
    "remove_own": (VERSION_PUBLISHED, VERSION_REMOVED),
    "remove_admin": (VERSION_PUBLISHED, VERSION_REMOVED),
    "restore_own": (VERSION_REMOVED, VERSION_PUBLISHED),
    "restore_admin": (VERSION_REMOVED, VERSION_PUBLISHED),
}
STARTING_POINTS = [(status, REMOVER_AUTHOR) for status in VERSION_STATUSES] + [(VERSION_REMOVED, REMOVER_ADMIN)]


def attempt(repo, user, version, operation):
    pid = version.public_id
    if operation == "submit":
        return repo.submit_for_review(user.id, pid, now=LATER)
    if operation == "withdraw":
        return repo.withdraw(user.id, pid)
    if operation == "approve":
        return repo.approve_version(pid, now=LATER)
    if operation == "reject":
        return repo.reject_version(pid, "other_concern", "再看看", LATER)
    if operation == "remove_own":
        return repo.remove_own(user.id, pid, LATER)
    if operation == "remove_admin":
        return repo.remove_by_admin(pid, "other_policy", "下架備註", LATER)
    if operation == "restore_own":
        return repo.restore_own(user.id, pid)
    return repo.restore_by_admin(pid)


def expected_error(operation, status, removed_by):
    """這個組合被拒絕時的例外型別；組合合法時為 None。"""
    source, _target = EXPECTED_TARGET[operation]
    if status != source:
        return InvalidVersionTransitionError
    if operation == "restore_own" and removed_by != REMOVER_AUTHOR:
        return RemovalLockedError
    if operation == "restore_admin" and removed_by != REMOVER_ADMIN:
        return RemovedByAuthorError
    return None


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize(("status", "removed_by"), STARTING_POINTS)
def test_only_the_fixed_set_of_transitions_is_allowed_and_a_refusal_changes_nothing(member_session, repo, status, removed_by, operation):
    alice = make_user(member_session)
    version = put_in_status(repo, alice, status, removed_by=removed_by)
    member_session.commit()
    before = snapshot(member_session, version)
    assert before[0] == status
    error = expected_error(operation, status, removed_by)
    if error is None:
        result = attempt(repo, alice, version, operation)
        assert result.status == EXPECTED_TARGET[operation][1]
    else:
        with pytest.raises(error):
            attempt(repo, alice, version, operation)
        assert snapshot(member_session, version) == before            # 拒絕之後狀態、輔助欄位與全部中繼資料都不變


def test_the_whitelist_is_exactly_the_documented_set_of_pairs():
    assert script_repository._ALLOWED_TRANSITIONS == {
        (VERSION_DRAFT, VERSION_PENDING), (VERSION_PENDING, VERSION_DRAFT), (VERSION_PENDING, VERSION_PUBLISHED),
        (VERSION_PENDING, VERSION_REJECTED), (VERSION_PUBLISHED, VERSION_SUPERSEDED), (VERSION_PUBLISHED, VERSION_REMOVED),
        (VERSION_REMOVED, VERSION_PUBLISHED), (VERSION_REMOVED, VERSION_SUPERSEDED),
    }
    for source in VERSION_STATUSES:                                  # 終點狀態沒有任何出口（`rejected`、`superseded`）
        if source in (VERSION_REJECTED, VERSION_SUPERSEDED):
            assert not any(pair[0] == source for pair in script_repository._ALLOWED_TRANSITIONS)


@pytest.mark.parametrize(
    ("sources", "target"),
    [
        ((VERSION_REMOVED,), VERSION_PENDING),                         # 下架的不能回到送審
        ((VERSION_REMOVED,), VERSION_REJECTED),
        ((VERSION_REJECTED,), VERSION_PUBLISHED),                      # 被退回的不能上架
        ((VERSION_SUPERSEDED,), VERSION_PUBLISHED),                    # 被取代的不能恢復
        ((VERSION_PENDING, VERSION_REMOVED), VERSION_PUBLISHED),       # 只要有一個來源不合法就整組拒絕（核可不能借用恢復的來源）
        ((VERSION_PUBLISHED, VERSION_DRAFT), VERSION_REMOVED),
        ((), VERSION_PUBLISHED),                                       # 沒有來源
    ],
)
def test_a_source_and_target_pair_outside_the_whitelist_is_refused_before_any_update(member_session, repo, sources, target):
    alice = make_user(member_session)
    version = put_in_status(repo, alice, VERSION_PUBLISHED)
    member_session.commit()
    before = snapshot(member_session, version)
    with pytest.raises(InvalidVersionTransitionError):
        repo._transition(version, target, sources=sources)
    assert snapshot(member_session, version) == before


# ---- 核可取代上架的版本與下架的版本 ---------------------------------------------------------------------------

def test_approving_a_new_version_supersedes_a_removed_one_so_it_can_no_longer_be_restored(member_session, repo):
    alice = make_user(member_session)
    first = put_in_status(repo, alice, VERSION_REMOVED)
    second = new_draft(repo, alice, script_public_id(repo, first), title="第二版標題")
    publish(repo, alice, second, now=LATER)
    member_session.commit()
    old, new = refreshed(member_session, first), refreshed(member_session, second)
    assert (old.status, new.status) == (VERSION_SUPERSEDED, VERSION_PUBLISHED)
    assert (old.live_script_id, old.open_script_id, new.live_script_id) == (None, None, new.script_id)
    assert old.removed_by == REMOVER_AUTHOR and old.decided_at == NOW        # 下架的紀錄保留，只是不可再恢復
    with pytest.raises(InvalidVersionTransitionError):
        repo.restore_own(alice.id, first.public_id)
    assert refreshed(member_session, first).status == VERSION_SUPERSEDED


def test_approving_supersedes_both_the_live_version_and_a_removed_one_of_the_same_script_only(member_session, repo):
    alice, bob = make_user(member_session, 1), make_user(member_session, 2)
    removed = put_in_status(repo, alice, VERSION_REMOVED)
    other_scripts_live = put_in_status(repo, bob, VERSION_PUBLISHED)
    other_scripts_removed = put_in_status(repo, bob, VERSION_REMOVED, removed_by=REMOVER_ADMIN)
    second = new_draft(repo, alice, script_public_id(repo, removed))
    publish(repo, alice, second, now=LATER)
    member_session.commit()
    assert refreshed(member_session, removed).status == VERSION_SUPERSEDED
    assert refreshed(member_session, other_scripts_live).status == VERSION_PUBLISHED          # 別的劇本不受影響
    assert refreshed(member_session, other_scripts_removed).status == VERSION_REMOVED


def test_a_failed_approval_leaves_a_removed_version_removed(member_session, repo):
    alice = make_user(member_session)
    first = put_in_status(repo, alice, VERSION_REMOVED)
    draft = new_draft(repo, alice, script_public_id(repo, first))                 # 草稿不是待審：核可必須失敗，且取代一併回復
    member_session.commit()
    with pytest.raises(InvalidVersionTransitionError):
        repo.approve_version(draft.public_id, now=LATER)
    assert refreshed(member_session, first).status == VERSION_REMOVED
    assert refreshed(member_session, draft).status == VERSION_DRAFT


# ---- 下架與恢復的權限與結果 -----------------------------------------------------------------------------------

def test_the_author_removes_and_restores_without_review_and_nothing_but_the_status_and_decision_changes(member_session, repo):
    alice = make_user(member_session)
    version = put_in_status(repo, alice, VERSION_PUBLISHED)
    member_session.commit()
    published = refreshed(member_session, version)
    content_before = repo.content_of(published)
    keep = (published.content_hash, published.published_at, published.review_hash, published.title, published.version_no)

    removed = repo.remove_own(alice.id, version.public_id, LATER)
    assert (removed.status, removed.removed_by, removed.decision_category, removed.decision_note, removed.decided_at) == (
        VERSION_REMOVED, REMOVER_AUTHOR, None, None, LATER,
    )
    assert (removed.live_script_id, removed.open_script_id) == (None, None)       # 讓出「上架」的位置
    restored = repo.restore_own(alice.id, version.public_id)
    assert restored.status == VERSION_PUBLISHED and restored.live_script_id == restored.script_id
    assert all(getattr(restored, name) is None for name in DECISION_FIELDS)        # 四個決定欄位清為空
    assert (restored.content_hash, restored.published_at, restored.review_hash, restored.title, restored.version_no) == keep
    assert repo.content_of(restored) == content_before                            # 內容與雜湊都沒有變


def test_the_admin_removes_with_a_category_and_note_and_only_the_admin_restores(member_session, repo):
    alice = make_user(member_session)
    version = put_in_status(repo, alice, VERSION_PUBLISHED)
    removed = repo.remove_by_admin(version.public_id, "minor_suspected", "疑似未成年角色，需作者說明", LATER)
    assert (removed.status, removed.removed_by, removed.decision_category, removed.decision_note, removed.decided_at) == (
        VERSION_REMOVED, REMOVER_ADMIN, "minor_suspected", "疑似未成年角色，需作者說明", LATER,
    )
    member_session.commit()
    before = snapshot(member_session, version)
    with pytest.raises(RemovalLockedError):                                       # 作者不能恢復管理員下架的
        repo.restore_own(alice.id, version.public_id)
    assert snapshot(member_session, version) == before
    restored = repo.restore_by_admin(version.public_id)
    assert restored.status == VERSION_PUBLISHED and all(getattr(restored, name) is None for name in DECISION_FIELDS)


def test_the_admin_does_not_restore_a_version_the_author_removed(member_session, repo):
    alice = make_user(member_session)
    version = put_in_status(repo, alice, VERSION_REMOVED, removed_by=REMOVER_AUTHOR)
    member_session.commit()
    before = snapshot(member_session, version)
    with pytest.raises(RemovedByAuthorError):
        repo.restore_by_admin(version.public_id)
    assert snapshot(member_session, version) == before


def test_a_builtin_script_cannot_be_removed(member_session, repo):
    result = validated()
    version, _ = repo.publish_builtin("builtin-key", content=result.content, content_hash=result.content_hash, total_chars=result.total_chars, now=NOW)
    member_session.commit()
    before = snapshot(member_session, version)
    with pytest.raises(BuiltinNotRemovableError):
        repo.remove_by_admin(version.public_id, "other_policy", None, LATER)
    assert snapshot(member_session, version) == before and before[0] == VERSION_PUBLISHED


def test_someone_elses_version_and_an_unknown_id_are_not_found_and_nothing_changes(member_session, repo):
    alice, bob = make_user(member_session, 1), make_user(member_session, 2)
    published = put_in_status(repo, alice, VERSION_PUBLISHED)
    removed = put_in_status(repo, alice, VERSION_REMOVED)
    member_session.commit()
    before = (snapshot(member_session, published), snapshot(member_session, removed))
    unknown = "00000000-0000-4000-8000-000000000000"
    assert repo.remove_own(bob.id, published.public_id, LATER) is None
    assert repo.restore_own(bob.id, removed.public_id) is None
    assert repo.remove_own(alice.id, unknown, LATER) is None and repo.restore_own(alice.id, unknown) is None
    assert repo.remove_by_admin(unknown, "other_policy", None, LATER) is None
    assert repo.restore_by_admin(unknown) is None
    assert repo.reject_version(unknown, "other_policy", None, LATER) is None
    assert (snapshot(member_session, published), snapshot(member_session, removed)) == before


def test_restoring_when_another_version_of_the_script_is_already_live_is_a_conflict_and_the_version_stays_removed(member_session, repo):
    """核可會取代下架的舊版，所以這個衝突只在「繞過核可的並行寫入」時出現：資料庫的部分唯一索引是最後一道防線。"""
    alice = make_user(member_session)
    first = put_in_status(repo, alice, VERSION_REMOVED)
    second = new_draft(repo, alice, script_public_id(repo, first))
    repo.submit_for_review(alice.id, second.public_id, now=NOW)
    member_session.commit()
    member_session.execute(                                                      # 測試專用：模擬並行的另一條路徑讓第 2 版先上架
        update(ScriptVersion).where(ScriptVersion.id == second.id)
        .values(status=VERSION_PUBLISHED, live_script_id=second.script_id, open_script_id=None, published_at=NOW)
        .execution_options(synchronize_session=False)
    )
    member_session.commit()
    before = snapshot(member_session, first)
    with pytest.raises(LiveVersionExistsError):
        repo.restore_own(alice.id, first.public_id)
    assert snapshot(member_session, first) == before and before[0] == VERSION_REMOVED
    assert refreshed(member_session, second).status == VERSION_PUBLISHED          # 現有的上架版本沒有被動到，Session 仍可使用


# ---- 決定欄位的寫入與清除 -------------------------------------------------------------------------------------

def test_submitting_records_the_time_and_withdrawing_clears_it(member_session, repo):
    alice = make_user(member_session)
    version = new_script(repo, alice)
    assert version.submitted_at is None
    submitted = repo.submit_for_review(alice.id, version.public_id, now=LATER)
    assert (submitted.status, submitted.submitted_at) == (VERSION_PENDING, LATER)
    withdrawn = repo.withdraw(alice.id, version.public_id)
    assert (withdrawn.status, withdrawn.submitted_at) == (VERSION_DRAFT, None)


def test_submitting_without_a_time_uses_the_current_utc_time(member_session, repo):
    alice = make_user(member_session)
    version = new_script(repo, alice)
    before = utcnow()
    repo.submit_for_review(alice.id, version.public_id)
    member_session.commit()
    assert before <= refreshed(member_session, version).submitted_at <= utcnow()


def test_rejecting_records_the_category_note_and_time_and_the_content_stays(member_session, repo):
    alice = make_user(member_session)
    version = new_script(repo, alice)
    repo.submit_for_review(alice.id, version.public_id, now=NOW)
    content_hash = version.content_hash
    rejected = repo.reject_version(version.public_id, "real_person_suspected", "請改用虛構人名", LATER)
    assert (rejected.status, rejected.decision_category, rejected.decision_note, rejected.decided_at, rejected.removed_by) == (
        VERSION_REJECTED, "real_person_suspected", "請改用虛構人名", LATER, None,
    )
    assert rejected.content_hash == content_hash and rejected.submitted_at == NOW        # 送審時間保留；退回不改內容
    assert (rejected.live_script_id, rejected.open_script_id) == (None, None)


@pytest.mark.parametrize("category", DECISION_CATEGORIES)
def test_every_decision_category_is_accepted_for_both_rejecting_and_removing(member_session, repo, category):
    alice = make_user(member_session)
    pending = new_script(repo, alice)
    repo.submit_for_review(alice.id, pending.public_id, now=NOW)
    assert repo.reject_version(pending.public_id, category, None, LATER).decision_category == category
    live = new_script(repo, alice)
    publish(repo, alice, live)
    assert repo.remove_by_admin(live.public_id, category, None, LATER).decision_category == category


def test_the_decision_categories_are_the_review_categories_plus_other_policy():
    assert DECISION_CATEGORIES == REVIEW_CATEGORIES + ("other_policy",)


@pytest.mark.parametrize("category", ["", "OTHER_POLICY", "spam", None, "tier_mismatch "])
def test_a_category_outside_the_enumeration_is_refused_for_rejecting_and_removing_and_nothing_changes(member_session, repo, category):
    alice = make_user(member_session)
    pending = new_script(repo, alice)
    repo.submit_for_review(alice.id, pending.public_id, now=NOW)
    live = new_script(repo, alice)
    publish(repo, alice, live)
    member_session.commit()
    before = (snapshot(member_session, pending), snapshot(member_session, live))
    with pytest.raises(ValueError):
        repo.reject_version(pending.public_id, category, None, LATER)
    with pytest.raises(ValueError):
        repo.remove_by_admin(live.public_id, category, None, LATER)
    assert (snapshot(member_session, pending), snapshot(member_session, live)) == before


@pytest.mark.parametrize(("length", "accepted"), [(0, True), (1, True), (MAX_DECISION_NOTE_CHARS, True), (MAX_DECISION_NOTE_CHARS + 1, False)])
def test_the_note_is_at_most_five_hundred_characters_and_an_empty_note_is_stored_as_empty(member_session, repo, length, accepted):
    alice = make_user(member_session)
    live = new_script(repo, alice)
    publish(repo, alice, live)
    member_session.commit()
    before = snapshot(member_session, live)
    if accepted:
        removed = repo.remove_by_admin(live.public_id, "other_policy", "字" * length, LATER)
        assert removed.decision_note == ("字" * length if length else None)
    else:
        with pytest.raises(ValueError):
            repo.remove_by_admin(live.public_id, "other_policy", "字" * length, LATER)
        assert snapshot(member_session, live) == before


# ---- 開存檔與下一個草稿 ---------------------------------------------------------------------------------------

def test_a_removed_version_cannot_be_started_by_anyone_including_its_author_until_it_is_restored(member_session, repo):
    alice, bob = make_user(member_session, 1), make_user(member_session, 2)
    version = put_in_status(repo, alice, VERSION_PUBLISHED)
    assert repo.get_startable(bob.id, version.public_id) is not None
    repo.remove_own(alice.id, version.public_id, LATER)
    assert repo.get_startable(alice.id, version.public_id) is None                 # 作者自己也不行
    assert repo.get_startable(bob.id, version.public_id) is None
    repo.restore_own(alice.id, version.public_id)
    assert repo.get_startable(bob.id, version.public_id) is not None


def test_the_author_can_still_start_her_other_non_live_versions_but_a_removed_one_is_the_only_exception(member_session, repo):
    alice = make_user(member_session)
    draft = new_script(repo, alice)
    rejected = put_in_status(repo, alice, VERSION_REJECTED)
    removed = put_in_status(repo, alice, VERSION_REMOVED, removed_by=REMOVER_ADMIN)
    assert repo.get_startable(alice.id, draft.public_id) is not None
    assert repo.get_startable(alice.id, rejected.public_id) is not None
    assert repo.get_startable(alice.id, removed.public_id) is None


def test_the_latest_closed_version_includes_a_removed_one_so_the_author_can_build_the_next_draft_from_it(member_session, repo):
    alice = make_user(member_session)
    first = put_in_status(repo, alice, VERSION_REMOVED)
    pid = script_public_id(repo, first)
    latest = repo.latest_closed_version(alice.id, pid)
    assert latest is not None and latest.id == first.id and latest.status == VERSION_REMOVED
    draft = new_draft(repo, alice, pid, title="由下架版本延伸")
    assert (draft.version_no, draft.status) == (2, VERSION_DRAFT)


def test_removing_the_live_version_frees_the_live_slot_for_another_version(member_session, repo):
    alice = make_user(member_session)
    first = put_in_status(repo, alice, VERSION_PUBLISHED)
    second = new_draft(repo, alice, script_public_id(repo, first))
    repo.submit_for_review(alice.id, second.public_id, now=NOW)
    repo.remove_own(alice.id, first.public_id, LATER)
    member_session.commit()
    assert repo.approve_version(second.public_id, now=LATER).status == VERSION_PUBLISHED      # 不會撞上部分唯一索引
    assert repo.get_published_by_script(script_public_id(repo, first)).id == second.id


# ---- 並行 -----------------------------------------------------------------------------------------------------

def run_in_threads(engine, workers):
    barrier = Barrier(len(workers))

    def task(index: int):
        with Session(engine) as s:
            barrier.wait(timeout=30)
            outcome = workers[index](s)
            s.commit()
            return outcome

    with ThreadPoolExecutor(max_workers=len(workers)) as pool:
        return [f.result(timeout=60) for f in [pool.submit(task, i) for i in range(len(workers))]]


def test_two_restores_at_the_same_time_succeed_at_most_once(concurrent_engine):
    with Session(concurrent_engine) as s:
        repo = ScriptRepository(s)
        alice = make_user(s)
        version = put_in_status(repo, alice, VERSION_REMOVED)
        owner, pid = alice.id, version.public_id
        s.commit()

    def worker(s: Session) -> str:
        try:
            ScriptRepository(s).restore_own(owner, pid)
            return "restored"
        except InvalidVersionTransitionError:
            return "refused"

    assert sorted(run_in_threads(concurrent_engine, [worker, worker])) == ["refused", "restored"]
    with Session(concurrent_engine) as s:
        row = s.scalar(select(ScriptVersion).where(ScriptVersion.public_id == pid))
        assert (row.status, row.live_script_id == row.script_id) == (VERSION_PUBLISHED, True)
        assert all(getattr(row, name) is None for name in DECISION_FIELDS)


def test_a_restore_and_an_approval_at_the_same_time_leave_exactly_one_live_version(concurrent_engine):
    with Session(concurrent_engine) as s:
        repo = ScriptRepository(s)
        alice = make_user(s)
        first = put_in_status(repo, alice, VERSION_REMOVED)
        second = new_draft(repo, alice, script_public_id(repo, first))
        repo.submit_for_review(alice.id, second.public_id, now=NOW)
        owner, first_pid, second_pid = alice.id, first.public_id, second.public_id
        s.commit()

    def restoring(s: Session) -> str:
        try:
            ScriptRepository(s).restore_own(owner, first_pid)
            return "restored"
        except InvalidVersionTransitionError:
            return "refused"

    def approving(s: Session) -> str:
        ScriptRepository(s).approve_version(second_pid, now=LATER)
        return "approved"

    outcomes = run_in_threads(concurrent_engine, [restoring, approving])
    assert outcomes[1] == "approved" and outcomes[0] in ("restored", "refused"), outcomes
    with Session(concurrent_engine) as s:
        rows = {r.public_id: r for r in s.scalars(select(ScriptVersion))}
        assert rows[second_pid].status == VERSION_PUBLISHED
        assert rows[first_pid].status == VERSION_SUPERSEDED                         # 無論誰先：舊版最後都是被取代
        assert sum(1 for r in rows.values() if r.live_script_id is not None) == 1


def test_a_removal_and_a_withdrawal_of_different_versions_do_not_interfere(concurrent_engine):
    with Session(concurrent_engine) as s:
        repo = ScriptRepository(s)
        alice = make_user(s)
        live = put_in_status(repo, alice, VERSION_PUBLISHED)
        pending = new_draft(repo, alice, script_public_id(repo, live))
        repo.submit_for_review(alice.id, pending.public_id, now=NOW)
        owner, live_pid, pending_pid = alice.id, live.public_id, pending.public_id
        s.commit()

    def removing(s: Session) -> str:
        ScriptRepository(s).remove_own(owner, live_pid, LATER)
        return "removed"

    def withdrawing(s: Session) -> str:
        ScriptRepository(s).withdraw(owner, pending_pid)
        return "withdrawn"

    assert sorted(run_in_threads(concurrent_engine, [removing, withdrawing])) == ["removed", "withdrawn"]
    with Session(concurrent_engine) as s:
        rows = {r.public_id: r for r in s.scalars(select(ScriptVersion))}
        assert (rows[live_pid].status, rows[pending_pid].status) == (VERSION_REMOVED, VERSION_DRAFT)
        assert rows[pending_pid].submitted_at is None and rows[live_pid].removed_by == REMOVER_AUTHOR
