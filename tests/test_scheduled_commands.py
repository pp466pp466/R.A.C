"""deployment-plan 規格：排程指令的清單與應用程式實際提供的指令一致。"""
import re

import pytest

from app import cli, create_app
from app.config import load_config


@pytest.fixture(scope="module")
def registered() -> set[str]:
    app = create_app(load_config(environ={"APP_ENV": "testing"}))
    return set(app.cli.commands)


def test_every_registered_command_is_either_scheduled_or_manual(registered):
    declared = set(cli.SCHEDULED_COMMANDS) | set(cli.MANUAL_COMMANDS)
    assert registered - declared == set(), f"這些指令沒有分類為排程或人工操作：{sorted(registered - declared)}"
    assert declared - registered == set(), f"這些分類的指令不存在：{sorted(declared - registered)}"


def test_no_command_is_both_scheduled_and_manual():
    assert set(cli.SCHEDULED_COMMANDS).isdisjoint(cli.MANUAL_COMMANDS)


def test_the_five_scheduled_commands_and_their_frequencies():
    assert set(cli.SCHEDULED_COMMANDS) == {
        "check-alerts", "purge-unconfirmed-accounts", "execute-account-deletions", "purge-expired-data", "purge-inactive-accounts",
    }
    assert cli.SCHEDULED_COMMANDS["check-alerts"] == "*/5 * * * *"
    # 逾時未完成年齡確認的帳號每小時清除一次（24 小時的補填期限）；其餘三個是每日、錯開時間
    assert cli.SCHEDULED_COMMANDS["purge-unconfirmed-accounts"] == "0 * * * *"
    daily = {name: cron for name, cron in cli.SCHEDULED_COMMANDS.items() if name not in ("check-alerts", "purge-unconfirmed-accounts")}
    assert len(daily) == 3 and len(set(daily.values())) == 3, "三個每日指令應錯開時間"
    for cron in daily.values():
        minute, hour, day, month, weekday = cron.split()
        assert (day, month, weekday) == ("*", "*", "*") and hour == "3" and minute.isdigit()


def test_every_cron_expression_has_five_fields():
    for name, cron in cli.SCHEDULED_COMMANDS.items():
        assert len(cron.split()) == 5 and re.fullmatch(r"[\d*/, -]+", cron), name


def test_a_command_added_without_classification_would_be_caught(registered, monkeypatch):
    monkeypatch.setattr(cli, "MANUAL_COMMANDS", tuple(name for name in cli.MANUAL_COMMANDS if name != "age-stats"))
    declared = set(cli.SCHEDULED_COMMANDS) | set(cli.MANUAL_COMMANDS)
    assert registered - declared == {"age-stats"}
