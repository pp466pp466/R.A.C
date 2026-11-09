"""runtime-limits 規格：存在資料庫的營運限制——鍵與合法範圍、初始值與保底預設一致、每次現讀、缺值或不合法時的保底、
以指令修改並留下稽核、沒有任何網路端點可以寫入。

服務層的測試在 SQLite 與真實 SQL Server（標記 sqlserver）上各跑一次；指令層用測試應用程式與 CLI runner。
"""
import importlib.util
import re

import pytest
from flask import Flask
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import cli, create_app
from app.config import PROJECT_ROOT, load_config
from app.models import AdminAuditLog, Base, RuntimeLimit
from app.repositories.runtime_limit_repository import RuntimeLimitRepository
from app.services.runtime_limits import LIMIT_KEYS, LIMIT_SPECS, LimitError, RuntimeLimits
from tests.support.auth import FakeClock

SCRIPT_KEYS = (
    "script_limit.free", "script_limit.plus", "script_limit.vip",
    "script_submit_daily.free", "script_submit_daily.plus", "script_submit_daily.vip",
    "protagonist_suggest_daily.free", "protagonist_suggest_daily.plus", "protagonist_suggest_daily.vip",
    "script_reject_streak",
)
WALLET_KEYS = ("coin.turn_cost.standard", "coin.signup_grant", "coin.checkin_grant", "coin.free_cap", "coin.hold_timeout_seconds")
RPG_KEYS = ("rpg.max_months_per_turn",)
ALL_KEYS = SCRIPT_KEYS + WALLET_KEYS + RPG_KEYS


def make_limits(session, audit=None, clock=None):
    return RuntimeLimits(limits=RuntimeLimitRepository(session), clock=clock or FakeClock(), audit=audit)


def put(session, key, value):
    session.add(RuntimeLimit(key=key, int_value=value, updated_at=FakeClock()()))
    session.flush()


def migration_initial_limits() -> dict[str, int]:
    """各遷移寫入的初始限制合計（0013 的七個、0014 的三個、0015 的五個加上 0016 的一個）。"""
    limits: dict[str, int] = {}
    for filename, name in (
        ("20261104_0013_rpg_script_authoring.py", "migration_0013"), ("20261104_0014_rpg_character_creation.py", "migration_0014"),
        ("20261105_0015_game_wallet.py", "migration_0015"), ("20261109_0016_rpg_turn_engine.py", "migration_0016"),
    ):
        spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / "migrations" / "versions" / filename)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert not set(limits) & set(dict(module.INITIAL_LIMITS)), "兩個遷移不應寫入同一個鍵"
        limits.update(dict(module.INITIAL_LIMITS))
    return limits


# ---- 鍵、範圍與初始值 -----------------------------------------------------------------


def test_the_sixteen_keys_are_fixed_in_three_groups():
    assert LIMIT_KEYS == ALL_KEYS and set(LIMIT_SPECS) == set(ALL_KEYS)
    assert {key for key, spec in LIMIT_SPECS.items() if spec.group == "script"} == set(SCRIPT_KEYS)
    assert {key for key, spec in LIMIT_SPECS.items() if spec.group == "wallet"} == set(WALLET_KEYS)
    assert {key for key, spec in LIMIT_SPECS.items() if spec.group == "rpg"} == set(RPG_KEYS)


def test_the_rpg_max_months_setting_ranges_from_0_to_240_with_a_default_of_24():
    spec = LIMIT_SPECS["rpg.max_months_per_turn"]
    assert (spec.default, spec.minimum, spec.maximum) == (24, 0, 240)


def test_the_rpg_max_months_is_read_each_time_and_falls_back_to_the_default(member_session, log_lines):
    limits = make_limits(member_session)
    assert limits.rpg_max_months_per_turn() == 24                                       # 缺值：保底預設
    put(member_session, "rpg.max_months_per_turn", 6)
    assert make_limits(member_session).rpg_max_months_per_turn() == 6                   # 下一次使用就是新值
    member_session.query(RuntimeLimit).filter_by(key="rpg.max_months_per_turn").one().int_value = 241
    member_session.flush()
    assert make_limits(member_session).rpg_max_months_per_turn() == 24                  # 範圍外：保底預設，不是無上限
    member_session.query(RuntimeLimit).filter_by(key="rpg.max_months_per_turn").one().int_value = 0
    member_session.flush()
    assert make_limits(member_session).rpg_max_months_per_turn() == 0                   # 0 合法
    assert any(line.get("event") == "limits.invalid" and line["reason"] == "rpg.max_months_per_turn" for line in log_lines())


