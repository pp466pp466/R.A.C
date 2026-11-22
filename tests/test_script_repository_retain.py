"""script-anonymous-retention 規格「抹除時依偏好保留目前上架的版本」與「匿名保留的版本改為已註銷的作者」：
`ScriptRepository.retain_published_anonymously` 的完整情境矩陣。

作者有：①版本被取代＋上架＋草稿的劇本 ②只有上架版本的劇本 ③只有草稿 ④作者下架 ⑤管理員下架 ⑥被退回 ⑦待審；
另有別的作者的上架劇本（必須原封不動）、各種檢舉與別人的存檔。只保留「上架中」的版本（①的第二版與②），其餘一律刪除；
保留的劇本解除作者關聯、筆名改為「已註銷的作者」、短代號清空、筆名鎖定時間不變；被刪版本的檢舉與存檔來源參照清掉，
保留版本的檢舉與參照原樣保留。

以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行：兩個無動作外鍵（`sessions.source_version_id`、
`script_reports.version_id`）只在 SQL Server 上才有「刪除順序不對就失敗」的意義，SQLite 開啟了外鍵所以同樣會擋。
"""
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select, update

from app.models import RpgSession, Script, ScriptReport, ScriptVersion, ScriptVersionTag, User
from app.models.script import (
    REMOVER_ADMIN, RETIRED_AUTHOR_NAME, VERSION_PENDING, VERSION_PUBLISHED, VERSION_REJECTED, VERSION_REMOVED,
    VERSION_SUPERSEDED,
)
from app.models.script_report import CLOSED_DISMISSED, CLOSED_SUPERSEDED, REASON_OTHER
from app.repositories import script_repository
from app.repositories.script_report_repository import ScriptReportRepository
from app.repositories.script_repository import RetainResult, ScriptRepository
from app.repositories.session_repository import SessionRepository
from tests.support.auth import START
from tests.support.protagonist import protagonist
from tests.support.script_review import at, make_author, new_draft, new_script, put_in_status, script_public_id


def add_report(reports, reporter, version, closed: tuple | None = None):
    row = reports.add(reporter.id, version.id, [REASON_OTHER], False, None, now=at(1))
    if closed is not None:
        reports.close_open(version.id, closed[0], closed[1])             # 結案整個版本（這個測試每個版本只有一位檢舉人需要結案）
    return row


def open_save(saves, scripts, reader, version):
    return saves.create(
        reader.id, source_version_id=version.id, snapshot=scripts.content_of(version), snapshot_spec_version=1,
        protagonist=protagonist("讀者", 18), content_tier="safe", now=START,
    )


@pytest.fixture()
def world(member_session):
    s = member_session
    scripts, reports, saves = ScriptRepository(s), ScriptReportRepository(s), SessionRepository(s)
    author, other, r1, r2, reader = (make_author(s, n) for n in (1, 2, 3, 4, 5))

    s1_old = put_in_status(scripts, author, VERSION_SUPERSEDED, title="S1")                 # 第 1 版被取代、第 2 版上架
    s1_script = script_public_id(scripts, s1_old)
    s1_live = scripts.get_published_by_script(s1_script)
    s1_draft = new_draft(scripts, author, s1_script, title="S1 草稿")
    s2 = put_in_status(scripts, author, VERSION_PUBLISHED, title="S2", tags=["單一版本"])
    s3 = new_script(scripts, author, title="S3 只有草稿")
    s4 = put_in_status(scripts, author, VERSION_REMOVED, title="S4 作者下架")
    s5 = put_in_status(scripts, author, VERSION_REMOVED, removed_by=REMOVER_ADMIN, title="S5 管理員下架")
    s6 = put_in_status(scripts, author, VERSION_REJECTED, title="S6 被退回")
    s7 = put_in_status(scripts, author, VERSION_PENDING, title="S7 待審")
    theirs = put_in_status(scripts, other, VERSION_PUBLISHED, title="別人的劇本")
    s.flush()

    add_report(reports, r1, s1_old, (CLOSED_SUPERSEDED, at(2)))          # 被刪版本的已結案檢舉
    add_report(reports, r1, s1_live), add_report(reports, r2, s1_live)  # 保留版本的未結案檢舉
    add_report(reports, r1, s2, (CLOSED_DISMISSED, at(3)))              # 保留版本的已結案檢舉
    add_report(reports, r1, s4)                                         # 被刪版本上的檢舉（直接寫入，維持未結案）
    add_report(reports, r1, theirs)                                     # 別人的版本
    saves_made = SimpleNamespace(
        kept=open_save(saves, scripts, reader, s1_live), dropped_old=open_save(saves, scripts, reader, s1_old),
        dropped_admin_removed=open_save(saves, scripts, reader, s5), others=open_save(saves, scripts, reader, theirs),
    )
    s.commit()
    return SimpleNamespace(
        session=s, scripts=scripts, reports=reports, author=author, other=other, reporters=(r1, r2),
        kept=(s1_live, s2), s1_old=s1_old, s1_draft=s1_draft, dropped=(s1_old, s1_draft, s3, s4, s5, s6, s7), theirs=theirs, saves=saves_made,
    )


