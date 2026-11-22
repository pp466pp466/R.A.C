"""script-anonymous-retention 與 account-deletion 規格：抹除與匿名保留的整合行為（rpg-script-reports 任務 4.2）。

與 `test_account_erasure_retain.py`（單元：直接呼叫 `AccountErasureService`）不同，這裡走排程指令實際使用的組裝
（`registry.build_account_deletion_executor`、`build_account_retention_service`、`build_deletion_replay_service`），
用一個「多劇本、多版本、有人檢舉、有別人的存檔」的作者世界，檢查抹除之後留下什麼、刪掉什麼，以及留下的版本仍然能被
目錄顯示、開存檔、被檢舉、被管理員下架。以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行：
SQL Server 的無動作外鍵（錯誤 1785）只有在那裡才會暴露抹除順序的錯誤。

另有 SQLite 專用的兩組：兩條排程指令（`execute-account-deletions`、`purge-inactive-accounts`）經 Flask CLI 的實際輸出，
以及年齡確認發現未滿 18 歲時（HTTP）一律全刪。
"""
from dataclasses import dataclass
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base, DeletionTombstone, RpgSession, Script, ScriptReport, ScriptVersion, User
from app.models.script import (
    REMOVER_ADMIN, RETIRED_AUTHOR_NAME, VERSION_DRAFT, VERSION_PUBLISHED, VERSION_REMOVED, VERSION_SUPERSEDED, ScriptVersionTag,
)
from app.models.script_report import CLOSED_REMOVED, CLOSED_SUPERSEDED, REASON_OTHER, RED_LINE_REASONS
from app.models.user import STATUS_ACTIVE, STATUS_DELETION_PENDING
from app.repositories.deletion_tombstone_repository import DeletionTombstoneRepository
from app.repositories.runtime_limit_repository import RuntimeLimitRepository
from app.repositories.script_report_repository import ScriptReportRepository
from app.repositories.script_repository import ScriptRepository
from app.repositories.session_repository import SessionRepository
from app.repositories.user_repository import UserRepository
from app.services.access_policy import AccessPolicy
from app.services.account_deletion_executor import AccountDeletionExecutor
from app.services.account_deletion_service import AccountDeletionService
from app.services.account_erasure_service import AccountErasureService
from app.services.account_retention_service import AccountRetentionService
from app.services.current_user import CurrentUser
from app.services.game_entry_service import GameEntryService
from app.services.registry import (
    build_account_deletion_executor, build_account_retention_service, build_deletion_replay_service,
)
from app.services.runtime_limits import RuntimeLimits
from app.services.script_report_service import ScriptReportService
from app.services.startable import StartableVersions
from tests.support.admin import AdminKit
from tests.support.age import age_fields
from tests.support.auth import GOOD_PASSWORD, START, FakeClock, add_user, make_auth_service, sign_in_as
from tests.support.protagonist import protagonist
from tests.support.script_review import new_draft, put_in_status

PATHS = ["pending", "inactive"]               # 兩條依偏好保留的抹除原因：使用者申請刪除（寬限期滿）與連續 12 個月未登入
TAGS = ["奇幻", "冒險"]


def count(session, model) -> int:
    session.expire_all()
    return session.scalar(select(func.count()).select_from(model))


def reader_save(session, reader_id: int, version: ScriptVersion, scripts: ScriptRepository) -> int:
    """另一位玩家用某個版本開的存檔（來源參照指向該版本）。"""
    save = SessionRepository(session).create(
        reader_id, source_version_id=version.id, snapshot=scripts.content_of(version), snapshot_spec_version=1,
        protagonist=protagonist("讀者", 18), content_tier="safe", now=START,
    )
    return save.id


