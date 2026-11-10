"""`scripts/acceptance_rpg_turn.py` 與 `scripts/acceptance_rpg_real_model.py` 的自我檢查（rpg-turn-engine 任務 6.1、6.2）。

這兩個腳本是人工執行的驗收工具（對本機驗收環境），不在 pytest 裡執行完整旅程；這裡只驗證它們的安全邊界與事件解析：
位址不是本機一律拒絕（腳本會建立玩家、花費遊戲幣，也會呼叫真實模型）、SSE 事件解析正確、`stop_after` 會中途關閉連線。
"""
import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def load(name: str):
    sys.path.insert(0, str(SCRIPTS))                 # real_model 腳本用 `from acceptance_rpg_turn import …`
    try:
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPTS))


turn_script = load("acceptance_rpg_turn")
real_script = load("acceptance_rpg_real_model")
harness = load("rpg_acceptance_harness")


@pytest.mark.parametrize("url", [
    "https://localhost:5010", "http://example.com:5010", "http://10.0.0.5:5010", "http://localhost", "http://localhost:5010/path",
    "http://user:pw@localhost:5010", "http://staging.example.com:80", "ftp://localhost:21",
])
def test_addresses_that_are_not_a_plain_local_http_port_are_refused(url):
    with pytest.raises(turn_script.TargetRefused):
        turn_script.check_local(url, "--base-url")


@pytest.mark.parametrize("url,expected", [("http://localhost:5010", ("localhost", 5010)), ("http://127.0.0.1:5001", ("127.0.0.1", 5001)), ("http://localhost:5010/", ("localhost", 5010))])
def test_local_addresses_are_accepted(url, expected):
    assert turn_script.check_local(url, "--base-url") == expected


@pytest.mark.parametrize("module,args", [
    (turn_script, ["--base-url", "http://example.com:5010"]), (turn_script, ["--helper-url", "https://localhost:5001"]),
    (turn_script, ["--model-url", "http://10.1.2.3:5002"]),
])
def test_the_acceptance_script_refuses_to_run_against_a_remote_address_before_sending_anything(module, args, capsys):
    assert module.main(args) == 2
    assert "拒絕執行" in capsys.readouterr().err


def test_the_real_model_script_refuses_a_remote_address(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["acceptance_rpg_real_model.py", "--base-url", "http://example.com:5011"])
    assert real_script.main() == 2
    assert "拒絕執行" in capsys.readouterr().err


