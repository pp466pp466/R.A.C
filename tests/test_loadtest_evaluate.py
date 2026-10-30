"""load-testing 規格「通過標準由腳本判定」：p95 < 8 秒、錯誤率 < 1%、無鎖定錯誤、CPU < 70%（未提供則未驗證、整體不通過）。"""
import csv

import pytest

from loadtest import evaluate

CHAT = "POST /api/cs/conversations/[id]/messages"


def stats(*, p95_ms=3000, requests=1000, failures=2, chat_p95_ms=None):
    rows = {
        "Aggregated": {"Name": "Aggregated", "Request Count": str(requests), "Failure Count": str(failures), "95%": str(p95_ms)},
        CHAT: {"Name": CHAT, "Request Count": "500", "Failure Count": "0", "95%": str(chat_p95_ms if chat_p95_ms is not None else p95_ms)},
    }
    return rows


def verdicts(result) -> dict[str, str]:
    return {item.name: item.verdict for item in result.items}


def test_everything_within_the_standards_passes():
    result = evaluate.evaluate(stats(p95_ms=3000, requests=1000, failures=2), log_text="一切正常\n", cpu_percent=55)
    assert verdicts(result) == {"端到端 p95": "通過", "錯誤率": "通過", "資料庫鎖定錯誤": "通過", "CPU 使用率": "通過"}
    assert result.passed and result.exit_code == 0


@pytest.mark.parametrize(("p95_ms", "expected"), [(7999, "通過"), (8000, "未通過"), (9000, "未通過")])
def test_the_p95_must_be_below_eight_seconds_and_the_chat_row_is_the_one_that_counts(p95_ms, expected):
    result = evaluate.evaluate(stats(p95_ms=100, chat_p95_ms=p95_ms), log_text="", cpu_percent=10)
    assert verdicts(result)["端到端 p95"] == expected
    if expected == "未通過":
        assert not result.passed and result.exit_code == 1
        assert "8" in next(item.detail for item in result.items if item.name == "端到端 p95")


def test_without_a_chat_row_the_aggregate_row_is_used():
    only_aggregate = {"Aggregated": stats(p95_ms=9500)["Aggregated"]}
    assert verdicts(evaluate.evaluate(only_aggregate, log_text="", cpu_percent=10))["端到端 p95"] == "未通過"


@pytest.mark.parametrize(("requests", "failures", "expected"), [(1000, 9, "通過"), (1000, 10, "未通過"), (1000, 50, "未通過"), (1000, 0, "通過")])
def test_the_error_rate_must_be_below_one_percent(requests, failures, expected):
    result = evaluate.evaluate(stats(requests=requests, failures=failures), log_text="", cpu_percent=10)
    assert verdicts(result)["錯誤率"] == expected


@pytest.mark.parametrize("line", [
    "sqlite3.OperationalError: database is locked", "(pyodbc.Error) Msg 1205 Transaction was deadlocked", "Lock request time out period exceeded. (1222)",
    "SQLITE_BUSY", "死結",
])
def test_a_database_lock_error_in_the_log_fails_the_run(line):
    result = evaluate.evaluate(stats(), log_text=f"ok\n{line}\nok\n", cpu_percent=10)
    assert verdicts(result)["資料庫鎖定錯誤"] == "未通過" and result.exit_code == 1


def test_latency_numbers_that_look_like_error_codes_are_not_mistaken_for_lock_errors():
    result = evaluate.evaluate(stats(), log_text='{"latency_ms": 1205}\n{"latency_ms": 1222}\n', cpu_percent=10)
    assert verdicts(result)["資料庫鎖定錯誤"] == "通過"


def test_without_a_cpu_figure_the_item_is_unverified_and_the_whole_is_not_a_pass():
    result = evaluate.evaluate(stats(), log_text="", cpu_percent=None)
    assert verdicts(result)["CPU 使用率"] == "未驗證"
    assert not result.passed and result.exit_code == 2, "全部達標但 CPU 未提供：不判為通過，但也不是失敗"


@pytest.mark.parametrize(("cpu", "expected"), [(69.9, "通過"), (70, "未通過"), (95, "未通過")])
def test_cpu_must_be_below_seventy_percent_when_given(cpu, expected):
    assert verdicts(evaluate.evaluate(stats(), log_text="", cpu_percent=cpu))["CPU 使用率"] == expected


def test_a_missing_log_is_unverified_not_a_silent_pass():
    result = evaluate.evaluate(stats(), log_text=None, cpu_percent=10)
    assert verdicts(result)["資料庫鎖定錯誤"] == "未驗證" and not result.passed


def test_a_failure_wins_over_an_unverified_item():
    assert evaluate.evaluate(stats(failures=100), log_text=None, cpu_percent=None).exit_code == 1


def write_csv(tmp_path, rows):
    path = tmp_path / "run_stats.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["Type", "Name", "Request Count", "Failure Count", "95%"])
        writer.writeheader()
        for row in rows.values():
            writer.writerow({"Type": "POST", **row})
    return str(tmp_path / "run")


def test_the_command_reads_locusts_csv_and_prints_only_aggregate_numbers(tmp_path):
    prefix = write_csv(tmp_path, stats())
    log = tmp_path / "app.log"
    log.write_text("secret.person@example.com 的訊息\n", encoding="utf-8")
    messages: list[str] = []
    assert evaluate.main(["--csv-prefix", prefix, "--log", str(log), "--cpu-percent", "40"], out=messages.append) == 0
    text = "\n".join(messages)
    assert "整體：通過" in text and "secret.person" not in text and "訊息" not in text and str(log) not in text


def test_the_command_exits_nonzero_with_a_clear_message_for_a_missing_file(tmp_path):
    messages: list[str] = []
    assert evaluate.main(["--csv-prefix", str(tmp_path / "missing-secret-dir" / "run")], out=messages.append) == 1
    assert "missing-secret-dir" not in " ".join(messages)


def test_the_command_exits_2_when_only_cpu_is_missing(tmp_path):
    prefix = write_csv(tmp_path, stats())
    log = tmp_path / "app.log"
    log.write_text("ok\n", encoding="utf-8")
    messages: list[str] = []
    assert evaluate.main(["--csv-prefix", prefix, "--log", str(log)], out=messages.append) == 2
    assert any("未驗證" in line for line in messages)
