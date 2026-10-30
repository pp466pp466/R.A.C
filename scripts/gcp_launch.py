"""Google Cloud 部署腳本（deployment-plan 規格）：預設只印出將執行的指令，`--execute` 才逐步確認並執行。

用法（專案、區域、環境與資料庫執行個體必須明確提供，沒有預設的專案）：
    python scripts/gcp_launch.py plan --project P --region R --env staging --sql-instance cs-sql-staging \\
        --image-tag abc1234 --vars-file staging.vars          # 預覽完整計畫（不執行任何指令）
    python scripts/gcp_launch.py migrate --project ... --execute   # 逐步確認並實際執行 migration 那一組

子命令：plan（全部）、secrets（API、服務帳號、機密）、build（映像）、migrate（備份與 migration）、deploy（服務與驗證）、
schedule（四個排程工作）、alerts（平台告警定義）。順序固定；migration 失敗即停止，不會部署新版程式。

- `--vars-file`：一般設定（KEY=VALUE，UTF-8）；**機密不得放在裡面**（含機密名稱的檔案會被拒絕）。
  機密的值只在 `--execute` 時從標準輸入餵給 `gcloud secrets versions add --data-file=-`（互動時以不回顯的方式輸入，
  或加 `--secrets-from-env` 從呼叫者的環境變數讀取同名變數），不會出現在指令參數、輸出或日誌。
- 本腳本只組出與執行 `gcloud` 指令（參數以清單傳遞，不經 shell）；步驟的內容與順序在 `scripts/launch_plan.py`（純函式，有測試）。
- 腳本沒有對真實的 gcloud 與雲端專案驗證過（C-5a）；第一次實際執行在 C-5b 的預備環境，發現的差異回寫到計畫。
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))   # 讓 `python scripts/gcp_launch.py` 能找到同目錄的 launch_plan

import launch_plan as plan  # noqa: E402

EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_DECLINED = 3
HTTP_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[tuple[str, ...], "str | None"], RunResult]


def subprocess_runner(argv: tuple[str, ...], stdin_text: str | None) -> RunResult:
    """真正執行外部指令：參數以清單傳遞（不經 shell）；標準輸入只用於機密的值。"""
    completed = subprocess.run(list(argv), input=stdin_text, capture_output=True, text=True, encoding="utf-8", check=False)  # noqa: S603
    return RunResult(completed.returncode, completed.stdout, completed.stderr)


def interactive_confirm(prompt: str) -> bool:
    return input(prompt).strip().lower() in ("y", "yes")


def http_status(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_SECONDS) as response:  # noqa: S310 - 網址來自 gcloud 回報的服務網址
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0


def make_secret_reader(from_env: bool) -> Callable[[str], str]:
    def read(spec: str) -> str:
        name = spec.split(":", 1)[1]
        if from_env:
            value = os.environ.get(name, "")
            if not value:
                raise RuntimeError(f"環境變數 {name} 沒有設定（--secrets-from-env）")
            return value
        value = getpass.getpass(f"請輸入 {name} 的值（不會顯示）：")
        if not value:
            raise RuntimeError(f"{name} 的值是空的")
        return value

    return read


def read_vars_file(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    from dotenv import dotenv_values

    values = dotenv_values(path, encoding="utf-8-sig")
    return {key: value for key, value in values.items() if value is not None}


def show_step(index: int, total: int, step: plan.Step, out: Callable[[str], None]) -> None:
    out(f"[{index}/{total}] {step.title}")
    if step.http_paths:
        out("    檢查 " + "、".join(step.http_paths) + f"（網址取自步驟「{step.url_from}」）")
        return
    if step.exists_argv:
        out("    先檢查是否已存在：" + shlex.join(step.exists_argv))
        out("    不存在時執行：" + shlex.join(step.argv))
        out("    已存在時：" + ("改執行 " + shlex.join(step.update_argv) if step.update_argv else "略過"))
    else:
        out("    " + shlex.join(step.argv))
    if step.stdin_from:
        out(f"    （值從標準輸入讀取：{step.stdin_from}；不出現在指令參數）")


TOKEN = re.compile(r"\{\{([a-z0-9-]+)(?:\|(host|last))?\}\}")


def substitute(argv: tuple[str, ...], captured: dict[str, str]) -> tuple[str, ...]:
    """把 `{{名稱}}`（先前步驟擷取的資源名稱）代入；`|host` 取網址的主機、`|last` 取資源路徑的最後一段。找不到就失敗。"""
    def resolve(match: re.Match[str]) -> str:
        name, modifier = match.group(1), match.group(2)
        if name not in captured or not captured[name]:
            raise RuntimeError(f"找不到先前步驟擷取的「{name}」（相依的步驟沒有執行或沒有輸出）")
        value = captured[name]
        if modifier == "host":
            return urllib.parse.urlparse(value).hostname or value
        if modifier == "last":
            return value.rstrip("/").rsplit("/", 1)[-1]
        return value

    return tuple(TOKEN.sub(resolve, part) for part in argv)


def run_steps(
    steps: list[plan.Step], *, execute: bool, runner: Runner, confirm: Callable[[str], bool],
    secret_reader: Callable[[str], str], status_of: Callable[[str], int], out: Callable[[str], None],
) -> int:
    if not execute:
        for index, step in enumerate(steps, 1):
            show_step(index, len(steps), step, out)
        out("")
        out("這是預覽：沒有執行任何指令。加上 --execute 才會逐步確認並執行。")
        return 0

    captured: dict[str, str] = {}
    for index, step in enumerate(steps, 1):
        show_step(index, len(steps), step, out)
        if not confirm("要執行這一步嗎？[y/N] "):
            out("已停止：不執行這一步與之後的步驟。")
            return EXIT_DECLINED
        if step.http_paths:
            base = captured.get(step.url_from or "", "").rstrip("/")
            if not base:
                out(f"失敗：沒有取得服務網址（步驟「{step.url_from}」）。")
                return EXIT_FAILED
            for path in step.http_paths:
                status = status_of(base + path)
                out(f"    {path} → {status}")
                if status != 200:
                    out(f"失敗：{path} 沒有回 200。")
                    return EXIT_FAILED
            continue
        argv = step.argv
        if step.exists_argv:
            found = runner(step.exists_argv, None)
            exists = found.returncode == 0 and (bool(found.stdout.strip()) if step.exists_nonempty else True)
            if exists:
                if step.exists_capture:
                    captured[step.exists_capture] = found.stdout.strip()
                if not step.update_argv:
                    out("    已存在，略過。")
                    continue
                argv = step.update_argv
        try:
            argv = substitute(argv, captured)
            stdin_text = secret_reader(step.stdin_from) if step.stdin_from else None
        except RuntimeError as error:
            out(f"失敗：{error}")
            return EXIT_FAILED
        result = runner(argv, stdin_text)
        if result.returncode != 0:
            out(f"失敗：指令以代碼 {result.returncode} 結束，停止後續步驟。")
            if result.stderr.strip():
                out("    " + result.stderr.strip().splitlines()[-1])
            return EXIT_FAILED
        if step.capture:
            captured[step.capture] = result.stdout.strip()
    out("全部完成。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Google Cloud 部署腳本（預設只預覽，--execute 才執行）。")
    parser.add_argument("group", choices=("plan", *plan.GROUPS), help="plan = 全部；其餘為單一步驟組")
    parser.add_argument("--project", required=True, help="Google Cloud 專案識別碼（沒有預設值）")
    parser.add_argument("--region", required=True, help="區域，例如 asia-east1")
    parser.add_argument("--env", required=True, choices=plan.ENVIRONMENTS, help="環境名稱，會出現在所有資源名稱")
    parser.add_argument("--sql-instance", required=True, help="既有的 Cloud SQL 執行個體名稱（必須含環境名稱；本腳本不建立執行個體）")
    parser.add_argument("--image-tag", help="映像標籤；省略時取目前 git 的短 commit")
    parser.add_argument("--vars-file", help="一般設定檔（KEY=VALUE；不得含機密）")
    parser.add_argument("--with-secret", action="append", default=[], metavar="NAME", help="除必要機密外要建立與注入的機密（例如 OPENROUTER_API_KEY）")
    parser.add_argument("--rotate", action="append", default=[], metavar="NAME", help="輪替：secrets 只為這些機密寫入新版本（可重複），不做其他準備步驟")
    parser.add_argument("--network", help="Direct VPC egress 的網路（Cloud SQL for SQL Server 走私有 IP）")
    parser.add_argument("--subnet", help="Direct VPC egress 的子網路（與 --network 一起提供才生效）")
    parser.add_argument("--min-instances", type=int, default=0)
    parser.add_argument("--max-instances", type=int, default=4)
    parser.add_argument("--notify-email", action="append", default=[], metavar="ADDRESS", help="平台告警的收件人（alerts 必填，沒有預設值）")
    parser.add_argument("--execute", action="store_true", help="實際執行（每一步執行前都會要求確認）；沒有這個參數只預覽")
    parser.add_argument("--secrets-from-env", action="store_true", help="建立機密時，從呼叫者的環境變數讀取同名變數，而不是互動輸入")
    return parser


def git_short_commit() -> str:
    completed = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=plan.PROJECT_ROOT, check=False)  # noqa: S607
    return completed.stdout.strip() if completed.returncode == 0 else ""


def main(
    argv: list[str] | None = None, *, runner: Runner = subprocess_runner, confirm: Callable[[str], bool] = interactive_confirm,
    secret_reader: Callable[[str], str] | None = None, status_of: Callable[[str], int] = http_status,
    out: Callable[[str], None] = print, image_tag_default: Callable[[], str] = git_short_commit,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        target = plan.Target(
            project=args.project, region=args.region, env=args.env, sql_instance=args.sql_instance,
            image_tag=args.image_tag or image_tag_default(), network=args.network, subnet=args.subnet,
            min_instances=args.min_instances, max_instances=args.max_instances, with_secrets=tuple(args.with_secret),
            notify_emails=tuple(args.notify_email), rotate=tuple(args.rotate),
        )
        groups = plan.GROUPS if args.group == "plan" else (args.group,)
        steps = plan.build_plan(target, read_vars_file(args.vars_file), groups=groups)
    except plan.PlanError as error:
        print(f"錯誤：{error}", file=sys.stderr)
        return EXIT_USAGE
    return run_steps(
        steps, execute=args.execute, runner=runner, confirm=confirm,
        secret_reader=secret_reader or make_secret_reader(args.secrets_from_env), status_of=status_of, out=out,
    )


if __name__ == "__main__":
    sys.exit(main())