class _EventServer(BaseHTTPRequestHandler):
    """送出一個固定的串流：meta、兩個 token、done；其中一個 data 行故意不是 JSON。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for name, data in (("meta", json.dumps({"replayed": False})), ("token", json.dumps({"text": "你好。"}, ensure_ascii=False)),
                           ("token", "not-json"), ("done", json.dumps({"status": "complete"}))):
            payload = f"event: {name}\ndata: {data}\n\n".encode("utf-8")
            self.wfile.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")


@pytest.fixture
def event_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EventServer)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_the_client_parses_chunked_event_streams_and_reports_malformed_data(event_server):
    turn = turn_script.Client(event_server, "--base-url").stream("/x", {})
    assert turn.status == 200 and turn.names == ["meta", "token", "token", "done"]
    assert turn.of("meta") == [{"replayed": False}] and turn.text == "你好。" and turn.done == {"status": "complete"}
    assert turn.malformed == ["token"] and not turn.aborted


def test_stop_after_closes_the_connection_at_the_first_matching_event(event_server):
    turn = turn_script.Client(event_server, "--base-url").stream("/x", {}, stop_after=lambda name, data: name == "token")
    assert turn.aborted and turn.names == ["meta", "token"] and turn.done is None


# ---- 假模型（rpg_acceptance_harness）：與驗收腳本約定的關鍵字行為 ---------------------------------------------------------

def _action_request(action: str | None, *, stream: bool = True, system: str = "你是敘事者。") -> dict:
    user = "資料。" if action is None else f"資料。\n\n【本次行動】\n{action}\n【/本次行動】"
    return {"model": "m", "stream": stream, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}


def _post(base: str, body: dict) -> bytes:
    import http.client

    connection = http.client.HTTPConnection(base.split("//")[1].split(":")[0], int(base.rsplit(":", 1)[1]), timeout=10)
    connection.request("POST", "/v1/chat/completions", json.dumps(body, ensure_ascii=False).encode("utf-8"), {"Content-Type": "application/json"})
    try:
        return connection.getresponse().read()
    except Exception as error:                     # 模擬上游中途斷線時讀取會失敗（IncompleteRead 等），其餘情況不應發生
        return getattr(error, "partial", b"")
    finally:
        connection.close()


@pytest.fixture
def fake_model(monkeypatch):
    monkeypatch.setattr(harness, "STORY_SENTENCES", ("甲。", "乙。"))
    monkeypatch.setattr(harness, "RPG_ATTEMPTS", {})
    monkeypatch.setattr(harness, "RPG_STATS", {"stream_calls": 0, "state_calls": 0, "upstream_closed": 0})
    monkeypatch.setattr(harness.time, "sleep", lambda seconds: None)                          # 不真的等待
    server = ThreadingHTTPServer(("127.0.0.1", 0), harness.FakeRpgModel)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _texts(raw: bytes) -> list[str]:
    found = []
    for line in raw.decode("utf-8").split("\n"):
        if line.startswith("data: ") and line != "data: [DONE]":
            choice = json.loads(line[6:])["choices"][0]
            found.append(choice["delta"].get("content", ""))
    return [text for text in found if text]


def test_the_fake_model_streams_a_normal_turn_and_ends_with_done_and_counts_the_call(fake_model):
    raw = _post(fake_model, _action_request("正常 驗收 x"))
    assert _texts(raw) == ["甲。", "乙。"] and raw.rstrip().endswith(b"data: [DONE]")
    assert json.loads(_post_status(fake_model))["stream_calls"] == 1


def _post_status(base: str) -> str:
    import urllib.request

    return urllib.request.urlopen(base + "/_status", timeout=5).read().decode("utf-8")


def test_a_blocked_once_action_contains_the_banned_sentence_only_on_the_first_call(fake_model):
    first = _texts(_post(fake_model, _action_request("測試被擋一次 驗收 a")))
    second = _texts(_post(fake_model, _action_request("測試被擋一次 驗收 a")))
    assert harness.BAD_SENTENCE in first and harness.BAD_SENTENCE not in second
    always = [_texts(_post(fake_model, _action_request("測試被擋兩次 驗收 b"))) for _ in range(2)]
    assert all(harness.BAD_SENTENCE in texts for texts in always)


def test_an_interrupt_action_cuts_the_connection_on_the_first_call_and_succeeds_on_the_resend(fake_model):
    first = _post(fake_model, _action_request("測試中斷 驗收 c"))
    second = _post(fake_model, _action_request("測試中斷 驗收 c"))
    assert b"[DONE]" not in first and _texts(first)[:1] == ["甲。"]
    assert b"[DONE]" in second


def test_the_opening_has_no_action_and_the_state_call_follows_the_ending_keyword(fake_model):
    assert _texts(_post(fake_model, _action_request(None))) == list(harness.OPENING_SENTENCES)
    state = _post(fake_model, _action_request("測試結局 驗收 d", stream=False, system="你是文字冒險遊戲的「狀態記錄員」。"))
    assert json.loads(json.loads(state)["choices"][0]["message"]["content"])["ending_proposed"] is True
    review = json.loads(_post(fake_model, _action_request("任何", stream=False)))
    assert json.loads(review["choices"][0]["message"]["content"])["verdict"] == "pass"


def test_the_ending_script_content_validates_as_a_custom_ending_script():
    from app.services.script_spec import validate_script

    result = validate_script(harness.ending_script_content(), max_tier="adult")
    assert result.content["ending_mode"] == "custom"
