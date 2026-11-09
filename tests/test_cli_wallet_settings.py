"""runtime-limits 規格「只有部署者以指令修改並留下稽核」的遊戲幣部分：`flask set-wallet-setting`、`flask show-wallet-settings`——
修改並稽核、範圍外被拒、用錯指令被拒並指出正確的指令、輸出沒有任何個資、`show-script-limits` 仍只輸出十個鍵、沒有網路端點。
"""
import re

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import cli, create_app
from app.config import load_config
from app.models import AdminAuditLog, Base, RuntimeLimit
from tests.support.auth import FakeClock

WALLET_KEYS = ("coin.turn_cost.standard", "coin.signup_grant", "coin.checkin_grant", "coin.free_cap", "coin.hold_timeout_seconds")


@pytest.fixture()
def app():
    application = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


@pytest.fixture()
def runner(app):
    return app.test_cli_runner()


def stored(app) -> dict[str, int]:
    with Session(app.extensions["db_engine"]) as s:
        return {row.key: row.int_value for row in s.scalars(select(RuntimeLimit))}


def audit_rows(app):
    with Session(app.extensions["db_engine"]) as s:
        return list(s.scalars(select(AdminAuditLog)))


def test_the_commands_are_registered_documented_and_classified_as_manual(app):
    for name in ("set-wallet-setting", "show-wallet-settings"):
        assert name in app.cli.commands and name in (cli.__doc__ or "")
        assert name in cli.MANUAL_COMMANDS and name not in cli.SCHEDULED_COMMANDS


def test_the_set_command_changes_the_value_prints_the_old_and_new_and_audits_only_the_key_and_values(app, runner):
    result = runner.invoke(args=["set-wallet-setting", "coin.checkin_grant", "300"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "coin.checkin_grant：200 → 300"
    assert stored(app) == {"coin.checkin_grant": 300}
    (row,) = audit_rows(app)
    assert (row.action, row.admin_user_id, row.target_user_public_id) == ("limit.set", None, None)
    assert row.detail_json == {"limit_key": "coin.checkin_grant", "old_value": 200, "new_value": 300}
    second = runner.invoke(args=["set-wallet-setting", "coin.checkin_grant", "250"])
    assert second.output.strip() == "coin.checkin_grant：300 → 250"


@pytest.mark.parametrize(
    "key, value",
    [("coin.turn_cost.standard", "0"), ("coin.free_cap", "1000001"), ("coin.hold_timeout_seconds", "59"), ("coin.signup_grant", "-1"),
     ("coin.gold", "5"), ("coin.turn_cost.premium", "30")],
)
def test_out_of_range_values_and_unknown_keys_are_refused_without_changing_anything(app, runner, key, value):
    result = runner.invoke(args=["set-wallet-setting", "--", key, value])           # `--`：負數不會被當成選項
    assert result.exit_code != 0 and key in result.output
    assert stored(app) == {} and audit_rows(app) == []


def test_a_non_integer_value_is_refused(app, runner):
    result = runner.invoke(args=["set-wallet-setting", "coin.free_cap", "lots"])
    assert result.exit_code != 0 and stored(app) == {}


def test_each_command_refuses_the_other_groups_keys_and_names_the_right_command(app, runner):
    wrong_one = runner.invoke(args=["set-script-limit", "coin.free_cap", "500"])
    assert wrong_one.exit_code != 0 and "set-wallet-setting" in wrong_one.output
    wrong_two = runner.invoke(args=["set-wallet-setting", "script_limit.free", "5"])
    assert wrong_two.exit_code != 0 and "set-script-limit" in wrong_two.output
    assert stored(app) == {} and audit_rows(app) == []


def test_the_show_command_lists_exactly_the_five_keys_and_marks_the_fallback_defaults(app, runner):
    runner.invoke(args=["set-wallet-setting", "coin.free_cap", "800"])
    result = runner.invoke(args=["show-wallet-settings"])
    assert result.exit_code == 0
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert [line.split()[0] for line in lines] == list(WALLET_KEYS)
    by_key = {line.split()[0]: line for line in lines}
    assert "coin.free_cap = 800" in by_key["coin.free_cap"] and "保底預設" not in by_key["coin.free_cap"]
    assert "coin.checkin_grant = 200" in by_key["coin.checkin_grant"] and "保底預設" in by_key["coin.checkin_grant"]
    assert "範圍 1 至 10000" in by_key["coin.turn_cost.standard"]


def test_show_script_limits_still_lists_only_the_ten_script_keys(app, runner):
    result = runner.invoke(args=["show-script-limits"])
    keys = [line.split()[0] for line in result.output.splitlines() if line.strip()]
    assert len(keys) == 10 and not any(key.startswith("coin.") for key in keys)


def test_the_output_contains_only_keys_and_numbers(app, runner):
    runner.invoke(args=["set-wallet-setting", "coin.signup_grant", "250"])
    for args in (["show-wallet-settings"], ["set-wallet-setting", "coin.signup_grant", "260"]):
        result = runner.invoke(args=args)
        assert not re.search(r"[@/\\]|password|token|email", result.output, re.I)


def test_a_changed_setting_is_read_by_the_wallet_immediately(app, runner):
    """部署者改了之後，下一個請求就使用新值（每次現讀，不必重新部署）。"""
    from tests.support.auth import add_user, sign_in_as

    runner.invoke(args=["set-wallet-setting", "coin.signup_grant", "321"])
    with Session(app.extensions["db_engine"]) as s:
        user = add_user(s, 1, password=None)
        uid, tv = user.id, user.token_version
    client = app.test_client()
    sign_in_as(client, uid, tv)
    assert client.get("/api/wallet").get_json()["wallet"]["free_balance"] == 321
