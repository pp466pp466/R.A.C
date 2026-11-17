"""scripts/aws_demo_probe_stream.py（task 6.4）：經入口量測串流事件到達時間的探測腳本。

測試不連任何真實位址：判斷函式用合成的時間，端到端用本機的假伺服器（HTTP，緩衝與否由伺服器「何時寫出」模擬，
探測端看到的和被入口緩衝的回應一樣）。輸出必須只有項目與數字，不含 cookie、CSRF 權杖與劇情文字。
"""
import ast
import json
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import pytest

from app.services import rpg_runner_base
from tests.support.scripts import load_script

probe = load_script("aws_demo_probe_stream")

COOKIE_NAME, COOKIE_VALUE = "__Host-session", "SESSION-VALUE-never-printed"
CSRF_TOKEN = "CSRF-TOKEN-never-printed"
STORY = "STORYTEXT-never-printed"
DOMAIN = "rac001.duckdns.org"


# ---- 合成的結果（判斷函式）---------------------------------------------------------------------------------------

def result_with(events, *, ended=None):
    """events 是 (名稱, 到達秒數) 的清單；token 事件帶 10 個字。"""
    return probe.StreamResult(200, {}, 0.01, [(name, at, 10 if name == "token" else 0) for name, at in events], ended)


STREAMING = result_with([("meta", 0.1), ("token", 1.0), ("token", 2.0), ("token", 3.0), ("state", 4.0), ("done", 4.2)])
BUFFERED = result_with([("meta", 4.1), ("token", 4.1), ("token", 4.1), ("state", 4.1), ("done", 4.1)])
TOO_FAST = result_with([("meta", 0.05), ("token", 0.2), ("done", 0.4)])


def test_events_that_arrive_spread_out_mean_the_ingress_is_not_buffering():
    status, detail = probe.judge_buffering(STREAMING)
    assert status == probe.PASS and "早 4.10 秒" in detail


def test_events_that_all_arrive_together_after_a_wait_are_reported_as_buffered():
    status, detail = probe.judge_buffering(BUFFERED)
    assert status == probe.FAIL and "疑似被入口緩衝" in detail and "4.10" in detail


def test_a_response_too_short_to_tell_is_unverified_not_passed():
    status, detail = probe.judge_buffering(TOO_FAST)
    assert status == probe.UNVERIFIED and "來不及分辨" in detail


@pytest.mark.parametrize("events", [[], [("meta", 0.1), ("token", 1.0)], [("meta", 0.1), ("ping", 1.0)]])
def test_without_a_done_event_the_buffering_is_not_judged(events):
    status, detail = probe.judge_buffering(result_with(events))
    assert status == probe.UNVERIFIED and "done" in detail


def test_the_thresholds_are_read_when_judging_so_they_can_be_adjusted(monkeypatch):
    borderline = result_with([("meta", 0.1), ("token", 0.5), ("done", 0.9)])
    assert probe.judge_buffering(borderline)[0] == probe.UNVERIFIED
    monkeypatch.setattr(probe, "MIN_LEAD_SECONDS", 0.5)
    assert probe.judge_buffering(borderline)[0] == probe.PASS
    assert probe.judge_buffering(borderline, min_lead=5.0)[0] == probe.UNVERIFIED


def test_without_any_ping_the_heartbeat_is_unverified_and_the_max_gap_is_reported():
    status, detail = probe.judge_heartbeat(STREAMING)
    assert status == probe.UNVERIFIED and "沒有出現心跳" in detail and "1.00 秒" in detail


def test_pings_with_gaps_inside_the_heartbeat_period_pass():
    result = result_with([("meta", 0.1), ("token", 1.0), ("ping", 15.2), ("ping", 30.1), ("token", 31.0), ("done", 32.0)])
    status, detail = probe.judge_heartbeat(result)
    assert status == probe.PASS and "2 個心跳" in detail and "15.2" in detail


