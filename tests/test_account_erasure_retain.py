"""script-anonymous-retention 規格「抹除時依偏好保留目前上架的版本」「未滿 18 歲抹除與逾時未確認不保留」「匿名保留的操作是原子的」：
`AccountErasureService` 依帳號偏好呼叫 `ScriptRepository.retain_published_anonymously` 的整合行為。

- 四種抹除原因各自的行為：申請刪除（寬限期滿）與連續 12 個月未登入依偏好保留；未滿 18 歲與逾時未確認年齡一律全刪；
  預設（沒有傳 `retain_scripts`）全刪；
- 偏好在刪除當下讀取（申請之後、寬限期內改的偏好以期滿當下為準）；
- 條件式刪除沒有刪到（剛好登入、剛撤銷刪除）時整個交易回復：劇本、版本、檢舉、存檔來源參照與作者關聯都維持原狀，也沒有事件；
- 事件在提交之後才記錄，只含內部使用者編號與保留、刪除的版本數。

以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行。
"""
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import DeletionTombstone, RpgSession, Script, ScriptReport, ScriptVersion, User
from app.models.script import RETIRED_AUTHOR_NAME, VERSION_DRAFT, VERSION_PUBLISHED, VERSION_REMOVED
from app.models.script_report import REASON_OTHER
from app.models.user import STATUS_DELETION_PENDING
from app.repositories.deletion_tombstone_repository import DeletionTombstoneRepository
from app.repositories.script_report_repository import ScriptReportRepository
from app.repositories.script_repository import ScriptRepository
from app.repositories.session_repository import SessionRepository
from app.repositories.user_repository import UserRepository
from app.services.account_erasure_service import AccountErasureService
from tests.support.auth import START, add_user
from tests.support.protagonist import protagonist
from tests.support.script_review import make_author, put_in_status

CUTOFF = START - timedelta(days=7)               # 申請刪除的寬限期截止
IDLE_CUTOFF = START - timedelta(days=365)        # 連續 12 個月未登入的截止
TITLE = "上架中的劇本"


def make_service(session) -> AccountErasureService:
    return AccountErasureService(
        users=UserRepository(session), scripts=ScriptRepository(session), tombstones=DeletionTombstoneRepository(session),
        commit=session.commit, rollback=session.rollback,
    )


def author_with_scripts(session, n: int = 1, *, keep: bool, **overrides) -> User:
    """一位作者：有一個上架的劇本、一個下架的劇本與一個草稿劇本；偏好為 `keep`。**已提交**（回復測試要靠這個）。"""
    overrides.setdefault("last_login_at", START - timedelta(days=400))
    overrides.setdefault("created_at", START - timedelta(days=500))
    user = add_user(session, n, keep_published_scripts=keep, **overrides)
    scripts = ScriptRepository(session)
    put_in_status(scripts, user, VERSION_PUBLISHED, title=TITLE)
    put_in_status(scripts, user, VERSION_REMOVED, title="下架的劇本")
    put_in_status(scripts, user, VERSION_DRAFT, title="草稿劇本")
    session.commit()
    return user


def titles(session) -> list[str]:
    session.expire_all()
    return sorted(session.scalars(select(ScriptVersion.title)))


def count(session, model) -> int:
    session.expire_all()
    return session.scalar(select(func.count()).select_from(model))


def erase_inactive(session, user: User) -> bool:
    return make_service(session).erase_inactive(user.id, cutoff=IDLE_CUTOFF, now=START)


def erase_pending(session, user: User) -> bool:
    return make_service(session).erase_pending_due(user.id, cutoff=CUTOFF, now=START)


def pending_author(session, n: int = 1, *, keep: bool) -> User:
    return author_with_scripts(
        session, n, keep=keep, status=STATUS_DELETION_PENDING, deletion_requested_at=START - timedelta(days=8),
    )


# ---- 四種抹除原因 -----------------------------------------------------------------------------


@pytest.mark.parametrize("erase", [erase_inactive, erase_pending], ids=["inactive-12-months", "user-requested"])
def test_the_two_reasons_that_honour_the_preference_keep_the_live_version_when_it_is_on(member_session, erase):
    s = member_session
    user = author_with_scripts(s, keep=True) if erase is erase_inactive else pending_author(s, keep=True)
    user_id = user.id

    assert erase(s, user) is True

    assert s.get(User, user_id) is None
    assert titles(s) == [TITLE]                                                 # 只剩上架中的版本
    s.expire_all()
    (script,) = s.scalars(select(Script)).all()
    assert script.creator_id is None and script.seed_key is None
    assert script.author_name == RETIRED_AUTHOR_NAME and script.author_code is None
    assert count(s, DeletionTombstone) == 1