def author_overrides(how: str) -> dict:
    """依抹除原因設定作者帳號，使排程指令只會選中他：`pending`＝申請刪除已滿 8 天；`inactive`＝400 天沒登入；
    `unconfirmed`＝沒有生日且 24 小時期限已過；`active`＝最近登入（不會被任何排程選中）。"""
    if how == "pending":
        return dict(status=STATUS_DELETION_PENDING, deletion_requested_at=START - timedelta(days=8), status_before_deletion=STATUS_ACTIVE,
                    created_at=START - timedelta(days=500), last_login_at=START - timedelta(days=9))
    if how == "inactive":
        return dict(created_at=START - timedelta(days=500), last_login_at=START - timedelta(days=400))
    if how == "unconfirmed":
        return dict(age=None, age_deadline_at=START - timedelta(hours=1), created_at=START - timedelta(days=2))
    assert how == "active"
    return dict(created_at=START - timedelta(days=90), last_login_at=START)


def run_path(session, how: str, clock: FakeClock | None = None):
    """跑對應的排程組裝（與命令列相同），回傳 `PurgeResult`。"""
    clock = clock or FakeClock()
    if how == "pending":
        return build_account_deletion_executor(session, clock=clock).execute_due()
    service = build_account_retention_service(session, clock=clock)
    return service.purge_inactive() if how == "inactive" else service.purge_unconfirmed()


# ---- 世界：一位作者與周邊的人 ----------------------------------------------------------------------------


@dataclass
class World:
    author_id: int
    reader_ids: tuple[int, int, int]
    script_live: int            # 保留的兩份劇本
    script_other: int
    live_id: int                # 保留的兩個上架版本
    live_public: str
    other_id: int
    other_public: str
    superseded_id: int          # 會被刪除的版本
    gone_version_ids: list[int]
    kept_reports: list[int]
    kept_tags: list[str]        # 兩個保留版本的標籤（上架版的預設標籤加丁的 TAGS）
    saves: dict[str, int]


def make_world(s, *, keep: bool, how: str) -> World:
    """作者 1（偏好 `keep`）：
    - 劇本甲：第 1 版被取代、第 2 版上架、第 3 版草稿；
    - 劇本乙：只有草稿；劇本丙：只有被管理員下架的版本；劇本丁：上架（兩個標籤）；劇本戊：只有作者自己下架的版本。
    讀者 2、3、4：2 檢舉甲的上架版（含紅線）與丁，並檢舉甲的舊版（已結案為被取代）；3 檢舉甲的上架版；
    讀者 2 用上架版開存檔、讀者 3 用舊版開存檔、讀者 4 用被管理員下架的版本開存檔；作者自己也有一個存檔。"""
    author = add_user(s, 1, keep_published_scripts=keep, **author_overrides(how))
    scripts = ScriptRepository(s)
    old = put_in_status(scripts, author, VERSION_SUPERSEDED, title="甲·舊版")
    script_a = scripts.get_script_of(old)
    live = s.scalar(select(ScriptVersion).where(ScriptVersion.script_id == script_a.id, ScriptVersion.status == VERSION_PUBLISHED))
    draft_a = new_draft(scripts, author, script_a.public_id, title="甲·草稿")
    only_draft = put_in_status(scripts, author, VERSION_DRAFT, title="乙·只有草稿")
    admin_removed = put_in_status(scripts, author, VERSION_REMOVED, removed_by=REMOVER_ADMIN, title="丙·管理員下架")
    other = put_in_status(scripts, author, VERSION_PUBLISHED, title="丁·上架", tags=TAGS)
    self_removed = put_in_status(scripts, author, VERSION_REMOVED, title="戊·作者下架")
    s.flush()
    readers = tuple(add_user(s, n).id for n in (2, 3, 4))

    reports = ScriptReportRepository(s)
    kept = [
        reports.add(readers[0], live.id, [REASON_OTHER], False, "第一筆", now=START).id,
        reports.add(readers[1], live.id, [RED_LINE_REASONS[0], REASON_OTHER], True, None, now=START).id,
        reports.add(readers[0], other.id, [REASON_OTHER], False, None, now=START).id,
    ]
    reports.add(readers[0], old.id, [REASON_OTHER], False, "舊版的檢舉", now=START)
    assert reports.close_open(old.id, CLOSED_SUPERSEDED, START) == 1          # 舊版的檢舉在版本被取代時已結案

    saves = {
        "on_live": reader_save(s, readers[0], live, scripts),
        "on_old": reader_save(s, readers[1], old, scripts),
        "on_removed": reader_save(s, readers[2], admin_removed, scripts),
        "own": reader_save(s, author.id, live, scripts),
    }
    assert scripts.tags_of(old) and scripts.tags_of(draft_a)                    # 被刪除的版本也有標籤，下面的斷言才有意義
    kept_tags = sorted(scripts.tags_of(live) + scripts.tags_of(other))
    s.commit()
    return World(
        author_id=author.id, reader_ids=readers, script_live=script_a.id, script_other=scripts.get_script_of(other).id,
        live_id=live.id, live_public=live.public_id, other_id=other.id, other_public=other.public_id, superseded_id=old.id,
        gone_version_ids=[old.id, draft_a.id, only_draft.id, admin_removed.id, self_removed.id], kept_reports=kept, kept_tags=kept_tags,
        saves=saves,
    )


