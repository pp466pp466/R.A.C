"""operational-alerts 規格：告警服務（進入只寄一封、升級、提醒、恢復、寄信失敗的重試、並行搶佔、單項失敗隔離、唯讀模式、事件）。

用真的 Repository 與資料庫（`member_session` 同時在 SQLite 與 SQL Server 上跑），假的只有時鐘與寄信器。
"""
from datetime import timedelta
from threading import Barrier

import pytest
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import UsageSettings
from app.models import AlertState, LlmCallLog, User
from app.models.alert_state import SEVERITY_CRITICAL, SEVERITY_WARNING, STATE_FIRING, STATE_OK
from app.repositories.alert_state_repository import AlertStateRepository
from app.repositories.llm_call_log_repository import LlmCallLogRepository
from app.repositories.user_repository import UserRepository
from app.services import alert_service as svc
from app.services.alert_service import AlertService
from tests.support.auth import START, FakeClock, add_user

RECIPIENT = "ops-watch@zz-host.example"
OTHER = "second-watch@zz-host.example"
USAGE = UsageSettings(daily_token_limit=1000, daily_call_limit=1000)


class Sender:
    """記錄寄出的信；可指定哪些收件人會失敗。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []
        self.failing: set[str] = set()
        self.fail_all = False

    def send(self, *, to: str, subject: str, body: str) -> None:
        if self.fail_all or to in self.failing:
            raise ConnectionError("SMTP 無回應：內部細節 secret-smtp-detail")
        self.sent.append({"to": to, "subject": subject, "body": body})

    def subjects(self) -> list[str]:
        return [m["subject"] for m in self.sent]


def make(session, clock, sender, *, recipients=(RECIPIENT,), usage=USAGE, ledger=None) -> AlertService:
    return AlertService(
        states=AlertStateRepository(session), ledger=ledger or LlmCallLogRepository(session), users=UserRepository(session),
        usage_settings=usage, recipients=recipients, mail_sender=sender if recipients else None,
        clock=clock, commit=session.commit, rollback=session.rollback,
    )


def burn(session, clock, tokens: int, *, status="ok", kind="chat", ago=timedelta(hours=1), latency=100) -> None:
    session.add(LlmCallLog(
        user_id=None, conversation_id=None, kind=kind, provider="gemini", model="m", attempt=1, status=status, counted=False,
        prompt_tokens=tokens, completion_tokens=0, latency_ms=latency, created_at=clock() - ago,
    ))
    session.commit()


def lock_accounts(session, clock, count: int, *, first: int = 1) -> None:
    for n in range(first, first + count):
        add_user(session, n, locked_until=clock() + timedelta(days=2), failed_login_count=5)


def state_of(session, key: str) -> AlertState:
    session.expire_all()
    return session.scalars(select(AlertState).where(AlertState.alert_key == key)).one()


def result_of(report, key: str):
    return next(r for r in report.results if r.key == key)


# ---- 進入、持續、升級 ----------------------------------------------------------------

def test_entering_an_alert_sends_one_mail_and_a_steady_state_sends_no_more(member_session):
    clock, sender = FakeClock(), Sender()
    burn(member_session, clock, 800)                                       # 剛好 80%
    service = make(member_session, clock, sender)

    first = service.run()
    entered = result_of(first, "usage_budget")
    assert (entered.outcome, entered.action, entered.sent) == (svc.OUTCOME_NOTIFIED, "firing", 1)
    assert sender.subjects() == ["【智能客服】告警：全站用量（進入告警）"] and sender.sent[0]["to"] == RECIPIENT
    row = state_of(member_session, "usage_budget")
    assert (row.state, row.severity, row.last_notified_at, row.changed_at) == (STATE_FIRING, SEVERITY_WARNING, clock(), clock())

    for _ in range(3):                                                      # 連續三次檢查，狀態不變
        clock.advance(minutes=5)
        again = service.run()
        assert result_of(again, "usage_budget").outcome == svc.OUTCOME_UNCHANGED
    assert len(sender.sent) == 1
    assert state_of(member_session, "usage_budget").last_value == pytest.approx(0.8)


def test_other_alerts_stay_quiet_and_are_only_recorded(member_session):
    clock, sender = FakeClock(), Sender()
    burn(member_session, clock, 800)
    report = make(member_session, clock, sender).run()
    assert [r.key for r in report.results] == ["usage_budget", "llm_error_rate", "llm_config_error", "llm_latency_p95", "login_lockouts"]
    for key in ("llm_error_rate", "llm_config_error", "llm_latency_p95", "login_lockouts"):
        assert result_of(report, key).outcome == svc.OUTCOME_UNCHANGED and result_of(report, key).sent == 0
    assert member_session.query(AlertState).count() == 5 and report.failed == []


def test_escalation_to_critical_sends_a_second_mail_and_a_drop_back_is_silent_but_remembered(member_session):
    clock, sender = FakeClock(), Sender()
    burn(member_session, clock, 800)
    service = make(member_session, clock, sender)
    service.run()
    clock.advance(minutes=5)
    burn(member_session, clock, 250)                                       # 1050 / 1000
    escalated = result_of(service.run(), "usage_budget")
    assert (escalated.action, escalated.outcome) == ("escalate", svc.OUTCOME_NOTIFIED)
    assert sender.subjects()[-1] == "【智能客服】告警：全站用量（升級）" and "嚴重度：嚴重" in sender.sent[-1]["body"]
    assert state_of(member_session, "usage_budget").severity == SEVERITY_CRITICAL

    member_session.query(LlmCallLog).filter(LlmCallLog.prompt_tokens == 250).delete()
    member_session.commit()
    clock.advance(minutes=5)
    lowered = result_of(service.run(), "usage_budget")
    assert (lowered.action, lowered.outcome, len(sender.sent)) == ("lower", svc.OUTCOME_QUIET, 2)         # 降級不寄信
    assert state_of(member_session, "usage_budget").severity == SEVERITY_WARNING

    burn(member_session, clock, 250)
    clock.advance(minutes=5)
    assert result_of(service.run(), "usage_budget").action == "escalate" and len(sender.sent) == 3        # 之後再升高會再通知


# ---- 提醒與恢復 ----------------------------------------------------------------------

def test_a_reminder_is_sent_after_six_hours_but_not_before(member_session):
    clock, sender = FakeClock(), Sender()
    lock_accounts(member_session, clock, 5)
    service = make(member_session, clock, sender)
    service.run()
    assert sender.subjects() == ["【智能客服】告警：登入失敗激增（進入告警）"]

    clock.advance(hours=6, seconds=-1)
    assert result_of(service.run(), "login_lockouts").outcome == svc.OUTCOME_UNCHANGED and len(sender.sent) == 1
    clock.advance(seconds=1)
    reminder = result_of(service.run(), "login_lockouts")
    assert (reminder.action, reminder.sent) == ("remind", 1) and sender.subjects()[-1].endswith("（提醒）")
    assert state_of(member_session, "login_lockouts").last_notified_at == clock()
    clock.advance(hours=5)
    service.run()
    assert len(sender.sent) == 2                                           # 剛提醒過，又不滿 6 小時


def test_recovery_sends_one_resolved_mail_then_nothing(member_session):
    clock, sender = FakeClock(), Sender()
    lock_accounts(member_session, clock, 5)
    service = make(member_session, clock, sender)
    service.run()
    member_session.query(User).update({"locked_until": None})
    member_session.commit()
    clock.advance(minutes=5)
    resolved = result_of(service.run(), "login_lockouts")
    assert (resolved.action, resolved.outcome) == ("resolve", svc.OUTCOME_NOTIFIED)
    assert sender.subjects()[-1] == "【智能客服】告警：登入失敗激增（已恢復）" and "恢復時間" in sender.sent[-1]["body"]
    row = state_of(member_session, "login_lockouts")
    assert (row.state, row.severity) == (STATE_OK, None)
    clock.advance(minutes=5)
    service.run()
    assert len(sender.sent) == 2


def test_the_since_time_in_the_resolved_mail_is_when_the_alert_started(member_session):
    clock, sender = FakeClock(), Sender()
    lock_accounts(member_session, clock, 5)
    service = make(member_session, clock, sender)
    service.run()
    member_session.query(User).update({"locked_until": None})
    member_session.commit()
    clock.advance(hours=3)
    service.run()
    body = sender.sent[-1]["body"]
    assert "開始時間：2026 年 10 月 27 日 20:00（台灣時間）" in body and "恢復時間：2026 年 10 月 27 日 23:00（台灣時間）" in body


# ---- 樣本不足 ------------------------------------------------------------------------

def test_an_insufficient_sample_keeps_a_firing_alert_firing_and_never_declares_recovery(member_session):
    clock, sender = FakeClock(), Sender()
    repo = AlertStateRepository(member_session)
    repo.ensure("llm_error_rate", clock())
    assert repo.claim(
        "llm_error_rate", expected_state=STATE_OK, expected_severity=None, expected_notified_at=None, state=STATE_FIRING,
        severity=SEVERITY_WARNING, changed_at=clock(), notified_at=clock(), value=0.5, now=clock(),
    )
    member_session.commit()
    clock.advance(minutes=30)                                              # 窗口內完全沒有流量
    report = make(member_session, clock, sender).run()
    assert result_of(report, "llm_error_rate").outcome == svc.OUTCOME_UNCHANGED and sender.sent == []
    assert state_of(member_session, "llm_error_rate").state == STATE_FIRING


def test_a_real_error_burst_fires_the_error_rate_alert(member_session):
    clock, sender = FakeClock(), Sender()
    for _ in range(17):
        burn(member_session, clock, 1, ago=timedelta(minutes=2))
    for status in ("rate_limited", "timeout", "server_error"):
        burn(member_session, clock, 0, status=status, ago=timedelta(minutes=1))
    report = make(member_session, clock, sender).run()
    assert (result_of(report, "llm_error_rate").action, result_of(report, "llm_error_rate").evaluation.value) == ("firing", pytest.approx(0.15))
    assert any("模型錯誤率" in s for s in sender.subjects())


def test_a_single_config_error_is_critical_and_the_latency_alert_uses_the_database_p95(member_session):
    clock, sender = FakeClock(), Sender()
    burn(member_session, clock, 0, status="config_error", kind="summary", ago=timedelta(minutes=1))
    for _ in range(94):
        burn(member_session, clock, 1, latency=2_000, ago=timedelta(minutes=10))
    for _ in range(6):
        burn(member_session, clock, 1, latency=12_000, ago=timedelta(minutes=10))
    report = make(member_session, clock, sender).run()
    assert result_of(report, "llm_config_error").evaluation.severity == SEVERITY_CRITICAL
    latency = result_of(report, "llm_latency_p95").evaluation
    assert (latency.severity, latency.value) == (SEVERITY_WARNING, 12_000)


# ---- 寄信失敗 ------------------------------------------------------------------------

def test_when_every_recipient_fails_the_state_is_restored_and_the_next_check_retries(member_session, log_lines):
    clock, sender = FakeClock(), Sender()
    sender.fail_all = True
    lock_accounts(member_session, clock, 5)
    service = make(member_session, clock, sender)

    failed = service.run()
    outcome = result_of(failed, "login_lockouts")
    assert (outcome.outcome, outcome.sent, outcome.failed_recipients) == (svc.OUTCOME_DELIVERY_FAILED, 0, 1)
    assert failed.failed == ["login_lockouts"]
    row = state_of(member_session, "login_lockouts")
    assert (row.state, row.last_notified_at) == (STATE_OK, None)           # 沒有標記為已通知
    assert [line["reason"] for line in log_lines() if line.get("event") == "alert.delivery_failed"] == ["login_lockouts"]
    assert not [line for line in log_lines() if line.get("event") == "alert.firing"]

    sender.fail_all = False
    clock.advance(minutes=5)
    retried = result_of(service.run(), "login_lockouts")
    assert (retried.outcome, retried.action, retried.sent) == (svc.OUTCOME_NOTIFIED, "firing", 1)
    assert state_of(member_session, "login_lockouts").state == STATE_FIRING


def test_when_only_some_recipients_fail_it_counts_as_notified_and_is_not_resent(member_session):
    clock, sender = FakeClock(), Sender()
    sender.failing = {OTHER}
    lock_accounts(member_session, clock, 5)
    service = make(member_session, clock, sender, recipients=(RECIPIENT, OTHER))

    report = service.run()
    outcome = result_of(report, "login_lockouts")
    assert (outcome.outcome, outcome.sent, outcome.failed_recipients) == (svc.OUTCOME_DELIVERY_FAILED, 1, 1)
    assert report.failed == ["login_lockouts"]                                # 指令仍以非零代碼結束
    assert [m["to"] for m in sender.sent] == [RECIPIENT]
    assert state_of(member_session, "login_lockouts").state == STATE_FIRING
    clock.advance(minutes=5)
    service.run()
    assert len(sender.sent) == 1                                              # 已收到的人不會重複收到


def test_every_recipient_gets_its_own_mail(member_session):
    clock, sender = FakeClock(), Sender()
    lock_accounts(member_session, clock, 5)
    make(member_session, clock, sender, recipients=(RECIPIENT, OTHER)).run()
    assert sorted(m["to"] for m in sender.sent) == sorted([RECIPIENT, OTHER])
    assert RECIPIENT not in sender.sent[0]["body"] and OTHER not in sender.sent[0]["body"]


# ---- 隔離 ----------------------------------------------------------------------------

def test_a_failing_query_isolates_that_alert_and_the_others_still_work(member_session, log_lines):
    class Breaks(LlmCallLogRepository):
        def chat_status_counts(self, *, start, end):
            raise RuntimeError("資料庫暫時失敗 secret-db-detail")

    clock, sender = FakeClock(), Sender()
    lock_accounts(member_session, clock, 5)
    report = make(member_session, clock, sender, ledger=Breaks(member_session)).run()
    assert result_of(report, "llm_error_rate").outcome == svc.OUTCOME_CHECK_FAILED
    assert result_of(report, "login_lockouts").outcome == svc.OUTCOME_NOTIFIED
    assert report.failed == ["llm_error_rate"]
    assert [line["reason"] for line in log_lines() if line.get("event") == "alert.check_failed"] == ["llm_error_rate"]
    assert "secret-db-detail" not in log_lines.raw()


# ---- 唯讀 ----------------------------------------------------------------------------

def test_dry_run_evaluates_and_reports_the_would_be_action_but_changes_nothing(member_session, log_lines):
    clock, sender = FakeClock(), Sender()
    lock_accounts(member_session, clock, 5)
    report = make(member_session, clock, sender).run(dry_run=True)
    assert report.dry_run and report.read_only
    entry = result_of(report, "login_lockouts")
    assert (entry.outcome, entry.action) == (svc.OUTCOME_PREVIEW, "firing")      # 預覽顯示「正式執行會進入告警」
    assert sender.sent == [] and member_session.query(AlertState).count() == 0
    assert [line for line in log_lines() if line.get("event", "").startswith("alert.")] == []


def test_without_recipients_it_only_evaluates_and_leaves_the_state_alone(member_session):
    clock, sender = FakeClock(), Sender()
    lock_accounts(member_session, clock, 5)
    report = make(member_session, clock, sender, recipients=()).run()
    assert not report.recipients_configured and report.read_only
    assert result_of(report, "login_lockouts").evaluation.severity == SEVERITY_WARNING
    assert sender.sent == [] and member_session.query(AlertState).count() == 0


def test_after_recipients_are_configured_an_already_active_alert_is_notified_normally(member_session):
    clock, sender = FakeClock(), Sender()
    lock_accounts(member_session, clock, 5)
    make(member_session, clock, sender, recipients=()).run()
    make(member_session, clock, sender).run()
    assert sender.subjects() == ["【智能客服】告警：登入失敗激增（進入告警）"]


def test_recipients_without_a_sender_is_a_programming_error(member_session):
    with pytest.raises(ValueError):
        AlertService(
            states=AlertStateRepository(member_session), ledger=LlmCallLogRepository(member_session), users=UserRepository(member_session),
            usage_settings=USAGE, recipients=(RECIPIENT,), mail_sender=None, clock=FakeClock(), commit=member_session.commit, rollback=member_session.rollback,
        )


# ---- 事件與不外洩 ---------------------------------------------------------------------

def test_events_and_logs_never_contain_the_recipient_or_the_smtp_detail(member_session, log_lines):
    clock, sender = FakeClock(), Sender()
    lock_accounts(member_session, clock, 5)
    sender.failing = {OTHER}
    make(member_session, clock, sender, recipients=(RECIPIENT, OTHER)).run()
    firing = [line for line in log_lines() if line.get("event") == "alert.firing"]
    assert [(line["reason"], line["severity"], line["count"]) for line in firing] == [("login_lockouts", "warning", 5)]
    for secret in (RECIPIENT, OTHER, "secret-smtp-detail", "zz-host.example"):
        assert secret not in log_lines.raw()


# ---- 並行 ----------------------------------------------------------------------------

def test_two_checks_at_the_same_time_send_a_single_mail(concurrent_engine):
    clock, sender = FakeClock(), Sender()
    with Session(concurrent_engine) as s:
        lock_accounts(s, clock, 5)
    barrier = Barrier(2)

    def check(_index: int):
        with Session(concurrent_engine) as session:
            service = make(session, clock, sender)
            barrier.wait(timeout=30)
            return service.run()

    with ThreadPoolExecutor(max_workers=2) as pool:
        reports = [f.result(timeout=60) for f in [pool.submit(check, i) for i in range(2)]]
    assert len(sender.sent) == 1 and sender.sent[0]["subject"].endswith("（進入告警）")
    notified = [r for report in reports for r in report.results if r.key == "login_lockouts" and r.outcome == svc.OUTCOME_NOTIFIED]
    assert len(notified) == 1
