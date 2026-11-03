"""在「目標環境內」建立測試玩家並簽出登入用的 session cookie（load-testing 與 staging-verification 規格）。

玩家只用 Google 登入，沒有可呼叫的註冊或密碼登入端點，而 Google 登入無法自動化，所以負載測試、冒煙驗收與容器驗收需要另一種
取得玩家身分的方式。做法與 `scripts/verify_container.py` 對管理員已採用的相同：產生一小段程式，在**目標環境的程序內**執行——它用
該環境自己的設定與 `SECRET_KEY` 建立測試玩家，並簽出與真實登入完全相同的 session cookie（內容只有 `uid` 與 `tv`）。

- **映像不新增任何能簽發登入狀態的指令或端點**：這個檔案在 `scripts/`（不進映像），被執行的只是一段由這裡產生的文字。
- **拒絕在 production 執行**（程式一開始就結束，沒有建立任何帳號）。
- 測試玩家：信箱屬於保留的 `loadtest.invalid` 網域、狀態 `active`、已完成年齡確認的成年人、條款已同意、沒有密碼。
- 輸出檔（預設 `test-sessions.json`，UTF-8，已列入 .gitignore）內是**有效的登入憑證**，只屬於這些測試帳號；不要貼到對話或日誌裡。

注意：`--local` 以**執行這個腳本的 shell** 的環境變數（以及非 production 的 `.env`）載入應用程式；簽出的 cookie 只有在**目標伺服器使用相同的
`SECRET_KEY` 與 `DATABASE_URL`** 時才會被接受。本機同時啟動伺服器時，兩邊都明確設定這兩個變數（尤其是 `SECRET_KEY`：腳本讀得到 `.env`，
而以 `loadtest.fake_app` 啟動的伺服器不讀）；對容器或雲端環境用 `--docker-exec`（或在該環境執行 `--print-snippet` 印出的程式），就不會有這個落差。

用法：
    # 本機（以目前的環境變數載入應用程式，例如預備環境的本機預演）
    python scripts/mint_test_sessions.py --count 20 --local
    # 容器內（容器名稱由使用者提供；WSL 的 Docker 請加 --docker-prefix "wsl docker"）
    python scripts/mint_test_sessions.py --count 20 --docker-exec <容器名稱>
    # 只印出會執行的程式，不執行
    python scripts/mint_test_sessions.py --count 20 --print-snippet

之後：負載測試設定 `LOADTEST_SESSIONS=test-sessions.json`；冒煙驗收設定環境變數 `SMOKE_SESSION_COOKIE=<cookie 名稱>=<值>`
（取檔案中 `cookie_name` 與任一筆 `sessions[].value`，或用 `--smoke-cookie` 印出一組）。
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_EMAIL_DOMAIN = "loadtest.invalid"         # RFC 2606 保留的 .invalid：永遠不會有真實的信箱
MARKER = "MINTED:"
MAX_COUNT = 5000
DEFAULT_OUTPUT = "test-sessions.json"
PREFIX_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")

REFUSED_TOKEN = "mint_test_sessions-refused-in-production"          # 純 ASCII：容器的 stderr 編碼不一定是 UTF-8，靠它辨識拒絕
REFUSED_IN_PRODUCTION = "拒絕：APP_ENV 為 production，不簽出測試登入狀態，也沒有建立任何帳號。"

EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2

_SNIPPET = '''\
import json, os, sys
if os.environ.get("APP_ENV") == "production":
    sys.exit(%(refused_token)r + ' | ' + %(refused)s)
from datetime import datetime, timezone

from app import create_app
from app.models.user import STATUS_ACTIVE, User
from app.repositories.user_repository import UserRepository
from app.services.age_policy import DEFAULT_AGE_POLICY, range_for_age, taipei_today
from app.services.legal import CURRENT_TERMS_VERSION
from app.services.normalization import canonical_email, normalize_email

COUNT, PREFIX, DOMAIN, AGE = %(count)d, %(prefix)r, %(domain)r, 30
app = create_app()
now = datetime.now(timezone.utc)
today = taipei_today(now)
born = today.replace(year=today.year - AGE, month=6, day=15) if (today.month, today.day) >= (6, 15) else today.replace(year=today.year - AGE - 1, month=6, day=15)
chosen = range_for_age(AGE, DEFAULT_AGE_POLICY)
serializer = app.session_interface.get_signing_serializer(app)
sessions = []
with app.extensions["db_session_factory"]() as session:
    users = UserRepository(session)
    for number in range(1, COUNT + 1):
        email = f"{PREFIX}-{number}@{DOMAIN}"
        user = User(
            email=normalize_email(email), email_canonical=canonical_email(email), display_name=f"%(player)s{number}", password_hash=None,
            email_verified_at=now, status=STATUS_ACTIVE, terms_version=CURRENT_TERMS_VERSION, terms_accepted_at=now,
            birth_date=born, age_range=chosen.code, is_adult_at_confirmation=True, age_confirmed_at=now,
            age_policy_version=DEFAULT_AGE_POLICY.version,
        )
        users.add_user(user)
        sessions.append({"email": email, "value": serializer.dumps({"uid": user.id, "tv": user.token_version})})
    session.commit()
print(%(marker)r + json.dumps({"cookie_name": app.config["SESSION_COOKIE_NAME"], "sessions": sessions}, ensure_ascii=False))
'''


class MintError(Exception):
    """參數不合法、被拒絕（production），或執行失敗。訊息可以直接顯示（不含 cookie 的值）。`refused` 為真時結束代碼是 2。"""

    def __init__(self, message: str, *, refused: bool = False) -> None:
        super().__init__(message)
        self.refused = refused


def new_prefix() -> str:
    return "loadtest-" + secrets.token_hex(4)


def render_snippet(count: int, prefix: str) -> str:
    """在目標環境內執行的程式文字（純 ASCII，容器內可直接由標準輸入傳入）。`count` 與 `prefix` 都經過驗證，所以不可能夾帶其他程式。"""
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= MAX_COUNT:
        raise MintError(f"--count 必須是 1 至 {MAX_COUNT} 的整數。", refused=True)
    if not PREFIX_PATTERN.fullmatch(prefix):
        raise MintError("--prefix 只能是小寫英數字與連字號（最長 31 字，不以連字號開頭）。", refused=True)
    return _SNIPPET % {
        "refused_token": REFUSED_TOKEN, "refused": ascii(REFUSED_IN_PRODUCTION), "player": "player-", "count": count, "prefix": prefix,
        "domain": TEST_EMAIL_DOMAIN, "marker": MARKER,
    }


Runner = Callable[[list[str], str], subprocess.CompletedProcess]


def _run(command: list[str], snippet: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        command + ["-c", snippet], cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )


def parse_output(stdout: str) -> dict:
    """取出程式最後輸出的那一行（日誌行可能在它前面）。"""
    lines = [line for line in stdout.splitlines() if line.startswith(MARKER)]
    if not lines:
        raise MintError("目標環境沒有回傳簽出的登入狀態（程式沒有成功結束）。")
    try:
        data = json.loads(lines[-1][len(MARKER):])
    except ValueError:
        raise MintError("目標環境回傳的內容無法解析。") from None
    if not isinstance(data.get("cookie_name"), str) or not isinstance(data.get("sessions"), list) or not data["sessions"]:
        raise MintError("目標環境回傳的內容缺少登入狀態。")
    return data


def mint(command: list[str], count: int, prefix: str, *, runner: Runner = _run) -> dict:
    """在 `command`（例如 `[python]` 或 `[docker, exec, 容器, python]`）指定的環境內執行，回傳解析後的結果。"""
    result = runner(command, render_snippet(count, prefix))
    if result.returncode != 0:
        if REFUSED_TOKEN in (result.stderr or ""):
            raise MintError(REFUSED_IN_PRODUCTION, refused=True)
        raise MintError(f"目標環境執行失敗（結束代碼 {result.returncode}）；詳細原因請在該環境查看日誌。")
    return parse_output(result.stdout)


def write_output(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def smoke_cookie(data: dict, index: int = 0) -> str:
    return f"{data['cookie_name']}={data['sessions'][index]['value']}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="在目標環境內建立測試玩家並簽出 session cookie（拒絕 production）。")
    parser.add_argument("--count", type=int, default=1, help=f"測試玩家數（1 至 {MAX_COUNT}），負載測試要與虛擬使用者數相同或更多")
    parser.add_argument("--prefix", default=None, help="信箱前綴（預設隨機；同一環境重複執行請用不同前綴）")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--local", action="store_true", help="以目前的環境變數在本機載入應用程式並執行")
    target.add_argument("--docker-exec", metavar="容器", help="在指定的執行中容器內執行（映像內的 python）")
    target.add_argument("--print-snippet", action="store_true", help="只印出會在目標環境執行的程式，不執行")
    parser.add_argument("--docker-prefix", default="docker", help='Docker 指令前綴（預設 "docker"；WSL 請用 "wsl docker"）')
    parser.add_argument("--out", default=DEFAULT_OUTPUT, help=f"輸出檔（預設 {DEFAULT_OUTPUT}，已列入 .gitignore）")
    parser.add_argument("--smoke-cookie", action="store_true", help="另外印出第一組的 <名稱>=<值>（給 SMOKE_SESSION_COOKIE；這是有效的憑證）")
    return parser


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print, runner: Runner = _run) -> int:
    args = build_parser().parse_args(argv)
    prefix = args.prefix or new_prefix()
    try:
        if args.print_snippet:
            out(render_snippet(args.count, prefix))
            return EXIT_OK
        command = [sys.executable] if args.local else [*shlex.split(args.docker_prefix), "exec", args.docker_exec, "python"]
        data = mint(command, args.count, prefix, runner=runner)
    except MintError as error:
        out(str(error))
        return EXIT_REFUSED if error.refused else EXIT_FAILED
    write_output(Path(args.out), data)
    out(f"已建立 {len(data['sessions'])} 位測試玩家（信箱前綴 {prefix}，網域 {TEST_EMAIL_DOMAIN}），登入狀態寫入 {args.out}。")
    if args.smoke_cookie:
        out(smoke_cookie(data))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