def test_pings_that_leave_a_gap_longer_than_the_period_fail():
    result = result_with([("meta", 0.1), ("ping", 15.2), ("ping", 40.0), ("done", 41.0)])
    status, detail = probe.judge_heartbeat(result)
    assert status == probe.FAIL and "超過心跳週期" in detail


def test_stream_result_metrics():
    assert STREAMING.first_event_at == 0.1 and STREAMING.first_token_at == 1.0 and STREAMING.done_at == 4.2
    assert STREAMING.token_chars == 30 and STREAMING.names[0] == "meta" and STREAMING.names[-1] == "done"
    assert STREAMING.max_gap == pytest.approx(1.0) and BUFFERED.max_gap == pytest.approx(4.1), "第一個間隔從送出請求算起"
    empty = probe.StreamResult(0, {})
    assert empty.first_event_at is None and empty.done_at is None and empty.first_token_at is None and empty.max_gap == 0


def test_the_ping_period_is_the_one_the_application_uses():
    assert probe.SERVER_PING_SECONDS == rpg_runner_base.PING_SECONDS


# ---- 位址與參數守門 --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://rac001.duckdns.org", "rac001.duckdns.org", "https://rac001.duckdns.org/game", "https://rac001.duckdns.org/?a=1",
    "https://user:pass@rac001.duckdns.org", "https://rac001.duckdns.org#x", "https://production.example.com", "https://", "https://x:99999",
])
def test_unacceptable_targets_are_refused_before_any_client_is_created(url):
    lines = []

    def never(*args, **kwargs):
        raise AssertionError("不該建立連線")

    code = probe.main([url, "--allow-model-call"], out=lines.append, environ={probe.SESSION_COOKIE_ENV: f"{COOKIE_NAME}={COOKIE_VALUE}"}, client_factory=never)
    assert code == probe.EXIT_REFUSED and lines and COOKIE_VALUE not in " ".join(lines)


@pytest.mark.parametrize("address", ["10.0.0.5", "192.168.1.1", "localhost", "0.0.0.0", "rac001.duckdns.org", "::2"])
def test_the_connect_address_must_be_a_loopback_address(address):
    code = probe.main([f"https://{DOMAIN}", "--allow-model-call", "--connect-address", address], out=lambda line: None,
                      environ={probe.SESSION_COOKIE_ENV: f"{COOKIE_NAME}={COOKIE_VALUE}"}, client_factory=lambda *a, **k: pytest.fail("不該建立連線"))
    assert code == probe.EXIT_REFUSED


def test_without_allow_model_call_nothing_is_sent():
    lines = []
    code = probe.main([f"https://{DOMAIN}"], out=lines.append, environ={probe.SESSION_COOKIE_ENV: f"{COOKIE_NAME}={COOKIE_VALUE}"},
                      client_factory=lambda *a, **k: pytest.fail("不該建立連線"))
    assert code == probe.EXIT_REFUSED and "--allow-model-call" in lines[0]


@pytest.mark.parametrize("raw", [None, "", "  ", "no-equals", "=value", "name=", "bad name=value", "a;b=c"])
def test_a_missing_or_malformed_cookie_is_refused_without_echoing_it(raw):
    lines = []
    environ = {} if raw is None else {probe.SESSION_COOKIE_ENV: raw}
    code = probe.main([f"https://{DOMAIN}", "--allow-model-call"], out=lines.append, environ=environ, client_factory=lambda *a, **k: pytest.fail("不該建立連線"))
    assert code == probe.EXIT_REFUSED and lines and (not raw or raw not in " ".join(lines))


def test_the_cookie_is_read_only_from_the_environment_and_there_is_no_cookie_argument():
    parser = probe.build_parser()
    options = {option for action in parser._actions for option in action.option_strings}
    assert not any("cookie" in option and option != "--connect-address" for option in options)
    assert probe.parse_session_cookie(f" {COOKIE_NAME}={COOKIE_VALUE} ") == (COOKIE_NAME, COOKIE_VALUE)
    assert probe.validate_target("https://RAC001.duckdns.org") == (DOMAIN, 443) and probe.validate_target("https://rac001.duckdns.org:8443") == (DOMAIN, 8443)


