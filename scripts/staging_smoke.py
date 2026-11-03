"""預備環境冒煙驗收（staging-verification 規格「冒煙驗收腳本」與「腳本只對預備環境執行」）。

    python scripts/staging_smoke.py https://<預備環境網址> [--allow-model-call]

對一個預備環境網址依序檢查，逐項輸出「通過」、「失敗」、「略過」或「未驗證」（結束代碼：沒有失敗為 0，有失敗為 1，位址被拒絕為 2）：
1. `/healthz` 與 `/readyz` 回 200；
2. 首頁帶有五個安全標頭（含 HSTS）且**沒有** `Content-Security-Policy`（刻意不送，見營運文件）；
3. 沒有 CSRF 權杖的 POST 被拒絕（403）；
4. 已移除的 `POST /api/auth/login` 與 `/api/auth/register` 得到 404（玩家只用 Google 登入）；
5. 未登入存取需要登入的 API 得到 401；
6. **需要登入的項目**（帶測試玩家的 cookie 進入、開新對話、session 與 CSRF cookie 的屬性、一般會員存取管理員 API 得到 404）：
   玩家只用 Google 登入，沒有註冊與密碼登入可用，所以只在環境變數 `SMOKE_SESSION_COOKIE`（`<cookie 名稱>=<值>`，由
   `scripts/mint_test_sessions.py` 在預備環境內簽出）存在時執行；**沒有提供時這些項目標為「未驗證」**，不是通過；
7. **預設不呼叫任何模型**（標為略過）；加 `--allow-model-call` 才送出一則訊息，並檢查回覆不是降級文案。

- **只對 https 的預備環境執行**：主機名稱含 `production` 或位於 `loadtest/guard.py` 列出的正式網域時一律拒絕，**沒有任何確認參數可以繞過**；
  非 https、帶路徑、帳密、查詢字串或片段的位址也拒絕，而且是在送出任何請求之前。
- 輸出**只有項目名稱與結果**：不含 cookie、權杖、密碼或回應內容。失敗的原因只說明是哪個標頭、狀態碼或屬性。
- 測試玩家的信箱屬於保留的 `.invalid` 網域（RFC 2606），不會寄出信件也不屬於任何真人；cookie 是有效的登入憑證，只從環境變數讀取，
  不接受命令列參數、不輸出、不寫入日誌。
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
import os
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from loadtest.actions import CSRF_META, Member, Reply, degraded_reply  # noqa: E402 - 需要先把專案根目錄放進 sys.path
from loadtest.guard import TargetRefused, check_target  # noqa: E402

PASS, FAIL, SKIP, UNVERIFIED = "通過", "失敗", "略過", "未驗證"
SESSION_COOKIE_ENV = "SMOKE_SESSION_COOKIE"
SECURITY_HEADERS = (
    "Strict-Transport-Security", "X-Content-Type-Options", "Referrer-Policy", "X-Frame-Options", "Permissions-Policy",
)
CSP_HEADERS = ("Content-Security-Policy", "Content-Security-Policy-Report-Only")
NGROK_MARKERS = ("ngrok",)
REQUEST_TIMEOUT_SECONDS = 20

EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2
NO_SESSION_DETAIL = f"沒有提供測試玩家的登入 cookie（環境變數 {SESSION_COOKIE_ENV}；以 scripts/mint_test_sessions.py 在預備環境內簽出）"


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

    def add_cookie(self, name: str, value: str) -> None: ...

    def fresh(self) -> "Transport": ...


class UrllibTransport:
    """標準函式庫的實作：每個實例有自己的 cookie jar（`fresh()` 給「未登入」的檢查用）。連線失敗回狀態碼 0。"""

    def __init__(self, base_url: str) -> None:
        self.origin = base_url.rstrip("/")
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))
        host = (urlsplit(self.origin).hostname or "").lower()
        self._extra = {"ngrok-skip-browser-warning": "1"} if any(marker in host for marker in NGROK_MARKERS) else {}

    def fresh(self) -> "UrllibTransport":
        return UrllibTransport(self.origin)

    def add_cookie(self, name: str, value: str) -> None:
        """放進這個實例的 cookie jar（之後的請求照常帶上，也和伺服器發的 CSRF cookie 並存）。"""
        parts = urlsplit(self.origin)
        secure = parts.scheme == "https"                 # __Host- 前綴的 cookie 是 Secure：只在 https 上送出（預備環境一律是 https）
        self._jar.set_cookie(http.cookiejar.Cookie(
            0, name, value, None, False, parts.hostname or "", True, False, "/", True, secure, int(time.time()) + 3600, False, None, None, {},
        ))

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
    """把 `Transport` 包成 `loadtest.actions` 的 `Session`，讓虛擬會員的登入（帶 cookie）與開對話動作可以直接重用。
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

    def set_cookie(self, name: str, value: str) -> None:
        self.transport.add_cookie(name, value)

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
    session_cookie: tuple[str, str] | None = None
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
    response = ctx.transport.request("POST", "/api/auth/logout", headers={"Origin": ctx.transport.origin}, json_body={})
    ctx.cookie_log.extend(response.set_cookies())
    return Outcome(PASS) if response.status == 403 else Outcome(FAIL, f"沒有權杖的 POST 得到狀態碼 {response.status}（應被拒絕為 403）")


REMOVED_ENDPOINTS = ("/api/auth/login", "/api/auth/register")


