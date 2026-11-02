"""fake-model-server 規格：Gemini 相容的假端點、延遲與錯誤率可設定、只接受測試金鑰且不記錄內容、獨立映像不進主映像。"""
import ast
import json
import logging
import re
import threading

import pytest
from werkzeug.serving import make_server

from app.config import PROJECT_ROOT
from app.llm.gemini import GeminiProvider
from app.llm.types import FINISH_STOP, LLMContent, LLMRequest
from loadtest import provider as local_provider
from loadtest.fake_llm import server
from loadtest.fake_llm.server import FakeLlmConfigError, create_fake_llm_app

KEY = "fake-llm-test-key-0001"
PATH = "/v1beta/models/gemini-test:generateContent"
FAKE_DIR = PROJECT_ROOT / "loadtest" / "fake_llm"
PROMPT_MARKER = "PROMPT-BODY-MARKER-9f3a"
SYSTEM_MARKER = "SYSTEM-INSTRUCTION-MARKER-77c1"


def env(**overrides: str) -> dict[str, str]:
    values = {"FAKE_LLM_API_KEY": KEY, "LOADTEST_LATENCY_SECONDS": "0", "LOADTEST_JITTER_SECONDS": "0", "LOADTEST_SEED": "7"}
    values.update(overrides)
    return values


def make(sleeps: list | None = None, **overrides: str):
    log = [] if sleeps is None else sleeps
    return create_fake_llm_app(env(**overrides), sleep=log.append)


def call(app, *, key: str | None = KEY, system: str = "你是客服。", text: str = "問題"):
    headers = {"x-goog-api-key": key} if key is not None else {}
    body = {"system_instruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": text}]}]}
    return app.test_client().post(PATH, json=body, headers=headers)


def llm_request(system: str = "你是客服。") -> LLMRequest:
    return LLMRequest(
        model="gemini-test", system_instruction=system, contents=(LLMContent("user", ("問題",)),),
        response_schema=None, timeout_seconds=5.0, max_output_tokens=256,
    )


@pytest.fixture()
def live_server():
    """真正的本機 HTTP 伺服器（多執行緒），讓專案的 Gemini 轉接器以真實的傳輸呼叫它。"""
    app = make()
    httpd = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield app, f"http://127.0.0.1:{httpd.server_port}/v1beta"
    httpd.shutdown()
    thread.join(timeout=5)


# ---- Gemini 相容的假端點 -----------------------------------------------------------------------


def test_the_projects_gemini_adapter_calls_the_fake_server_and_parses_the_reply(live_server):
    _, base_url = live_server
    response = GeminiProvider(KEY, base_url=base_url).generate(llm_request())
    assert response.finish_reason == FINISH_STOP and response.model == "gemini-test"
    assert (response.prompt_tokens, response.completion_tokens) == (server.PROMPT_TOKENS, server.COMPLETION_TOKENS)
    reply = json.loads(response.text)
    assert {"intent", "confidence", "entities", "reply_text", "suggested_actions"} <= set(reply)
    assert reply["reply_text"] == server.REPLY["reply_text"] and "負載測試" in reply["reply_text"]


def test_a_summary_call_gets_the_summary_shape_and_a_chat_call_gets_the_structured_reply(live_server):
    _, base_url = live_server
    provider = GeminiProvider(KEY, base_url=base_url)
    summary = json.loads(provider.generate(llm_request(system=f"你負責維護一段客服對話的{server.SUMMARY_MARKER}。")).text)
    assert set(summary) == {"summary"} and summary["summary"]
    assert "intent" in json.loads(provider.generate(llm_request()).text)


def test_the_replies_match_the_local_fake_provider_so_the_two_cannot_drift():
    assert server.REPLY == local_provider.REPLY
    assert server.SUMMARY_MARKER == local_provider.SUMMARY_MARKER
    assert (server.PROMPT_TOKENS, server.COMPLETION_TOKENS) == (local_provider.PROMPT_TOKENS, local_provider.COMPLETION_TOKENS)
    assert server.SUMMARY_REPLY == {"summary": "負載測試的假摘要。"}


def test_the_liveness_endpoint_needs_no_key():
    response = make().test_client().get("/healthz")
    assert response.status_code == 200 and response.get_json() == {"status": "ok"}


def test_only_the_generate_content_action_exists():
    assert make().test_client().post("/v1beta/models/gemini-test:streamGenerateContent", json={}, headers={"x-goog-api-key": KEY}).status_code == 404