def test_the_protagonist_suggestion_keys_range_from_0_to_100():
    for tier in ("free", "plus", "vip"):
        spec = LIMIT_SPECS[f"protagonist_suggest_daily.{tier}"]
        assert (spec.minimum, spec.maximum) == (0, 100)


def test_the_fallback_defaults_equal_the_values_the_migration_writes():
    """保底預設與遷移寫入的初始值是同一份數字；兩邊分歧會讓「缺值時的行為」與「剛部署時的行為」不同。"""
    assert {key: spec.default for key, spec in LIMIT_SPECS.items()} == migration_initial_limits()
    assert migration_initial_limits() == {
        "script_limit.free": 3, "script_limit.plus": 10, "script_limit.vip": 30,
        "script_submit_daily.free": 3, "script_submit_daily.plus": 5, "script_submit_daily.vip": 10,
        "protagonist_suggest_daily.free": 5, "protagonist_suggest_daily.plus": 15, "protagonist_suggest_daily.vip": 40,
        "script_reject_streak": 3,
        "coin.turn_cost.standard": 30, "coin.signup_grant": 200, "coin.checkin_grant": 200, "coin.free_cap": 1000,
        "coin.hold_timeout_seconds": 180,
        "rpg.max_months_per_turn": 24,
    }


def test_every_default_is_inside_its_own_range():
    for spec in LIMIT_SPECS.values():
        assert spec.minimum <= spec.default <= spec.maximum, spec.key


# ---- 讀取：每次現讀、保底預設 ---------------------------------------------------------


def test_reading_returns_the_stored_value(member_session):
    put(member_session, "script_limit.plus", 12)
    limits = make_limits(member_session)
    assert limits.script_limit("plus") == 12
    assert limits.script_limit("free") == 3                   # 沒有那一列：保底預設


def test_a_changed_value_is_seen_by_the_next_request_without_restarting(concurrent_engine):
    """每次使用時現讀，沒有行程內快取：另一個 worker（另一個 Session）提交修改後，下一個請求就讀到新值。"""
    with Session(concurrent_engine) as worker_a:
        put(worker_a, "script_limit.free", 3)
        worker_a.commit()
    with Session(concurrent_engine) as request_one:
        assert make_limits(request_one).script_limit("free") == 3
    with Session(concurrent_engine) as other_worker:
        other_worker.execute(select(RuntimeLimit))
        row = other_worker.scalar(select(RuntimeLimit).where(RuntimeLimit.key == "script_limit.free"))
        row.int_value = 7
        other_worker.commit()
    with Session(concurrent_engine) as request_two:
        assert make_limits(request_two).script_limit("free") == 7
    with Session(concurrent_engine) as request_three:                         # 第三個請求（另一個 worker）也一致
        assert make_limits(request_three).script_limit("free") == 7


def test_the_same_session_sees_a_value_changed_after_its_first_read(member_session):
    """欄位查詢不經過身分對應表，所以同一個 Session 內修改後再讀也是新值（不是第一次讀到的快取）。"""
    put(member_session, "script_limit.free", 3)
    limits = make_limits(member_session)
    assert limits.script_limit("free") == 3
    RuntimeLimitRepository(member_session).set_value("script_limit.free", 9, FakeClock()())
    assert limits.script_limit("free") == 9


def test_a_missing_key_uses_the_default_and_logs_only_the_key_name(member_session, log_lines):
    limits = make_limits(member_session)
    assert limits.script_limit("vip") == 30
    events = [line for line in log_lines() if line.get("event") == "limits.invalid"]
    assert len(events) == 1 and events[0]["reason"] == "script_limit.vip"
    assert set(events[0]) <= {"timestamp", "time", "level", "logger", "message", "event", "reason", "request_id"}


@pytest.mark.parametrize("bad", [-1, 501, 10_000])
def test_an_out_of_range_value_falls_back_to_the_default(member_session, log_lines, bad):
    put(member_session, "script_limit.free", bad)
    assert make_limits(member_session).script_limit("free") == 3
    assert any(line.get("event") == "limits.invalid" and line["reason"] == "script_limit.free" for line in log_lines())