def versions_of(session, user_id: int) -> list[ScriptVersion]:
    session.expire_all()
    return list(session.scalars(
        select(ScriptVersion).join(Script, Script.id == ScriptVersion.script_id).where(Script.creator_id == user_id).order_by(ScriptVersion.id)
    ))


def count(session, model) -> int:
    session.expire_all()
    return session.scalar(select(func.count()).select_from(model))


def retain(world) -> RetainResult:
    return world.scripts.retain_published_anonymously(world.author.id)


# ---- 去留 ------------------------------------------------------------------------------------


def test_only_the_published_versions_survive_and_the_counts_are_reported(world):
    live_ids = {version.id for version in world.kept}
    dropped_ids = {version.id for version in world.dropped}

    result = retain(world)

    assert result == RetainResult(kept=2, deleted=7)
    world.session.expire_all()
    surviving = set(world.session.scalars(select(ScriptVersion.id)))
    assert live_ids <= surviving and not (dropped_ids & surviving)
    assert all(world.session.get(ScriptVersion, vid).status == VERSION_PUBLISHED for vid in live_ids)


def test_scripts_without_a_published_version_disappear_with_all_their_versions(world):
    retain(world)
    world.session.expire_all()
    titles = set(world.session.scalars(select(ScriptVersion.title)))
    assert titles == {"青雲問道", "S2", "別人的劇本"}                      # S1 保留的是第二版（接續版本用預設標題）
    assert count(world.session, Script) == 3                           # S1、S2 與別人的；S3–S7 整份刪除
    assert count(world.session, ScriptVersion) == 3


def test_kept_scripts_lose_the_author_but_keep_the_content_and_the_name_lock(world):
    scripts_before = {
        version.id: (world.scripts.get_script_of(version).id, world.scripts.get_script_of(version).author_locked_at,
                     world.scripts.get_script_of(version).public_id)
        for version in world.kept
    }
    contents_before = {version.id: world.scripts.content_of(version) for version in world.kept}

    retain(world)

    world.session.expire_all()
    for version in world.kept:
        script = world.session.get(Script, scripts_before[version.id][0])
        assert script.creator_id is None and script.seed_key is None                          # 沒有作者，但不是內建劇本
        assert script.author_name == RETIRED_AUTHOR_NAME == "已註銷的作者" and script.author_code is None
        assert script.author_locked_at == scripts_before[version.id][1] and script.author_locked_at is not None
        assert script.public_id == scripts_before[version.id][2]                              # 對外識別碼不變（連結不失效）
        stored = world.session.get(ScriptVersion, version.id)
        assert stored.status == VERSION_PUBLISHED and stored.live_script_id == script.id      # 仍是上架版本
        assert world.scripts.content_of(stored) == contents_before[version.id]                # 內容不變


def test_the_review_note_of_a_kept_version_is_cleared(world):
    live = world.kept[0]
    world.session.execute(
        update(ScriptVersion).where(ScriptVersion.id == live.id).values(decision_note="只有管理員看得到的備註")
        .execution_options(synchronize_session=False)
    )
    retain(world)
    world.session.expire_all()
    assert world.session.get(ScriptVersion, live.id).decision_note is None


def test_tags_follow_their_versions(world):
    before = count(world.session, ScriptVersionTag)
    kept_tags = {version.id: sorted(world.scripts.tags_of(version)) for version in world.kept}
    assert before > sum(len(tags) for tags in kept_tags.values())          # 被刪的版本原本也有標籤

    retain(world)

    world.session.expire_all()
    for version in world.kept:
        assert sorted(world.scripts.tags_of(world.session.get(ScriptVersion, version.id))) == kept_tags[version.id]
    theirs = sorted(world.scripts.tags_of(world.session.get(ScriptVersion, world.theirs.id)))
    assert count(world.session, ScriptVersionTag) == sum(len(tags) for tags in kept_tags.values()) + len(theirs)


# ---- 檢舉與存檔的參照 -----------------------------------------------------------------------------


def test_reports_on_kept_versions_stay_and_reports_on_deleted_versions_go(world):
    kept_ids = {version.id for version in world.kept}
    before_on_kept = list(world.session.execute(
        select(ScriptReport.id, ScriptReport.closed_at, ScriptReport.closed_result).where(ScriptReport.version_id.in_(kept_ids)).order_by(ScriptReport.id)
    ).all())
    assert len(before_on_kept) == 3
    on_theirs = world.session.scalars(select(ScriptReport.id).where(ScriptReport.version_id == world.theirs.id)).all()

    retain(world)

    world.session.expire_all()
    after_on_kept = list(world.session.execute(
        select(ScriptReport.id, ScriptReport.closed_at, ScriptReport.closed_result).where(ScriptReport.version_id.in_(kept_ids)).order_by(ScriptReport.id)
    ).all())
    assert after_on_kept == before_on_kept                                  # 未結案與已結案的都原樣（含結案時間與結果）
    assert world.session.scalars(select(ScriptReport.id).where(ScriptReport.version_id == world.theirs.id)).all() == on_theirs
    assert count(world.session, ScriptReport) == 3 + len(on_theirs)         # 被刪版本的兩筆檢舉不見了


