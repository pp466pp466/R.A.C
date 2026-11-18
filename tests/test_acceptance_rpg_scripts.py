"""`scripts/acceptance_rpg_turn.py`、`scripts/acceptance_rpg_real_model.py`、`scripts/acceptance_rpg_notebook.py` 與
`scripts/acceptance_rpg_revision.py` 的自我檢查（rpg-turn-engine 任務 6.1、6.2；rpg-notebook 任務 5.1；rpg-turn-revision 任務 5.2）。

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
    with player_app.extensions["db_session_factory"]() as session:
        for user in session.scalars(select(User)):               # 與真實的 Google 註冊相同：已聲明年滿 18 歲、尚未年齡確認（adult-only-signup）
            assert user.adult_declared_at is not None and user.adult_declaration_version
            assert user.birth_date is None and user.age_deadline_at is not None


# ---- 結束回應的時序（eof 案例，rpg-notebook 任務 5.2）、容器驗收腳本與真實模型腳本（任務 5.2、5.3）-----------------------------

def test_the_client_records_when_the_server_ends_the_response_and_not_when_the_client_leaves(event_server):
    client = turn_script.Client(event_server, "--base-url")
    finished = client.stream("/x", {})
    assert finished.ended_at is not None and finished.ended_at >= finished.first_at("done")
    left = client.stream("/x", {}, stop_after=lambda name, data: name == "token")
    assert left.aborted and left.ended_at is None                      # 客戶端自己離開不算「伺服器結束了回應」


def _run_eof_case(monkeypatch, *, gap, at_eof, assert_eof, ended=True):
    """以替身跑 eof 案例：第 11 輪的 done 在 1.0 秒，伺服器在 done 之後 `gap` 秒結束回應，結束當下的記事簿是 `at_eof`。"""
    report = turn_script.Report()
    case = notebook_script.NotebookAcceptance("http://localhost:5010", "http://localhost:5001", None, report, notebook_script.ServerLog(None), assert_eof=assert_eof)
    turn = turn_script.Turn(200, {}, events=[("done", {"status": "complete", "turn_number": 11}, 1.0)], ended_at=1.0 + gap if ended else None)
    final = {"text": "a\nb\nc", "version": 2, "length": 5, "capacity": 2000, "full": False}
    states = [at_eof, final]
    monkeypatch.setattr(case, "fresh", lambda coins: notebook_script.Player(None, {}, coins))
    monkeypatch.setattr(case, "start_save", lambda player: "save")
    monkeypatch.setattr(case, "play_turns", lambda *args, **kwargs: True)
    monkeypatch.setattr(case, "play", lambda *args, **kwargs: turn)
    monkeypatch.setattr(case, "notebook", lambda player, save: states.pop(0))
    monkeypatch.setattr(case, "wait_until", lambda predicate, **kwargs: True)
    monkeypatch.setattr(case, "wait_for_capture", lambda *args, **kwargs: None)
    monkeypatch.setattr(case, "ledger_is_clean", lambda *args, **kwargs: None)
    monkeypatch.setattr(case, "log_checks", lambda *args, **kwargs: None)
    case.case_eof()
    return report


EMPTY_AT_EOF = {"text": "", "version": 1, "length": 0}
FILLED_AT_EOF = {"text": "a\nb\nc", "version": 2, "length": 5}


def test_the_eof_case_passes_when_the_closing_chunk_comes_first_and_the_notebook_is_still_empty(monkeypatch, capsys):
    report = _run_eof_case(monkeypatch, gap=0.05, at_eof=EMPTY_AT_EOF, assert_eof=True)
    assert report.count(notebook_script.FAIL) == 0
    assert any("結束 chunk 先送出" in name for name, _ in report.results) and any("關閉回呼在結束 chunk 之後才執行" in name for name, _ in report.results)
    assert "實測：done 在 1.00 秒、結束回應在 1.05 秒" in capsys.readouterr().out          # 容器驗收把這行搬進驗收輸出


def test_the_eof_case_fails_when_the_response_ends_only_after_the_callback_has_run(monkeypatch):
    """間隔太長與結束當下記事簿已經有內容，各是一項失敗——這正是 D1 的前提不成立時的樣子。"""
    report = _run_eof_case(monkeypatch, gap=5.2, at_eof=FILLED_AT_EOF, assert_eof=True)
    failed = [name for name, status in report.results if status == notebook_script.FAIL]
    assert len(failed) == 2 and any("間隔" in name for name in failed) and any("記事簿還是空的" in name for name in failed)


def test_the_eof_case_without_the_flag_only_records_the_numbers(monkeypatch, capsys):
    """開發伺服器的行為不當作通過條件：同樣的壞時序，沒有 --assert-eof 時不失敗，只印一行資訊。"""
    report = _run_eof_case(monkeypatch, gap=5.2, at_eof=FILLED_AT_EOF, assert_eof=False)
    assert report.count(notebook_script.FAIL) == 0
    assert not any("結束 chunk" in name for name, _ in report.results)
    assert "沒有 --assert-eof，不計入通過或失敗" in capsys.readouterr().out


def test_the_eof_case_fails_when_the_server_never_ended_the_response(monkeypatch):
    report = _run_eof_case(monkeypatch, gap=0, at_eof=EMPTY_AT_EOF, assert_eof=True, ended=False)
    assert any(status == notebook_script.FAIL and "伺服器結束了回應" in name for name, status in report.results)


def test_the_eof_case_is_part_of_the_default_run_and_the_flag_is_accepted(capsys):
    assert notebook_script.CASES[-1] == "eof" and notebook_script.EOF_LIMIT_SECONDS == 1.0
    assert notebook_script.main(["--assert-eof", "--only", "nonsense"]) == 2          # 旗標有被解析：回的是「未知的案例」，不是 argparse 的用法錯誤
    assert "nonsense" in capsys.readouterr().err


# ---- scripts/verify_container.py（容器驗收）---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def container_script():
    import runpy

    return runpy.run_path(str(SCRIPTS / "verify_container.py"))


def test_the_container_verifier_runs_the_notebook_script_with_the_timing_assertion(container_script):
    source = (SCRIPTS / "verify_container.py").read_text(encoding="utf-8-sig")
    for needed in ("acceptance_rpg_notebook.py", "--assert-eof", "rpg.notebook_captured", "rpg.notebook_abandoned", "rpg.notebook_saved", "rpg.notebook_failed"):
        assert needed in source, needed
    assert container_script["NOTEBOOK_MIN_CHECKS"] >= 60


def test_the_container_leak_scan_covers_the_notebook_texts_that_the_fake_model_and_the_script_produce(container_script):
    leaks = container_script["RPG_LEAKS"]
    for text in (harness.NOTEBOOK_DEFAULT_ENTRIES[0], harness.NOTEBOOK_FILLER, notebook_script.EDIT_LINE):
        assert any(leak in text for leak in leaks), text


def _judge(container_script, output, minimum):
    results = container_script["results"]
    results.clear()
    container_script["judge_acceptance_output"](output, "X", minimum)
    return list(results)


def test_the_container_verifier_accepts_a_clean_output_and_ignores_unverified_log_items(container_script):
    output = "\n".join([f"通過  項目 {i}" for i in range(5)] + ["未驗證  基本案例：日誌的記事簿事件  — 未提供 --server-log", "", "摘要：通過 5、失敗 0、未驗證 1（1.0 秒）"])
    results = _judge(container_script, output, 5)
    assert [(name, ok) for name, ok, _ in results] == [("[RPG 串流池] X在容器內全部通過（5 項）", True)]


@pytest.mark.parametrize("output,minimum", [
    ("通過  a\n通過  b\n\n摘要：通過 2、失敗 0、未驗證 0（1.0 秒）", 3),                                 # 通過的項目比最低要求少
    ("通過  a\n失敗  壞掉的項目  — 原因\n\n摘要：通過 1、失敗 1、未驗證 0（1.0 秒）", 1),               # 有失敗
    ("通過  a\n未驗證  連不上假模型  — 原因\n\n摘要：通過 1、失敗 0、未驗證 1（1.0 秒）", 1),           # 與日誌無關的未驗證算失敗
    ("通過  a\n通過  b", 2),                                                                              # 沒有摘要行（腳本中途死掉）
])
def test_the_container_verifier_fails_on_too_few_checks_failures_non_log_unverified_and_a_missing_summary(container_script, output, minimum):
    results = _judge(container_script, output, minimum)
    assert results and results[-1][1] is False and "全部通過" in results[-1][0]


def test_a_failed_item_is_reported_with_its_own_title(container_script):
    results = _judge(container_script, "失敗  壞掉的項目  — 原因說明\n摘要：通過 0、失敗 1、未驗證 0（1.0 秒）", 1)
    assert results[0][:2] == ("[RPG 串流池] X：壞掉的項目", False) and "原因說明" in results[0][2]


# ---- scripts/acceptance_rpg_real_model.py（任務 5.3）-------------------------------------------------------------------------

def test_the_real_model_plan_tops_up_coins_only_for_the_notebook_run():
    actions, coins = real_script.plan(False)
    assert actions == real_script.ACTIONS[:3] and coins == 0                       # 預設行為與先前相同：3 輪、不補幣
    actions, coins = real_script.plan(True)
    assert len(actions) == 11 and coins == 11 * turn_script.TURN_COST == 330     # 首登的 200 幣只夠 6 輪


def test_the_real_model_actions_are_distinct_harmless_and_free_of_the_fake_models_keywords():
    actions = real_script.ACTIONS
    assert len(set(actions)) == len(actions)
    assert actions[0] == "我向守門的弟子問路，想知道宗門大測是什麼時候。"                    # rpg-turn-engine 的前 3 句不變
    assert not any("測試" in action for action in actions)                       # 真實模型不需要、也不該帶假模型的關鍵字


def test_the_real_model_script_and_the_application_agree_on_the_prompt_numbers_and_the_window():
    from app.services import notebook_policy, rpg_notebook_prompt

    assert (real_script.PROMPT_ENTRIES, real_script.PROMPT_ENTRY_CHARS) == (rpg_notebook_prompt.MAX_ENTRIES_PER_CAPTURE, rpg_notebook_prompt.MAX_ENTRY_CHARS)
    assert real_script.NOTEBOOK_TURNS == notebook_policy.WINDOW_TURNS + 1          # 完成 10 輪之後的下一輪（第 11 輪）才觸發第一次抓取


class _FakeApp:
    def __init__(self, notebooks):
        self.notebooks = list(notebooks)

    def call(self, method, path, body=None, timeout=60):
        import types

        value = self.notebooks.pop(0) if len(self.notebooks) > 1 else self.notebooks[0]
        return types.SimpleNamespace(json={"notebook": value})


def test_the_notebook_observation_polls_until_content_appears_and_reports_counts_and_overlong_entries(monkeypatch, capsys):
    monkeypatch.setattr(real_script.time, "sleep", lambda seconds: None)
    long_entry = "第 1 至 10 輪：" + "字" * 60
    text = "\n".join(["第 1 至 10 輪：主角答應送信。", "第 1 至 10 輪：主角買了乾糧。", long_entry])
    app = _FakeApp([{"text": "", "length": 0}, {"text": "", "length": 0}, {"text": text, "length": len(text), "capacity": 2000, "version": 2, "full": False}])
    real_script.observe_notebook(app, "save", real_script.time.monotonic())
    out = capsys.readouterr().out
    assert "3 條" in out and "超過的有 1 條" in out and "主角答應送信" in out and "版本 2" in out


def test_the_notebook_observation_says_so_when_the_notebook_stays_empty(monkeypatch, capsys):
    monkeypatch.setattr(real_script, "NOTEBOOK_WAIT_SECONDS", 0)
    real_script.observe_notebook(_FakeApp([{"text": "", "length": 0}]), "save", real_script.time.monotonic())
    assert "仍是空的" in capsys.readouterr().out


# ---- 修訂（重新生成與編輯最後一輪，rpg-turn-revision 任務 5.2）-----------------------------------------------------------

revision_script = load("acceptance_rpg_revision")


@pytest.mark.parametrize("args", [["--base-url", "http://example.com:5010"], ["--helper-url", "https://localhost:5001"], ["--model-url", "http://10.1.2.3:5002"]])
def test_the_revision_script_refuses_to_run_against_a_remote_address_before_sending_anything(args, capsys):
    assert revision_script.main(args) == 2
    assert "拒絕執行" in capsys.readouterr().err


def test_the_revision_script_refuses_an_unknown_case_name_before_sending_anything(capsys):
    assert revision_script.main(["--only", "journey,nonsense"]) == 2
    assert "nonsense" in capsys.readouterr().err


REGEN = dict(kind="regenerate", sentences=harness.REGEN_SENTENCES)


@pytest.mark.parametrize("action,attempt,expected", [
    (None, 1, harness.RevisionBehaviour()),                                                   # 第 0 幕沒有行動
    ("普通 驗收 a", 2, harness.RevisionBehaviour()),                                          # 沒有關鍵字：重送與自動重生的行為不變
    ("測試重生 驗收 a", 1, harness.RevisionBehaviour()),                                      # 原本那一輪（第 1 次呼叫）是普通回合
    ("測試重生 驗收 a", 2, harness.RevisionBehaviour(**REGEN)),
    ("測試重生 驗收 a", 5, harness.RevisionBehaviour(**REGEN)),
    ("測試重生被擋一次 驗收 a", 2, harness.RevisionBehaviour(**REGEN, blocked=True)),
    ("測試重生被擋一次 驗收 a", 3, harness.RevisionBehaviour(**REGEN)),
    ("測試重生被擋兩次 驗收 a", 2, harness.RevisionBehaviour(**REGEN, blocked=True)),
    ("測試重生被擋兩次 驗收 a", 3, harness.RevisionBehaviour(**REGEN, blocked=True)),
    ("測試重生被擋兩次 驗收 a", 4, harness.RevisionBehaviour(**REGEN)),                       # 最終被擋之後的下一次正常
    ("測試重生中斷 驗收 a", 2, harness.RevisionBehaviour(**REGEN, cut=True)),
    ("測試重生中斷 驗收 a", 3, harness.RevisionBehaviour(**REGEN)),
    ("測試重生慢速 驗收 a", 1, harness.RevisionBehaviour()),
    ("測試重生慢速 驗收 a", 2, harness.RevisionBehaviour(**REGEN, slow=True)),
    ("測試重生慢速 驗收 a", 3, harness.RevisionBehaviour(**REGEN, slow=True)),
    ("測試重生狀態失敗 驗收 a", 1, harness.RevisionBehaviour()),                              # 原本那一輪的狀態呼叫正常
    ("測試重生狀態失敗 驗收 a", 2, harness.RevisionBehaviour(**REGEN, state_fail=True)),
    ("測試編輯 驗收 a", 1, harness.RevisionBehaviour(kind="edit", sentences=harness.EDIT_SENTENCES)),
    ("測試編輯 測試狀態失敗 驗收 a", 1, harness.RevisionBehaviour(kind="edit", sentences=harness.EDIT_SENTENCES, state_fail=True)),
    ("測試狀態失敗 驗收 a", 1, harness.RevisionBehaviour(state_fail=True)),
])
def test_the_revision_behaviour_follows_the_keyword_and_the_call_number(action, attempt, expected):
    assert harness.revision_behaviour(action, attempt) == expected


def test_the_revision_keywords_cannot_be_mistaken_for_the_turn_or_notebook_keywords():
    """關鍵字是子字串比對：修訂的關鍵字不得包含（或被包含於）回合與記事簿的關鍵字，否則一個行動會同時觸發兩種行為。
    修訂家族內部的包含（`測試重生` 是 `測試重生被擋一次` 的一部分）是刻意的：細項都帶 `測試重生`。"""
    other = ("測試被擋一次", "測試被擋兩次", "測試中斷", "測試慢速", "測試結局",
             harness.KW_NOTEBOOK_OVER, harness.KW_NOTEBOOK_ABANDON, harness.KW_NOTEBOOK_VIOLATION, harness.KW_NOTEBOOK_SLOW)
    revision = (harness.KW_REGEN, harness.KW_EDIT, harness.KW_REGEN_BLOCK_ONCE, harness.KW_REGEN_BLOCK_TWICE, harness.KW_REGEN_CUT,
                harness.KW_REGEN_SLOW, harness.KW_REGEN_STATE_FAIL, harness.KW_STATE_FAIL)
    for keyword in revision:
        assert not any(keyword in turn or turn in keyword for turn in other), keyword
    assert all(keyword.startswith(harness.KW_REGEN) for keyword in revision[2:7])
    assert harness.KW_STATE_FAIL not in harness.KW_REGEN_STATE_FAIL and harness.KW_EDIT not in harness.KW_REGEN


def test_the_revision_script_and_the_harness_agree_on_keywords_marks_and_world_numbers():
    from app.models.rpg_session import REGEN_CATEGORIES

    s = revision_script
    assert (s.KW_REGEN, s.KW_EDIT, s.KW_REGEN_BLOCK_ONCE, s.KW_REGEN_BLOCK_TWICE, s.KW_REGEN_CUT, s.KW_REGEN_SLOW, s.KW_REGEN_STATE_FAIL, s.KW_STATE_FAIL) == (
        harness.KW_REGEN, harness.KW_EDIT, harness.KW_REGEN_BLOCK_ONCE, harness.KW_REGEN_BLOCK_TWICE, harness.KW_REGEN_CUT, harness.KW_REGEN_SLOW,
        harness.KW_REGEN_STATE_FAIL, harness.KW_STATE_FAIL)
    assert s.KW_BLOCK_TWICE == "測試被擋兩次" and s.KW_ENDING == "測試結局" and s.REASON_MARKER == harness.REASON_MARKER
    assert s.REGEN_MARK in harness.REGEN_SENTENCES[0] and s.EDIT_MARK in harness.EDIT_SENTENCES[0]
    assert not any(s.REGEN_MARK in sentence or s.EDIT_MARK in sentence for sentence in harness.STORY_SENTENCES)        # 普通劇情不含修訂的標記
    assert (s.NORMAL_WORLD, s.REGEN_WORLD, s.EDIT_WORLD) == tuple(
        (state["elapsed_months"], state["current_location"]) for state in (harness.STATE_NORMAL, harness.STATE_REGEN, harness.STATE_EDIT))
    assert len({s.NORMAL_WORLD, s.REGEN_WORLD, s.EDIT_WORLD}) == 3
    assert s.CATEGORIES == REGEN_CATEGORIES and s.TURN_COST == 30


def test_the_script_assumptions_match_the_application_defaults():
    from app.services.runtime_limits import COIN_TURN_COST_PREFIX, LIMIT_SPECS, RPG_FREE_REGENERATIONS_KEY

    assert revision_script.FREE_REGENERATIONS == LIMIT_SPECS[RPG_FREE_REGENERATIONS_KEY].default
    assert revision_script.TURN_COST == LIMIT_SPECS[f"{COIN_TURN_COST_PREFIX}.standard"].default


def test_the_revision_texts_pass_the_real_output_rule_layer():
    """假的修訂劇情必須一定通過真正的規則層，否則驗收的「成功」案例會被自己擋下。"""
    from app.services import rpg_output_rules

    for sentences in (harness.REGEN_SENTENCES, harness.EDIT_SENTENCES, harness.REGEN_SLOW_SENTENCES, harness.STORY_SENTENCES):
        previous = None
        for sentence in sentences:
            assert not rpg_output_rules.evaluate([sentence], previous, tier="standard", characters=()).blocked, sentence
            previous = sentence
    assert harness.REASON_MARKER not in "".join(harness.REGEN_SENTENCES + harness.EDIT_SENTENCES + harness.STORY_SENTENCES)


def _state_request(action: str, *, system: str = "你是文字冒險遊戲的「狀態記錄員」。") -> dict:
    """用應用程式**真正的**狀態呼叫內容（含「本回合行動」資料區塊）組出請求。"""
    from app.services.rpg_context import build_state_context
    from tests.support.protagonist import protagonist as make_protagonist
    from tests.support.rpg_pure import MARKER, SCRIPT, world

    _, contents = build_state_context(
        script=SCRIPT, world=world(), protagonist=make_protagonist("旅人", 18), action=action, narration="劇情。", turn_number=1, ending_due=False, marker=MARKER,
    )
    text = "\n".join(part for content in contents for part in content.parts)
    return {"model": "m", "stream": False, "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}]}


def _proposal(raw: bytes) -> dict:
    return json.loads(json.loads(raw)["choices"][0]["message"]["content"])


def test_the_fake_model_finds_the_action_in_the_real_state_prompt():
    for action in ("普通 驗收 a", "測試重生 驗收 b", "多行\n行動 驗收 c"):
        assert harness.state_action(_state_request(action)["messages"][1]["content"]) == action
    assert harness.state_action("沒有資料區塊") is None


def test_the_revision_state_call_proposes_another_world_only_after_the_regeneration_stream(fake_model):
    action = "測試重生 驗收 s1"
    for attempt, expected in ((1, harness.STATE_NORMAL), (2, harness.STATE_REGEN)):
        harness.RPG_ATTEMPTS[action] = attempt                                                  # 串流呼叫已發生 attempt 次
        assert _proposal(_post(fake_model, _state_request(action))) == expected
    harness.RPG_ATTEMPTS["測試編輯 驗收 s2"] = 1
    assert _proposal(_post(fake_model, _state_request("測試編輯 驗收 s2"))) == harness.STATE_EDIT
    assert _proposal(_post(fake_model, _state_request("普通 驗收 s3"))) == harness.STATE_NORMAL    # 沒有關鍵字：與修訂功能出現之前相同
    assert json.loads(_post_status(fake_model))["state_calls"] == 4


def test_the_state_failure_keywords_answer_500_and_the_others_do_not(fake_model):
    import http.client

    def status_of(action: str) -> int:
        connection = http.client.HTTPConnection(fake_model.split("//")[1].split(":")[0], int(fake_model.rsplit(":", 1)[1]), timeout=10)
        connection.request("POST", "/v1/chat/completions", json.dumps(_state_request(action), ensure_ascii=False).encode("utf-8"), {"Content-Type": "application/json"})
        try:
            return connection.getresponse().status
        finally:
            connection.close()

    harness.RPG_ATTEMPTS.update({"測試重生狀態失敗 驗收 f1": 2, "測試重生狀態失敗 驗收 f2": 1})
    assert status_of("測試重生狀態失敗 驗收 f1") == 500          # 重新生成（第 2 次呼叫）的狀態呼叫失敗
    assert status_of("測試重生狀態失敗 驗收 f2") == 200          # 原本那一輪的狀態呼叫正常
    assert status_of("測試編輯 測試狀態失敗 驗收 f3") == 500
    assert status_of("普通 驗收 f4") == 200


def test_the_fake_model_streams_a_different_story_on_the_regeneration_call_and_leaves_ordinary_actions_alone(fake_model):
    action = "測試重生 驗收 g1"
    first, second = _texts(_post(fake_model, _action_request(action))), _texts(_post(fake_model, _action_request(action)))
    assert first == ["甲。", "乙。"] and second == list(harness.REGEN_SENTENCES)
    assert _texts(_post(fake_model, _action_request("測試編輯 驗收 g2"))) == list(harness.EDIT_SENTENCES)
    plain = [_texts(_post(fake_model, _action_request("普通 驗收 g3"))) for _ in range(3)]
    assert plain == [["甲。", "乙。"]] * 3                                                       # 重送與自動重生的普通行動不受影響


def test_the_regeneration_block_cut_and_slow_cases_happen_on_the_documented_calls(fake_model):
    once = [_texts(_post(fake_model, _action_request("測試重生被擋一次 驗收 h1"))) for _ in range(3)]
    assert [harness.BAD_SENTENCE in texts for texts in once] == [False, True, False]            # 第 1 次是原本那一輪、第 2 次被擋、第 3 次（自動重生）正常
    twice = [_texts(_post(fake_model, _action_request("測試重生被擋兩次 驗收 h2"))) for _ in range(4)]
    assert [harness.BAD_SENTENCE in texts for texts in twice] == [False, True, True, False]
    cut = [_post(fake_model, _action_request("測試重生中斷 驗收 h3")) for _ in range(3)]
    assert [b"[DONE]" in raw for raw in cut] == [True, False, True]
    slow = [_texts(_post(fake_model, _action_request("測試重生慢速 驗收 h4"))) for _ in range(3)]
    assert [len(texts) for texts in slow] == [2, 12, 12] and all(text.startswith("重新生成後的第") for text in slow[1])


def test_a_request_carrying_the_reason_marker_is_counted_and_others_are_not(fake_model):
    _post(fake_model, _action_request("沒有標記 驗收 m1"))
    assert json.loads(_post_status(fake_model)).get("marker_calls", 0) == 0
    _post(fake_model, _action_request(f"{harness.REASON_MARKER} 驗收 m2"))
    _post(fake_model, _action_request("我的理由", system=f"系統指令也算：{harness.REASON_MARKER}"))
    assert json.loads(_post_status(fake_model))["marker_calls"] == 2


def test_the_harness_status_reports_the_marker_counter():
    assert harness.RPG_STATS.keys() >= {"stream_calls", "state_calls", "capture_calls", "upstream_closed", "marker_calls"}
    assert isinstance(harness.REASON_MARKER, str) and len(harness.REASON_MARKER) >= 4


# ---- 管理員不消耗遊戲幣（admin-unlimited-coins 任務 4.2）-------------------------------------------------------------------

admin_script = load("acceptance_rpg_admin_coins")


@pytest.mark.parametrize("args", [["--base-url", "http://example.com:5010"], ["--helper-url", "https://localhost:5001"], ["--model-url", "http://10.1.2.3:5002"]])
def test_the_admin_coins_script_refuses_to_run_against_a_remote_address_before_sending_anything(args, capsys):
    assert admin_script.main(args) == 2
    assert "拒絕執行" in capsys.readouterr().err


def test_the_admin_coins_script_refuses_an_unknown_case_name_before_sending_anything(capsys):
    assert admin_script.main(["--only", "journey,nonsense"]) == 2
    assert "nonsense" in capsys.readouterr().err


@pytest.mark.parametrize("path,expected", [
    ("/fresh/rpg?admin=1", True), ("/fresh/rpg?coins=700&admin=1", True), ("/fresh/rpg?admin=1&coins=700", True),
    ("/fresh/rpg", False), ("/fresh/rpg?admin=0", False), ("/fresh/rpg?admin=true", False), ("/fresh/rpg?admin=", False),
    ("/fresh/rpg?admin=11", False), ("/fresh/rpg?admin=yes", False), ("/fresh/rpg?coins=30", False),
])
def test_the_helper_url_admin_parameter_is_true_only_for_exactly_one(path, expected):
    assert harness.admin_from_path(path) is expected


def test_a_fresh_admin_is_an_active_admin_without_coins_even_when_coins_are_asked_for_and_a_fresh_player_stays_a_player(player_app):
    from sqlalchemy import select

    from app.models.user import ROLE_ADMIN, ROLE_USER, STATUS_ACTIVE, User
    from app.repositories.wallet_repository import WalletRepository

    harness.fresh_player(player_app, coins=700, admin=True)          # 管理員不補給付費幣：coins 被忽略
    harness.fresh_player(player_app, coins=700)
    harness.fresh_player(player_app)
    with player_app.extensions["db_session_factory"]() as session:
        users = list(session.scalars(select(User).order_by(User.id)))
        wallets = [WalletRepository(session).state(user.id) for user in users]
    assert [(user.role, user.status) for user in users] == [(ROLE_ADMIN, STATUS_ACTIVE), (ROLE_USER, STATUS_ACTIVE), (ROLE_USER, STATUS_ACTIVE)]
    assert wallets[0] is None                                       # 沒有錢包、沒有任何幣
    assert (wallets[1].free_balance, wallets[1].paid_balance) == (0, 700) and wallets[2] is None
    assert users[0].email.startswith("accept-rpg-admin-") and users[0].email.endswith("@" + harness.EMAIL_DOMAIN)
    assert users[0].birth_date is None                              # 尚未年齡確認：腳本自己走帳號設定頁的補填


def test_the_cookie_of_a_fresh_admin_signs_in_and_the_wallet_says_the_account_does_not_spend_coins(player_app):
    cookie, _ = harness.fresh_player(player_app, admin=True)
    name, _, rest = cookie.partition("=")
    client = player_app.test_client()
    client.set_cookie(name, rest.split(";")[0])
    wallet = client.get("/api/wallet").get_json()["wallet"]
    assert (wallet["coin_exempt"], wallet["turn_cost"], wallet["total"]) == (True, {"standard": 0}, 0)
    player_cookie, _ = harness.fresh_player(player_app)
    player = player_app.test_client()
    player_name, _, player_rest = player_cookie.partition("=")
    player.set_cookie(player_name, player_rest.split(";")[0])
    assert player.get("/api/wallet").status_code == 403             # 一般玩家尚未年齡確認：什麼都不能用（管理員不受這個限制）


def test_the_admin_coins_script_and_the_application_agree_on_numbers_and_ledger_kinds():
    from app.models.wallet import ENTRY_TURN_HOLD, ENTRY_TURN_REFUND
    from app.services.runtime_limits import COIN_SIGNUP_GRANT_KEY, COIN_TURN_COST_PREFIX, LIMIT_SPECS, RPG_FREE_REGENERATIONS_KEY

    assert admin_script.CASES == ("journey", "interrupt", "ordinary")
    assert admin_script.LEDGER_KINDS == {ENTRY_TURN_HOLD, ENTRY_TURN_REFUND}
    assert admin_script.FREE_REGENERATIONS == LIMIT_SPECS[RPG_FREE_REGENERATIONS_KEY].default
    assert admin_script.TURN_COST == LIMIT_SPECS[f"{COIN_TURN_COST_PREFIX}.standard"].default == turn_script.TURN_COST
    assert admin_script.SIGNUP_GRANT == LIMIT_SPECS[COIN_SIGNUP_GRANT_KEY].default == turn_script.SIGNUP_GRANT
    assert admin_script.KW_CUT in "測試中斷 驗收" and admin_script.KW_REGEN == harness.KW_REGEN


def test_the_admin_coins_cases_are_a_subclass_of_the_revision_acceptance_so_the_helpers_are_shared():
    assert issubclass(admin_script.AdminCoinsAcceptance, revision_script.RevisionAcceptance)
    assert admin_script.EDIT_MARK == revision_script.EDIT_MARK and admin_script.REGEN_MARK == revision_script.REGEN_MARK
