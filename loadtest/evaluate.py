"""通過標準由腳本判定（load-testing 規格「通過標準由腳本判定」，建議標準見 report.md D-24）。

    python loadtest/evaluate.py --csv-prefix loadtest/result [--log app.log] [--cpu-percent 55]
        [--environment local|local-container|cloud] [--setting WEB_CONCURRENCY=2 ...] [--stairs start=20,step=20,steps=5,seconds=120]

讀取 Locust 的 `--csv` 輸出（`<prefix>_stats.csv`）：
- 對話訊息請求的 p95 **小於 8 秒**（端到端，含模型；沒有對話請求時改看彙總列）；
- 錯誤率（失敗數 / 請求數）**小於 1%**；
- 日誌檔中**沒有資料庫鎖定錯誤**（SQLite 的 `database is locked`、SQL Server 的死結 1205 與鎖逾時 1222，以及應用程式日誌裡的
  `"error_type":"OperationalError"`——日誌不記例外訊息，這是真實日誌裡唯一看得到的資料庫層錯誤；沒有提供日誌檔則標為未驗證）；
- CPU 使用率**低於 70%**：由平台量測，操作者以 `--cpu-percent` 填入；**未提供就標為「未驗證」，整體不判為通過**。
輸出只有彙總數字與各項判定，沒有任何使用者資料。結束代碼：0 全部通過、1 有項目未通過、2 沒有未通過但有未驗證。

每一輪的記錄（staging-deployment：階梯式加壓與留存）：`--environment` 標明執行環境，輸出會附上「容量依據」——**只有 `cloud`（雲端）的結果
可以作為正式環境的容量依據**，`local`（本機）與 `local-container`（本機容器）一律標為「不是容量依據」，沒有標明環境也不當作容量依據；
`--setting` 記下這一輪的伺服器與資源設定（名稱限於 `SETTING_NAMES`，值只能是短的英數字），`--stairs` 記下階梯的負載參數；
最後印出一列可直接貼進 README 壓測紀錄表的「記錄列」。
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:                   # 以 `python loadtest/evaluate.py` 執行時，專案根目錄不在匯入路徑上
    sys.path.insert(0, str(ROOT))

from loadtest.shapes import StairConfigError, parse_stairs  # noqa: E402 - 需要先把專案根目錄放進 sys.path

P95_LIMIT_SECONDS = 8.0
ERROR_RATE_LIMIT = 0.01
CPU_LIMIT_PERCENT = 70.0
CHAT_NAME_PREFIX = "POST /api/cs/conversations/[id]/messages"
LOCK_PATTERNS = (
    re.compile(r"database is locked|SQLITE_BUSY", re.I),
    re.compile(r"deadlock|Msg 1205|error 1205|\(1205\)|死結", re.I),                # SQL Server 死結（1205）
    re.compile(r"lock request time out|Msg 1222|error 1222|\(1222\)", re.I),       # SQL Server 鎖逾時（1222）
    # 應用程式日誌依規範（AGENTS.md 第 11 節）只記錯誤類型、不記例外訊息，所以上面三條在真實的日誌裡永遠比對不到。
    # 資料庫層的 OperationalError（SQLite 的鎖定逾時、SQL Server 的死結、鎖逾時與連線中斷都是這個類型）是唯一看得到的線索，一律計入
    # （staging-deployment 的本機容器預演就是靠它抓到：16 條執行緒的那一輪有 6 次 HTTP 500，原本卻回報「0 筆」）
    re.compile(r'"error_type"\s*:\s*"OperationalError"'),
)

PASS, FAIL, UNVERIFIED = "通過", "未通過", "未驗證"

ENVIRONMENTS = {"local": "本機", "local-container": "本機容器", "cloud": "雲端"}
CAPACITY_BASIS_ENVIRONMENTS = ("cloud",)              # 只有雲端的結果可以作為正式環境的容量依據
SETTING_NAMES = (
    "WEB_CONCURRENCY", "GUNICORN_THREADS", "CPU", "MEMORY", "CONCURRENCY", "MIN_INSTANCES", "MAX_INSTANCES", "USERS", "DURATION_SECONDS",
)
_SETTING_VALUE = re.compile(r"[0-9A-Za-z._-]{1,16}")


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


def capacity_basis(environment: str | None) -> tuple[bool, str]:
    """這一輪的數字可不可以作為正式環境的容量依據；回傳（可以嗎，說明）。只有雲端可以。"""
    if environment in CAPACITY_BASIS_ENVIRONMENTS:
        return True, "是（雲端環境的結果）"
    if environment in ENVIRONMENTS:
        return False, f"否（不是容量依據：{ENVIRONMENTS[environment]}的結果不代表雲端的容量）"
    return False, "否（沒有標明環境，不當作容量依據）"


def parse_settings(pairs: list[str]) -> dict[str, str]:
    """`--setting NAME=VALUE`：名稱限於 `SETTING_NAMES`，值只能是短的英數字（不回顯不合法的值）。"""
    settings: dict[str, str] = {}
    for pair in pairs:
        name, separator, value = pair.partition("=")
        if not separator or name not in SETTING_NAMES:
            raise ValueError("--setting 的名稱必須是：" + "、".join(SETTING_NAMES) + "，格式為 名稱=值")
        if not _SETTING_VALUE.fullmatch(value):
            raise ValueError(f"--setting {name} 的值只能是最長 16 字的英數字、點、底線與連字號")
        settings[name] = value
    return settings


def record_row(environment: str | None, settings: dict[str, str], load: str, verdict: Verdict) -> str:
    """可直接貼進 README 壓測紀錄表的一列：環境、設定、負載、各項數字與判定、是否為容量依據。"""
    numbers = "；".join(
        f"{item.name} {UNVERIFIED}" if item.verdict == UNVERIFIED else f"{item.name} {item.detail.split('（')[0]}（{item.verdict}）"
        for item in verdict.items
    )
    overall = "通過" if verdict.passed else ("未通過" if verdict.exit_code == 1 else "有未驗證項目")
    cells = [
        ENVIRONMENTS.get(environment or "", "未標明"),
        "；".join(f"{name}={value}" for name, value in settings.items()) or "未提供",
        load,
        numbers,
        overall,
        "是" if capacity_basis(environment)[0] else "否",
    ]
    return "| " + " | ".join(cells) + " |"


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(description="依建議的通過標準判定 Locust 的結果。")
    parser.add_argument("--csv-prefix", required=True, help="Locust --csv 的前綴（讀取 <前綴>_stats.csv）")
    parser.add_argument("--log", help="應用程式日誌檔，用來找資料庫鎖定錯誤")
    parser.add_argument("--cpu-percent", type=float, help="平台量測的 CPU 使用率（未提供則標為未驗證）")
    parser.add_argument("--environment", choices=tuple(ENVIRONMENTS), help="這一輪的執行環境；只有 cloud 可以作為容量依據")
    parser.add_argument("--setting", action="append", default=[], metavar="NAME=VALUE", help="這一輪的伺服器與資源設定（可重複）")
    parser.add_argument("--stairs", help="這一輪的階梯設定（與 LOADTEST_STAIRS 相同的格式），用來記錄負載參數")
    args = parser.parse_args(argv)
    try:
        settings = parse_settings(args.setting)
        load = parse_stairs(args.stairs).describe() if args.stairs else "未提供"
    except (ValueError, StairConfigError) as error:
        out(f"失敗：{error}")
        return 1
    try:
        stats = read_stats(Path(f"{args.csv_prefix}_stats.csv"))
        log_text = Path(args.log).read_text(encoding="utf-8-sig", errors="replace") if args.log else None
    except (OSError, KeyError, csv.Error):
        out("失敗：無法讀取 Locust 的結果檔或日誌檔。")
        return 1
    verdict = evaluate(stats, log_text=log_text, cpu_percent=args.cpu_percent)
    for item in verdict.items:
        out(f"{item.verdict}  {item.name}：{item.detail}")
    out(f"環境：{ENVIRONMENTS.get(args.environment or '', '未標明')}")
    out(f"容量依據：{capacity_basis(args.environment)[1]}")
    out("設定：" + ("；".join(f"{name}={value}" for name, value in settings.items()) or "未提供"))
    out(f"負載：{load}")
    out("記錄列：" + record_row(args.environment, settings, load, verdict))
    out("整體：通過" if verdict.passed else ("整體：未通過" if verdict.exit_code == 1 else "整體：尚有未驗證的項目，不判為通過"))
    return verdict.exit_code


if __name__ == "__main__":
    sys.exit(main())