def assert_retained(s, w: World) -> None:
    """偏好開啟且原因是申請刪除或連續 12 個月未登入：只剩兩個上架版本，其餘（含它們的標籤、檢舉與存檔來源參照）都處理掉。"""
    s.expire_all()
    assert s.get(User, w.author_id) is None
    versions = s.scalars(select(ScriptVersion).order_by(ScriptVersion.id)).all()
    assert [v.id for v in versions] == sorted([w.live_id, w.other_id])
    assert all(v.status == VERSION_PUBLISHED and v.decision_note is None and v.live_script_id is not None for v in versions)
    scripts = s.scalars(select(Script).order_by(Script.id)).all()
    assert [sc.id for sc in scripts] == sorted([w.script_live, w.script_other])
    for script in scripts:
        assert script.creator_id is None and script.seed_key is None
        assert script.author_name == RETIRED_AUTHOR_NAME and script.author_code is None and script.author_locked_at is not None
    assert sorted(s.scalars(select(ScriptVersionTag.tag))) == w.kept_tags                   # 只剩保留版本的標籤
    assert sorted(s.scalars(select(ScriptReport.id))) == sorted(w.kept_reports)             # 保留版本上的檢舉保留，被刪版本上的檢舉刪除
    assert all(report.closed_at is None for report in s.scalars(select(ScriptReport)))
    assert s.get(RpgSession, w.saves["on_live"]).source_version_id == w.live_id              # 別人對保留版本的存檔來源參照保留
    assert s.get(RpgSession, w.saves["on_old"]).source_version_id is None                    # 對被刪版本的參照清為空值，存檔本身還在
    assert s.get(RpgSession, w.saves["on_removed"]).source_version_id is None
    assert s.get(RpgSession, w.saves["own"]) is None                                         # 作者自己的存檔隨帳號刪除
    assert count(s, RpgSession) == 3
    assert count(s, DeletionTombstone) == 1


def assert_everything_deleted(s, w: World) -> None:
    s.expire_all()
    assert s.get(User, w.author_id) is None
    for model in (Script, ScriptVersion, ScriptVersionTag, ScriptReport):
        assert count(s, model) == 0, model.__name__
    assert count(s, RpgSession) == 3 and all(
        s.get(RpgSession, save_id).source_version_id is None for key, save_id in w.saves.items() if key != "own"
    )
    assert count(s, DeletionTombstone) == 1


# ---- 兩條依偏好保留的路徑 ----------------------------------------------------------------------------------


@pytest.mark.parametrize("how", PATHS)
def test_with_the_preference_on_only_the_live_versions_survive_with_their_tags_and_reports(member_session, how):
    s = member_session
    w = make_world(s, keep=True, how=how)
    result = run_path(s, how)
    assert (result.matched, result.deleted) == (1, 1)
    assert_retained(s, w)


