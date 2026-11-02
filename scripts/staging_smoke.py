"""預備環境冒煙驗收（staging-verification 規格「冒煙驗收腳本」與「腳本只對預備環境執行」）。

    python scripts/staging_smoke.py https://<預備環境網址> [--allow-model-call]

對一個預備環境網址依序檢查，逐項輸出「通過」、「失敗」或「略過」（結束代碼：沒有失敗為 0，有失敗為 1，位址被拒絕為 2）：
1. `/healthz` 與 `/readyz` 回 200；
2. 首頁帶有五個安全標頭（含 HSTS）且**沒有** `Content-Security-Policy`（刻意不送，見營運文件）；
3. 沒有 CSRF 權杖的 POST 被拒絕（403）；
4. 註冊（保留的 `.invalid` 測試信箱，由 `loadtest/actions.py` 的虛擬會員）並登入、開新對話；
5. session 與 CSRF cookie 使用 `__Host-` 前綴、帶 `Secure`、不含 `Domain`，session 為 `HttpOnly`；
6. 一般會員存取管理員 API 得到 404（與不存在的端點相同，不透露後台存在）；
7. 未登入存取需要登入的 API 得到 401；
8. **預設不呼叫任何模型**（標為略過）；加 `--allow-model-call` 才送出一則訊息，並檢查回覆不是降級文案。

- **只對 https 的預備環境執行**：主機名稱含 `production` 或位於 `loadtest/guard.py` 列出的正式網域時一律拒絕，**沒有任何確認參數可以繞過**；
  非 https、帶路徑、帳密、查詢字串或片段的位址也拒絕，而且是在送出任何請求之前。
- 輸出**只有項目名稱與結果**：不含 cookie、權杖、密碼或回應內容。失敗的原因只說明是哪個標頭、狀態碼或屬性。
- 註冊的帳號使用保留的 `.invalid` 網域（RFC 2606），不會寄出信件也不屬於任何真人；每執行一次會在預備環境留下一個這樣的測試帳號。
- 對經 ngrok 的網址會多帶 `ngrok-skip-browser-warning` 標頭，略過免費方案的瀏覽器警告頁。
- 傳輸（HTTP）可注入：`Transport` 協定的實作在執行時是標準函式庫的 `urllib`，測試則用 Flask 測試客戶端驅動同一組檢查。
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import sys
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from loadtest.actions import Member, Reply, degraded_reply  # noqa: E402 - 需要先把專案根目錄放進 sys.path
from loadtest.guard import TargetRefused, check_target  # noqa: E402

PASS, FAIL, SKIP = "通過", "失敗", "略過"
SECURITY_HEADERS = (
    "Strict-Transport-Security", "X-Content-Type-Options", "Referrer-Policy", "X-Frame-Options", "Permissions-Policy",
)
CSP_HEADERS = ("Content-Security-Policy", "Content-Security-Policy-Report-Only")
NGROK_MARKERS = ("ngrok",)
REQUEST_TIMEOUT_SECONDS = 20

EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2


# ---- 傳輸 ---------------------------------------------------------------------------------------


@dataclass
class HttpResponse:
    status: int
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""

    def header(self, name: str) -> str | None:
        return next((value for key, value in self.headers if key.lower() == name.lower()), None)

    def set_cookies(self) -> list[str]:
        return [value for key, value in self.headers if key.lower() == "set-cookie"]


class Transport(Protocol):
    origin: str

    def request(self, method: str, path: str, *, headers: dict[str, str] | None = None, json_body: Any = None) -> HttpResponse: ...

    def fresh(self) -> "Transport": ...


class UrllibTransport:
    """標準函式庫的實作：每個實例有自己的 cookie jar（`fresh()` 給「未登入」的檢查用）。連線失敗回狀態碼 0。"""

    def __init__(self, base_url: str) -> None:
        self.origin = base_url.rstrip("/")
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        host = (urlsplit(self.origin).hostname or "").lower()
        self._extra = {"ngrok-skip-browser-warning": "1"} if any(marker in host for marker in NGROK_MARKERS) else {}

    def fresh(self) -> "UrllibTransport":
        return UrllibTransport(self.origin)

    def request(self, method: str, path: str, *, headers: dict[str, str] | None = None, json_body: Any = None) -> HttpResponse:
        data = None
        merged = {"User-Agent": "staging-smoke/1", **self._extra, **(headers or {})}
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            merged["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(self.origin + path, data=data, headers=merged, method=method)
        try:
            with self._opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return HttpResponse(response.status, list(response.headers.items()), response.read())
        except urllib.error.HTTPError as error:                 # 4xx 與 5xx 是我們要檢查的回應，不是例外
            with error:
                return HttpResponse(error.code, list(error.headers.items()), error.read())
        except (urllib.error.URLError, TimeoutError, OSError):
            return HttpResponse(0)


class SmokeSession:
    """把 `Transport` 包成 `loadtest.actions` 的 `Session`，讓虛擬會員的註冊、登入與開對話動作可以直接重用。
    收到的 Set-Cookie 行記進 `cookie_log`（只用來檢查屬性；輸出不含 cookie 的值）。"""

    def __init__(self, transport: Transport, cookie_log: list[str]) -> None:
        self.transport = transport
        self.origin = transport.origin
        self._cookie_log = cookie_log

    def _send(self, method: str, path: str, **kwargs: Any) -> Reply:
        response = self.transport.request(method, path, **kwargs)
        self._cookie_log.extend(response.set_cookies())
        text = response.body.decode("utf-8", errors="replace")
        body = None
        if (response.header("Content-Type") or "").startswith("application/json"):
            try:
                body = json.loads(text)
            except ValueError:
                body = None
        return Reply(response.status, body, text if body is None else "")

    def get(self, path: str, *, name: str | None = None) -> Reply:
        return self._send("GET", path)

    def post(self, path: str, *, json: dict, headers: dict[str, str], name: str | None = None, fail_if: Callable[[Reply], str | None] | None = None) -> Reply:
        return self._send("POST", path, headers=headers, json_body=json)


# ---- 位址守門 -----------------------------------------------------------------------------------


def validate_target(url: str) -> str:
    """回傳規範化的來源（`https://主機`）；不允許時拋出 `TargetRefused`。這個函式不送出任何請求。"""
    parsed = urlsplit(url if "://" in url else f"https://{url}")
    check_target(url, confirm_remote=True)        # 正式環境（主機名稱含 production 或在正式網域清單內）一律拒絕，沒有參數可繞過
    if parsed.scheme != "https":
        raise TargetRefused("拒絕：預備環境必須是 https（HSTS 與 Secure cookie 在 http 上無法驗證）。")
    try:
        _ = parsed.port
    except ValueError:
        raise TargetRefused("目標網址不合法。") from None
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise TargetRefused("目標網址只能是來源（https://主機），不含帳密、路徑、查詢字串與片段。")
    return f"https://{parsed.netloc}"


# ---- 檢查 ---------------------------------------------------------------------------------------


@dataclass
class Outcome:
    status: str
    detail: str = ""


@dataclass
class Context:
    transport: Transport
    allow_model_call: bool
    member: Member
    cookie_log: list[str] = field(default_factory=list)
    signed_in: bool = False
    conversation_open: bool = False


def _get(ctx: Context, path: str) -> HttpResponse:
    response = ctx.transport.request("GET", path)
    ctx.cookie_log.extend(response.set_cookies())
    return response


def check_healthz(ctx: Context) -> Outcome:
    status = _get(ctx, "/healthz").status
    return Outcome(PASS) if status == 200 else Outcome(FAIL, f"狀態碼 {status}（應為 200）")


def check_readyz(ctx: Context) -> Outcome:
    status = _get(ctx, "/readyz").status
    return Outcome(PASS) if status == 200 else Outcome(FAIL, f"狀態碼 {status}（應為 200）")


def check_headers(ctx: Context) -> Outcome:
    response = _get(ctx, "/")
    if response.status != 200:
        return Outcome(FAIL, f"首頁狀態碼 {response.status}（應為 200）")
    missing = [name for name in SECURITY_HEADERS if response.header(name) is None]
    present_csp = [name for name in CSP_HEADERS if response.header(name) is not None]
    problems = []
    if missing:
        problems.append("缺少標頭：" + "、".join(missing))
    if present_csp:
        problems.append("不該有的標頭：" + "、".join(present_csp))
    return Outcome(FAIL, "；".join(problems)) if problems else Outcome(PASS)


def check_csrf(ctx: Context) -> Outcome:
    _get(ctx, "/")                                   # 先取得 CSRF cookie；刻意不帶 X-CSRF-Token 標頭
    response = ctx.transport.request(
        "POST", "/api/auth/login", headers={"Origin": ctx.transport.origin}, json_body={"email": "smoke@loadtest.invalid", "password": "x"},
    )
    ctx.cookie_log.extend(response.set_cookies())
    return Outcome(PASS) if response.status == 403 else Outcome(FAIL, f"沒有權杖的 POST 得到狀態碼 {response.status}（應被拒絕為 403）")


def check_register_and_login(ctx: Context) -> Outcome:
    if not ctx.member.register_and_login():
        return Outcome(FAIL, "註冊或登入失敗（預備環境應關閉信箱驗證，並已建立資料表）")
    ctx.signed_in = True
    return Outcome(PASS)


def check_open_conversation(ctx: Context) -> Outcome:
    if not ctx.signed_in:
        return Outcome(SKIP, "前一項失敗，無法進行")
    if not ctx.member.open_conversation():
        return Outcome(FAIL, "開新對話失敗")
    ctx.conversation_open = True
    return Outcome(PASS)


def _cookie_attributes(line: str) -> tuple[str, set[str], dict[str, str]]:
    name = line.split("=", 1)[0].strip()
    flags: set[str] = set()
    pairs: dict[str, str] = {}
    for part in line.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if value:
            pairs[key.lower()] = value
        else:
            flags.add(key.lower())
    return name, flags, pairs


def check_cookies(ctx: Context) -> Outcome:
    if not ctx.signed_in:
        return Outcome(SKIP, "前一項失敗，無法進行")
    parsed = [_cookie_attributes(line) for line in ctx.cookie_log]
    problems = []
    for base in ("session", "csrf"):
        matching = [cookie for cookie in parsed if cookie[0].removeprefix("__Host-") == base]
        if not matching:
            problems.append(f"沒有收到 {base} cookie")
            continue
        name, flags, pairs = matching[-1]                # 登入會換發權杖，看最後一次發放的
        if not name.startswith("__Host-"):
            problems.append(f"{base} cookie 沒有 __Host- 前綴")
        if "secure" not in flags:
            problems.append(f"{base} cookie 沒有 Secure")
        if "domain" in pairs:
            problems.append(f"{base} cookie 帶有 Domain")
        if pairs.get("path") != "/":
            problems.append(f"{base} cookie 的 Path 不是 /")
        if base == "session" and "httponly" not in flags:
            problems.append("session cookie 沒有 HttpOnly")
    return Outcome(FAIL, "；".join(problems)) if problems else Outcome(PASS)


def check_admin_hidden(ctx: Context) -> Outcome:
    if not ctx.signed_in:
        return Outcome(SKIP, "前一項失敗，無法進行")
    status = ctx.transport.request("GET", "/api/admin/overview").status
    return Outcome(PASS) if status == 404 else Outcome(FAIL, f"一般會員打管理員 API 得到狀態碼 {status}（應為 404，與不存在的端點相同）")


def check_unauthenticated(ctx: Context) -> Outcome:
    status = ctx.transport.fresh().request("GET", "/api/cs/conversations").status
    return Outcome(PASS) if status == 401 else Outcome(FAIL, f"未登入存取得到狀態碼 {status}（應為 401）")


def check_model_reply(ctx: Context) -> Outcome:
    if not ctx.allow_model_call:
        return Outcome(SKIP, "未提供 --allow-model-call，略過模型呼叫（預設不送出任何訊息）")
    if not ctx.conversation_open:
        return Outcome(SKIP, "沒有可用的對話，無法進行")
    reply = ctx.member.send_message()
    if not reply.ok:
        return Outcome(FAIL, f"送出訊息得到狀態碼 {reply.status}")
    if degraded_reply(reply):
        return Outcome(FAIL, "回覆是降級文案（模型暫時性錯誤且備援也失敗）")
    return Outcome(PASS)


CHECKS: tuple[tuple[str, Callable[[Context], Outcome]], ...] = (
    ("存活檢查 /healthz 回 200", check_healthz),
    ("就緒檢查 /readyz 回 200", check_readyz),
    ("首頁帶五個安全標頭且沒有 CSP", check_headers),
    ("沒有 CSRF 權杖的 POST 被拒絕", check_csrf),
    ("註冊（.invalid 測試信箱）並登入", check_register_and_login),
    ("開新對話", check_open_conversation),
    ("session 與 CSRF cookie 使用 __Host- 前綴、Secure、無 Domain，session 為 HttpOnly", check_cookies),
    ("一般會員存取管理員 API 得到 404", check_admin_hidden),
    ("未登入存取需要登入的 API 得到 401", check_unauthenticated),
    ("模型呼叫：送出一則訊息且回覆不是降級文案", check_model_reply),
)


def run_checks(transport: Transport, *, allow_model_call: bool = False) -> list[tuple[str, Outcome]]:
    cookie_log: list[str] = []
    member = Member(SmokeSession(transport, cookie_log), run_id=f"smoke{uuid.uuid4().hex[:8]}", number=1)
    ctx = Context(transport, allow_model_call, member, cookie_log)
    results = []
    for title, check in CHECKS:
        try:
            outcome = check(ctx)
        except Exception as error:  # noqa: BLE001 - 一項檢查出錯不應中斷其餘項目；只回報例外的類別，不回報內容
            outcome = Outcome(FAIL, f"檢查執行時發生例外（{type(error).__name__}）")
        results.append((title, outcome))
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="預備環境冒煙驗收：只對 https 的預備環境執行，輸出只有項目與結果。")
    parser.add_argument("url", help="預備環境的網址（https://主機）")
    parser.add_argument("--allow-model-call", action="store_true", help="同意送出一則訊息並檢查回覆（預設不呼叫模型；會消耗模型額度）")
    return parser


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print, transport_factory: Callable[[str], Transport] = UrllibTransport) -> int:
    args = build_parser().parse_args(argv)
    try:
        origin = validate_target(args.url)
    except TargetRefused as error:
        out(str(error))
        return EXIT_REFUSED
    results = run_checks(transport_factory(origin), allow_model_call=args.allow_model_call)
    for title, outcome in results:
        out(f"{outcome.status}  {title}" + (f"：{outcome.detail}" if outcome.detail else ""))
    counts = {status: sum(1 for _, outcome in results if outcome.status == status) for status in (PASS, FAIL, SKIP)}
    out(f"結果：{counts[PASS]} 項通過、{counts[FAIL]} 項失敗、{counts[SKIP]} 項略過")
    return EXIT_FAILED if counts[FAIL] else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