def test_a_corrupt_value_never_means_unlimited(member_session):
    """範圍外的值（例如被誤改成很大的數字）不會放行無上限，而是退回保守的預設。"""
    put(member_session, "script_submit_daily.free", 10**9)
    put(member_session, "script_reject_streak", 0)
    limits = make_limits(member_session)
    assert limits.submit_daily("free") == 3 and limits.reject_streak() == 3


def test_an_unknown_tier_gets_the_most_conservative_free_value(member_session):
    put(member_session, "script_limit.free", 2)
    limits = make_limits(member_session)
    assert limits.script_limit("gold") == 2 and limits.script_limit(None) == 2 and limits.submit_daily(123) == 3


def test_zero_is_a_legal_limit(member_session):
    put(member_session, "script_limit.free", 0)
    assert make_limits(member_session).script_limit("free") == 0


def test_asking_for_an_unknown_key_is_an_error():
    with pytest.raises(LimitError):
        RuntimeLimits(limits=None, clock=FakeClock()).get("script_limit.gold")          # 鍵檢查在碰資料庫之前


def test_rows_reports_every_key_with_its_effective_value_and_where_it_comes_from(member_session):
    put(member_session, "script_limit.plus", 20)
    put(member_session, "script_limit.vip", 9999)                                       # 不合法
    rows = {row.key: row for row in make_limits(member_session).rows()}
    assert list(rows) == list(ALL_KEYS)
    assert list(r.key for r in make_limits(member_session).rows("script")) == list(SCRIPT_KEYS)
    assert list(r.key for r in make_limits(member_session).rows("wallet")) == list(WALLET_KEYS)
    assert list(r.key for r in make_limits(member_session).rows("rpg")) == list(RPG_KEYS)
    assert rows["script_limit.plus"].value == 20 and rows["script_limit.plus"].from_database
    assert rows["script_limit.vip"].value == 30 and not rows["script_limit.vip"].from_database
    assert rows["script_limit.free"].value == 3 and not rows["script_limit.free"].from_database


def test_suggest_daily_reads_the_tier_value_each_time_and_falls_back_to_free_for_unknown_tiers(member_session):
    limits = make_limits(member_session)
    assert [limits.suggest_daily(tier) for tier in ("free", "plus", "vip")] == [5, 15, 40]
    assert limits.suggest_daily("gold") == limits.suggest_daily(None) == limits.suggest_daily(7) == 5
    put(member_session, "protagonist_suggest_daily.plus", 20)
    assert make_limits(member_session).suggest_daily("plus") == 20                      # 下一次呼叫立即讀到新值
    member_session.query(RuntimeLimit).filter_by(key="protagonist_suggest_daily.plus").one().int_value = 101
    member_session.flush()
    assert make_limits(member_session).suggest_daily("plus") == 15                      # 範圍外：保底預設


def test_setting_the_new_keys_is_audited_and_out_of_range_values_are_refused(member_session):
    calls, audit = recording_audit()
    limits = make_limits(member_session, audit)
    assert limits.set("protagonist_suggest_daily.vip", 50) == (40, 50)
    assert calls[-1][2] == {"limit_key": "protagonist_suggest_daily.vip", "old_value": 40, "new_value": 50}
    with pytest.raises(LimitError):
        limits.set("protagonist_suggest_daily.vip", 101)
    with pytest.raises(LimitError):
        limits.set("protagonist_suggest_daily.vip", -1)
    assert limits.suggest_daily("vip") == 50


# ---- 修改：驗證與稽核 -------------------------------------------------------------------


def recording_audit():
    calls = []
    return calls, lambda action, target, detail: calls.append((action, target, dict(detail)))


def test_setting_a_legal_value_writes_it_and_audits_only_the_key_and_the_old_and_new_values(member_session):
    calls, audit = recording_audit()
    limits = make_limits(member_session, audit)
    assert limits.set("script_limit.plus", 12) == (10, 12)
    assert limits.script_limit("plus") == 12
    assert calls == [("limit.set", None, {"limit_key": "script_limit.plus", "old_value": 10, "new_value": 12})]
    assert limits.set("script_limit.plus", 14) == (12, 14)                              # 第二次：舊值是上一次的值
    assert calls[-1][2]["old_value"] == 12


@pytest.mark.parametrize("key, value", [("script_submit_daily.free", 101), ("script_limit.free", -1), ("script_reject_streak", 0)])
def test_an_out_of_range_value_is_refused_and_the_old_value_stays(member_session, key, value):
    calls, audit = recording_audit()
    limits = make_limits(member_session, audit)
    before = limits.get(key)
    with pytest.raises(LimitError):
        limits.set(key, value)
    assert limits.get(key) == before and calls == []


