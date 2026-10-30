"""通過標準由腳本判定（load-testing 規格「通過標準由腳本判定」，建議標準見 report.md D-24）。

    python loadtest/evaluate.py --csv-prefix loadtest/result [--log app.log] [--cpu-percent 55]

讀取 Locust 的 `--csv` 輸出（`<prefix>_stats.csv`）：
- 對話訊息請求的 p95 **小於 8 秒**（端到端，含模型；沒有對話請求時改看彙總列）；
- 錯誤率（失敗數 / 請求數）**小於 1%**；
- 日誌檔中**沒有資料庫鎖定錯誤**（SQLite 的 `database is locked`、SQL Server 的死結 1205 與鎖逾時 1222；沒有提供日誌檔則標為未驗證）；
- CPU 使用率**低於 70%**：由平台量測，操作者以 `--cpu-percent` 填入；**未提供就標為「未驗證」，整體不判為通過**。
輸出只有彙總數字與各項判定，沒有任何使用者資料。結束代碼：0 全部通過、1 有項目未通過、2 沒有未通過但有未驗證。
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

P95_LIMIT_SECONDS = 8.0
ERROR_RATE_LIMIT = 0.01
CPU_LIMIT_PERCENT = 70.0
CHAT_NAME_PREFIX = "POST /api/cs/conversations/[id]/messages"
LOCK_PATTERNS = (
    re.compile(r"database is locked|SQLITE_BUSY", re.I),
    re.compile(r"deadlock|Msg 1205|error 1205|\(1205\)|死結", re.I),                # SQL Server 死結（1205）
    re.compile(r"lock request time out|Msg 1222|error 1222|\(1222\)", re.I),       # SQL Server 鎖逾時（1222）
)

PASS, FAIL, UNVERIFIED = "通過", "未通過", "未驗證"


@dataclass(frozen=True)
class Item:
    name: str
    verdict: str
    detail: str


@dataclass(frozen=True)
class Verdict:
    items: tuple[Item, ...]

    @property
    def passed(self) -> bool:
        return all(item.verdict == PASS for item in self.items)

    @property
    def exit_code(self) -> int:
        if any(item.verdict == FAIL for item in self.items):
            return 1
        return 0 if self.passed else 2


def read_stats(path: Path) -> dict[str, dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {row["Name"]: row for row in csv.DictReader(handle)}


def count_lock_errors(text: str) -> int:
    return sum(1 for line in text.splitlines() if any(pattern.search(line) for pattern in LOCK_PATTERNS))


def evaluate(
    stats: dict[str, dict[str, str]], *, log_text: str | None = None, cpu_percent: float | None = None,
) -> Verdict:
    items: list[Item] = []
    chat = next((row for name, row in stats.items() if name.startswith(CHAT_NAME_PREFIX)), None) or stats.get("Aggregated")
    if chat is None:
        items.append(Item("端到端 p95", UNVERIFIED, "結果檔沒有可用的資料列"))
    else:
        p95 = float(chat.get("95%", chat.get("95%ile", "nan"))) / 1000.0
        items.append(Item("端到端 p95", PASS if p95 < P95_LIMIT_SECONDS else FAIL, f"{p95:.2f} 秒（標準：小於 {P95_LIMIT_SECONDS:g} 秒）"))
    total = stats.get("Aggregated")
    if total is None or not int(total.get("Request Count", "0")):
        items.append(Item("錯誤率", UNVERIFIED, "結果檔沒有請求紀錄"))
    else:
        requests, failures = int(total["Request Count"]), int(total["Failure Count"])
        rate = failures / requests
        items.append(Item("錯誤率", PASS if rate < ERROR_RATE_LIMIT else FAIL, f"{rate * 100:.2f}%（{failures} / {requests}；標準：小於 {ERROR_RATE_LIMIT * 100:g}%）"))
    if log_text is None:
        items.append(Item("資料庫鎖定錯誤", UNVERIFIED, "沒有提供應用程式日誌（--log）"))
    else:
        locks = count_lock_errors(log_text)
        items.append(Item("資料庫鎖定錯誤", PASS if locks == 0 else FAIL, f"{locks} 筆"))
    if cpu_percent is None:
        items.append(Item("CPU 使用率", UNVERIFIED, f"由平台量測後以 --cpu-percent 提供（標準：低於 {CPU_LIMIT_PERCENT:g}%）"))
    else:
        items.append(Item("CPU 使用率", PASS if cpu_percent < CPU_LIMIT_PERCENT else FAIL, f"{cpu_percent:g}%（標準：低於 {CPU_LIMIT_PERCENT:g}%）"))
    return Verdict(tuple(items))


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(description="依建議的通過標準判定 Locust 的結果。")
    parser.add_argument("--csv-prefix", required=True, help="Locust --csv 的前綴（讀取 <前綴>_stats.csv）")
    parser.add_argument("--log", help="應用程式日誌檔，用來找資料庫鎖定錯誤")
    parser.add_argument("--cpu-percent", type=float, help="平台量測的 CPU 使用率（未提供則標為未驗證）")
    args = parser.parse_args(argv)
    try:
        stats = read_stats(Path(f"{args.csv_prefix}_stats.csv"))
        log_text = Path(args.log).read_text(encoding="utf-8-sig", errors="replace") if args.log else None
    except (OSError, KeyError, csv.Error):
        out("失敗：無法讀取 Locust 的結果檔或日誌檔。")
        return 1
    verdict = evaluate(stats, log_text=log_text, cpu_percent=args.cpu_percent)
    for item in verdict.items:
        out(f"{item.verdict}  {item.name}：{item.detail}")
    out("整體：通過" if verdict.passed else ("整體：未通過" if verdict.exit_code == 1 else "整體：尚有未驗證的項目，不判為通過"))
    return verdict.exit_code


if __name__ == "__main__":
    sys.exit(main())