@pytest.mark.parametrize("erase", [erase_inactive, erase_pending], ids=["inactive-12-months", "user-requested"])
def test_with_the_preference_off_everything_is_deleted(member_session, erase):
    s = member_session
    user = author_with_scripts(s, keep=False) if erase is erase_inactive else pending_author(s, keep=False)
    assert erase(s, user) is True
    assert count(s, Script) == 0 and count(s, ScriptVersion) == 0


def test_an_underage_erasure_deletes_everything_even_when_the_preference_is_on(member_session):
    s = member_session
    user = author_with_scripts(s, keep=True, age=None)                          # 還沒有生日的玩家帳號（補填時發現未滿 18 歲）
    assert make_service(s).erase_underage(user.id, now=START) is True
    assert count(s, Script) == 0 and count(s, ScriptVersion) == 0


def test_an_unconfirmed_account_cleanup_deletes_everything_even_when_the_preference_is_on(member_session):
    s = member_session
    user = author_with_scripts(s, keep=True, age=None, age_deadline_at=START - timedelta(hours=1))
    assert make_service(s).erase_unconfirmed(user.id, now=START) is True
    assert count(s, Script) == 0 and count(s, ScriptVersion) == 0


def test_erase_does_not_retain_unless_it_is_asked_to(member_session):
    """預設 `retain_scripts=False`（備份還原後的重新抹除與日後新增的呼叫者都走這裡）：偏好開啟也全刪，偏向刪除。"""
    s = member_session
    user = author_with_scripts(s, keep=True)
    assert make_service(s).erase(
        user.id, delete=lambda uid: UserRepository(s).delete_inactive(uid, IDLE_CUTOFF), reason="inactive_12_months", now=START,
    ) is True
    assert count(s, Script) == 0 and count(s, ScriptVersion) == 0


# ---- 偏好在刪除當下讀取 ---------------------------------------------------------------------------


def test_a_preference_switched_on_during_the_grace_period_is_honoured(member_session):
    s = member_session
    user = pending_author(s, keep=False)                                        # 申請刪除時偏好為關閉
    assert UserRepository(s).set_keep_published_scripts(user, True) is True    # 寬限期內改為開啟
    s.commit()

    assert erase_pending(s, user) is True
    assert titles(s) == [TITLE]


def test_a_preference_switched_off_during_the_grace_period_is_honoured_too(member_session):
    s = member_session
    user = pending_author(s, keep=True)
    UserRepository(s).set_keep_published_scripts(user, False)
    s.commit()

    assert erase_pending(s, user) is True
    assert count(s, ScriptVersion) == 0


# ---- 原子性：條件式刪除沒刪到時整個回復 -----------------------------------------------------------------


@pytest.mark.parametrize("erase", [erase_inactive, erase_pending], ids=["inactive-12-months", "user-requested"])
def test_when_the_conditional_delete_misses_nothing_changes(member_session, erase):
    s = member_session
    # 剛好登入的閒置帳號、剛好撤銷刪除的待刪除帳號：兩者都已回到 active 且最近登入過，條件式刪除不成立
    user = author_with_scripts(s, keep=True, last_login_at=START, created_at=START - timedelta(days=500))
    reader = make_author(s, 9)
    scripts, reports, saves = ScriptRepository(s), ScriptReportRepository(s), SessionRepository(s)
    versions = list(s.scalars(select(ScriptVersion).join(Script, Script.id == ScriptVersion.script_id).where(Script.creator_id == user.id)))
    live = next(version for version in versions if version.status == VERSION_PUBLISHED)
    draft = next(version for version in versions if version.status == VERSION_DRAFT)
    reports.add(reader.id, draft.id, [REASON_OTHER], False, None, now=START)                  # 會被刪除的版本上的檢舉
    save = saves.create(
        reader.id, source_version_id=draft.id, snapshot=scripts.content_of(draft), snapshot_spec_version=1,
        protagonist=protagonist("讀者", 18), content_tier="safe", now=START,
    )
    s.commit()
    save_id, user_id, draft_id = save.id, user.id, draft.id
    authors_before = sorted((v.id, v.script_id) for v in versions)

    assert erase(s, user) is False

    s.expire_all()
    assert s.get(User, user_id) is not None and s.get(User, user_id).keep_published_scripts is True
    assert sorted((v.id, v.script_id) for v in s.scalars(select(ScriptVersion))) == authors_before      # 草稿與下架的版本都還在
    assert {script.creator_id for script in s.scalars(select(Script))} == {user_id}                    # 作者關聯與筆名都原狀
    assert all(script.author_name != RETIRED_AUTHOR_NAME for script in s.scalars(select(Script)))
    assert count(s, ScriptReport) == 1                                                                  # 被刪版本上的檢舉也還在
    assert s.get(RpgSession, save_id).source_version_id == draft_id                                    # 存檔的來源參照也復原
    assert count(s, DeletionTombstone) == 0


