"""`scripts/acceptance_rpg_turn.py`、`scripts/acceptance_rpg_real_model.py` 與 `scripts/acceptance_rpg_notebook.py` 的自我檢查
（rpg-turn-engine 任務 6.1、6.2；rpg-notebook 任務 5.1）。

這幾個腳本是人工執行的驗收工具（對本機驗收環境），不在 pytest 裡執行完整旅程；這裡只驗證它們的安全邊界與事件解析：
位址不是本機一律拒絕（腳本會建立玩家、花費遊戲幣，也會呼叫真實模型）、SSE 事件解析正確、`stop_after` 會中途關閉連線；
以及假模型（`rpg_acceptance_harness`）的關鍵字行為——記事簿抓取的條目用**真正的**提示詞組出請求、再經過**真正的**合併與長度規則，
所以假模型與應用程式的約定（系統指令的角色名稱、精簡重試的區塊標題、容量與預備空間）一旦分歧，這裡就會失敗。
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
notebook_script = load("acceptance_rpg_notebook")
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
    (notebook_script, ["--base-url", "http://example.com:5010"]), (notebook_script, ["--helper-url", "https://localhost:5001"]),
    (notebook_script, ["--model-url", "http://10.1.2.3:5002"]),
])
def test_the_acceptance_script_refuses_to_run_against_a_remote_address_before_sending_anything(module, args, capsys):
    assert module.main(args) == 2
    assert "拒絕執行" in capsys.readouterr().err


def test_the_notebook_script_refuses_an_unknown_case_name_before_sending_anything(capsys):
    assert notebook_script.main(["--only", "basic,nonsense"]) == 2
    assert "nonsense" in capsys.readouterr().err


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
    monkeypatch.setattr(harness, "RPG_STATS", {"stream_calls": 0, "state_calls": 0, "capture_calls": 0, "upstream_closed": 0})
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


# ---- 假模型的記事簿抓取（rpg-notebook 任務 5.1）--------------------------------------------------------------------------

def _window(keyword: str = "", *, first: int = 1) -> dict[int, str]:
    """視窗內 10 輪的玩家行動；關鍵字放在視窗的第 2 輪（驗收腳本也是這樣放）。"""
    return {first + index: (f"{keyword} 動作 {first + index}" if index == 1 else f"動作 {first + index}") for index in range(10)}


def _capture_request(actions: dict[int, str], *, condense: bool = False) -> dict:
    """用應用程式**真正的**提示詞組出抓取呼叫（系統指令與使用者內容）：假模型認得的角色名稱與區塊標題若與應用程式分歧，測試會失敗。"""
    from app.services import rpg_notebook_prompt as prompt
    from app.services.rpg_context import HistoryTurn

    turns = [HistoryTurn(number, "complete", action, "劇情。") for number, action in actions.items()]
    system = prompt.build_capture_system("standard", "zh-Hant-TW", "測試標記-abc")
    if condense:
        contents = prompt.build_condense_contents(notebook="", capacity=2000, turns=turns, previous_entries=["上次寫得太長的條目。"], room=500)
    else:
        contents = prompt.build_capture_contents(notebook="", capacity=2000, turns=turns)
    return {"model": "m", "stream": False, "messages": [{"role": "system", "content": system}, {"role": "user", "content": "\n".join(contents[0].parts)}]}


def _capture(base: str, actions: dict[int, str], *, condense: bool = False) -> list[str]:
    reply = json.loads(_post(base, _capture_request(actions, condense=condense)))
    return json.loads(reply["choices"][0]["message"]["content"])["entries"]


def _no_block(entries: list[str]) -> bool:
    from app.services import rpg_output_rules

    previous = None
    for entry in entries:
        outcome = rpg_output_rules.evaluate([entry], previous, tier="standard", characters=())
        if outcome.blocked:
            return False
        previous = entry
    return True


def test_the_fake_model_recognises_the_real_capture_prompt_and_answers_with_three_short_entries(fake_model):
    entries = _capture(fake_model, _window())
    assert len(entries) == 3 and all(entry.startswith("第 1 至 10 輪：") and 0 < len(entry) <= 50 for entry in entries)
    status = json.loads(_post_status(fake_model))
    assert status["capture_calls"] == 1 and status["state_calls"] == 0 and status["stream_calls"] == 0
    assert _no_block(entries)                                   # 預設條目一定通過真正的規則層


def test_the_capture_system_prompt_is_not_mistaken_for_the_state_recorder():
    from app.services import rpg_notebook_prompt as prompt

    system = prompt.build_capture_system("standard", "zh-Hant-TW", "m")
    assert harness.NOTEBOOK_SYSTEM_MARKER in system and "狀態記錄員" not in system


def test_entries_of_different_windows_are_distinct_so_the_second_capture_is_not_deduplicated(fake_model):
    first, second = _capture(fake_model, _window()), _capture(fake_model, _window(first=11))
    assert not set(first) & set(second) and all(entry.startswith("第 11 至 20 輪：") for entry in second)


def test_the_over_case_fills_the_free_notebook_without_exceeding_the_ai_limit(fake_model):
    from app.services import notebook_policy

    entries = _capture(fake_model, _window(harness.KW_NOTEBOOK_OVER))
    merged = notebook_policy.merge_entries("", entries)
    assert len(merged.entries) == 5 and all(len(entry) == harness.NOTEBOOK_OVER_ENTRY_CHARS for entry in merged.entries)
    assert merged.length == 2204                               # 5 × 440 ＋ 4 個換行（驗收腳本與說明寫的數字）
    assert notebook_policy.is_full(merged.length, "free") and merged.length <= notebook_policy.ai_limit_for("free")
    assert _no_block(entries)


def test_the_abandon_case_exceeds_the_ai_limit_in_the_first_call_and_in_the_condense_retry(fake_model):
    from app.services import notebook_policy

    for condense in (False, True):
        entries = _capture(fake_model, _window(harness.KW_NOTEBOOK_ABANDON), condense=condense)
        merged = notebook_policy.merge_entries("", entries)
        assert merged.length == 2604 > notebook_policy.ai_limit_for("free")
        assert _no_block(entries)                              # 是長度被放棄，不是規則層
    assert json.loads(_post_status(fake_model))["capture_calls"] == 2


def test_the_violation_case_adds_the_banned_sentence_which_the_real_rule_layer_blocks(fake_model):
    from app.services import rpg_output_rules

    entries = _capture(fake_model, _window(harness.KW_NOTEBOOK_VIOLATION))
    assert harness.BAD_SENTENCE in entries and len(entries) == 4 and _no_block(entries[:-1])
    assert rpg_output_rules.evaluate([harness.BAD_SENTENCE], entries[-2], tier="standard", characters=()).blocked


def test_the_slow_case_waits_before_replying_and_the_other_cases_do_not(fake_model, monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(harness.time, "sleep", waits.append)
    _capture(fake_model, _window())
    assert waits == []
    _capture(fake_model, _window(harness.KW_NOTEBOOK_SLOW))
    assert waits == [harness.NOTEBOOK_SLOW_SECONDS]


def test_the_notebook_keywords_cannot_be_mistaken_for_the_turn_keywords():
    """動作文字裡的關鍵字是子字串比對：記事簿的關鍵字不得包含（或被包含於）回合的關鍵字，否則一個動作會同時觸發兩種行為。"""
    turn_keywords = ("測試被擋一次", "測試被擋兩次", "測試中斷", "測試慢速", "測試結局")
    notebook_keywords = (harness.KW_NOTEBOOK_OVER, harness.KW_NOTEBOOK_ABANDON, harness.KW_NOTEBOOK_VIOLATION, harness.KW_NOTEBOOK_SLOW)
    for notebook in notebook_keywords:
        assert not any(turn in notebook or notebook in turn for turn in turn_keywords)
    assert len(set(notebook_keywords)) == 4


def test_the_script_and_the_harness_agree_on_keywords_numbers_and_the_application_limits():
    from app.services import notebook_policy, rpg_notebook_prompt

    assert (notebook_script.KW_SLOW, notebook_script.KW_OVER, notebook_script.KW_ABANDON, notebook_script.KW_VIOLATION) == (
        harness.KW_NOTEBOOK_SLOW, harness.KW_NOTEBOOK_OVER, harness.KW_NOTEBOOK_ABANDON, harness.KW_NOTEBOOK_VIOLATION)
    assert notebook_script.SLOW_SECONDS == harness.NOTEBOOK_SLOW_SECONDS
    assert harness.NOTEBOOK_FILLER.startswith(notebook_script.FILLER)
    assert notebook_script.DEFAULT_ENTRIES == len(harness.NOTEBOOK_DEFAULT_ENTRIES)
    assert (notebook_script.CAPACITY, notebook_script.RESERVE, notebook_script.WINDOW) == (
        notebook_policy.capacity_for("free"), notebook_policy.reserve_for("free"), notebook_policy.WINDOW_TURNS)
    assert harness.NOTEBOOK_CONDENSE_LABEL == rpg_notebook_prompt.LABEL_PREVIOUS
    assert notebook_script.BAD_SENTENCE == harness.BAD_SENTENCE and notebook_script.TURN_COST == 30


# ---- 小幫手補給付費幣（?coins=N）------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path,expected", [
    ("/fresh/rpg", 0), ("/fresh/rpg?coins=700", 700), ("/fresh/rpg?coins=999999", harness.FRESH_COINS_MAX), ("/fresh/rpg?coins=-5", 0),
    ("/fresh/rpg?coins=abc", 0), ("/fresh/rpg?coins=", 0), ("/fresh/rpg?other=1&coins=30", 30),
])
def test_the_helper_url_coins_parameter_is_clamped_and_defaults_to_zero(path, expected):
    assert harness.coins_from_path(path) == expected


@pytest.fixture()
def player_app():
    from app import create_app
    from app.config import load_config
    from app.models import Base
    from tests.support.auth import FakeClock
    from tests.support.config_env import AUTH_ENV
    from tests.test_llm_providers_by_purpose import BOTH

    app = create_app(load_config(environ={"APP_ENV": "testing", **BOTH, **AUTH_ENV}), clock=FakeClock())
    engine = app.extensions["db_engine"]
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield app
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_a_fresh_player_gets_paid_coins_only_when_asked_and_the_signup_grant_still_waits_for_the_age_check(player_app):
    from sqlalchemy import select

    from app.models.user import User
    from app.repositories.wallet_repository import WalletRepository

    harness.fresh_player(player_app)
    cookie, _ = harness.fresh_player(player_app, coins=700)
    with player_app.extensions["db_session_factory"]() as session:
        plain, rich = (WalletRepository(session).state(user.id) for user in session.scalars(select(User).order_by(User.id)))
    assert plain is None                                        # 不要求補幣：還沒有錢包，行為與先前完全相同
    assert (rich.free_balance, rich.paid_balance) == (0, 700)  # 首登的 200 免費幣在年齡確認通過時才發放
    assert cookie.startswith(player_app.config["SESSION_COOKIE_NAME"] + "=")