def check_removed_endpoints(ctx: Context) -> Outcome:
    """玩家只用 Google 登入：舊的密碼登入與註冊端點必須不存在（帶著有效的 CSRF 權杖與同源 Origin，才不會被 CSRF 檢查的 403 蓋過 404）。"""
    transport = ctx.transport.fresh()
    page = transport.request("GET", "/terms")
    match = CSRF_META.search(page.body.decode("utf-8", errors="replace"))
    if page.status != 200 or not match:
        return Outcome(FAIL, f"取不到 CSRF 權杖（/terms 狀態碼 {page.status}）")
    headers = {"Origin": transport.origin, "X-CSRF-Token": match.group(1)}
    wrong = [f"{path} 得到 {status}" for path in REMOVED_ENDPOINTS
             for status in [transport.request("POST", path, headers=headers, json_body={"email": "smoke@loadtest.invalid", "password": "x"}).status]
             if status != 404]
    return Outcome(FAIL, "；".join(wrong) + "（應為 404）") if wrong else Outcome(PASS)


def _needs_session(ctx: Context) -> Outcome | None:
    """需要登入的項目共同的前置：沒有提供 cookie 是「未驗證」，有提供但進不去是前一項的失敗而「略過」。"""
    if ctx.session_cookie is None:
        return Outcome(UNVERIFIED, NO_SESSION_DETAIL)
    if not ctx.signed_in:
        return Outcome(SKIP, "前一項失敗，無法進行")
    return None


def check_sign_in(ctx: Context) -> Outcome:
    if ctx.session_cookie is None:
        return Outcome(UNVERIFIED, NO_SESSION_DETAIL)
    name, value = ctx.session_cookie
    if not ctx.member.sign_in(name, value):
        return Outcome(FAIL, "帶著測試玩家的登入 cookie 進入失敗（cookie 無效、已過期，或不是這個環境簽出的）")
    ctx.signed_in = True
    return Outcome(PASS)


def check_open_conversation(ctx: Context) -> Outcome:
    if (blocked := _needs_session(ctx)) is not None:
        return blocked
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
    if (blocked := _needs_session(ctx)) is not None:
        return blocked
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
    if (blocked := _needs_session(ctx)) is not None:
        return blocked
    status = ctx.transport.request("GET", "/api/admin/overview").status
    return Outcome(PASS) if status == 404 else Outcome(FAIL, f"一般會員打管理員 API 得到狀態碼 {status}（應為 404，與不存在的端點相同）")


def check_unauthenticated(ctx: Context) -> Outcome:
    status = ctx.transport.fresh().request("GET", "/api/cs/conversations").status
    return Outcome(PASS) if status == 401 else Outcome(FAIL, f"未登入存取得到狀態碼 {status}（應為 401）")


def check_model_reply(ctx: Context) -> Outcome:
    if not ctx.allow_model_call:
        return Outcome(SKIP, "未提供 --allow-model-call，略過模型呼叫（預設不送出任何訊息）")
    if ctx.session_cookie is None:
        return Outcome(UNVERIFIED, NO_SESSION_DETAIL)
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
    ("已移除的密碼登入與註冊端點得到 404", check_removed_endpoints),
    ("帶測試玩家的登入 cookie 進入", check_sign_in),
    ("開新對話", check_open_conversation),
    ("session 與 CSRF cookie 使用 __Host- 前綴、Secure、無 Domain，session 為 HttpOnly", check_cookies),
    ("一般會員存取管理員 API 得到 404", check_admin_hidden),
    ("未登入存取需要登入的 API 得到 401", check_unauthenticated),
    ("模型呼叫：送出一則訊息且回覆不是降級文案", check_model_reply),
)


def run_checks(
    transport: Transport, *, allow_model_call: bool = False, session_cookie: tuple[str, str] | None = None
) -> list[tuple[str, Outcome]]:
    cookie_log: list[str] = []
    member = Member(SmokeSession(transport, cookie_log), run_id=f"smoke{uuid.uuid4().hex[:8]}", number=1)
    ctx = Context(transport, allow_model_call, member, session_cookie, cookie_log)
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


def parse_session_cookie(raw: str | None) -> tuple[str, str] | None:
    """`<名稱>=<值>`；沒有設定回傳 None，格式不對拋出 `ValueError`（訊息不含值）。"""
    if not raw or not raw.strip():
        return None
    name, separator, value = raw.strip().partition("=")
    if not separator or not name or not value or not name.replace("-", "").replace("_", "").isalnum():
        raise ValueError(f"環境變數 {SESSION_COOKIE_ENV} 的格式必須是 <cookie 名稱>=<值>。")
    return name, value


def main(
    argv: list[str] | None = None, *, out: Callable[[str], None] = print,
    transport_factory: Callable[[str], Transport] = UrllibTransport, environ: Mapping[str, str] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        origin = validate_target(args.url)
        session_cookie = parse_session_cookie((os.environ if environ is None else environ).get(SESSION_COOKIE_ENV))
    except TargetRefused as error:
        out(str(error))
        return EXIT_REFUSED
    except ValueError as error:
        out(str(error))
        return EXIT_REFUSED
    results = run_checks(transport_factory(origin), allow_model_call=args.allow_model_call, session_cookie=session_cookie)
    for title, outcome in results:
        out(f"{outcome.status}  {title}" + (f"：{outcome.detail}" if outcome.detail else ""))
    counts = {status: sum(1 for _, outcome in results if outcome.status == status) for status in (PASS, FAIL, SKIP, UNVERIFIED)}
    summary = f"結果：{counts[PASS]} 項通過、{counts[FAIL]} 項失敗、{counts[SKIP]} 項略過"
    out(summary + (f"、{counts[UNVERIFIED]} 項未驗證" if counts[UNVERIFIED] else ""))
    return EXIT_FAILED if counts[FAIL] else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