@pytest.mark.parametrize("how", PATHS)
def test_with_the_preference_off_everything_of_the_author_is_deleted_and_nothing_blocks_the_erasure(member_session, how):
    s = member_session
    w = make_world(s, keep=False, how=how)
    result = run_path(s, how)
    assert (result.matched, result.deleted) == (1, 1)
    assert_everything_deleted(s, w)


@pytest.mark.parametrize("how", PATHS)
def test_a_retained_version_is_still_listed_playable_reportable_and_removable_by_an_admin(member_session, how):
    s = member_session
    w = make_world(s, keep=True, how=how)
    run_path(s, how)
    s.expire_all()
    clock = FakeClock()
    policy = AccessPolicy(clock=clock)
    reader = CurrentUser.from_model(s.get(User, w.reader_ids[2]))                            # 還沒有檢舉過保留版本的讀者

    # 目錄：兩張卡片，作者都是「已註銷的作者」、沒有短代號，也不是「官方」
    catalog = GameEntryService(
        scripts=ScriptRepository(s), sessions=SessionRepository(s), policy=policy, reports=ScriptReportRepository(s),
    ).catalog(reader)
    cards = {card["id"]: card for card in catalog["scripts"]}
    assert set(cards) == {w.live_public, w.other_public}
    assert {(c["author_name"], c["author_code"]) for c in cards.values()} == {(RETIRED_AUTHOR_NAME, None)}
    assert sorted(cards[w.other_public]["tags"]) == sorted(TAGS)

    # 開存檔：內容與保留的版本相同
    startable = StartableVersions(scripts=ScriptRepository(s), policy=policy).resolve(reader, w.live_public)
    created = SessionRepository(s).create(
        reader.id, source_version_id=startable.version.id, snapshot=startable.content, snapshot_spec_version=1,
        protagonist=protagonist("新讀者", 18), content_tier="safe", now=START,
    )
    s.commit()
    assert s.get(RpgSession, created.id).source_version_id == w.live_id

    # 檢舉：匿名保留的版本可以被檢舉（它不是內建劇本）
    ScriptReportService(
        scripts=ScriptRepository(s), reports=ScriptReportRepository(s), policy=policy,
        limits=RuntimeLimits(limits=RuntimeLimitRepository(s), clock=clock), clock=clock,
    ).submit(reader, w.live_public, [REASON_OTHER], "保留版本的新檢舉")
    s.commit()
    assert count(s, ScriptReport) == len(w.kept_reports) + 1

    # 管理員下架：沒有作者也可以；該版本所有未結案的檢舉由系統結為「下架」，另一個保留的劇本不受影響
    kit = AdminKit(s)
    admin = kit.admin(90)
    kit.service.remove_script(admin, w.live_public, category="other_policy", password=GOOD_PASSWORD)
    s.commit()
    s.expire_all()
    assert s.get(ScriptVersion, w.live_id).status == VERSION_REMOVED and s.get(ScriptVersion, w.other_id).status == VERSION_PUBLISHED
    on_live = s.execute(select(ScriptReport.closed_result).where(ScriptReport.version_id == w.live_id)).scalars().all()
    assert on_live == [CLOSED_REMOVED] * 3
    assert s.scalar(select(ScriptReport.closed_at).where(ScriptReport.version_id == w.other_id)) is None


# ---- 申請刪除的視窗：偏好一併寫入，期滿後才被採用 ---------------------------------------------------------------


