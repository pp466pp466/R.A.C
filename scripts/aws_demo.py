"""AWS 展示環境的執行器（openspec/changes/aws-demo-environment 的 design D2、D6、D14 與四份規格）。

步驟的內容與順序都在 `scripts/aws_demo_plan.py`（純函式，有測試）；這個腳本只負責「預覽、逐步確認、執行、驗證」。
**預設只預覽：不呼叫任何外部指令**。加上 `--execute` 才會逐步執行，每一步執行前都會列出並等待你確認，拒絕就停止。

用法（帳號編號必須明確提供，沒有預設值；區域只接受 ap-southeast-2，其他區域在呼叫任何指令之前就被拒絕）：

    python scripts/aws_demo.py provision --account-id 123456789012 --notify-email ops@example.com
        # 預覽完整的建立計畫；`--group network` 只看某一組（可重複）；`--execute` 逐步確認並執行
    python scripts/aws_demo.py provision ... --accept-cost --execute
        # 沒有 --accept-cost 時，建立 RDS、EC2、Elastic IP 的步驟只留說明、不輸出指令，執行到那一步會停止
    python scripts/aws_demo.py provision ... --vars-file aws-demo.vars --group host-init --execute
        # 主機初始化（交換檔、docker、RDS 的 CA 套件、主機檔案、單元啟用、停用 sshd）；放置檔案的那一步需要 --vars-file
    python scripts/aws_demo.py provision ... --only create-host-sg --yes
        # 只執行指定的單一步驟，不問確認（--yes 必須和 --only 一起用，沒有「整份計畫一律同意」）
    python scripts/aws_demo.py deploy --account-id ... --tag abc1234 --execute      # 快照 -> 建置 -> 遷移 -> 重啟 -> 驗證
    python scripts/aws_demo.py deploy --account-id ... --tag abc1234 --group build --execute   # 第一次部署分組執行
    python scripts/aws_demo.py rollback --account-id ... --tag abc1234 --execute    # 只把兩個池換回舊標籤，不動資料庫
    python scripts/aws_demo.py teardown --account-id ... --no-final-snapshot --execute
        # 拆除：必須明確選 --final-snapshot 或 --no-final-snapshot；先列出部署前快照並要求你回覆要保留的例外
    python scripts/aws_demo.py duckdns --account-id ... --execute                   # 把網域指向 Elastic IP（權杖來自環境變數 DUCKDNS_TOKEN）

機密：
- 值來自 `--secrets-file`（KEY=VALUE，UTF-8，不做變數插值；檔名建議 `aws-demo.secrets`，`*.secrets` 已被 .gitignore 忽略）
  或由腳本隨機產生（`SECRET_KEY`、資料庫兩個密碼）；`DATABASE_URL` 由腳本用 RDS 端點與專屬登入組成。
- 值只經暫存檔（權限限本人、用後一定刪除）交給 `aws ssm put-parameter --cli-input-json file://…`，不進命令列、輸出與日誌；
  所有輸出另外經過遮蔽（出現已知的機密值就以 *** 取代）。

`provision` 的各組與 `host-init` 已在雲端批次（tasks 4.2 至 4.5）對真實 AWS 執行並回寫差異；`deploy`、`rollback`、`teardown` 與 `duckdns`
尚未對真實 AWS 執行，之後的批次發現的差異同樣先回寫到計畫模組與測試。
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import secrets as randomness
import shlex
import string
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))   # 讓 `python scripts/aws_demo.py` 找得到同目錄的 aws_demo_plan

import aws_demo_plan as plan  # noqa: E402

EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_DECLINED = 3
EXIT_BLOCKED = 4

COMMAND_TIMEOUT_SECONDS = 900             # 單一外部指令的上限（`aws s3 rb --force` 與上傳都在這之內）
HTTP_TIMEOUT_SECONDS = 15
NO_VALUE = ("", "None")                   # AWS CLI 的文字輸出把 null 印成 None；兩者都視為「沒有值」
MIN_REDACT_LENGTH = 6                     # 太短的字串不當作機密遮蔽（會把一般輸出弄亂）
SNAPSHOT_REPLY_ATTEMPTS = 3               # 例外清單最多問幾次：沉默或打錯名稱都算一次，用完就停止
NO_EXCEPTIONS_REPLY = "沒有例外"          # 明確表示「沒有要保留的部署前快照」的唯一說法；沉默與 Enter 不算
SECRETS_FILE_HINT = "aws-demo.secrets"
VARS_FILE_HINT = "aws-demo.vars"
DISPLAY_CAPTURES = ("remaining-snapshots",)      # 擷取後只印出給人看、不被其他步驟使用的名稱；輸出可以是空的
SNAPSHOT_STEP_PREFIX = "td-delete-snapshot-"    # 拆除計畫裡每個部署前快照一個刪除步驟：td-delete-snapshot-<名稱>

DUCKDNS_TOKEN_ENV = "DUCKDNS_TOKEN"
DUCKDNS_SUFFIX = ".duckdns.org"
DUCKDNS_UPDATE_URL = "https://www.duckdns.org/update"
DUCKDNS_LABEL_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
ENDPOINT_PATTERN = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?")   # RDS 端點主機名稱；組進連線字串之前先檢查
DB_DRIVER = "ODBC Driver 18 for SQL Server"

PASSWORD_LENGTH = 40
PASSWORD_ALPHABET = string.ascii_letters + string.digits    # 不含 / " @ 空白與連線字串的分隔符號；SQL Server 的複雜度規則另由 generate_password 保證
PASSWORD_SYMBOLS = "-_"
SECRET_KEY_BYTES = 32                                       # 64 個十六進位字元（design D6：至少 64）

_TEMPLATE_TOKEN = re.compile(r"\{\{(?:secret:([A-Z][A-Z0-9_]*)|([a-z][a-z0-9_-]*))\}\}")


class DemoError(RuntimeError):
    """執行器遇到的失敗；訊息只含名稱、狀態與錯誤類別，不含機密值或含權杖的網址。"""

    def __init__(self, message: str, code: int = EXIT_FAILED) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[tuple[str, ...]], RunResult]


# ---- 外部世界（測試時注入替身）-----------------------------------------------------------------------------


def subprocess_runner(argv: tuple[str, ...]) -> RunResult:
    """真正執行外部指令：參數以清單傳遞（不經 shell）。子行程不會拿到 DuckDNS 權杖；
    `AWS_CLI_FILE_ENCODING=UTF-8` 讓 `file://` 的 JSON 在繁體中文 Windows 上也以 UTF-8 讀取（預設是系統地區編碼 cp950）。"""
    env = {key: value for key, value in os.environ.items() if key != DUCKDNS_TOKEN_ENV}
    env.update({"AWS_CLI_FILE_ENCODING": "UTF-8", "AWS_PAGER": ""})
    try:
        done = subprocess.run(  # noqa: S603 - 參數是清單，不經 shell
            list(argv), capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
            cwd=str(plan.PROJECT_ROOT), env=env, timeout=COMMAND_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return RunResult(124, "", f"指令逾時（超過 {COMMAND_TIMEOUT_SECONDS} 秒）")
    except FileNotFoundError:
        return RunResult(127, "", f"找不到指令：{argv[0]}")
    return RunResult(done.returncode, done.stdout, done.stderr)


def interactive_confirm(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def interactive_ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


def default_fetch(url: str) -> tuple[int, str]:
    """對 DuckDNS 發出 GET；失敗時只回報錯誤類別。網址含權杖，所以不得出現在任何例外訊息或例外鏈裡。"""
    kind = ""
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_SECONDS) as response:  # noqa: S310 - 固定的 https 網址
            return response.status, response.read(256).decode("ascii", errors="replace")
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        return status, ""
    except Exception as error:  # noqa: BLE001 - 任何例外都只取類別名稱
        kind = type(error).__name__
    raise DemoError(f"DuckDNS 連線失敗（錯誤類型：{kind}）")           # 在 except 之外丟出：沒有 __context__，不帶含權杖的網址


def taipei_today() -> date:
    """台灣時間的當日（固定 UTC+8，台灣沒有日光節約時間；不依賴系統是否安裝時區資料庫）。"""
    return datetime.now(timezone(timedelta(hours=8))).date()


def git_short_commit() -> str:
    completed = subprocess.run(  # noqa: S603
        ["git", "rev-parse", "--short=7", "HEAD"], capture_output=True, text=True, cwd=str(plan.PROJECT_ROOT), check=False)  # noqa: S607
    return completed.stdout.strip() if completed.returncode == 0 else ""


# ---- 遮蔽與機密 --------------------------------------------------------------------------------------------


class Redactor:
    """所有輸出的最後一道防線：出現已知的機密值（含 URL 編碼後的形式）就以 *** 取代。"""

    def __init__(self) -> None:
        self._values: set[str] = set()

    def add(self, value: str | None) -> None:
        if value and len(value) >= MIN_REDACT_LENGTH:
            self._values.add(value)

    def __call__(self, text: str) -> str:
        for value in sorted(self._values, key=len, reverse=True):
            text = text.replace(value, "***")
        return text


def generate_secret_key() -> str:
    return randomness.token_hex(SECRET_KEY_BYTES)


def generate_password(length: int = PASSWORD_LENGTH) -> str:
    """隨機密碼：只含英數與 `-_`（不含 RDS 禁用的 / " @ 與空白，也不會破壞 URL 與連線字串），
    保證有大寫、小寫、數字與符號各至少一個（SQL Server 的密碼複雜度），洗牌後輸出。"""
    if length < 12:
        raise DemoError("密碼長度至少 12 個字元")
    chars = [randomness.choice(string.ascii_uppercase), randomness.choice(string.ascii_lowercase),
             randomness.choice(string.digits), randomness.choice(PASSWORD_SYMBOLS)]
    chars += [randomness.choice(PASSWORD_ALPHABET) for _ in range(length - len(chars))]
    randomness.SystemRandom().shuffle(chars)
    return "".join(chars)


def json_escape(value: str) -> str:
    """放進 JSON 字串的值：跳脫引號、反斜線與控制字元，並把非 ASCII 寫成 \\u 形式（Windows 的 AWS CLI 讀檔不受地區編碼影響）。"""
    return json.dumps(value)[1:-1]


def compose_database_url(*, endpoint: str, password: str) -> str:
    """專屬登入的 `DATABASE_URL`（design D7 候選 A、D8）：驗證伺服器憑證（`Encrypt=yes` 加 RDS 的 CA 套件路徑），
    **沒有 `TrustServerCertificate`**，不是 `sa`；密碼經 URL 編碼。"""
    if not ENDPOINT_PATTERN.fullmatch(endpoint or ""):
        raise DemoError("RDS 端點不是合法的主機名稱，不組成連線字串")
    if not password:
        raise DemoError("缺少資料庫登入密碼，不組成連線字串")
    query = urllib.parse.urlencode(
        {"driver": DB_DRIVER, "Encrypt": "yes", "ServerCertificate": plan.RDS_CA_BUNDLE_PATH}, quote_via=urllib.parse.quote_plus)
    login = urllib.parse.quote(plan.DB_APP_LOGIN, safe="")
    return f"mssql+pyodbc://{login}:{urllib.parse.quote(password, safe='')}@{endpoint}:{plan.RDS_PORT}/{plan.DB_NAME}?{query}"


def read_secrets_file(path: str | None) -> dict[str, str]:
    """`--secrets-file`：KEY=VALUE，UTF-8（容許 BOM），**不做 ${VAR} 插值**（機密可能含 $ 與大括號）。"""
    if not path:
        return {}
    from dotenv import dotenv_values

    file = Path(path)
    if not file.is_file():
        raise DemoError(f"找不到機密檔：{file.name}", EXIT_USAGE)
    values = dotenv_values(file, encoding="utf-8-sig", interpolate=False)
    return {key: value for key, value in values.items() if value is not None}


def read_vars_file(path: str | None) -> dict[str, str] | None:
    """`--vars-file`：一般設定（非機密），KEY=VALUE，UTF-8（容許 BOM），不做變數插值。沒有給時回傳 None（主機初始化放置檔案的那一步被擋下）。
    內容是否合法（不得含機密變數、不認得的變數、缺少必要設定）由計畫模組檢查，訊息只含變數名稱。"""
    if not path:
        return None
    from dotenv import dotenv_values

    file = Path(path)
    if not file.is_file():
        raise DemoError(f"找不到一般設定檔：{file.name}", EXIT_USAGE)
    values = dotenv_values(file, encoding="utf-8-sig", interpolate=False)
    return {key: value for key, value in values.items() if value is not None}


def validate_secrets_file(target: plan.Target, values: dict[str, str], *, required: tuple[str, ...] = ()) -> None:
    """檔案只能放「來自你」的機密：未知的名稱、由腳本產生或組成的機密不得出現；`required` 內的機密必須有值。訊息只含變數名稱。"""
    scripted = sorted(name for name in values if name in plan.COMPOSED_SECRETS or name in plan.DB_ONLY_SECRETS)
    if scripted:
        raise DemoError("這些機密由腳本產生或組成，不能放在機密檔：" + "、".join(scripted), EXIT_USAGE)
    unknown = sorted(name for name in values if name not in plan.SECRET_VARS)
    if unknown:
        raise DemoError("機密檔含不是機密的變數名稱：" + "、".join(unknown), EXIT_USAGE)
    missing = [var for var in required if not (values.get(var) or "").strip()]
    if missing:
        raise DemoError("機密檔缺少這些機密（或值是空的）：" + "、".join(missing), EXIT_USAGE)


class SecretStore:
    """機密值的來源。只有「建立該參數的步驟」（`put-parameter-<名稱>`）可以產生或讀檔取得新值；
    其他步驟（例如建立 RDS 時用的主要使用者密碼）一律從 SSM 讀回已存在的值，讀不到就失敗——
    絕不為已存在的資源另外產生一個沒有存下來的密碼。值只放在記憶體，並登記到遮蔽器。"""

    def __init__(self, target: plan.Target, provided: dict[str, str], runner: Runner, redactor: Redactor) -> None:
        self._target = target
        self._provided = provided
        self._runner = runner
        self._redactor = redactor
        self._cache: dict[str, str] = {}

    def value(self, var: str, *, creating: bool, lookup: Callable[[str], str]) -> str:
        if var not in self._cache:
            value = self._create(var, lookup) if creating else self._read(var)
            self._cache[var] = value
            self._redactor.add(value)
        return self._cache[var]

    def _create(self, var: str, lookup: Callable[[str], str]) -> str:
        source = plan.secret_source(var)
        if source == "generated":
            if var == "SECRET_KEY":
                return (self._provided.get(var) or "").strip() or generate_secret_key()
            return generate_password()
        if source == "provided":
            value = self._provided.get(var) or ""
            if not value.strip():
                raise DemoError(f"機密檔缺少 {var}（或值是空的）：請放進 --secrets-file，例如 {SECRETS_FILE_HINT}")
            return value
        endpoint = lookup("rds-endpoint")
        password = self.value("DB_APP_PASSWORD", creating=False, lookup=lookup)
        self._redactor.add(urllib.parse.quote(password, safe=""))
        return compose_database_url(endpoint=endpoint, password=password)

    def _read(self, var: str) -> str:
        argv = ("aws", "ssm", "get-parameter", "--name", plan.parameter_name(var), "--with-decryption",
                "--query", "Parameter.Value", "--output", "text", f"--region={self._target.region}")
        result = self._runner(argv)
        value = result.stdout.strip() if result.returncode == 0 else ""
        if value in NO_VALUE:
            raise DemoError(f"機密 {var} 還沒有建立：請先執行 put-parameter-{var}（不會另外產生一個沒有存下來的值）")
        return value


# ---- 執行 --------------------------------------------------------------------------------------------------


def is_read_only_lookup(step: plan.Step) -> bool:
    """可以在需要它的擷取值遺失時安全重跑的步驟：只有純查詢（沒有建立資源、計費、人工、檔案輸入，指令是 describe／get／list）。"""
    if step.resource or step.billing or step.interactive or step.input_json or step.exists_argv or not step.argv:
        return False
    return len(step.argv) >= 3 and step.argv[0] == "aws" and step.argv[2].startswith(("describe-", "get-", "list-"))


class Executor:
    """逐步執行一份步驟清單：預覽、確認、`exists`／`run_if_found` 判斷、佔位符代入、輪詢與驗證。"""

    def __init__(
        self, *, target: plan.Target, catalog: list[plan.Step], runner: Runner, confirm: Callable[[str], bool],
        out: Callable[[str], None], sleep: Callable[[float], None], clock: Callable[[], float], redactor: Redactor,
        secrets: SecretStore, today: date, project_root: Path,
    ) -> None:
        self.target = target
        self.catalog = catalog            # 整份計畫（取回遺失的擷取值時用；執行的步驟可能只是其中幾組或一步）
        self.captured: dict[str, str] = {}
        self._runner = runner
        self._confirm = confirm
        self._out = out
        self._sleep = sleep
        self._clock = clock
        self._redact = redactor
        self._secrets = secrets
        self._today = today
        self._project_root = project_root
        self._cost_shown = False
        self._recovering: list[str] = []

    def say(self, text: str) -> None:
        self._out(self._redact(text))

    # -- 預覽 --

    def show_step(self, index: int, total: int, step: plan.Step) -> None:
        self.say(f"[{index}/{total}] {step.title}")
        if step.blocked:
            self.say("    （被擋下：沒有輸出指令，執行到這一步會停止）")
        if step.interactive:
            self.say("    人工步驟：需要你親自完成，回來確認後才繼續")
        if step.exists_argv:
            label = "先查詢（失敗或沒有輸出就略過本步）" if step.run_if_found else "先檢查是否已存在（已存在就略過本步）"
            self.say(f"    {label}：{shlex.join(step.exists_argv)}")
        if step.argv:
            self.say(f"    執行：{shlex.join(step.argv)}")
        if step.input_json:
            try:
                body = json.dumps(json.loads(step.input_json), indent=2, ensure_ascii=False)
            except json.JSONDecodeError:
                body = step.input_json
            self.say("    暫存檔內容（{{input_file}}；機密只以名稱參照，值不會顯示）：")
            for line in body.splitlines():
                self.say("      " + line)
        if step.poll:
            self.say(f"    輪詢：{shlex.join(step.poll.argv)}（預期 {step.poll.expect}；每 {step.interval_seconds} 秒、最多 {step.timeout_seconds} 秒）")
        for check in step.verify:
            self.say(f"    驗證：{shlex.join(check.argv)}（預期 {check.expect}）")

    def show_cost(self) -> None:
        if self._cost_shown:
            return
        self._cost_shown = True
        self.say("預估費用（開始計費的步驟）：")
        for line in plan.cost_lines(self._today):
            self.say(line)

    def preview(self, steps: list[plan.Step], *, header: list[str] | None = None) -> int:
        for line in header or []:
            self.say(line)
        if any(step.billing for step in steps):
            self.show_cost()
        for index, step in enumerate(steps, 1):
            self.show_step(index, len(steps), step)
        self.say("")
        self.say("這是預覽：沒有呼叫任何外部指令。加上 --execute 才會逐步確認並執行。")
        return 0

    # -- 執行 --

    def run(self, steps: list[plan.Step], *, assume_yes: bool) -> int:
        """逐步執行；回傳結束代碼（0 成功、3 使用者拒絕、4 被擋下、1 失敗）。"""
        blocked = [step.id for step in steps if step.blocked]
        if blocked:
            self.say("注意：這些步驟被擋下（沒有輸出指令），執行到那裡會停止：" + "、".join(blocked))
        for index, step in enumerate(steps, 1):
            self.show_step(index, len(steps), step)
            if step.blocked:
                self.say("已停止：這一步被擋下，沒有輸出指令（見上面的說明）。")
                return EXIT_BLOCKED
            if step.billing:
                self.show_cost()
            if step.interactive:
                question = "做完了嗎？確認後才繼續（--yes 不能代替人工動作）[y/N] "
            elif assume_yes:
                question = ""
            else:
                question = "要執行這一步嗎？[y/N] "
            if question and not self._confirm(question):
                self.say("已停止：不執行這一步與之後的步驟。")
                return EXIT_DECLINED
            try:
                self.execute_step(step)
            except DemoError as error:
                self.say(f"失敗：{error}")
                return error.code
        self.say("全部完成。")
        return 0

    def execute_step(self, step: plan.Step) -> None:
        skipped = False
        if step.exists_argv:
            found = self._lookup(step.exists_argv)
            text = found.stdout.strip() if found.returncode == 0 else ""
            if step.run_if_found:
                if text in NO_VALUE:
                    self.say("    沒有符合的資源（或查詢失敗），略過。")
                    return
                if step.capture:
                    self.captured[step.capture] = text
            else:
                exists = found.returncode == 0 and (text not in NO_VALUE if step.exists_nonempty else True)
                if exists:
                    if step.capture and text not in NO_VALUE:
                        self.captured[step.capture] = text
                    self.say("    已存在，略過。")
                    skipped = True
        if not skipped:
            if step.argv:
                self._run_main(step)
            if step.poll:
                self._poll(step)
        self._verify(step)

    def _run_main(self, step: plan.Step) -> None:
        if step.argv and step.argv[0] == "git" and "archive" in step.argv:
            (self._project_root / plan.LOCAL_ARCHIVE_DIR).mkdir(parents=True, exist_ok=True)   # `git archive --output` 不會建立目錄
        with self._input_file(step) as path:
            result = self._runner(self._argv(step.argv, input_file=path))
        if result.returncode != 0:
            raise DemoError(self._failure_text(result))
        if step.capture in DISPLAY_CAPTURES:               # 只給人看的輸出（例如拆除後仍留著的快照）：空的也是合法的答案
            shown = result.stdout.strip()
            self.captured[step.capture] = "" if shown == "None" else shown
            self.say("    輸出：" + (shown if shown not in NO_VALUE else "（沒有）"))
        elif step.capture and not step.run_if_found:       # 拆除步驟的 capture 是先前查詢的輸出（已存好），不是刪除指令的輸出
            value = result.stdout.strip()
            if value in NO_VALUE:
                raise DemoError(f"沒有取得要擷取的值（{step.capture}）：指令成功但沒有輸出，請先確認資源是否存在")
            self.captured[step.capture] = value

    def _failure_text(self, result: RunResult) -> str:
        lines = [line for line in result.stderr.strip().splitlines() if line.strip()]
        detail = f"：{lines[-1].strip()[:300]}" if lines else ""
        return f"指令以代碼 {result.returncode} 結束，停止後續步驟{detail}"

    # -- 佔位符 --

    def get_capture(self, name: str) -> str:
        if name not in self.captured:
            self._recover(name)
        return self.captured[name]

    def _ensure(self, texts: list[str]) -> None:
        for text in texts:
            for match in _TEMPLATE_TOKEN.finditer(text):
                name = match.group(2)
                if name and name != "input_file":
                    self.get_capture(name)

    def _recover(self, name: str) -> None:
        """先前步驟的擷取值遺失（`--only`、分組執行、或在另一次執行之後繼續）：只用唯讀的方式向 AWS 取回。
        建立資源的步驟只重跑它的「已存在」查詢；純查詢的步驟重跑它自己；其餘（例如 SSM 的命令編號）無法取回，要求先執行該步驟。"""
        producer = next((step for step in self.catalog if step.capture == name), None)
        if producer is None or name in self._recovering:
            raise DemoError(f"找不到先前步驟擷取的「{name}」：請先執行產生它的步驟（或在同一次執行中一起執行）")
        self._recovering.append(name)
        try:
            if producer.exists_argv and (producer.run_if_found or producer.exists_nonempty):
                argv = producer.exists_argv
            elif is_read_only_lookup(producer):
                argv = producer.argv
            else:
                raise DemoError(f"「{name}」由步驟 {producer.id} 產生，無法安全地重新取得：請先在同一次執行中執行它")
            result = self._runner(self._argv(argv))
            value = result.stdout.strip() if result.returncode == 0 else ""
            if value in NO_VALUE:
                raise DemoError(f"取不回「{name}」：沒有找到對應的資源，請先執行步驟 {producer.id}")
            self.captured[name] = value
            self.say(f"    （向 AWS 唯讀取回先前步驟的結果：{name}）")
        finally:
            self._recovering.pop()

    def _argv(self, argv: tuple[str, ...], *, input_file: str | None = None) -> tuple[str, ...]:
        self._ensure(list(argv))
        result: list[str] = []
        for part in argv:
            whole = _TEMPLATE_TOKEN.fullmatch(part)
            if whole and whole.group(2) and whole.group(2) != "input_file":
                tokens = self.captured[whole.group(2)].split()
                if len(tokens) > 1:                       # 多個值（例如多台主機的編號）：一個值一個引數
                    result.extend(tokens)
                    continue
            result.append(self._text(part, input_file))
        return tuple(result)

    def _text(self, text: str, input_file: str | None = None) -> str:
        def replace(match: re.Match[str]) -> str:
            name = match.group(2)
            if name == "input_file":
                if input_file is None:
                    raise DemoError("命令列用到 {{input_file}}，但這一步沒有暫存檔的內容")
                return input_file
            if match.group(1):
                raise DemoError("機密參照只能出現在暫存檔的 JSON 範本")
            return self.captured[name]
        self._ensure([text])
        return _TEMPLATE_TOKEN.sub(replace, text)

    def _render_json(self, step: plan.Step) -> str:
        """JSON 範本代入：一次掃描（代入的值不會被再次當成範本）；值先經 JSON 跳脫，代入後必須仍是合法的 JSON。"""
        template = step.input_json or ""
        self._ensure([template])

        def replace(match: re.Match[str]) -> str:
            var, name = match.group(1), match.group(2)
            if var:
                return json_escape(self._secrets.value(var, creating=step.id == f"put-parameter-{var}", lookup=self.get_capture))
            if name == "input_file":
                raise DemoError("JSON 範本不能參照 {{input_file}}")
            return json_escape(self.captured[name])

        body = _TEMPLATE_TOKEN.sub(replace, template)
        try:
            json.loads(body)
        except json.JSONDecodeError:
            raise DemoError(f"步驟 {step.id} 的 JSON 範本代入後不是合法的 JSON（內容不顯示）") from None
        return body

    @contextmanager
    def _input_file(self, step: plan.Step) -> Iterator[str | None]:
        """把 JSON 範本寫成暫存檔交給 `file://`：權限限本人（`mkstemp` 為 0600），用後一定刪除（含失敗與中斷）。"""
        if step.input_json is None:
            yield None
            return
        body = self._render_json(step)
        handle, path = tempfile.mkstemp(prefix="aws-demo-", suffix=".json")
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="") as file:
                file.write(body)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            yield path
        finally:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

    def _lookup(self, argv: tuple[str, ...]) -> RunResult:
        return self._runner(self._argv(argv))

    # -- 輪詢與驗證 --

    def _poll(self, step: plan.Step) -> None:
        check = step.poll
        if check is None or step.timeout_seconds is None or step.interval_seconds is None:
            raise DemoError(f"步驟 {step.id} 的輪詢沒有逾時或間隔設定")
        argv = self._argv(check.argv)
        expect = self._text(check.expect)
        deadline = self._clock() + step.timeout_seconds
        last_shown = None
        last = ""
        while True:
            result = self._runner(argv)
            if result.returncode == 0:
                text = result.stdout.strip()
                if text == expect:
                    self.say(f"    已達成（{text}）。")
                    return
                if text in step.poll_fail:
                    self._diagnose(step, argv)
                    raise DemoError(f"等待的對象進入失敗狀態（{text}），停止")
                last = text or "（空）"
            else:
                last = "（查詢失敗，可能還沒就緒）"             # 例如命令剛送出時 get-command-invocation 回 InvocationDoesNotExist：視為還沒好
            if last != last_shown:
                self.say(f"    等待中：目前 {last}")
                last_shown = last
            if self._clock() >= deadline:
                raise DemoError(f"逾時：等待超過 {step.timeout_seconds} 秒仍未達成（預期 {expect}，最後一次：{last}）")
            self._sleep(step.interval_seconds)

    def _diagnose(self, step: plan.Step, poll_argv: tuple[str, ...]) -> None:
        """SSM 命令失敗時，印出該命令的錯誤輸出前幾百字（主機腳本的訊息是英文、不含機密）。"""
        if poll_argv[:3] != ("aws", "ssm", "get-command-invocation"):
            return
        kept: list[str] = []
        skip = False
        for part in poll_argv:
            if skip:
                skip = False
            elif part in ("--query", "--output"):
                skip = True
            else:
                kept.append(part)
        result = self._runner((*kept, "--query", "StandardErrorContent", "--output", "text"))
        text = result.stdout.strip() if result.returncode == 0 else ""
        if text and text not in NO_VALUE:
            self.say(f"    主機上的錯誤輸出（前 500 字，步驟 {step.id}）：")
            for line in text[:500].splitlines():
                self.say("      " + line)

    def _verify(self, step: plan.Step) -> None:
        for index, check in enumerate(step.verify, 1):
            result = self._runner(self._argv(check.argv))
            if result.returncode != 0:
                raise DemoError(f"驗證 {index}/{len(step.verify)} 的查詢失敗：{self._failure_text(result)}")
            expected = self._text(check.expect)
            actual = result.stdout.strip()
            if actual != expected:
                raise DemoError(f"驗證 {index}/{len(step.verify)} 不符：預期 {expected[:200]}，實際 {actual[:200] or '（空）'}")
        if step.verify:
            self.say(f"    驗證通過（{len(step.verify)} 項）。")

    # -- 帳號確認 --

    def ensure_identity(self, selected: list[plan.Step]) -> None:
        """執行前先確認目前的 AWS 帳號就是預期的帳號（唯讀）；選取的步驟已含 check-identity 就不重複。"""
        if any(step.id == "check-identity" for step in selected):
            return
        step = plan.build_plan(self.target, groups=("preflight",))[0]
        self.say("帳號確認（唯讀）：" + step.title)
        self.execute_step(step)


# ---- 拆除前的快照決定（design D14）----------------------------------------------------------------------------


def decide_snapshots(
    target: plan.Target, *, runner: Runner, ask: Callable[[str], str], say: Callable[[str], None],
) -> tuple[str, ...]:
    """唯讀列出該資料庫的手動快照，要求使用者明確回覆要保留的例外（「沒有例外」也要明說；沉默與 Enter 不算），
    回傳要刪除的部署前快照名單（列出的部署前快照減去例外）。最終快照與其他名稱永遠不在名單內。
    沒有得到明確回覆就丟 `DemoError`（結束代碼 3），此時還沒有刪除任何東西。"""
    step = plan.snapshot_listing_step(target)
    say("先唯讀列出這個資料庫的手動快照（不會刪除任何東西）：")
    say("    " + shlex.join(step.argv))
    result = runner(step.argv)
    if result.returncode != 0:
        raise DemoError(f"無法列出快照，不繼續拆除：{result.stderr.strip().splitlines()[-1][:200] if result.stderr.strip() else '沒有錯誤訊息'}")
    listed = tuple(dict.fromkeys(result.stdout.split()))
    predeploy = tuple(name for name in listed if plan.is_predeploy_snapshot(target, name))
    kept_always = tuple(name for name in listed if name not in predeploy)
    if kept_always:
        say("這些快照不是部署前快照（例如最終快照），拆除不會刪除它們：" + "、".join(kept_always))
    if not predeploy:
        say("沒有部署前快照，沒有要刪的。")
        return ()
    say("部署前快照（預設會刪除，除非你指定要保留）：")
    for name in predeploy:
        say("    " + name)
    prompt = f"要保留哪些部署前快照？請輸入名稱（以空白或逗號分隔）；沒有要保留的請輸入「{NO_EXCEPTIONS_REPLY}」：\n> "
    for _ in range(SNAPSHOT_REPLY_ATTEMPTS):
        reply = ask(prompt).strip()
        if not reply:
            say("沉默或直接按 Enter 不算回覆：請明確輸入要保留的名稱，或輸入「" + NO_EXCEPTIONS_REPLY + "」。")
            continue
        words = tuple(word for word in re.split(r"[\s,，、]+", reply) if word)
        if NO_EXCEPTIONS_REPLY in words:
            if words != (NO_EXCEPTIONS_REPLY,):
                say(f"「{NO_EXCEPTIONS_REPLY}」不能和快照名稱一起輸入，請重打。")
                continue
            keep: tuple[str, ...] = ()
        else:
            keep = words
        try:
            names = plan.snapshots_to_delete(target, listed, keep)
        except plan.PlanError as error:
            say(str(error))
            continue
        say("將刪除：" + ("、".join(names) if names else "（沒有）"))
        say("將保留：" + ("、".join(name for name in predeploy if name not in names) or "（沒有）"))
        return names
    raise DemoError("沒有得到明確的回覆，停止拆除（沒有刪除任何東西）", EXIT_DECLINED)


# ---- DuckDNS（task 2.3）------------------------------------------------------------------------------------


def duckdns_label(domain: str) -> str:
    if not domain.endswith(DUCKDNS_SUFFIX):
        raise DemoError(f"網域不是 {DUCKDNS_SUFFIX} 的子網域，不更新 DuckDNS")
    label = domain[: -len(DUCKDNS_SUFFIX)]
    if not DUCKDNS_LABEL_PATTERN.fullmatch(label):
        raise DemoError("DuckDNS 的子網域名稱不合法（只能是單層的小寫英數與連字號）")
    return label


def eip_lookup_argv(target: plan.Target) -> tuple[str, ...]:
    n = plan.names(target)
    return ("aws", "ec2", "describe-addresses", "--filters", f"Name=tag:Name,Values={n.eip}",
            f"Name=tag:Project,Values={plan.PROJECT_TAG}", f"Name=tag:Environment,Values={plan.ENV}",
            "--query", "Addresses[0].PublicIp", "--output", "text", f"--region={target.region}")


def update_duckdns(label: str, token: str, ip: str, *, fetch: Callable[[str], tuple[int, str]]) -> None:
    """更新 DuckDNS 的 A 記錄。權杖在網址的查詢字串裡（DuckDNS 的 API 只有這種形式）：網址只存在於這個函式的區域變數，
    不印出、不放進例外；`fetch` 只回傳狀態與內容。"""
    url = DUCKDNS_UPDATE_URL + "?" + urllib.parse.urlencode({"domains": label, "token": token, "ip": ip})
    status, body = fetch(url)
    if status != 200:
        raise DemoError(f"DuckDNS 回覆 HTTP {status}，沒有更新")
    if body.strip() != "OK":
        raise DemoError("DuckDNS 沒有回覆 OK：請確認權杖（環境變數 DUCKDNS_TOKEN）與子網域名稱是否正確")


# ---- 命令列 ------------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AWS 展示環境執行器（預設只預覽，--execute 才逐步確認並執行）。")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--account-id", required=True, help="12 位數字的 AWS 帳號編號（沒有預設值；執行前會與目前登入的帳號比對）")
    common.add_argument("--region", default=plan.REGION, help=f"只接受 {plan.REGION}；其他區域在呼叫任何指令之前就被拒絕")
    common.add_argument("--domain", default=plan.DEMO_DOMAIN, help="展示網域")
    common.add_argument("--execute", action="store_true", help="實際執行（每一步執行前都要確認）；沒有這個參數只預覽")
    sub = parser.add_subparsers(dest="command", required=True)

    def with_steps(sub_parser: argparse.ArgumentParser, groups: tuple[str, ...] | None) -> None:
        sub_parser.add_argument("--only", metavar="STEP", help="只處理指定的單一步驟（步驟代碼見預覽輸出）")
        sub_parser.add_argument("--yes", action="store_true", help="--only 的單步執行不再詢問確認（必須搭配 --only；人工步驟與快照例外仍會詢問）")
        if groups is not None:
            sub_parser.add_argument("--group", action="append", default=[], choices=groups, help="只處理這一組（可重複）")

    provision = sub.add_parser("provision", parents=[common], help="建立展示環境（預算、網路、角色、儲存、機密、RDS、EC2、告警）")
    with_steps(provision, plan.GROUPS)
    provision.add_argument("--notify-email", action="append", default=[], metavar="ADDRESS", help="預算與狀態告警的收件人（沒有預設值）")
    provision.add_argument("--budget-usd", type=int, default=plan.BUDGET_LIMIT_USD)
    provision.add_argument("--with-secret", action="append", default=[], metavar="NAME", help="除必要機密之外要建立的機密名稱")
    provision.add_argument("--secrets-file", help=f"機密的值（KEY=VALUE；例如 {SECRETS_FILE_HINT}，不進版本庫）")
    provision.add_argument("--vars-file", help=f"一般設定（非機密，KEY=VALUE；例如 {VARS_FILE_HINT}，不進版本庫）：主機初始化放置 plain.env 時使用，沒有它那一步被擋下")
    provision.add_argument("--accept-cost", action="store_true", help="同意建立計費資源（沒有它，RDS、EC2、Elastic IP 的步驟不輸出指令）")

    deploy = sub.add_parser("deploy", parents=[common], help="部署：快照、建置映像、遷移、重啟兩個池、驗證")
    with_steps(deploy, plan.DEPLOY_GROUPS)
    deploy.add_argument("--tag", help="要部署的提交短碼（7 至 40 位小寫十六進位）；省略時取目前 git 的短提交")

    rollback = sub.add_parser("rollback", parents=[common], help="回復：只把兩個池換回前一版映像標籤（不動資料庫）")
    with_steps(rollback, None)
    rollback.add_argument("--tag", required=True, help="要回到的映像標籤（主機上的 release.previous.env 有部署前的標籤）")

    teardown = sub.add_parser("teardown", parents=[common], help="拆除：只刪帶本專案標籤的資源")
    with_steps(teardown, plan.TEARDOWN_GROUPS)
    final = teardown.add_mutually_exclusive_group()
    final.add_argument("--final-snapshot", action="store_true", help="刪除 RDS 前保留最終快照")
    final.add_argument("--no-final-snapshot", action="store_true", help="不保留最終快照（資料庫內容永久消失）")

    sub.add_parser("duckdns", parents=[common], help=f"把展示網域指向 Elastic IP（權杖來自環境變數 {DUCKDNS_TOKEN_ENV}）")
    return parser


def _target_from(args: argparse.Namespace) -> plan.Target:
    extra: dict[str, object] = {}
    if args.command == "provision":
        extra = dict(notify_emails=tuple(args.notify_email), budget_usd=args.budget_usd, with_secrets=tuple(args.with_secret))
    return plan.Target(account_id=args.account_id, region=args.region, domain=args.domain, **extra)


def _build_steps(
    args: argparse.Namespace, target: plan.Target, *, tag_default: Callable[[], str], today: date,
    delete_snapshots: tuple[str, ...] | None = None, plain: dict[str, str] | None = None,
) -> tuple[list[plan.Step], list[plan.Step], list[str]]:
    """回傳（整份計畫, 這次要處理的步驟, 預覽用的提醒）。整份計畫用來在擷取值遺失時唯讀取回；
    `--group` 取其中幾組（部署會自動帶上找主機的步驟），`--only` 取單一步驟。"""
    groups = tuple(getattr(args, "group", None) or ())           # rollback 沒有 --group（固定三個步驟）
    if args.only and groups:
        raise DemoError("--only 與 --group 不能一起用", EXIT_USAGE)
    notes: list[str] = []
    if args.command == "provision":
        catalog = plan.build_plan(target, accept_cost=args.accept_cost, plain=plain)
        narrowed = plan.build_plan(target, accept_cost=args.accept_cost, groups=groups, plain=plain) if groups else catalog
    elif args.command == "deploy":
        tag = args.tag or tag_default()
        catalog = plan.deploy_plan(target, tag)
        narrowed = plan.deploy_plan(target, tag, groups=groups) if groups else catalog
        notes.append(f"映像標籤 {tag}；原始碼來自 git archive（只含已提交的內容，不含 .env 與未提交的變更）。")
    elif args.command == "rollback":
        catalog = narrowed = plan.rollback_plan(target, args.tag)
    else:
        final: bool | None = True if args.final_snapshot else False if args.no_final_snapshot else None
        catalog = plan.teardown_plan(target, final_snapshot=final, today=today, delete_snapshots=delete_snapshots)
        narrowed = [step for step in catalog if step.group in groups] if groups else catalog
    if args.only and args.command == "teardown" and args.only.startswith(SNAPSHOT_STEP_PREFIX) and delete_snapshots is None:
        narrowed = []                  # 單一快照的刪除步驟要等執行時列出快照、使用者回覆例外之後才會產生（main 會重新建立計畫）
    elif args.only:
        chosen = [step for step in catalog if step.id == args.only]
        if not chosen:
            raise DemoError(f"沒有這個步驟：{args.only}。可用的步驟：" + "、".join(step.id for step in catalog), EXIT_USAGE)
        narrowed = chosen
    return catalog, list(narrowed), notes


def _check_secrets_file(target: plan.Target, provided: dict[str, str], selected: list[plan.Step]) -> None:
    """機密檔只放「來自你」的機密；選取的步驟需要哪些就必須齊全（缺的名稱一次列出，值不顯示）。"""
    needed = tuple(
        var for var in plan.secrets_in_use(target)
        if plan.secret_source(var) == "provided" and any(step.id == f"put-parameter-{var}" for step in selected))
    validate_secrets_file(target, provided, required=needed)


def main(
    argv: list[str] | None = None, *, runner: Runner | None = None, confirm: Callable[[str], bool] | None = None,
    ask: Callable[[str], str] | None = None, out: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic, environ: dict[str, str] | None = None,
    fetch: Callable[[str], tuple[int, str]] = default_fetch, tag_default: Callable[[], str] = git_short_commit,
    today_fn: Callable[[], date] = taipei_today, project_root: Path | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    runner = runner or subprocess_runner
    confirm = confirm or interactive_confirm
    ask = ask or interactive_ask
    environ = os.environ if environ is None else environ
    project_root = project_root or plan.PROJECT_ROOT
    redactor = Redactor()
    assume_yes = bool(getattr(args, "yes", False))
    execute = args.execute or assume_yes

    def say(text: str) -> None:
        out(redactor(text))

    try:
        if assume_yes and not args.only:
            raise DemoError("--yes 只能搭配 --only 使用：沒有「整份計畫一律同意」", EXIT_USAGE)
        if args.command == "teardown" and args.only == "td-delete-snapshots":
            raise DemoError("td-delete-snapshots 只是佔位步驟（沒有指令）：要刪特定的部署前快照請用 --only td-delete-snapshot-<名稱>，"
                            "執行時仍會先列出快照並要求你回覆例外", EXIT_USAGE)
        target = _target_from(args)
        today = today_fn()
        header = [f"目標：帳號 {target.account_id}、區域 {target.region}、網域 {target.domain}（展示環境，APP_ENV=staging）"]
        if args.command == "duckdns":
            return _run_duckdns(args, target, runner=runner, confirm=confirm, say=say, environ=environ, fetch=fetch,
                                redactor=redactor, sleep=sleep, clock=clock, today=today, project_root=project_root, header=header)

        provided = read_secrets_file(args.secrets_file) if args.command == "provision" else {}
        for value in provided.values():
            redactor.add(value)
        plain = read_vars_file(args.vars_file) if args.command == "provision" else None
        catalog, selected, notes = _build_steps(args, target, tag_default=tag_default, today=today, plain=plain)
        header += notes
        if args.command == "provision":
            if plain is not None:
                header.append(f"一般設定檔：{len(plain)} 個設定，檢查通過（只檢查名稱，值不顯示）。")
            if args.secrets_file:
                _check_secrets_file(target, provided, selected)
                header.append("機密檔：名稱與完整性檢查通過（只檢查名稱，值不顯示）。")
            elif any(step.id.startswith("put-parameter-") for step in selected):
                header.append(f"提醒：機密的值由 --secrets-file 提供（例如 {SECRETS_FILE_HINT}）或由腳本產生；加上 --secrets-file 時會檢查內容是否齊全。")

        executor = Executor(target=target, catalog=catalog, runner=runner, confirm=confirm, out=out, sleep=sleep, clock=clock,
                            redactor=redactor, secrets=SecretStore(target, provided, runner, redactor), today=today, project_root=project_root)
        deferred = args.command == "teardown" and bool(args.only) and args.only.startswith(SNAPSHOT_STEP_PREFIX)
        if not execute:
            if deferred:
                header.append("這一步要在執行時先唯讀列出快照、由你明確回覆要保留的例外之後才會產生；預覽只能顯示列出快照的指令。")
                return executor.preview([plan.snapshot_listing_step(target)], header=header)
            return executor.preview(selected, header=header)

        full_teardown = args.command == "teardown" and not args.only
        if full_teardown and not (args.final_snapshot or args.no_final_snapshot):
            raise DemoError("拆除要明確選擇是否保留最終快照：加上 --final-snapshot 或 --no-final-snapshot（沒有預設值）", EXIT_USAGE)
        for line in header:
            say(line)
        executor.ensure_identity(selected)
        if args.command == "teardown" and (full_teardown or deferred):
            # 刪快照之前：先唯讀列出、由使用者明確回覆要保留的例外，之後才產生含刪除指令的計畫（--yes 不能略過這一步）
            names = decide_snapshots(target, runner=runner, ask=ask, say=say)
            catalog, selected, _ = _build_steps(args, target, tag_default=tag_default, today=today, delete_snapshots=names)
            executor.catalog = catalog
        return executor.run(selected, assume_yes=assume_yes)
    except plan.PlanError as error:
        print(f"錯誤：{redactor(str(error))}", file=sys.stderr)
        return EXIT_USAGE
    except DemoError as error:
        print(f"錯誤：{redactor(str(error))}", file=sys.stderr)
        return error.code


def _run_duckdns(
    args: argparse.Namespace, target: plan.Target, *, runner: Runner, confirm: Callable[[str], bool], say: Callable[[str], None],
    environ: dict[str, str], fetch: Callable[[str], tuple[int, str]], redactor: Redactor, sleep: Callable[[float], None],
    clock: Callable[[], float], today: date, project_root: Path, header: list[str],
) -> int:
    label = duckdns_label(target.domain)
    lookup = eip_lookup_argv(target)
    for line in header:
        say(line)
    say(f"DuckDNS：把 {target.domain} 的 A 記錄指向帶本專案標籤的 Elastic IP")
    say("    先唯讀查出位址：" + shlex.join(lookup))
    say(f"    再呼叫 {DUCKDNS_UPDATE_URL}（權杖來自環境變數 {DUCKDNS_TOKEN_ENV}，不顯示、不經命令列）")
    if not args.execute:
        say("")
        say("這是預覽：沒有呼叫任何外部指令。加上 --execute 才會查出位址並在你確認後更新。")
        return 0
    token = environ.get(DUCKDNS_TOKEN_ENV, "").strip()
    if not token:
        raise DemoError(f"環境變數 {DUCKDNS_TOKEN_ENV} 沒有設定（DuckDNS 的權杖不經命令列）", EXIT_USAGE)
    redactor.add(token)
    store = SecretStore(target, {}, runner, redactor)
    executor = Executor(target=target, catalog=[], runner=runner, confirm=confirm, out=lambda text: say(text), sleep=sleep, clock=clock,
                        redactor=redactor, secrets=store, today=today, project_root=project_root)
    try:
        executor.ensure_identity([])
    except DemoError as error:
        say(f"失敗：{error}")
        return error.code
    result = runner(lookup)
    address = result.stdout.strip() if result.returncode == 0 else ""
    try:
        ip = str(ipaddress.IPv4Address(address))
    except ValueError:
        raise DemoError("查不到帶本專案標籤的 Elastic IP（或位址格式不對）：請先建立主機與 Elastic IP") from None
    say(f"Elastic IP：{ip}")
    if not confirm(f"要把 {target.domain} 指向 {ip} 嗎？[y/N] "):
        say("已停止：沒有更新。")
        return EXIT_DECLINED
    update_duckdns(label, token, ip, fetch=fetch)
    say(f"已更新（DuckDNS 回覆 OK）：{target.domain} 指向 {ip}。DNS 生效可能需要幾分鐘。")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")     # 繁體中文 Windows 的主控台是 cp950：遇到不能編碼的字元以 ? 取代，不要崩潰
        except (AttributeError, ValueError):
            pass
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("已中斷。", file=sys.stderr)
        sys.exit(130)
