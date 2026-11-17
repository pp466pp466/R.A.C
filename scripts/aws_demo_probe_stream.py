"""AWS 展示環境的串流探測（aws-demo-environment 的 task 6.4）：經入口量測 RPG 串流的事件到達時間，判斷入口有沒有緩衝。

    SMOKE_SESSION_COOKIE=<cookie 名稱>=<值> python scripts/aws_demo_probe_stream.py https://<展示網域> --allow-model-call
    ... --connect-address 127.0.0.1        # 在主機內執行時：仍以展示網域建立 TLS 與 Host，但連到本機的入口，不依賴主機連自己的 Elastic IP

做的事（只送一則最小的請求，沒有任何破壞性操作）：取得 CSRF 權杖 → 讀劇本清單與創角資料 → 開一個測試存檔（`POST /api/sessions`）→
`POST /api/game/sessions/<id>/opening/stream`（第 0 幕，不消耗遊戲幣）並記錄每個事件到達的時間，逐項輸出「通過」、「失敗」或「未驗證」
（結束代碼：沒有失敗為 0，有失敗為 1，位址或參數被拒絕為 2）。

- **會呼叫模型**：開存檔的模型層審查與第 0 幕都用 RPG 的模型供應商，所以必須明確加 `--allow-model-call`；沒有它就在送出任何請求之前拒絕。
- **只對 https 的展示環境執行**：主機名稱含 `production`、非 https、帶帳密、路徑、查詢字串或片段的位址一律拒絕，沒有參數可以繞過；
  `--connect-address` 只接受本機回送位址（cookie 不會被送到別的位址）。
- **判斷「有沒有被緩衝」的依據**：第 0 幕的第一個事件（`meta`）在模型開始產生之前就送出，`done` 在最後。入口即時轉送時，第一個事件比 `done` 早到好幾秒；
  入口緩衝整段回應時，所有事件幾乎同時到達。回應太短（來不及分辨）時標為「未驗證」，不當成通過。
- **心跳**：伺服器在沒有其他輸出時每 15 秒送一個 `ping`。模型持續有輸出時不會出現心跳，這一項就標為「未驗證」；有出現時檢查最大的事件間隔沒有超過心跳週期。
- **限制**：用標準函式庫的 HTTP/1.1 連線（瀏覽器是 HTTP/2；Caddy 對事件串流的轉送行為與協定無關，但這裡沒有量測 HTTP/2）。
- 輸出**只有項目名稱、數字與結果**：不含 cookie、權杖、劇情文字或任何回應內容。cookie 只從環境變數 `SMOKE_SESSION_COOKIE` 讀取
  （`<名稱>=<值>`，由 `scripts/mint_test_sessions.py` 在目標環境內簽出的測試玩家；與 `scripts/staging_smoke.py` 相同的慣例）。
- 測試玩家與測試存檔留在目標環境（信箱屬於保留的 `.invalid` 網域），需要時由操作者另外清除；這個腳本不刪除任何東西。
- 只用標準函式庫（可以放進映像的一次性容器執行）；輸出含繁體中文，容器內請設 `PYTHONIOENCODING=utf-8`。
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import socket
import ssl
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

PASS, FAIL, UNVERIFIED = "通過", "失敗", "未驗證"
EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2
SESSION_COOKIE_ENV = "SMOKE_SESSION_COOKIE"
CSRF_META = re.compile(r'<meta\s+name="csrf-token"\s+content="([^"]+)"')
CSRF_PAGE = "/terms"                       # 公開頁面：帶有 CSRF 權杖的 meta 與 cookie
LOOPBACK_ADDRESSES = frozenset({"127.0.0.1", "::1"})

SERVER_PING_SECONDS = 15.0                 # 與 app/services/rpg_runner_base.py 的 PING_SECONDS 相同（測試比對）；沒有其他輸出時每隔這麼久一個 ping
PING_TOLERANCE_SECONDS = 2.0               # 事件間隔容許超過心跳週期的量（網路與排程的抖動）
MIN_LEAD_SECONDS = 1.0                     # 第一個事件至少比 done 早到這麼久，才判定入口沒有緩衝
MAX_BUFFERED_LEAD_SECONDS = 0.25           # 第一個事件與 done 相差不到這麼久、且整段回應不短，判定為被緩衝
MIN_TOTAL_SECONDS = 1.0                    # 整段回應短於這個時間，來不及分辨有沒有緩衝
IDLE_TIMEOUT_SECONDS = 60.0                # 連線上超過這麼久沒有任何位元組就放棄（心跳每 15 秒一次，正常不會發生）
REQUEST_TIMEOUT_SECONDS = 90.0             # 開存檔的模型層審查最壞約 30 秒（劇本尚未審查過時再加一次）

PROTAGONIST = {"name": "林舟", "personality": "沉默愛思考", "age": 19, "appearance": "高瘦，常穿灰衣"}
ANCHOR_TEXT = "他是我少年時的舊識。"


class Refused(Exception):
    """位址或參數不被允許。訊息可以直接顯示。"""


# ---- 位址與參數守門（在送出任何請求之前）------------------------------------------------------------------------

def validate_target(url: str) -> tuple[str, int]:
    """回傳（主機名稱, 埠）。只接受 `https://主機[:埠]`。"""
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise Refused("拒絕：展示環境必須是 https。")
    try:
        port = parsed.port
    except ValueError:
        raise Refused("目標網址不合法。") from None
    name = (parsed.hostname or "").lower()
    if not name or "production" in name:
        raise Refused("拒絕：不得對正式環境或不合法的位址執行。")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise Refused("目標網址只能是來源（https://主機），不含帳密、路徑、查詢字串與片段。")
    return name, port or 443


def validate_connect_address(address: str | None) -> str | None:
    if address is not None and address not in LOOPBACK_ADDRESSES:
        raise Refused("--connect-address 只接受本機回送位址（127.0.0.1 或 ::1）：登入 cookie 不會被送到別的位址。")
    return address


def parse_session_cookie(raw: str | None) -> tuple[str, str]:
    """`<名稱>=<值>`；沒有設定或格式不對拋出 `Refused`（訊息不含值）。"""
    if not raw or not raw.strip():
        raise Refused(f"沒有提供測試玩家的登入 cookie（環境變數 {SESSION_COOKIE_ENV}，格式 <cookie 名稱>=<值>；由 scripts/mint_test_sessions.py 在目標環境內簽出）。")
    name, separator, value = raw.strip().partition("=")
    if not separator or not value or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise Refused(f"環境變數 {SESSION_COOKIE_ENV} 的格式必須是 <cookie 名稱>=<值>。")
    return name, value


# ---- HTTP -----------------------------------------------------------------------------------------------------

class _TlsConnection(http.client.HTTPSConnection):
    """連到 `address`（回送位址）、但 TLS 的 SNI 與憑證比對、Host 標頭都用展示網域：憑證仍要真的簽給展示網域、被系統憑證庫信任。"""

    def __init__(self, host: str, port: int, *, address: str, timeout: float, context: ssl.SSLContext) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._address = address
        self._tls_context = context

    def connect(self) -> None:
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._tls_context.wrap_socket(sock, server_hostname=self.host)


@dataclass
class Reply:
    status: int
    headers: dict[str, str]
    text: str

    @property
    def json(self) -> dict[str, Any]:
        try:
            value = json.loads(self.text)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}


@dataclass
class StreamResult:
    """一次串流請求的結果。時間都是距送出請求的秒數；事件只記名稱、到達時間與文字長度（不保存任何文字）。"""

    status: int
    headers: dict[str, str]
    headers_at: float = 0.0
    events: list[tuple[str, float, int]] = field(default_factory=list)
    ended_at: float | None = None
    error: str | None = None
    malformed: int = 0

    def times(self, name: str) -> list[float]:
        return [at for event, at, _ in self.events if event == name]

    @property
    def names(self) -> list[str]:
        return [event for event, _, _ in self.events]

    @property
    def first_event_at(self) -> float | None:
        return self.events[0][1] if self.events else None

    @property
    def done_at(self) -> float | None:
        done = self.times("done")
        return done[0] if done else None

    @property
    def first_token_at(self) -> float | None:
        tokens = self.times("token")
        return tokens[0] if tokens else None

    @property
    def token_chars(self) -> int:
        return sum(size for event, _, size in self.events if event == "token")

    @property
    def max_gap(self) -> float:
        """相鄰兩次收到事件的最大間隔（第一個間隔從送出請求算起）。"""
        previous, largest = 0.0, 0.0
        for _, at, _ in self.events:
            largest, previous = max(largest, at - previous), at
        return largest


class Client:
    def __init__(self, host: str, port: int = 443, *, tls: bool = True, address: str | None = None, cookie: tuple[str, str] | None = None) -> None:
        self.host, self.port, self.tls, self.address = host, port, tls, address
        self.cookies: dict[str, str] = {cookie[0]: cookie[1]} if cookie else {}
        self.csrf: str | None = None

    @property
    def origin(self) -> str:
        default = 443 if self.tls else 80
        return f"{'https' if self.tls else 'http'}://{self.host}" + ("" if self.port == default else f":{self.port}")

    def _connect(self, timeout: float) -> http.client.HTTPConnection:
        if self.tls:
            context = ssl.create_default_context()
            if self.address is not None:
                return _TlsConnection(self.host, self.port, address=self.address, timeout=timeout, context=context)
            return http.client.HTTPSConnection(self.host, self.port, timeout=timeout, context=context)
        return http.client.HTTPConnection(self.address or self.host, self.port, timeout=timeout)

    def _send(self, method: str, path: str, body: dict | None, timeout: float) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse, float]:
        headers = {"Accept": "application/json, text/event-stream", "User-Agent": "aws-demo-probe-stream/1"}
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{name}={value}" for name, value in self.cookies.items())
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if method != "GET":
            headers["Origin"] = self.origin
            if self.csrf:
                headers["X-CSRF-Token"] = self.csrf
        connection = self._connect(timeout)
        started = time.monotonic()
        try:
            connection.request(method, path, payload, headers)
            response = connection.getresponse()
        except BaseException:
            connection.close()
            raise
        for line in response.headers.get_all("Set-Cookie") or []:
            name, _, rest = line.split(";")[0].partition("=")
            if "max-age=0" in line.lower():
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = rest
        return connection, response, started

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = REQUEST_TIMEOUT_SECONDS) -> Reply:
        connection, response, _ = self._send(method, path, body, timeout)
        try:
            return Reply(response.status, {k.lower(): v for k, v in response.getheaders()}, response.read().decode("utf-8", "replace"))
        finally:
            connection.close()

    def load_csrf(self) -> bool:
        match = CSRF_META.search(self.call("GET", CSRF_PAGE).text)
        self.csrf = match.group(1) if match else None
        return self.csrf is not None

    def stream(self, path: str, body: dict, *, idle_timeout: float = IDLE_TIMEOUT_SECONDS) -> StreamResult:
        """送出串流請求並逐行讀取事件。逾時、連線中斷與格式錯誤記在結果裡，不拋出。"""
        result = StreamResult(0, {})
        try:
            connection, response, started = self._send("POST", path, body, idle_timeout)
        except (OSError, http.client.HTTPException) as error:
            result.error = f"無法取得回應（{type(error).__name__}）"
            return result
        result.status = response.status
        result.headers = {k.lower(): v for k, v in response.getheaders()}
        result.headers_at = time.monotonic() - started
        try:
            if "text/event-stream" not in result.headers.get("content-type", ""):
                response.read()
                return result
            name, lines = None, []
            while True:
                raw = response.readline()
                if not raw:
                    result.ended_at = time.monotonic() - started
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("event: "):
                    name = line[7:]
                elif line.startswith("data: "):
                    lines.append(line[6:])
                elif line == "":
                    if name is None and not lines:
                        continue
                    size = 0
                    try:
                        data = json.loads("".join(lines))
                        size = len(str(data.get("text", ""))) if isinstance(data, dict) else 0
                    except ValueError:
                        result.malformed += 1
                    result.events.append((name or "?", time.monotonic() - started, size))
                    name, lines = None, []
                elif not line.startswith(":"):
                    result.malformed += 1
        except (OSError, http.client.HTTPException) as error:
            result.error = f"串流中斷（{type(error).__name__}）"
        finally:
            connection.close()
        return result


# ---- 判斷（純函式）----------------------------------------------------------------------------------------------

def judge_buffering(result: StreamResult, *, min_lead: float | None = None, max_buffered_lead: float | None = None,
                    min_total: float | None = None) -> tuple[str, str]:
    """第一個事件比 `done` 早到多久，決定入口有沒有緩衝。沒有 `done`（串流沒有正常結束）時不判斷。門檻預設取模組常數（執行時才讀，測試可以調整）。"""
    min_lead = MIN_LEAD_SECONDS if min_lead is None else min_lead
    max_buffered_lead = MAX_BUFFERED_LEAD_SECONDS if max_buffered_lead is None else max_buffered_lead
    min_total = MIN_TOTAL_SECONDS if min_total is None else min_total
    first, done = result.first_event_at, result.done_at
    if first is None or done is None:
        return UNVERIFIED, "沒有收到完整的事件序列（缺 done），無法判斷"
    lead = done - first
    if lead >= min_lead:
        return PASS, f"第一個事件比 done 早 {lead:.2f} 秒到達（done 在 {done:.2f} 秒）"
    if done >= min_total and lead <= max_buffered_lead:
        return FAIL, f"整段回應等了 {done:.2f} 秒才一次送達，第一個事件只比 done 早 {lead:.2f} 秒（疑似被入口緩衝）"
    return UNVERIFIED, f"整段回應只有 {done:.2f} 秒、第一個事件比 done 早 {lead:.2f} 秒，來不及分辨有沒有被緩衝"


def judge_heartbeat(result: StreamResult, *, ping_seconds: float | None = None, tolerance: float | None = None) -> tuple[str, str]:
    ping_seconds = SERVER_PING_SECONDS if ping_seconds is None else ping_seconds
    tolerance = PING_TOLERANCE_SECONDS if tolerance is None else tolerance
    pings = result.times("ping")
    limit = ping_seconds + tolerance
    if not pings:
        return UNVERIFIED, (f"整段回應沒有出現心跳（模型持續有輸出，最大事件間隔 {result.max_gap:.2f} 秒）；入口是否即時轉送心跳沒有驗證")
    if result.max_gap > limit:
        return FAIL, f"收到 {len(pings)} 個心跳，但最大事件間隔 {result.max_gap:.2f} 秒超過心跳週期 {ping_seconds:.0f} 秒加容許 {tolerance:.0f} 秒（疑似被入口緩衝）"
    return PASS, f"收到 {len(pings)} 個心跳（到達於 {', '.join(f'{at:.1f}' for at in pings[:5])} 秒），最大事件間隔 {result.max_gap:.2f} 秒"


# ---- 探測 -----------------------------------------------------------------------------------------------------

class Report:
    def __init__(self, out: Callable[[str], None]) -> None:
        self.out = out
        self.counts = {PASS: 0, FAIL: 0, UNVERIFIED: 0}

    def result(self, name: str, status: str, detail: str = "") -> bool:
        self.counts[status] += 1
        self.out(f"{status}  {name}" + (f"：{detail}" if detail else ""))
        return status == PASS

    def note(self, text: str) -> None:
        self.out(f"      {text}")


def _json_list(reply: Reply, key: str) -> list[dict[str, Any]]:
    value = reply.json.get(key)
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def open_save(client: Client, report: Report) -> str | None:
    """取得 CSRF、挑第一份可開存檔的劇本、創角並開存檔；回傳存檔識別碼。任何一步失敗都回報後回傳 None。"""
    if not report.result("取得 CSRF 權杖", PASS if client.load_csrf() else FAIL, "" if client.csrf else f"{CSRF_PAGE} 沒有 csrf-token"):
        return None
    catalog = client.call("GET", "/api/sessions/catalog")
    scripts = _json_list(catalog, "scripts")
    if not report.result("劇本清單可讀且至少有一份可開存檔的劇本", PASS if catalog.status == 200 and scripts else FAIL, f"狀態碼 {catalog.status}、{len(scripts)} 份"):
        return None
    version = scripts[0].get("id")
    setup = client.call("GET", f"/api/sessions/setup/{version}")
    characters = _json_list(setup, "characters")
    if not report.result("創角資料可讀", PASS if setup.status == 200 else FAIL, f"狀態碼 {setup.status}"):
        return None
    body: dict[str, Any] = {"version_id": version, "protagonist": PROTAGONIST}
    if characters and isinstance(characters[0].get("name"), str):
        body["anchor"] = {"kind": "character", "ref": characters[0]["name"], "text": ANCHOR_TEXT}
    else:
        body["skip_anchor"] = True
    started = client.call("POST", "/api/sessions", body, timeout=REQUEST_TIMEOUT_SECONDS)
    session = started.json.get("session")
    save = session.get("id") if isinstance(session, dict) else None
    if not report.result("開測試存檔（POST /api/sessions）回 201", PASS if started.status == 201 and isinstance(save, str) else FAIL, f"狀態碼 {started.status}"):
        return None
    return save


def check_headers(result: StreamResult, report: Report) -> None:
    headers = result.headers
    ok = (result.status == 200 and "text/event-stream" in headers.get("content-type", "") and headers.get("x-accel-buffering") == "no"
          and "no-cache" in headers.get("cache-control", "") and "no-transform" in headers.get("cache-control", "") and "content-encoding" not in headers)
    report.result("串流回應標頭：200、text/event-stream、no-cache、no-transform、X-Accel-Buffering: no、沒有壓縮", PASS if ok else FAIL,
                  "" if ok else f"狀態碼 {result.status}、標頭 {sorted(k for k in headers if k in ('content-type', 'cache-control', 'x-accel-buffering', 'content-encoding'))}")


def check_sequence(result: StreamResult, report: Report) -> bool:
    names = result.names
    ok = bool(names) and names[0] == "meta" and names[-1] == "done" and "error" not in names and result.malformed == 0 and result.error is None
    report.result("事件序列：meta 開頭、done 結尾、沒有 error 與格式錯誤", PASS if ok else FAIL,
                  "" if ok else f"事件 {names[:3]}…{names[-2:]}（共 {len(names)} 個）、格式錯誤 {result.malformed}、{result.error or '串流正常結束'}")
    return ok


def summarize(result: StreamResult, report: Report) -> None:
    def seconds(value: float | None) -> str:
        return "無" if value is None else f"{value:.2f} 秒"

    report.note(f"回應標頭 {seconds(result.headers_at)}、第一個事件 {seconds(result.first_event_at)}、第一個 token {seconds(result.first_token_at)}、"
                f"done {seconds(result.done_at)}、連線結束 {seconds(result.ended_at)}")
    report.note(f"事件 {len(result.events)} 個（token {len(result.times('token'))}、ping {len(result.times('ping'))}）、"
                f"文字共 {result.token_chars} 字、最大事件間隔 {result.max_gap:.2f} 秒")


def run_probe(client: Client, report: Report) -> int:
    save = open_save(client, report)
    if save is None:
        return EXIT_FAILED
    result = client.stream(f"/api/game/sessions/{save}/opening/stream", {})
    check_headers(result, report)
    check_sequence(result, report)
    summarize(result, report)
    report.result("入口沒有緩衝串流（第一個事件遠早於 done）", *judge_buffering(result))
    report.result("心跳經入口即時到達", *judge_heartbeat(result))
    return EXIT_FAILED if report.counts[FAIL] else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="展示環境的串流探測：經入口量測第 0 幕串流的事件到達時間（會呼叫模型，只輸出項目與數字）。")
    parser.add_argument("url", help="展示環境的網址（https://主機）")
    parser.add_argument("--allow-model-call", action="store_true", help="同意開一個測試存檔並產生第 0 幕（會消耗模型額度；沒有它就不送出任何請求）")
    parser.add_argument("--connect-address", metavar="位址", help="連到這個回送位址（127.0.0.1 或 ::1）而不是解析網域；TLS 與 Host 仍用展示網域")
    return parser


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print, environ: Mapping[str, str] | None = None,
         client_factory: Callable[..., Client] = Client) -> int:
    args = build_parser().parse_args(argv)
    try:
        host, port = validate_target(args.url)
        address = validate_connect_address(args.connect_address)
        if not args.allow_model_call:
            raise Refused("拒絕：這個探測會開存檔並呼叫 RPG 模型，必須明確加上 --allow-model-call。")
        cookie = parse_session_cookie((os.environ if environ is None else environ).get(SESSION_COOKIE_ENV))
    except Refused as error:
        out(str(error))
        return EXIT_REFUSED
    report = Report(out)
    report.note(f"目標 https://{host}" + ("" if port == 443 else f":{port}") + (f"（連到 {address}）" if address else "") + "；會呼叫模型")
    code = run_probe(client_factory(host, port, address=address, cookie=cookie), report)
    out(f"結果：{report.counts[PASS]} 項通過、{report.counts[FAIL]} 項失敗、{report.counts[UNVERIFIED]} 項未驗證")
    return code


if __name__ == "__main__":
    sys.exit(main())