@pytest.mark.parametrize(("keep", "remaining"), [(True, 1), (False, 0)], ids=["keep", "do-not-keep"])
def test_the_choice_made_in_the_deletion_dialog_is_honoured_when_the_grace_period_ends(member_session, keep, remaining):
    s = member_session
    clock = FakeClock()
    author = add_user(s, 1, last_login_at=START)
    scripts = ScriptRepository(s)
    put_in_status(scripts, author, VERSION_PUBLISHED, title="上架中")
    put_in_status(scripts, author, VERSION_DRAFT, title="草稿")
    s.commit()
    service = AccountDeletionService(
        users=UserRepository(s), confirm_password=make_auth_service(s, clock=clock).confirm_password, clock=clock,
    )
    service.request(CurrentUser.from_model(s.get(User, author.id)), password=GOOD_PASSWORD, keep_published_scripts=keep)
    s.commit()

    clock.advance(days=6)
    assert build_account_deletion_executor(s, clock=clock).execute_due().deleted == 0          # 寬限期內不刪
    assert count(s, ScriptVersion) == 2
    clock.advance(days=2)
    assert build_account_deletion_executor(s, clock=clock).execute_due().deleted == 1

    assert count(s, ScriptVersion) == remaining and count(s, User) == 0
    s.expire_all()
    assert [sc.author_name for sc in s.scalars(select(Script))] == [RETIRED_AUTHOR_NAME] * remaining


# ---- 兩個不保留的原因 ----------------------------------------------------------------------------------------


def test_an_unconfirmed_account_is_erased_completely_even_with_the_preference_on(member_session):
    s = member_session
    w = make_world(s, keep=True, how="unconfirmed")
    result = run_path(s, "unconfirmed")
    assert (result.matched, result.deleted) == (1, 1)
    assert_everything_deleted(s, w)


def test_an_underage_erasure_is_complete_even_with_the_preference_on(member_session):
    s = member_session
    w = make_world(s, keep=True, how="unconfirmed")
    service = AccountErasureService(
        users=UserRepository(s), scripts=ScriptRepository(s), tombstones=DeletionTombstoneRepository(s), commit=s.commit, rollback=s.rollback,
    )
    assert service.erase_underage(w.author_id, now=START) is True
    assert_everything_deleted(s, w)


# ---- 檢舉人被抹除：只刪他送出的檢舉，被檢舉的版本與作者不受影響 ------------------------------------------------------


@pytest.mark.parametrize("how", PATHS)
def test_erasing_a_reporter_deletes_only_the_reports_they_sent(member_session, how):
    """account-deletion 規格「檢舉一併移除」：`script_reports.reporter_id` 串聯刪除；被檢舉的版本、作者與其他人的檢舉原樣。"""
    s = member_session
    light = light_author(s, 1, keep=True, how="active")                         # 作者最近登入過，不會被任何排程選中
    other_reporter = add_user(s, 30)
    ScriptReportRepository(s).add(other_reporter.id, light.live_id, [REASON_OTHER], False, None, now=START)
    s.execute(update(User).where(User.id == light.reader_id).values(**{k: v for k, v in author_overrides(how).items() if k != "age"}))
    s.commit()

    result = run_path(s, how)

    assert (result.matched, result.deleted) == (1, 1)
    s.expire_all()
    assert s.get(User, light.reader_id) is None and s.get(User, light.author_id) is not None
    assert [r.reporter_id for r in s.scalars(select(ScriptReport))] == [other_reporter.id]
    assert s.get(ScriptVersion, light.live_id) is not None and s.get(ScriptVersion, light.draft_id) is not None
    assert s.get(RpgSession, light.save_id) is None                              # 檢舉人自己的存檔隨帳號刪除
    assert {sc.creator_id for sc in s.scalars(select(Script))} == {light.author_id}


# ---- 備份還原後的重新抹除不讀偏好 -----------------------------------------------------------------------------------