@pytest.mark.parametrize("key", ["script_limit.gold", "script_limit", "", "SCRIPT_LIMIT.FREE"])
def test_an_unknown_key_is_refused(member_session, key):
    calls, audit = recording_audit()
    with pytest.raises(LimitError):
        make_limits(member_session, audit).set(key, 1)
    assert calls == []


@pytest.mark.parametrize("value", [True, 1.5, "3", None])
def test_a_value_that_is_not_an_integer_is_refused(member_session, value):
    with pytest.raises(LimitError):
        make_limits(member_session, recording_audit()[1]).set("script_limit.free", value)


def test_the_audit_row_has_no_administrator_and_no_target_and_carries_only_the_three_detail_fields(member_session):
    """用真正的稽核寫入函式：`limit.set` 的管理員欄位為空、沒有目標，細節只有鍵與新舊數值。"""
    from app.services.registry import build_runtime_limits

    limits = build_runtime_limits(member_session, clock=FakeClock())
    limits.set("script_reject_streak", 5)
    row = member_session.scalar(select(AdminAuditLog))
    assert row.action == "limit.set" and row.admin_user_id is None and row.target_user_public_id is None
    assert row.detail_json == {"limit_key": "script_reject_streak", "old_value": 3, "new_value": 5}


# ---- 指令 ---------------------------------------------------------------------------------


@pytest.fixture()
def app():
    application = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


def stored(app) -> dict[str, int]:
    with Session(app.extensions["db_engine"]) as s:
        return {row.key: row.int_value for row in s.scalars(select(RuntimeLimit))}


def test_the_commands_are_registered_documented_and_classified_as_manual(app):
    for name in ("set-script-limit", "show-script-limits"):
        assert name in app.cli.commands and name in (cli.__doc__ or "")
        assert name in cli.MANUAL_COMMANDS and name not in cli.SCHEDULED_COMMANDS