# ---- 連線：回送位址加展示網域的 TLS ----------------------------------------------------------------------------------

def test_the_connect_address_changes_only_where_the_socket_goes_not_the_name_used_for_tls_and_host(monkeypatch):
    seen = {}

    class FakeContext:
        def wrap_socket(self, sock, server_hostname=None):
            seen["server_hostname"] = server_hostname
            return sock

    def fake_connect(address, timeout=None):
        seen["address"] = address
        seen["timeout"] = timeout
        return socket.socket()

    monkeypatch.setattr(probe.socket, "create_connection", fake_connect)
    connection = probe._TlsConnection(DOMAIN, 443, address="127.0.0.1", timeout=7, context=FakeContext())
    connection.connect()
    connection.sock.close()
    assert seen == {"address": ("127.0.0.1", 443), "timeout": 7, "server_hostname": DOMAIN}
    assert connection.host == DOMAIN, "Host 標頭與憑證比對用展示網域"


def test_a_real_tls_context_verifies_certificates_so_there_is_no_way_to_skip_verification():
    source = open(probe.__file__, encoding="utf-8-sig").read()
    assert "ssl.create_default_context()" in source
    for forbidden in ("CERT_NONE", "check_hostname = False", "_create_unverified_context", "verify=False"):
        assert forbidden not in source, forbidden
    context = ssl.create_default_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


def test_the_origin_omits_the_default_port_and_keeps_others():
    assert probe.Client(DOMAIN).origin == f"https://{DOMAIN}"
    assert probe.Client(DOMAIN, 8443).origin == f"https://{DOMAIN}:8443"
    assert probe.Client("127.0.0.1", 5000, tls=False).origin == "http://127.0.0.1:5000"


# ---- 端到端：本機假伺服器 ------------------------------------------------------------------------------------------

class Scenario:
    """`steps` 是 (寫出之前先等的秒數, 事件名稱, 資料)；`hold` 為真時全部累積到最後一次寫出（模擬被緩衝）；`cut_after` 在寫出這麼多個事件後關閉連線。"""

    def __init__(self, steps, *, hold=False, cut_after=None, stream_status=200, headers=None):
        self.steps, self.hold, self.cut_after, self.stream_status = steps, hold, cut_after, stream_status
        self.headers = {"Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"} if headers is None else headers


def event_bytes(name, data):
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8")