def test_replaying_deletions_ignores_the_preference_and_leaves_scripts_that_are_already_anonymous_alone(member_session):
    s = member_session
    earlier = light_author(s, 1, keep=True, how="inactive")
    assert run_path(s, "inactive").deleted == 1                                  # 先前真正被抹除並保留了一份劇本
    s.expire_all()
    retained = s.scalar(select(Script).where(Script.creator_id.is_(None)))
    assert retained is not None and retained.author_name == RETIRED_AUTHOR_NAME

    zombie = light_author(s, 2, keep=True, how="active")                         # 還原後復活的帳號（偏好開啟、有上架劇本）
    DeletionTombstoneRepository(s).record(user_id=zombie.author_id, reason="user_requested", deleted_at=START)
    s.commit()
    s.execute(update(User).where(User.id == zombie.author_id).values(created_at=START - timedelta(days=90)))
    s.commit()

    report = build_deletion_replay_service(s).run()

    assert report.erased == 1
    s.expire_all()
    assert s.get(User, zombie.author_id) is None
    assert s.get(ScriptVersion, zombie.live_id) is None and s.get(ScriptVersion, zombie.draft_id) is None      # 偏好開啟也全刪
    assert s.get(ScriptVersion, earlier.live_id) is not None                                                   # 先前保留的不受影響
    assert s.get(Script, retained.id).author_name == RETIRED_AUTHOR_NAME
    assert [r.version_id for r in s.scalars(select(ScriptReport))] == [earlier.live_id]


# ---- 撤銷刪除或剛好登入的競態：條件式刪除沒刪到，整個交易回復 ---------------------------------------------------------


@dataclass
class Light:
    author_id: int
    live_id: int
    draft_id: int
    reader_id: int
    save_id: int


def light_author(s, n: int, *, keep: bool, how: str) -> Light:
    """一位作者：一個上架版本（被讀者檢舉）與一個草稿（被讀者的存檔當作來源）。已提交。"""
    author = add_user(s, n, keep_published_scripts=keep, **author_overrides(how))
    scripts = ScriptRepository(s)
    live = put_in_status(scripts, author, VERSION_PUBLISHED, title=f"上架{n}")
    draft = put_in_status(scripts, author, VERSION_DRAFT, title=f"草稿{n}")
    reader = add_user(s, n + 10)
    ScriptReportRepository(s).add(reader.id, live.id, [REASON_OTHER], False, None, now=START)
    save_id = reader_save(s, reader.id, draft, scripts)
    s.commit()
    return Light(author.id, live.id, draft.id, reader.id, save_id)


class _RacingPendingUsers(UserRepository):
    """排程找到待刪帳號之後、逐一抹除之前，其中一位剛好撤銷了刪除（回到 active 並登入）。"""

    victim = 0

    def find_pending_due_ids(self, cutoff, *, limit):
        ids = super().find_pending_due_ids(cutoff, limit=limit)
        self._session.execute(update(User).where(User.id == self.victim).values(
            status=STATUS_ACTIVE, deletion_requested_at=None, status_before_deletion=None, last_login_at=START,
        ))
        self._session.commit()
        return ids


class _RacingInactiveUsers(UserRepository):
    """排程找到閒置帳號之後、逐一抹除之前，其中一位剛好登入了。"""

    victim = 0

    def find_inactive_ids(self, cutoff, *, limit):
        ids = super().find_inactive_ids(cutoff, limit=limit)
        self._session.execute(update(User).where(User.id == self.victim).values(last_login_at=START))
        self._session.commit()
        return ids


def assert_untouched(s, light: Light) -> None:
    """撤銷或剛登入的帳號：帳號、偏好、劇本與作者關聯、筆名、版本、檢舉與存檔來源參照都原狀（交易整個回復）。"""
    s.expire_all()
    user = s.get(User, light.author_id)
    assert user is not None and user.keep_published_scripts is True
    victims = list(
        s.scalars(select(Script).join(ScriptVersion, ScriptVersion.script_id == Script.id).where(ScriptVersion.id.in_([light.live_id, light.draft_id])))
    )
    assert len(victims) == 2 and all(sc.creator_id == light.author_id and sc.author_name != RETIRED_AUTHOR_NAME for sc in victims)
    assert s.get(ScriptVersion, light.live_id) is not None and s.get(ScriptVersion, light.draft_id) is not None
    assert [r.version_id for r in s.scalars(select(ScriptReport).where(ScriptReport.version_id == light.live_id))] == [light.live_id]
    assert s.get(RpgSession, light.save_id).source_version_id == light.draft_id