def test_saves_of_other_players_keep_a_reference_to_kept_versions_and_lose_it_for_deleted_ones(world):
    saves = world.saves
    retain(world)
    world.session.expire_all()
    assert world.session.get(RpgSession, saves.kept.id).source_version_id == world.kept[0].id
    assert world.session.get(RpgSession, saves.others.id).source_version_id == world.theirs.id
    for dropped in (saves.dropped_old, saves.dropped_admin_removed):
        row = world.session.get(RpgSession, dropped.id)
        assert row is not None and row.source_version_id is None            # 存檔保留，來源參照清為空值
        assert row.script_snapshot_json                                     # 存檔有自己的劇本複本


# ---- 不影響別人 ---------------------------------------------------------------------------------


def test_other_authors_scripts_are_left_exactly_as_they_were(world):
    theirs = world.theirs
    script_id = world.scripts.get_script_of(theirs).id
    before = (world.scripts.get_script_of(theirs).creator_id, world.scripts.get_script_of(theirs).author_name,
              world.scripts.get_script_of(theirs).author_code)

    retain(world)

    world.session.expire_all()
    script = world.session.get(Script, script_id)
    assert (script.creator_id, script.author_name, script.author_code) == before and script.creator_id == world.other.id
    assert world.session.get(ScriptVersion, theirs.id).status == VERSION_PUBLISHED


# ---- 邊界 ------------------------------------------------------------------------------------


def test_a_user_without_scripts_changes_nothing(world):
    nobody = make_author(world.session, 9)
    assert world.scripts.retain_published_anonymously(nobody.id) == RetainResult(kept=0, deleted=0)
    assert count(world.session, ScriptVersion) == 10                         # 沒有任何版本被動到


def test_running_it_twice_is_harmless(world):
    assert retain(world) == RetainResult(kept=2, deleted=7)
    assert retain(world) == RetainResult(kept=0, deleted=0)                  # 第二次：作者關聯已解除，找不到這位作者的劇本
    assert count(world.session, ScriptVersion) == 3


def test_an_author_with_only_deletable_scripts_loses_everything(member_session):
    s = member_session
    scripts = ScriptRepository(s)
    author = make_author(s, 1)
    put_in_status(scripts, author, VERSION_REMOVED), new_script(scripts, author, title="草稿")

    assert scripts.retain_published_anonymously(author.id) == RetainResult(kept=0, deleted=2)
    assert count(s, Script) == 0 and count(s, ScriptVersion) == 0 and count(s, ScriptVersionTag) == 0


def test_many_versions_are_handled_in_chunks(world, monkeypatch):
    """編號集合分批（SQL Server 的參數上限）：把批次調到 2，同一組情境的結果必須完全一樣。"""
    monkeypatch.setattr(script_repository, "_ID_CHUNK", 2)
    assert retain(world) == RetainResult(kept=2, deleted=7)
    world.session.expire_all()
    assert {version.id for version in versions_of(world.session, world.author.id)} == set()
    assert count(world.session, Script) == 3 and count(world.session, ScriptReport) == 3 + 1
    assert world.session.get(RpgSession, world.saves.dropped_old.id).source_version_id is None


# ---- 之後刪除使用者：保留的劇本不受串聯影響 ------------------------------------------------------------


def test_deleting_the_user_afterwards_leaves_the_retained_scripts_alone(world):
    live_ids = {version.id for version in world.kept}
    retain(world)
    world.session.execute(delete(User).where(User.id == world.author.id))
    world.session.expire_all()

    assert world.session.get(User, world.author.id) is None
    assert {row for row in world.session.scalars(select(ScriptVersion.id))} >= live_ids      # 作者關聯已是空值，串聯刪除碰不到
    assert count(world.session, Script) == 3
    assert count(world.session, ScriptReport) == 4


def test_without_the_retain_step_deleting_the_user_removes_every_script(world):
    """對照組（預設偏向刪除）：沒有呼叫匿名保留時，`creator_id` 的串聯刪除把作者的劇本全部帶走。
    別人的存檔對作者版本的參照是無動作外鍵，不先清掉就擋住刪除——這是抹除流程另有 `clear_script_version_refs` 的原因。"""
    world.session.execute(update(RpgSession).values(source_version_id=None).execution_options(synchronize_session=False))
    world.session.execute(delete(ScriptReport).where(ScriptReport.version_id.in_(
        select(ScriptVersion.id).join(Script, Script.id == ScriptVersion.script_id).where(Script.creator_id == world.author.id)
    )).execution_options(synchronize_session=False))
    world.session.execute(delete(User).where(User.id == world.author.id))
    world.session.expire_all()
    assert count(world.session, Script) == 1                                 # 只剩別人的
    assert world.session.get(ScriptVersion, world.theirs.id) is not None