class Server:
    def __init__(self, scenario, *, valid_session=True, with_anchor=True, start_status=201):
        self.scenario, self.valid_session, self.with_anchor, self.start_status = scenario, valid_session, with_anchor, start_status
        self.requests = []         # (方法, 路徑, 標頭, 本文)
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _body(self):
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length).decode("utf-8")) if length else None

            def _json(self, status, body, extra=()):
                payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                for name, value in extra:
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(payload)

            def _signed_in(self):
                return f"{COOKIE_NAME}={COOKIE_VALUE}" in (self.headers.get("Cookie") or "") and server.valid_session

            def do_GET(self):
                server.requests.append(("GET", self.path, dict(self.headers), None))
                if self.path == probe.CSRF_PAGE:
                    html = f'<html><head><meta name="csrf-token" content="{CSRF_TOKEN}"></head></html>'.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(html)))
                    self.send_header("Set-Cookie", f"csrf={CSRF_TOKEN}; Path=/; HttpOnly")
                    self.end_headers()
                    self.wfile.write(html)
                elif not self._signed_in():
                    self._json(401, {"ok": False})
                elif self.path.startswith("/api/sessions/catalog"):
                    self._json(200, {"ok": True, "scripts": [{"id": "v-1", "title": "T"}]})
                elif self.path == "/api/sessions/setup/v-1":
                    self._json(200, {"ok": True, "characters": [{"name": "阿青"}] if server.with_anchor else []})
                else:
                    self._json(404, {"ok": False})

            def do_POST(self):
                body = self._body()
                server.requests.append(("POST", self.path, dict(self.headers), body))
                cookies = self.headers.get("Cookie") or ""
                origin_ok = self.headers.get("Origin") == f"http://127.0.0.1:{server.port}"
                csrf_ok = self.headers.get("X-CSRF-Token") == CSRF_TOKEN and f"csrf={CSRF_TOKEN}" in cookies
                if not (self._signed_in() and origin_ok and csrf_ok):
                    return self._json(403, {"ok": False})
                if self.path == "/api/sessions":
                    return self._json(server.start_status, {"ok": True, "session": {"id": "s-1"}})
                if self.path == "/api/game/sessions/s-1/opening/stream":
                    return self._stream()
                self._json(404, {"ok": False})

            def _stream(self):
                scenario = server.scenario
                if scenario.stream_status != 200:
                    return self._json(scenario.stream_status, {"ok": False})
                self.send_response(200)
                for name, value in scenario.headers.items():
                    self.send_header(name, value)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                pending, sent = [], 0
                for wait, name, data in scenario.steps:
                    time.sleep(wait)
                    pending.append(event_bytes(name, data))
                    if scenario.hold:
                        continue
                    self._chunk(pending.pop())
                    sent += 1
                    if scenario.cut_after is not None and sent >= scenario.cut_after:
                        self.close_connection = True
                        return
                for chunk in pending:
                    self._chunk(chunk)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

            def _chunk(self, payload):
                self.wfile.write(f"{len(payload):x}\r\n".encode("ascii") + payload + b"\r\n")
                self.wfile.flush()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def story_steps(*, gap=0.25, tokens=4, pings=0):
    steps = [(0, "meta", {"kind": "opening"})]
    for index in range(tokens):
        steps.append((gap, "token", {"text": STORY}))
        if index < pings:
            steps.append((0, "ping", {}))
    steps.append((gap, "done", {"opening_state": "ready"}))
    return steps


SHORT_THRESHOLDS = dict(MIN_LEAD_SECONDS=0.6, MAX_BUFFERED_LEAD_SECONDS=0.2, MIN_TOTAL_SECONDS=0.6, SERVER_PING_SECONDS=0.5, PING_TOLERANCE_SECONDS=0.3)


def run_probe(server, *, extra_args=(), session_cookie=f"{COOKIE_NAME}={COOKIE_VALUE}"):
    """端到端測試用較短的門檻（只在這次執行期間），免得每個情境都要等好幾秒；純函式的測試用預設門檻。"""
    lines = []
    port = server.port

    def factory(host, tls_port, *, address, cookie):
        assert (host, tls_port) == (DOMAIN, 443) and address is None
        return probe.Client("127.0.0.1", port, tls=False, cookie=cookie)

    with mock.patch.multiple(probe, **SHORT_THRESHOLDS):
        code = probe.main([f"https://{DOMAIN}", "--allow-model-call", *extra_args], out=lines.append,
                          environ={probe.SESSION_COOKIE_ENV: session_cookie}, client_factory=factory)
    return code, lines


def status_of(lines, fragment):
    line = next(line for line in lines if fragment in line)
    return line.split("  ", 1)[0]


def test_a_streaming_response_passes_and_reports_only_names_and_numbers():
    with Server(Scenario(story_steps())) as server:
        code, lines = run_probe(server)
    text = "\n".join(lines)
    assert code == probe.EXIT_OK, text
    for name in ("取得 CSRF 權杖", "劇本清單可讀", "創角資料可讀", "開測試存檔", "串流回應標頭", "事件序列", "入口沒有緩衝串流"):
        assert status_of(lines, name) == probe.PASS, (name, text)
    assert status_of(lines, "心跳經入口即時到達") == probe.UNVERIFIED, "沒有任何心跳時不能算通過"
    assert lines[-1] == "結果：7 項通過、0 項失敗、1 項未驗證"
    for secret in (COOKIE_VALUE, CSRF_TOKEN, STORY, "s-1", "v-1", "阿青"):
        assert secret not in text, f"輸出不得含 {secret}"
    assert "事件 6 個（token 4、ping 0）" in text and f"文字共 {4 * len(STORY)} 字" in text


