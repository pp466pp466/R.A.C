"""Locust 情境（load-testing 規格）：每位虛擬使用者帶著預先簽出的登入 cookie 進入、開新對話，之後每 30 至 45 秒送一則訊息，並混入列表與增量拉取。

玩家只用 Google 登入，沒有註冊與密碼登入：測試開始前，先用 `scripts/mint_test_sessions.py` 在目標環境內建立測試玩家並簽出 cookie
（寫成 `test-sessions.json`，路徑可用環境變數 `LOADTEST_SESSIONS` 指定），數量必須不少於虛擬使用者數，否則在送出任何請求之前失敗。

    locust -f loadtest/locustfile.py --headless -u 100 -r 5 -t 15m --host http://127.0.0.1:5000 --csv loadtest/result --csv-full-history

- 預設 100 位虛擬使用者、持續 15 分鐘（`-u`、`-t` 可調；階梯式加壓用 `-r` 與 Locust 的 LoadTestShape）。
- **預設只對本機位址執行**；其他位址需要 `--confirm-remote`；正式環境一律拒絕（見 loadtest/guard.py）。
- 測試帳號使用保留的 `.invalid` 網域；本機以 `loadtest.fake_app`（假模型）啟動應用程式，所以不會呼叫真實的模型，也不會寄出任何真實信件。
- Locust 只在 requirements-dev.txt，不進映像。
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from locust import HttpUser, between, events, task  # noqa: E402

from loadtest.actions import Member, Reply  # noqa: E402
from loadtest.guard import TargetRefused, check_target  # noqa: E402
from loadtest.session_pool import DEFAULT_POOL_FILE, PoolError, load_pool, require  # noqa: E402

RUN_ID = uuid.uuid4().hex[:8]
_counter = iter(range(1, 10**9))
SESSIONS_FILE = os.environ.get("LOADTEST_SESSIONS", str(ROOT / DEFAULT_POOL_FILE))
try:
    POOL = load_pool(SESSIONS_FILE)
except PoolError as error:
    raise SystemExit(str(error)) from None


@events.init_command_line_parser.add_listener
def _add_arguments(parser) -> None:
    parser.add_argument("--confirm-remote", action="store_true", default=False, help="確認目標是測試或預備環境（非本機位址時必要；正式環境一律拒絕）")


@events.init.add_listener
def _guard_target(environment, **kwargs) -> None:
    try:
        check_target(environment.host or "http://127.0.0.1:5000", confirm_remote=bool(getattr(environment.parsed_options, "confirm_remote", False)))
    except TargetRefused as error:
        raise SystemExit(str(error)) from None
    needed = int(getattr(environment.parsed_options, "num_users", 0) or 0)
    shape = getattr(environment, "shape_class", None)
    if shape is not None and hasattr(shape, "peak_users"):
        needed = max(needed, shape.peak_users())
    try:
        require(POOL, needed)
    except PoolError as error:
        raise SystemExit(str(error)) from None


class LocustSession:
    """把 Locust 的 `HttpSession` 包成 `Session`；狀態碼 400 以上由 Locust 記為失敗。"""

    def __init__(self, client, origin: str) -> None:
        self.client, self.origin = client, origin

    @staticmethod
    def _reply(response) -> Reply:
        try:
            body = response.json() if response.headers.get("Content-Type", "").startswith("application/json") else None
        except ValueError:
            body = None
        return Reply(response.status_code, body, response.text if body is None else "")

    def set_cookie(self, name: str, value: str) -> None:
        self.client.cookies.set(name, value, path="/")

    def get(self, path: str, *, name: str | None = None) -> Reply:
        return self._reply(self.client.get(path, name=name or path))

    def post(self, path: str, *, json, headers, name: str | None = None, fail_if=None) -> Reply:
        # catch_response：狀態碼 400 以上，以及 `fail_if` 指出的失敗（例如降級回覆，HTTP 仍是 200）都標為失敗，同一個請求不重複計數
        with self.client.post(path, json=json, headers=headers, name=name or path, catch_response=True) as response:
            reply = self._reply(response)
            problem = None if reply.status < 400 else f"HTTP {reply.status}"
            if problem is None and fail_if is not None:
                problem = fail_if(reply)
            if problem:
                response.failure(problem)
            else:
                response.success()
            return reply


class MemberUser(HttpUser):
    wait_time = between(30, 45)

    def on_start(self) -> None:
        origin = (self.host or "").rstrip("/")
        number = next(_counter)
        self.member = Member(LocustSession(self.client, origin), run_id=RUN_ID, number=number)
        self.ready = number <= len(POOL) and self.member.sign_in(POOL.cookie_name, POOL.value_for(number)) and self.member.open_conversation()

    @task(10)
    def chat(self) -> None:
        if self.ready:
            self.member.send_message()

    @task(2)
    def list_conversations(self) -> None:
        if self.ready:
            self.member.list_conversations()

    @task(2)
    def pull_since(self) -> None:
        if self.ready:
            self.member.pull_since()