def test_an_account_that_cancelled_just_before_the_delete_keeps_everything_while_the_others_in_the_batch_are_retained(member_session):
    s = member_session
    victim = light_author(s, 1, keep=True, how="pending")
    other = light_author(s, 2, keep=True, how="pending")
    users = _RacingPendingUsers(s)
    users.victim = victim.author_id
    erasure = AccountErasureService(
        users=users, scripts=ScriptRepository(s), tombstones=DeletionTombstoneRepository(s), commit=s.commit, rollback=s.rollback,
    )

    result = AccountDeletionExecutor(users=users, erasure=erasure, clock=FakeClock()).execute_due()

    assert (result.matched, result.deleted) == (2, 1)
    assert_untouched(s, victim)
    s.expire_all()
    assert s.get(User, other.author_id) is None and s.get(ScriptVersion, other.live_id) is not None
    assert s.get(ScriptVersion, other.draft_id) is None and s.get(RpgSession, other.save_id).source_version_id is None
    assert count(s, DeletionTombstone) == 1


def test_an_idle_account_that_logged_in_just_before_the_delete_keeps_everything(member_session):
    s = member_session
    victim = light_author(s, 1, keep=True, how="inactive")
    users = _RacingInactiveUsers(s)
    users.victim = victim.author_id
    service = AccountRetentionService(
        users=users, scripts=ScriptRepository(s), tombstones=DeletionTombstoneRepository(s), clock=FakeClock(),
        commit=s.commit, rollback=s.rollback,
    )

    result = service.purge_inactive()

    assert (result.matched, result.deleted) == (1, 0)
    assert_untouched(s, victim)
    assert count(s, DeletionTombstone) == 0


# ---- SQLite：兩條排程指令與年齡確認（HTTP）-----------------------------------------------------------------------------


@pytest.fixture()
def app():
    application = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


@pytest.mark.parametrize(
    ("command", "how", "message"),
    [
        ("execute-account-deletions", "pending", "已刪除 1 個寬限期已滿的帳號。"),
        ("purge-inactive-accounts", "inactive", "已刪除 1 個連續 12 個月未登入的帳號。"),
    ],
)
def test_the_scheduled_commands_retain_the_live_version_and_report_the_usual_message(app, command, how, message):
    with Session(app.extensions["db_engine"]) as s:
        light = light_author(s, 1, keep=True, how=how)
    result = app.test_cli_runner().invoke(args=[command])
    assert result.exit_code == 0, result.output
    assert message in result.output
    with Session(app.extensions["db_engine"]) as s:
        assert s.get(User, light.author_id) is None
        assert s.get(ScriptVersion, light.live_id) is not None and s.get(ScriptVersion, light.draft_id) is None
        assert s.scalar(select(Script.author_name).join(ScriptVersion, ScriptVersion.script_id == Script.id)
                        .where(ScriptVersion.id == light.live_id)) == RETIRED_AUTHOR_NAME
    assert "上架" not in result.output and RETIRED_AUTHOR_NAME not in result.output                     # 輸出沒有劇本文字或筆名


def test_a_player_found_to_be_under_18_has_every_script_deleted_even_with_the_preference_on(app):
    with Session(app.extensions["db_engine"]) as s:
        light = light_author(s, 1, keep=True, how="unconfirmed")
        user = s.get(User, light.author_id)
        user_id, token_version = user.id, user.token_version
    client = app.test_client()
    sign_in_as(client, user_id, token_version)
    response = client.post("/api/auth/verify-age", json=age_fields(10, age_range="13_17"))
    assert response.status_code == 400
    with Session(app.extensions["db_engine"]) as s:
        assert s.get(User, light.author_id) is None
        assert count(s, Script) == 0 and count(s, ScriptVersion) == 0 and count(s, ScriptReport) == 0