def test_the_set_command_changes_the_value_prints_the_old_and_new_and_audits(app):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["set-script-limit", "script_limit.free", "5"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "script_limit.free：3 → 5"
    assert stored(app) == {"script_limit.free": 5}
    with Session(app.extensions["db_engine"]) as s:
        row = s.scalar(select(AdminAuditLog))
    assert row.action == "limit.set" and row.detail_json["new_value"] == 5 and row.admin_user_id is None
    second = runner.invoke(args=["set-script-limit", "script_limit.free", "6"])
    assert second.output.strip() == "script_limit.free：5 → 6"


def test_the_set_command_refuses_unknown_keys_and_out_of_range_values_without_changing_anything(app):
    runner = app.test_cli_runner()
    for args in (["script_limit.gold", "1"], ["script_limit.free", "9999"], ["script_reject_streak", "0"]):
        result = runner.invoke(args=["set-script-limit", *args])
        assert result.exit_code != 0 and ("script_limit" in result.output or "script_reject_streak" in result.output)
    assert stored(app) == {}
    with Session(app.extensions["db_engine"]) as s:
        assert s.scalar(select(AdminAuditLog)) is None


def test_the_set_command_refuses_a_non_integer_value(app):
    result = app.test_cli_runner().invoke(args=["set-script-limit", "script_limit.free", "abc"])
    assert result.exit_code != 0 and stored(app) == {}


def test_the_show_command_lists_the_ten_keys_and_marks_the_fallback_defaults(app):
    runner = app.test_cli_runner()
    runner.invoke(args=["set-script-limit", "script_limit.plus", "12"])
    result = runner.invoke(args=["show-script-limits"])
    assert result.exit_code == 0
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert [line.split()[0] for line in lines] == list(SCRIPT_KEYS)
    by_key = {line.split()[0]: line for line in lines}
    assert "script_limit.plus = 12" in by_key["script_limit.plus"] and "保底預設" not in by_key["script_limit.plus"]
    assert "script_limit.free = 3" in by_key["script_limit.free"] and "保底預設" in by_key["script_limit.free"]


def test_the_command_output_contains_only_keys_and_numbers(app):
    result = app.test_cli_runner().invoke(args=["show-script-limits"])
    assert not re.search(r"[@/\\]|password|token", result.output, re.I)


# ---- 沒有網路端點 -------------------------------------------------------------------------


def test_no_route_can_write_a_limit(app: Flask):
    writers = {"POST", "PUT", "PATCH", "DELETE"}
    for rule in app.url_map.iter_rules():
        if "limit" in rule.rule.lower():
            assert not (rule.methods & writers), f"{rule.rule} 不得提供寫入"


def test_no_controller_calls_the_limit_setter():
    for path in (PROJECT_ROOT / "app" / "controllers").glob("*.py"):
        text = path.read_text(encoding="utf-8-sig")
        assert not re.search(r"get_runtime_limits\(\)\s*\.\s*set\(", text), f"{path.name} 不得修改營運限制"
        assert "LimitError" not in text and "set_value" not in text, path.name


# ---- 遊戲幣組（game-wallet）---------------------------------------------------------------


@pytest.mark.parametrize(
    "key, default, low, high",
    [
        ("coin.turn_cost.standard", 30, 1, 10_000), ("coin.signup_grant", 200, 0, 100_000), ("coin.checkin_grant", 200, 0, 100_000),
        ("coin.free_cap", 1000, 0, 1_000_000), ("coin.hold_timeout_seconds", 180, 60, 1800),
    ],
)
def test_the_wallet_keys_have_their_documented_defaults_and_ranges(key, default, low, high):
    spec = LIMIT_SPECS[key]
    assert (spec.default, spec.minimum, spec.maximum) == (default, low, high)


def test_the_wallet_accessors_read_the_database_every_time_and_fall_back_to_the_defaults(member_session):
    limits = make_limits(member_session)
    assert (limits.coin_turn_cost("standard"), limits.coin_signup_grant(), limits.coin_checkin_grant()) == (30, 200, 200)
    assert (limits.coin_free_cap(), limits.coin_hold_timeout()) == (1000, 180)
    put(member_session, "coin.checkin_grant", 300)
    assert make_limits(member_session).coin_checkin_grant() == 300                       # 下一次讀就是新值
    RuntimeLimitRepository(member_session).set_value("coin.checkin_grant", 100, FakeClock()())
    assert limits.coin_checkin_grant() == 100                                            # 同一個 Session 也不快取


@pytest.mark.parametrize("key, bad", [("coin.turn_cost.standard", 0), ("coin.free_cap", 1_000_001), ("coin.hold_timeout_seconds", 59)])
def test_a_corrupt_wallet_value_uses_the_default_and_logs_only_the_key(member_session, log_lines, key, bad):
    put(member_session, key, bad)
    limits = make_limits(member_session)
    assert limits.get(key) == LIMIT_SPECS[key].default
    assert any(line.get("event") == "limits.invalid" and line["reason"] == key for line in log_lines())


def test_an_unknown_model_key_has_no_price(member_session):
    limits = make_limits(member_session)
    for bad in ("premium", "", None, 3):
        with pytest.raises(LimitError):
            limits.coin_turn_cost(bad)


def test_setting_wallet_values_is_validated_and_audited(member_session):
    calls, audit = recording_audit()
    limits = make_limits(member_session, audit)
    assert limits.set("coin.checkin_grant", 100, group="wallet") == (200, 100)
    assert calls[-1][2] == {"limit_key": "coin.checkin_grant", "old_value": 200, "new_value": 100}
    with pytest.raises(LimitError):
        limits.set("coin.free_cap", 1_000_001, group="wallet")
    with pytest.raises(LimitError):
        limits.set("coin.turn_cost.standard", 0, group="wallet")
    assert limits.coin_checkin_grant() == 100 and limits.coin_free_cap() == 1000


def test_each_group_refuses_the_other_groups_keys_and_names_the_right_command(member_session):
    calls, audit = recording_audit()
    limits = make_limits(member_session, audit)
    with pytest.raises(LimitError, match="set-wallet-setting"):
        limits.set("coin.free_cap", 500, group="script")
    with pytest.raises(LimitError, match="set-script-limit"):
        limits.set("script_limit.free", 4, group="wallet")
    assert calls == [] and limits.coin_free_cap() == 1000 and limits.script_limit("free") == 3


def test_unknown_keys_list_only_the_keys_of_the_chosen_group(member_session):
    with pytest.raises(LimitError) as caught:
        make_limits(member_session, recording_audit()[1]).set("coin.gold", 1, group="wallet")
    assert "coin.free_cap" in str(caught.value) and "script_limit.free" not in str(caught.value)
