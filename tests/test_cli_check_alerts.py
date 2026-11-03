"""operational-alerts 規格：排程指令 `check-alerts`（預覽不寄信不改狀態、重複執行不重寄、失敗的結束代碼、輸出與日誌不含收件人）。"""
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import AlertState, Base, LlmCallLog, User
from app.repositories.llm_call_log_repository import LlmCallLogRepository
from tests.support.auth import START, FakeClock, add_user
from tests.support.config_env import AUTH_ENV

RECIPIENT = "watch.me@zz-host.example"


class Sender:
    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []
        self.fail = False

    def send(self, *, to: str, subject: str, body: str) -> None:
        if self.fail:
            raise ConnectionError("SMTP 無回應：secret-smtp-detail")
        self.sent.append({"to": to, "subject": subject, "body": body})


def make_app(*, recipient: str | None = RECIPIENT, sender: Sender | None = None, clock: FakeClock | None = None):
    environ = {"APP_ENV": "testing", "USAGE_DAILY_TOKEN_LIMIT": "1000", "USAGE_DAILY_CALL_LIMIT": "1000"}
    if recipient:
        environ.update({"ALERT_EMAIL_TO": recipient, **{k: v for k, v in AUTH_ENV.items() if k.startswith("MAIL_")}})
    app = create_app(load_config(environ=environ), clock=clock or FakeClock(), mail_sender=sender)
    Base.metadata.create_all(app.extensions["db_engine"])
    return app


def lock_five(app, clock) -> None:
    with Session(app.extensions["db_engine"]) as s:
        for n in range(1, 6):
            add_user(s, n, email=f"secret.person{n}@example.com", email_canonical=f"secret.person{n}@example.com",
                     display_name="機密姓名", locked_until=clock() + timedelta(days=1), failed_login_count=5)


def states(app) -> int:
    with Session(app.extensions["db_engine"]) as s:
        return s.scalar(select(func.count(AlertState.id)))


@pytest.fixture()
def world():
    clock, sender = FakeClock(), Sender()
    app = make_app(sender=sender, clock=clock)
    return app, clock, sender, app.test_cli_runner()