# ---- 延遲與錯誤率可設定 ------------------------------------------------------------------------


def test_ten_percent_server_errors_show_up_as_about_ten_percent_500s_and_the_rest_succeed():
    app = make(LOADTEST_ERROR_RATES="server_error=0.1")
    statuses = [call(app).status_code for _ in range(1000)]
    errors = statuses.count(500)
    assert 70 <= errors <= 130, errors
    assert statuses.count(200) == 1000 - errors


def test_rate_limits_are_429_and_timeouts_hold_the_connection_for_the_configured_time():
    sleeps: list[float] = []
    app = make(sleeps, LOADTEST_ERROR_RATES="rate_limited=0.5,timeout=0.5", LOADTEST_TIMEOUT_SECONDS="31")
    statuses = {call(app).status_code for _ in range(60)}
    assert statuses == {429, 504}
    assert 31.0 in sleeps                          # 逾時類錯誤睡滿設定的秒數才失敗，佔住連線


def test_the_default_timeout_is_longer_than_the_adapters_single_call_timeout():
    assert server.DEFAULT_TIMEOUT_SECONDS > 20


def test_latency_and_jitter_are_applied_to_every_successful_call():
    sleeps: list[float] = []
    app = make(sleeps, LOADTEST_LATENCY_SECONDS="1.5", LOADTEST_JITTER_SECONDS="0.5")
    for _ in range(40):
        assert call(app).status_code == 200
    assert len(sleeps) == 40 and all(1.0 <= s <= 2.0 for s in sleeps) and len(set(sleeps)) > 1


def test_the_same_seed_gives_the_same_sequence_of_outcomes():
    app_a, app_b = make(LOADTEST_ERROR_RATES="server_error=0.3"), make(LOADTEST_ERROR_RATES="server_error=0.3")
    outcomes = [call(app_a).status_code for _ in range(50)]
    assert outcomes == [call(app_b).status_code for _ in range(50)]
    assert {200, 500} <= set(outcomes)


@pytest.mark.parametrize(
    "overrides, variable, leaked",
    [
        ({"LOADTEST_ERROR_RATES": "server_error=0.9,rate_limited=0.9"}, "LOADTEST_ERROR_RATES", "0.9"),
        ({"LOADTEST_ERROR_RATES": "explosion=0.1"}, "LOADTEST_ERROR_RATES", "explosion"),
        ({"LOADTEST_ERROR_RATES": "timeout=1.5"}, "LOADTEST_ERROR_RATES", "1.5"),
        ({"LOADTEST_ERROR_RATES": "timeout=abc"}, "LOADTEST_ERROR_RATES", "abc"),
        ({"LOADTEST_LATENCY_SECONDS": "-3"}, "LOADTEST_LATENCY_SECONDS", "-3"),
        ({"LOADTEST_JITTER_SECONDS": "lots"}, "LOADTEST_JITTER_SECONDS", "lots"),
        ({"LOADTEST_TIMEOUT_SECONDS": "nan"}, "LOADTEST_TIMEOUT_SECONDS", "nan"),
        ({"LOADTEST_SEED": "x1y2"}, "LOADTEST_SEED", "x1y2"),
    ],
)
def test_an_invalid_setting_stops_the_server_and_names_only_the_variable(overrides, variable, leaked):
    with pytest.raises(FakeLlmConfigError) as error:
        make(**overrides)
    assert variable in str(error.value) and leaked not in str(error.value).replace(variable, "")


def test_the_error_rate_rules_are_the_same_as_the_local_fake_provider():
    for spec in ("rate_limited=0.02,timeout=0.01,server_error=0.005", "", None, " timeout = 0.1 "):
        assert server.parse_error_rates(spec) == local_provider.parse_error_rates(spec)
    for bad in ("x=0.1", "timeout=2", "timeout=a", "timeout=0.6,server_error=0.6"):
        with pytest.raises(FakeLlmConfigError):
            server.parse_error_rates(bad)
        with pytest.raises(local_provider.LoadTestConfigError):
            local_provider.parse_error_rates(bad)


# ---- 只接受測試金鑰且不記錄內容 -----------------------------------------------------------------


@pytest.mark.parametrize("key", ["wrong-key-00000001", "", None, KEY + "x"])
def test_a_wrong_or_missing_key_gets_403_and_the_request_is_not_processed(key):
    sleeps: list[float] = []
    app = make(sleeps, LOADTEST_LATENCY_SECONDS="1.5")
    response = call(app, key=key)
    assert response.status_code == 403
    assert "candidates" not in response.get_json()
    assert sleeps == [] and app.extensions["fake_llm"].draws == 0        # 沒有睡眠，也沒有消耗亂數