def test_the_probe_sends_the_cookie_the_csrf_token_and_the_origin_the_application_requires():
    with Server(Scenario(story_steps(tokens=1, gap=0.7))) as server:
        code, _ = run_probe(server)
        posts = [request for request in server.requests if request[0] == "POST"]
    assert code == probe.EXIT_OK
    assert [path for _, path, _, _ in posts] == ["/api/sessions", "/api/game/sessions/s-1/opening/stream"]
    for _, _, headers, _ in posts:
        assert headers["Origin"] == f"http://127.0.0.1:{server.port}" and headers["X-CSRF-Token"] == CSRF_TOKEN
        assert f"{COOKIE_NAME}={COOKIE_VALUE}" in headers["Cookie"] and f"csrf={CSRF_TOKEN}" in headers["Cookie"]
    body = posts[0][3]
    assert body["version_id"] == "v-1" and body["protagonist"] == probe.PROTAGONIST
    assert body["anchor"] == {"kind": "character", "ref": "阿青", "text": probe.ANCHOR_TEXT} and "skip_anchor" not in body
    assert posts[1][3] == {}, "第 0 幕沒有本文欄位"


def test_a_script_without_a_contactable_character_opens_the_save_without_an_anchor():
    with Server(Scenario(story_steps(tokens=1, gap=0.7)), with_anchor=False) as server:
        code, _ = run_probe(server)
        body = next(request[3] for request in server.requests if request[1] == "/api/sessions" and request[0] == "POST")
    assert code == probe.EXIT_OK and body.get("skip_anchor") is True and "anchor" not in body


def test_a_response_held_until_the_end_is_reported_as_buffered_and_fails_the_run():
    steps = story_steps(gap=0.3, tokens=3)
    with Server(Scenario(steps, hold=True)) as server:
        code, lines = run_probe(server)
    assert code == probe.EXIT_FAILED
    assert status_of(lines, "入口沒有緩衝串流") == probe.FAIL and "疑似被入口緩衝" in "\n".join(lines)
    assert status_of(lines, "事件序列") == probe.PASS, "事件都收到了，只是同時到達"


def test_a_very_short_response_cannot_tell_buffering_and_is_unverified_without_failing_the_run():
    with Server(Scenario([(0, "meta", {}), (0, "token", {"text": STORY}), (0, "done", {})])) as server:
        code, lines = run_probe(server)
    assert code == probe.EXIT_OK and status_of(lines, "入口沒有緩衝串流") == probe.UNVERIFIED


def test_pings_arriving_on_time_make_the_heartbeat_check_pass():
    steps = [(0, "meta", {}), (0.35, "ping", {}), (0.35, "ping", {}), (0.35, "token", {"text": STORY}), (0.2, "done", {})]
    with Server(Scenario(steps)) as server:
        code, lines = run_probe(server)
    assert code == probe.EXIT_OK and status_of(lines, "心跳經入口即時到達") == probe.PASS
    assert "收到 2 個心跳" in "\n".join(lines)


def test_a_long_silence_between_events_fails_the_heartbeat_check():
    steps = [(0, "meta", {}), (0.2, "ping", {}), (1.2, "token", {"text": STORY}), (0.2, "done", {})]
    with Server(Scenario(steps)) as server:
        code, lines = run_probe(server)
    assert code == probe.EXIT_FAILED and status_of(lines, "心跳經入口即時到達") == probe.FAIL


@pytest.mark.parametrize("steps,why", [
    ([(0, "meta", {}), (0.3, "token", {"text": STORY}), (0.3, "error", {"code": "x"})], "以 error 結束"),
    ([(0, "token", {"text": STORY}), (0.3, "done", {})], "沒有以 meta 開頭"),
    ([(0, "meta", {}), (0.3, "token", {"text": STORY})], "沒有 done"),
    ([(0, "meta", {}), (0.3, "error", {"code": "x"}), (0.3, "done", {})], "error 在中間、之後仍有 done"),
])
def test_an_incomplete_or_failed_event_sequence_fails_the_sequence_check(steps, why):
    with Server(Scenario(steps)) as server:
        code, lines = run_probe(server)
    assert code == probe.EXIT_FAILED and status_of(lines, "事件序列") == probe.FAIL, why


