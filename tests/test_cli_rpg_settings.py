"""runtime-limits 規格「只有部署者以指令修改並留下稽核」的 RPG 部分：`flask set-rpg-setting`、`flask show-rpg-settings`——
修改並稽核、範圍外被拒、用錯指令被拒並指出正確的指令、輸出沒有任何個資、另外兩組的 show 指令不含 RPG 的鍵、沒有網路端點。
"""
import re

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import cli, create_app
from app.config import load_config
from app.models import AdminAuditLog, Base, RuntimeLimit
from tests.support.auth import FakeClock

RPG_KEYS = ("rpg.max_months_per_turn", "rpg.free_regenerations")


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
    for name in ("set-rpg-setting", "show-rpg-settings"):
        assert name in app.cli.commands and name in (cli.__doc__ or "")
        assert name in cli.MANUAL_COMMANDS and name not in cli.SCHEDULED_COMMANDS


def test_the_set_command_changes_the_value_prints_the_old_and_new_and_audits_only_the_key_and_values(app, runner):
    result = runner.invoke(args=["set-rpg-setting", "rpg.max_months_per_turn", "12"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "rpg.max_months_per_turn：24 → 12"                # 資料庫沒有那一列：舊值是保底預設
    assert stored(app) == {"rpg.max_months_per_turn": 12}
    (row,) = audit_rows(app)
    assert (row.action, row.admin_user_id, row.target_user_public_id) == ("limit.set", None, None)
    assert row.detail_json == {"limit_key": "rpg.max_months_per_turn", "old_value": 24, "new_value": 12}
    second = runner.invoke(args=["set-rpg-setting", "rpg.max_months_per_turn", "0"])          # 0 合法：不允許時間流逝
    assert second.output.strip() == "rpg.max_months_per_turn：12 → 0"


@pytest.mark.parametrize(
    "key, value",
    [
        ("rpg.max_months_per_turn", "241"), ("rpg.max_months_per_turn", "-1"), ("rpg.max_turns", "5"),
        ("rpg.free_regenerations", "21"), ("rpg.free_regenerations", "-1"),
    ],
)
def test_out_of_range_values_and_unknown_keys_are_refused_without_changing_anything(app, runner, key, value):
    result = runner.invoke(args=["set-rpg-setting", "--", key, value])           # `--`：負數不會被當成選項
    assert result.exit_code != 0 and key in result.output
    assert stored(app) == {} and audit_rows(app) == []


def test_a_non_integer_value_is_refused(app, runner):
    result = runner.invoke(args=["set-rpg-setting", "rpg.max_months_per_turn", "many"])
    assert result.exit_code != 0 and stored(app) == {}


def test_each_command_refuses_the_other_groups_keys_and_names_the_right_command(app, runner):
    wrong_one = runner.invoke(args=["set-rpg-setting", "coin.free_cap", "500"])
    assert wrong_one.exit_code != 0 and "set-wallet-setting" in wrong_one.output
    wrong_two = runner.invoke(args=["set-rpg-setting", "script_limit.free", "5"])
    assert wrong_two.exit_code != 0 and "set-script-limit" in wrong_two.output
    wrong_three = runner.invoke(args=["set-wallet-setting", "rpg.max_months_per_turn", "5"])
    assert wrong_three.exit_code != 0 and "set-rpg-setting" in wrong_three.output
    wrong_four = runner.invoke(args=["set-script-limit", "rpg.max_months_per_turn", "5"])
    assert wrong_four.exit_code != 0 and "set-rpg-setting" in wrong_four.output
    assert stored(app) == {} and audit_rows(app) == []


def test_the_show_command_lists_exactly_the_rpg_keys_and_marks_the_fallback_default(app, runner):
    before = runner.invoke(args=["show-rpg-settings"])
    assert before.exit_code == 0
    lines = [line for line in before.output.splitlines() if line.strip()]
    assert [line.split()[0] for line in lines] == list(RPG_KEYS)
    assert "rpg.max_months_per_turn = 24" in lines[0] and "保底預設" in lines[0] and "範圍 0 至 240" in lines[0]
    assert "rpg.free_regenerations = 2" in lines[1] and "保底預設" in lines[1] and "範圍 0 至 20" in lines[1]

    runner.invoke(args=["set-rpg-setting", "rpg.max_months_per_turn", "36"])
    after = [line for line in runner.invoke(args=["show-rpg-settings"]).output.splitlines() if line.strip()]
    assert "rpg.max_months_per_turn = 36" in after[0] and "保底預設" not in after[0]
    assert "保底預設" in after[1]                                                      # 只改了一個，另一個仍是保底預設


def test_the_free_regenerations_setting_is_changed_audited_and_read_on_the_next_use(app, runner):
    """每個回合前幾次重新生成免費：部署者以指令修改並留下稽核，下一次使用就是新值（rpg-turn-revision）。"""
    from app.repositories.runtime_limit_repository import RuntimeLimitRepository
    from app.services.runtime_limits import RuntimeLimits

    result = runner.invoke(args=["set-rpg-setting", "rpg.free_regenerations", "5"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "rpg.free_regenerations：2 → 5"                    # 資料庫沒有那一列：舊值是保底預設
    assert stored(app) == {"rpg.free_regenerations": 5}
    (row,) = audit_rows(app)
    assert (row.action, row.admin_user_id, row.target_user_public_id) == ("limit.set", None, None)
    assert row.detail_json == {"limit_key": "rpg.free_regenerations", "old_value": 2, "new_value": 5}
    with Session(app.extensions["db_engine"]) as s:
        assert RuntimeLimits(limits=RuntimeLimitRepository(s), clock=FakeClock()).rpg_free_regenerations() == 5
    assert runner.invoke(args=["set-rpg-setting", "rpg.free_regenerations", "0"]).output.strip() == "rpg.free_regenerations：5 → 0"     # 0 合法
    with Session(app.extensions["db_engine"]) as s:
        assert RuntimeLimits(limits=RuntimeLimitRepository(s), clock=FakeClock()).rpg_free_regenerations() == 0
    for wrong in (["set-wallet-setting", "rpg.free_regenerations", "3"], ["set-script-limit", "rpg.free_regenerations", "3"]):
        refused = runner.invoke(args=wrong)
        assert refused.exit_code != 0 and "set-rpg-setting" in refused.output
    assert stored(app) == {"rpg.free_regenerations": 0}


def test_the_other_show_commands_do_not_list_the_rpg_keys(app, runner):
    for command in ("show-script-limits", "show-wallet-settings"):
        keys = [line.split()[0] for line in runner.invoke(args=[command]).output.splitlines() if line.strip()]
        assert keys and not any(key.startswith("rpg.") for key in keys), command


def test_the_output_contains_only_keys_and_numbers(app, runner):
    runner.invoke(args=["set-rpg-setting", "rpg.max_months_per_turn", "10"])
    for args in (["show-rpg-settings"], ["set-rpg-setting", "rpg.max_months_per_turn", "11"]):
        result = runner.invoke(args=args)
        assert not re.search(r"[@/\\]|password|token|email", result.output, re.I)


def test_there_is_no_http_endpoint_that_can_change_the_setting(app):
    writers = {"POST", "PUT", "PATCH", "DELETE"}
    for rule in app.url_map.iter_rules():
        if "limit" in rule.rule.lower() or "setting" in rule.rule.lower():
            assert not (rule.methods & writers), f"{rule.rule} 不得提供寫入"


def test_a_changed_setting_is_read_on_the_next_use_without_restarting(app, runner):
    """部署者改了之後，下一次使用就是新值（每次現讀，不必重新部署）。"""
    from app.repositories.runtime_limit_repository import RuntimeLimitRepository
    from app.services.runtime_limits import RuntimeLimits

    runner.invoke(args=["set-rpg-setting", "rpg.max_months_per_turn", "6"])
    with Session(app.extensions["db_engine"]) as s:
        limits = RuntimeLimits(limits=RuntimeLimitRepository(s), clock=FakeClock())
        assert limits.rpg_max_months_per_turn() == 6
    runner.invoke(args=["set-rpg-setting", "rpg.max_months_per_turn", "7"])
    with Session(app.extensions["db_engine"]) as s:
        assert RuntimeLimits(limits=RuntimeLimitRepository(s), clock=FakeClock()).rpg_max_months_per_turn() == 7