def test_a_non_ascii_key_header_is_rejected_cleanly():
    assert call(make(), key="金鑰").status_code == 403


@pytest.mark.parametrize("value", ["", "short", "AIzaSyA-looks-like-a-real-google-key-0123456", "sk-or-v1-looks-like-a-real-key-0123456"])
def test_the_server_refuses_to_start_without_a_proper_test_key_and_never_echoes_it(value):
    with pytest.raises(FakeLlmConfigError) as error:
        create_fake_llm_app({"FAKE_LLM_API_KEY": value})
    assert "FAKE_LLM_API_KEY" in str(error.value) and (not value or value not in str(error.value))


def test_the_server_refuses_to_start_when_the_key_variable_is_missing():
    with pytest.raises(FakeLlmConfigError) as error:
        create_fake_llm_app({})
    assert "FAKE_LLM_API_KEY" in str(error.value)


def test_the_logs_contain_only_aggregates_never_the_key_the_request_or_the_reply(caplog, capfd):
    app = make(LOADTEST_ERROR_RATES="server_error=0.1")
    with caplog.at_level(logging.INFO, logger="fake_llm"):
        for _ in range(server.SUMMARY_EVERY * 2 + 5):
            call(app, system=f"{SYSTEM_MARKER} 你是客服。", text=PROMPT_MARKER)
        call(app, key="a-wrong-key-123456", text=PROMPT_MARKER)
        app.extensions["fake_llm"].log_summary()
    everything = caplog.text + capfd.readouterr().err
    for secret in (KEY, "a-wrong-key-123456", PROMPT_MARKER, SYSTEM_MARKER, server.REPLY["reply_text"], server.SUMMARY_REPLY["summary"]):
        assert secret not in everything
    summaries = [json.loads(record.getMessage()) for record in caplog.records if record.name == "fake_llm"]
    assert len(summaries) >= 3 and all(set(line) == {"event", "requests", "status"} for line in summaries)
    last = summaries[-1]
    assert last["requests"] == server.SUMMARY_EVERY * 2 + 6 and sum(last["status"].values()) == last["requests"]
    assert last["status"]["403"] == 1 and set(last["status"]) <= {"200", "403", "500"}


# ---- 獨立映像且不進入主映像 ---------------------------------------------------------------------


def test_the_main_image_neither_copies_nor_includes_the_fake_server():
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    copies = [line for line in dockerfile.splitlines() if line.strip().upper().startswith(("COPY", "ADD"))]
    assert copies and not any("loadtest" in line or "fake_llm" in line for line in copies)
    ignored = [line.strip() for line in (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()]
    assert "loadtest" in ignored                                           # 含 loadtest/fake_llm


def test_the_fake_server_has_its_own_dockerfile_that_copies_only_the_server_and_runs_as_non_root():
    dockerfile = (FAKE_DIR / "Dockerfile").read_text(encoding="utf-8")
    instructions = re.sub(r"\\\n", " ", dockerfile)
    copies = [line.split(None, 1)[1].strip() for line in instructions.splitlines() if line.upper().startswith("COPY ")]
    assert copies == ["requirements.txt ./", "server.py ./"], copies
    users = [line.split(None, 1)[1].strip() for line in instructions.splitlines() if line.upper().startswith("USER ")]
    assert users and users[-1] not in ("root", "0")
    assert "gunicorn" in instructions and "app.run" not in instructions
    assert "--no-control-socket" in instructions                                       # 非 root 且沒有家目錄，控制通道建不出來
    assert not re.search(r"^\s*(ENV|ARG)\s+FAKE_LLM_API_KEY", dockerfile, re.M)       # 測試金鑰在部署時設定，不寫在映像裡


def test_the_fake_servers_requirements_have_only_what_it_needs():
    names = {
        re.split(r"[=<>~!]", line, maxsplit=1)[0].strip().lower()
        for line in (FAKE_DIR / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert names == {"flask", "gunicorn"}
    for forbidden in ("pyodbc", "sqlalchemy", "alembic", "argon2-cffi", "pyjwt", "cryptography", "python-dotenv", "locust"):
        assert forbidden not in names


def test_the_server_module_imports_nothing_from_the_project_or_the_database_stack():
    tree = ast.parse((FAKE_DIR / "server.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"app", "loadtest", "sqlalchemy", "pyodbc", "alembic", "locust"}, imported
