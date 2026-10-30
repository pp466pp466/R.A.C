"""operational-alerts 規格：告警狀態的資料存取（建立、搶佔、還原、並行只有一個成功）。

一般測試以 `member_session` 同時在 SQLite 與真實 SQL Server 上執行；並行測試用 `concurrent_engine` 與真實的執行緒與獨立連線。
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

from sqlalchemy.orm import Session

from app.models import AlertState
from app.models.alert_state import SEVERITY_CRITICAL, SEVERITY_WARNING, STATE_FIRING, STATE_OK
from app.repositories.alert_state_repository import AlertStateRepository
from tests.support.auth import START

NOW = START
KEY = "usage_budget"


def claim_firing(repo, *, expected_state=STATE_OK, expected_severity=None, expected_notified=None, now=NOW, severity=SEVERITY_WARNING):
    return repo.claim(
        KEY, expected_state=expected_state, expected_severity=expected_severity, expected_notified_at=expected_notified,
        state=STATE_FIRING, severity=severity, changed_at=now, notified_at=now, value=0.85, now=now,
    )


def row_of(session) -> AlertState:
    session.expire_all()
    return session.query(AlertState).filter_by(alert_key=KEY).one()


# ---- 建立 -------------------------------------------------------------------------

def test_ensure_creates_the_row_as_ok_once_and_returns_the_same_row_afterwards(member_session):
    repo = AlertStateRepository(member_session)
    first = repo.ensure(KEY, NOW)
    assert (first.state, first.severity, first.last_notified_at, first.last_value) == (STATE_OK, None, None, None)
    assert first.changed_at == NOW
    second = repo.ensure(KEY, NOW + timedelta(hours=1))
    assert second.id == first.id and member_session.query(AlertState).count() == 1
    assert second.changed_at == NOW                                  # 沒有被覆蓋


def test_get_all_returns_every_row_by_key(member_session):
    repo = AlertStateRepository(member_session)
    repo.ensure("usage_budget", NOW)
    repo.ensure("login_lockouts", NOW)
    assert set(repo.get_all()) == {"usage_budget", "login_lockouts"}


def test_ensure_survives_a_row_created_by_someone_else_in_between(member_session):
    class Racing(AlertStateRepository):
        first = True

        def ensure(self, alert_key, now):
            if Racing.first:                                          # 先讀到沒有，但在我們建立之前別人已經建立了
                Racing.first = False
                self._session.add(AlertState(alert_key=alert_key, state=STATE_FIRING, severity=SEVERITY_CRITICAL, changed_at=now, updated_at=now))
                self._session.flush()
                with self._session.no_autoflush:
                    pass
            return super().ensure(alert_key, now)

    assert Racing(member_session).ensure(KEY, NOW).state == STATE_FIRING
    assert member_session.query(AlertState).count() == 1


# ---- 搶佔 -------------------------------------------------------------------------

def test_claim_succeeds_once_against_the_values_that_were_read(member_session):
    repo = AlertStateRepository(member_session)
    repo.ensure(KEY, NOW)
    assert claim_firing(repo) is True
    row = row_of(member_session)
    assert (row.state, row.severity, row.last_notified_at, row.changed_at, row.last_value) == (STATE_FIRING, SEVERITY_WARNING, NOW, NOW, 0.85)
    assert claim_firing(repo) is False                                # 第二個拿著舊值的行程搶不到


def test_claim_compares_the_severity_and_the_last_notified_time_too(member_session):
    repo = AlertStateRepository(member_session)
    repo.ensure(KEY, NOW)
    claim_firing(repo)
    later = NOW + timedelta(hours=6)
    wrong = dict(expected_state=STATE_FIRING, expected_severity=SEVERITY_CRITICAL, expected_notified=NOW)
    assert claim_firing(repo, now=later, **wrong) is False             # 嚴重度不符
    wrong = dict(expected_state=STATE_FIRING, expected_severity=SEVERITY_WARNING, expected_notified=NOW - timedelta(minutes=1))
    assert claim_firing(repo, now=later, **wrong) is False             # 最近通知時間不符
    right = dict(expected_state=STATE_FIRING, expected_severity=SEVERITY_WARNING, expected_notified=NOW)
    assert claim_firing(repo, now=later, **right) is True
    assert row_of(member_session).last_notified_at == later


def test_a_none_for_changed_at_or_notified_at_leaves_that_column_alone(member_session):
    repo = AlertStateRepository(member_session)
    repo.ensure(KEY, NOW)
    claim_firing(repo)
    later = NOW + timedelta(hours=7)
    assert repo.claim(
        KEY, expected_state=STATE_FIRING, expected_severity=SEVERITY_WARNING, expected_notified_at=NOW,
        state=STATE_FIRING, severity=SEVERITY_CRITICAL, changed_at=None, notified_at=None, value=1.2, now=later,
    )
    row = row_of(member_session)
    assert (row.severity, row.changed_at, row.last_notified_at, row.last_value, row.updated_at) == (SEVERITY_CRITICAL, NOW, NOW, 1.2, later)


def test_claim_on_a_missing_row_is_simply_false(member_session):
    assert claim_firing(AlertStateRepository(member_session)) is False


# ---- 還原 -------------------------------------------------------------------------

def test_restore_puts_back_the_previous_values_only_while_the_claim_is_still_ours(member_session):
    repo = AlertStateRepository(member_session)
    repo.ensure(KEY, NOW)
    claim_firing(repo)
    assert repo.restore(KEY, claimed_at=NOW, state=STATE_OK, severity=None, changed_at=NOW - timedelta(days=1), notified_at=None, now=NOW) is True
    row = row_of(member_session)
    assert (row.state, row.severity, row.last_notified_at, row.changed_at) == (STATE_OK, None, None, NOW - timedelta(days=1))


def test_restore_does_not_overwrite_a_newer_claim_by_someone_else(member_session):
    repo = AlertStateRepository(member_session)
    repo.ensure(KEY, NOW)
    claim_firing(repo)
    later = NOW + timedelta(minutes=5)
    claim_firing(repo, expected_state=STATE_FIRING, expected_severity=SEVERITY_WARNING, expected_notified=NOW, now=later, severity=SEVERITY_CRITICAL)
    assert repo.restore(KEY, claimed_at=NOW, state=STATE_OK, severity=None, changed_at=None, notified_at=None, now=later) is False
    assert row_of(member_session).severity == SEVERITY_CRITICAL


def test_touch_updates_only_the_value_and_the_time(member_session):
    repo = AlertStateRepository(member_session)
    repo.ensure(KEY, NOW)
    claim_firing(repo)
    repo.touch(KEY, value=0.91, now=NOW + timedelta(minutes=5))
    row = row_of(member_session)
    assert (row.state, row.severity, row.last_notified_at, row.last_value, row.updated_at) == (STATE_FIRING, SEVERITY_WARNING, NOW, 0.91, NOW + timedelta(minutes=5))
    assert claim_firing(repo, expected_state=STATE_FIRING, expected_severity=SEVERITY_WARNING, expected_notified=NOW, now=NOW + timedelta(hours=7))   # 比對欄位沒被動到


# ---- 並行 -------------------------------------------------------------------------

def run_in_threads(engine, worker, count: int = 6):
    barrier = Barrier(count)

    def task(index: int):
        with Session(engine) as s:
            barrier.wait(timeout=30)
            result = worker(s, index)
            s.commit()
            return result

    with ThreadPoolExecutor(max_workers=count) as pool:
        return [f.result(timeout=60) for f in [pool.submit(task, i) for i in range(count)]]


def test_parallel_claims_succeed_exactly_once(concurrent_engine):
    with Session(concurrent_engine) as s:
        AlertStateRepository(s).ensure(KEY, NOW)
        s.commit()

    everyone_has_read = Barrier(6)

    def worker(s: Session, _index: int) -> bool:
        repo = AlertStateRepository(s)
        row = repo.ensure(KEY, NOW)                                   # 每個行程都先讀到「正常」
        expected = dict(expected_state=row.state, expected_severity=row.severity, expected_notified=row.last_notified_at)
        assert expected["expected_state"] == STATE_OK
        everyone_has_read.wait(timeout=30)                            # 都讀完了才一起搶，否則晚到的會讀到別人已提交的新狀態
        return claim_firing(repo, **expected)

    results = run_in_threads(concurrent_engine, worker)
    assert sorted(results) == [False] * 5 + [True]


def test_parallel_first_time_creation_leaves_exactly_one_row(concurrent_engine):
    def worker(s: Session, _index: int) -> int:
        return AlertStateRepository(s).ensure(KEY, NOW).id

    ids = run_in_threads(concurrent_engine, worker)
    assert len(set(ids)) == 1
    with Session(concurrent_engine) as s:
        assert s.query(AlertState).count() == 1