def test_the_dry_run_lists_every_alert_and_the_action_it_would_take_without_touching_anything(world, log_lines):
    app, clock, sender, runner = world
    lock_five(app, clock)
    result = runner.invoke(args=["check-alerts", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "預覽" in result.output and "沒有寄信" in result.output
    for name in ("全站用量", "模型錯誤率", "模型設定錯誤", "對話延遲（p95）", "帳號鎖定激增"):
        assert name in result.output, name
    assert "帳號鎖定激增：警告（目前 5 個帳號，門檻 5 個帳號）；正式執行會：進入告警" in result.output
    assert "模型錯誤率：樣本不足，維持原狀" in result.output and "全站用量：正常" in result.output
    assert sender.sent == [] and states(app) == 0
    assert [line for line in log_lines() if line.get("event", "").startswith("alert.")] == []


def test_a_real_run_notifies_once_and_a_second_run_stays_silent(world):
    app, clock, sender, runner = world
    lock_five(app, clock)
    first = runner.invoke(args=["check-alerts"])
    assert first.exit_code == 0 and "告警檢查結果" in first.output
    assert "帳號鎖定激增：警告（目前 5 個帳號，門檻 5 個帳號）；進入告警，已通知" in first.output
    assert [m["to"] for m in sender.sent] == [RECIPIENT] and sender.sent[0]["subject"].endswith("帳號鎖定激增（進入告警）")

    clock.advance(minutes=5)
    second = runner.invoke(args=["check-alerts"])
    assert second.exit_code == 0 and "狀態不變" in second.output and len(sender.sent) == 1


def test_the_output_has_no_recipient_no_personal_data_and_no_secrets(world, log_lines):
    app, clock, sender, runner = world
    lock_five(app, clock)
    sender.fail = True
    dry = runner.invoke(args=["check-alerts", "--dry-run"])
    real = runner.invoke(args=["check-alerts"])
    everything = dry.output + real.output + log_lines.raw() + "".join(m["body"] + m["subject"] for m in sender.sent)
    for secret in (RECIPIENT, "zz-host.example", "secret.person", "機密姓名", "secret-smtp-detail"):
        assert secret not in everything, secret


def test_a_delivery_failure_exits_nonzero_names_the_alert_and_the_next_run_retries(world, log_lines):
    app, clock, sender, runner = world
    lock_five(app, clock)
    sender.fail = True
    failed = runner.invoke(args=["check-alerts"])
    assert failed.exit_code != 0 and "以下告警失敗：帳號鎖定激增" in failed.output and "通知失敗" in failed.output
    assert [line["reason"] for line in log_lines() if line.get("event") == "alert.delivery_failed"] == ["login_lockouts"]

    sender.fail = False
    clock.advance(minutes=5)
    retried = runner.invoke(args=["check-alerts"])
    assert retried.exit_code == 0 and len(sender.sent) == 1


def test_one_failing_evaluation_exits_nonzero_but_the_rest_are_still_reported(world, monkeypatch):
    app, clock, sender, runner = world
    lock_five(app, clock)

    def broken(self, *, start, end):
        raise RuntimeError("資料庫暫時失敗")

    monkeypatch.setattr(LlmCallLogRepository, "chat_status_counts", broken)
    result = runner.invoke(args=["check-alerts"])
    assert result.exit_code != 0 and "模型錯誤率：評估失敗" in result.output and "以下告警失敗：模型錯誤率" in result.output
    assert "帳號鎖定激增" in result.output and len(sender.sent) == 1


def test_without_recipients_it_says_so_and_changes_nothing():
    clock = FakeClock()
    app = make_app(recipient=None, clock=clock)
    lock_five(app, clock)
    result = app.test_cli_runner().invoke(args=["check-alerts"])
    assert result.exit_code == 0 and "沒有設定告警收件人" in result.output and "不寄信也不改變狀態" in result.output
    assert "帳號鎖定激增：警告" in result.output and states(app) == 0


def test_a_recovered_alert_is_reported_and_mailed_once(world):
    app, clock, sender, runner = world
    lock_five(app, clock)
    runner.invoke(args=["check-alerts"])
    with Session(app.extensions["db_engine"]) as s:
        s.query(User).update({"locked_until": None})
        s.commit()
    clock.advance(minutes=5)
    result = runner.invoke(args=["check-alerts"])
    assert "已恢復，已通知" in result.output and sender.sent[-1]["subject"].endswith("（已恢復）") and len(sender.sent) == 2


def test_the_command_is_registered_and_documented_in_the_cli_module_docstring(world):
    from app import cli

    app, _, _, runner = world
    assert "check-alerts" in app.cli.commands and "check-alerts" in (cli.__doc__ or "")


def heartbeats(log_lines) -> int:
    return sum(1 for line in log_lines() if line.get("event") == "alert.check_completed")


def test_a_successful_real_run_logs_exactly_one_heartbeat_with_only_the_event_name(world, log_lines):
    app, clock, sender, runner = world
    assert runner.invoke(args=["check-alerts"]).exit_code == 0
    assert heartbeats(log_lines) == 1
    (line,) = [line for line in log_lines() if line.get("event") == "alert.check_completed"]
    assert not {"reason", "severity", "count", "user_id"} & set(line)
    clock.advance(minutes=5)
    runner.invoke(args=["check-alerts"])
    assert heartbeats(log_lines) == 2


def test_a_delivery_failure_still_logs_the_heartbeat_because_the_check_itself_ran(world, log_lines):
    app, clock, sender, runner = world
    lock_five(app, clock)
    sender.fail = True
    assert runner.invoke(args=["check-alerts"]).exit_code != 0
    assert heartbeats(log_lines) == 1


def test_the_preview_the_read_only_mode_and_a_failed_evaluation_log_no_heartbeat(world, log_lines, monkeypatch):
    app, clock, sender, runner = world
    runner.invoke(args=["check-alerts", "--dry-run"])
    assert heartbeats(log_lines) == 0

    def broken(self, *, start, end):
        raise RuntimeError("資料庫暫時失敗")

    monkeypatch.setattr(LlmCallLogRepository, "chat_status_counts", broken)
    assert runner.invoke(args=["check-alerts"]).exit_code != 0
    assert heartbeats(log_lines) == 0


def test_without_recipients_there_is_no_heartbeat(log_lines):
    clock = FakeClock()
    app = make_app(recipient=None, clock=clock)
    assert app.test_cli_runner().invoke(args=["check-alerts"]).exit_code == 0
    assert heartbeats(log_lines) == 0