def test_a_connection_cut_in_the_middle_fails_the_sequence_and_leaves_buffering_unverified():
    with Server(Scenario(story_steps(), cut_after=2)) as server:
        code, lines = run_probe(server)
    assert code == probe.EXIT_FAILED
    assert status_of(lines, "事件序列") == probe.FAIL and status_of(lines, "入口沒有緩衝串流") == probe.UNVERIFIED


def test_a_non_event_stream_answer_fails_the_header_check_and_the_run():
    with Server(Scenario([], stream_status=503)) as server:
        code, lines = run_probe(server)
    assert code == probe.EXIT_FAILED and status_of(lines, "串流回應標頭") == probe.FAIL and "狀態碼 503" in "\n".join(lines)


STREAM_HEADERS = {"Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}


@pytest.mark.parametrize("name,replacement", [
    ("X-Accel-Buffering", None), ("X-Accel-Buffering", "yes"), ("Cache-Control", None), ("Cache-Control", "no-cache"), ("Cache-Control", "no-transform"),
    ("Cache-Control", "max-age=60"), ("Content-Encoding", "gzip"),
])
def test_a_stream_response_missing_a_no_buffering_header_or_compressed_fails_the_header_check(name, replacement):
    headers = {key: value for key, value in STREAM_HEADERS.items() if key != name}
    if replacement is not None:
        headers[name] = replacement
    with Server(Scenario(story_steps(tokens=1, gap=0.7), headers=headers)) as server:
        code, lines = run_probe(server)
    assert code == probe.EXIT_FAILED and status_of(lines, "串流回應標頭") == probe.FAIL


@pytest.mark.parametrize("status", [200, 202, 400, 409])
def test_opening_the_save_must_answer_201_even_when_the_body_carries_a_session_id(status):
    with Server(Scenario(story_steps()), start_status=status) as server:
        code, lines = run_probe(server)
        paths = [path for _, path, _, _ in server.requests]
    assert code == probe.EXIT_FAILED and status_of(lines, "開測試存檔") == probe.FAIL
    assert not any("stream" in path for path in paths), "開存檔沒成功就不送串流請求"


def test_a_rejected_login_cookie_stops_before_any_stream_request():
    with Server(Scenario(story_steps()), valid_session=False) as server:
        code, lines = run_probe(server)
        paths = [path for _, path, _, _ in server.requests]
    assert code == probe.EXIT_FAILED and status_of(lines, "劇本清單") == probe.FAIL
    assert not any("stream" in path for path in paths) and not any(method == "POST" for method, _, _, _ in server.requests)
    assert COOKIE_VALUE not in "\n".join(lines)


def test_an_unreachable_server_is_reported_as_a_failed_stream_not_an_exception():
    with Server(Scenario(story_steps())) as server:
        port = server.port
    client = probe.Client("127.0.0.1", port, tls=False)
    result = client.stream("/x", {}, idle_timeout=2)
    assert result.status == 0 and result.error and "無法取得回應" in result.error and not result.events


# ---- 腳本本身 ------------------------------------------------------------------------------------------------------

def test_the_script_uses_only_the_standard_library_so_it_can_run_in_a_one_off_container():
    tree = ast.parse(open(probe.__file__, encoding="utf-8-sig").read())
    modules = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    modules |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert modules and modules <= set(__import__("sys").stdlib_module_names), modules - set(__import__("sys").stdlib_module_names)


def test_the_script_never_writes_files_or_deletes_or_prints_credentials():
    source = open(probe.__file__, encoding="utf-8-sig").read()
    for forbidden in ("open(", "os.remove", "unlink", "shutil", "subprocess", '"DELETE"', "'DELETE'"):
        assert forbidden not in source, forbidden
    assert "print(self.cookies" not in source and "{client.cookies" not in source and "{self.csrf" not in source and "{client.csrf" not in source