# ---- 事件 ------------------------------------------------------------------------------------


def retained_events(log_lines) -> list[dict]:
    return [line for line in log_lines() if line.get("event") == "account.scripts_retained"]


def test_the_event_has_only_the_user_id_and_the_counts(member_session, log_lines):
    s = member_session
    user = author_with_scripts(s, keep=True)
    user_id = user.id

    assert erase_inactive(s, user) is True

    (event,) = retained_events(log_lines)
    assert event["user_id"] == user_id and event["kept"] == 1 and event["deleted"] == 2
    assert set(event) <= {"time", "level", "logger", "message", "event", "user_id", "kept", "deleted", "request_id"}
    raw = log_lines.raw()
    assert TITLE not in raw and "下架的劇本" not in raw and RETIRED_AUTHOR_NAME not in raw      # 沒有任何劇本文字或筆名


def test_no_event_when_the_preference_is_off_the_delete_missed_or_there_is_nothing_to_retain(member_session, log_lines):
    s = member_session
    off = author_with_scripts(s, 1, keep=False)
    assert erase_inactive(s, off) is True
    recent = author_with_scripts(s, 2, keep=True, last_login_at=START)
    assert erase_inactive(s, recent) is False                                    # 沒刪到：交易已回復，事件也不該出現
    nobody = add_user(s, 3, keep_published_scripts=True, last_login_at=START - timedelta(days=400))
    assert erase_inactive(s, nobody) is True                                     # 偏好開啟但沒有任何劇本
    assert retained_events(log_lines) == []


# ---- UserRepository 的偏好讀寫 ---------------------------------------------------------------------


def test_the_preference_defaults_to_off_and_can_be_switched(member_session):
    s = member_session
    users = UserRepository(s)
    user = add_user(s, 1)
    assert users.keep_published_scripts_of(user.id) is False
    assert users.set_keep_published_scripts(user, True) is True
    assert users.keep_published_scripts_of(user.id) is True
    assert users.set_keep_published_scripts(user, True) is True                  # 設成目前的值同樣成功（冪等）
    assert users.set_keep_published_scripts(user, False) is True
    assert users.keep_published_scripts_of(user.id) is False


def test_an_unknown_user_is_treated_as_off_and_a_non_boolean_is_a_programming_error(member_session):
    s = member_session
    users = UserRepository(s)
    user = add_user(s, 1)
    assert users.keep_published_scripts_of(987654) is False                      # 找不到時偏向全刪
    for bad in ("yes", 1, None, 0):
        with pytest.raises(ValueError):
            users.set_keep_published_scripts(user, bad)
    assert users.keep_published_scripts_of(user.id) is False


def test_the_preference_is_read_from_the_database_not_from_a_stale_object(member_session):
    """抹除要在刪除當下讀取：即使 Session 裡有舊的 User 物件，也以資料庫目前的值為準。"""
    s = member_session
    users = UserRepository(s)
    user = add_user(s, 1)
    assert user.keep_published_scripts is False                                   # 物件已載入
    users.set_keep_published_scripts(user, True)
    assert users.keep_published_scripts_of(user.id) is True


def test_the_retained_event_function_accepts_only_non_negative_integers():
    from app.observability import lifecycle_events

    lifecycle_events.log_scripts_retained(user_id=1, kept=0, deleted=0)
    for bad in (-1, "3", 1.5, None, True):
        with pytest.raises(ValueError):
            lifecycle_events.log_scripts_retained(user_id=1, kept=bad, deleted=0)
        with pytest.raises(ValueError):
            lifecycle_events.log_scripts_retained(user_id=1, kept=0, deleted=bad)
    assert "kept" in __import__("app.observability.json_logging", fromlist=["ALLOWED_FIELDS"]).ALLOWED_FIELDS
