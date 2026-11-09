"""llm-openai-compatible 規格「開發用的探測指令只限開發環境且不留下紀錄」：`flask llm-probe`。

用假傳輸驗證（不連網）。重點：只在 development 與 testing 執行、不寫任何資料表、輸出不含金鑰與供應商的錯誤本文。
"""
import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import create_app
from app.cli import PROBE_MAX_TEXT_CHARS, PROBE_SCHEMA, PROBE_TEXT
from app.config import load_config
from app.llm.openai_compatible import OpenAICompatibleProvider
from app.models import Base, LlmCallLog, User
from tests.support.config_env import production_like_env
from tests.test_openai_compatible_provider import FakeTransport, ok_payload

KEY = "sk-or-v1-fake-key-for-tests-0123456789"
MODEL = "vendor/narrator:free"
RPG = {"OPENROUTER_API_KEY": KEY, "OPENROUTER_MODEL": MODEL}
ECHO = "供應商的錯誤本文：回聲 SECRET-ECHO"


def make_app(transport=None, environ=None):
    config = load_config(environ={"APP_ENV": "testing", **RPG, **(environ or {})})
    injected = {"rpg_llm_provider": OpenAICompatibleProvider(KEY, transport=transport)} if transport else {}
    application = create_app(config, **injected)
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


def row_counts(app) -> dict[str, int]:
    with Session(app.extensions["db_engine"]) as session:
        return {table.name: session.scalar(select(func.count()).select_from(table)) for table in Base.metadata.sorted_tables}


def run(app, *args):
    return app.test_cli_runner().invoke(args=["llm-probe", *args])


# ---- 開發環境 ------------------------------------------------------------------------------------------

def test_it_prints_the_reply_model_latency_tokens_and_finish_reason_and_writes_nothing():
    transport = FakeTransport(payload=ok_payload("清晨的海風帶著鹹味。", usage={"prompt_tokens": 12, "completion_tokens": 34}))
    app = make_app(transport)
    before = row_counts(app)
    result = run(app)
    assert result.exit_code == 0, result.output
    for expected in (f"模型：{MODEL}", "毫秒", "輸入 12、輸出 34、思考 未提供", "結束原因：stop", "清晨的海風帶著鹹味。"):
        assert expected in result.output, expected
    assert row_counts(app) == before and sum(before.values()) == 0          # 沒有新增任何資料列（含帳本）
    assert len(transport.calls) == 1 and transport.calls[0]["body"]["messages"][1]["content"] == PROBE_TEXT


def test_a_custom_text_is_sent_instead_of_the_default():
    transport = FakeTransport(payload=ok_payload("好。"))
    result = run(make_app(transport), "--text", "請說一個很短的笑話")
    assert result.exit_code == 0 and transport.calls[0]["body"]["messages"][1]["content"] == "請說一個很短的笑話"


@pytest.mark.parametrize("text", ["", "字" * (PROBE_MAX_TEXT_CHARS + 1)])
def test_a_text_that_is_empty_or_too_long_is_refused_before_any_request(text):
    transport = FakeTransport(payload=ok_payload())
    result = run(make_app(transport), "--text", text)
    assert result.exit_code != 0 and transport.calls == []


def test_the_key_never_appears_in_any_output():
    result = run(make_app(FakeTransport(payload=ok_payload("好。"))))
    assert KEY not in result.output and "sk-or-" not in result.output


def test_it_uses_the_primary_rpg_model_and_the_shared_limits_from_the_config():
    transport = FakeTransport(payload=ok_payload("好。"))
    run(make_app(transport, {"LLM_CALL_TIMEOUT_SECONDS": "7", "LLM_MAX_OUTPUT_TOKENS": "999"}))
    call = transport.calls[0]
    assert call["body"]["model"] == MODEL and call["body"]["max_tokens"] == 999 and call["timeout"] == 7


# ---- 主控台編碼與推理設定（rpg-turn-engine 2.3）------------------------------------------------------------

UNENCODABLE = "😀✨"                                  # cp950（繁體中文 Windows 的主控台編碼）無法表示的字元


def test_the_control_case_really_is_unencodable_in_cp950():
    """對照：確認這個案例真的會讓天真的輸出中斷，下面的測試才有意義。"""
    with pytest.raises(UnicodeEncodeError):
        UNENCODABLE.encode("cp950")


def test_a_reply_with_characters_the_console_cannot_encode_is_printed_with_replacements_and_exits_zero():
    transport = FakeTransport(payload=ok_payload(f"清晨的海邊{UNENCODABLE}很安靜。", usage={"prompt_tokens": 5, "completion_tokens": 6}))
    app = make_app(transport)
    result = app.test_cli_runner(charset="cp950").invoke(args=["llm-probe"])
    assert result.exit_code == 0, result.output
    assert "清晨的海邊" in result.output and "很安靜。" in result.output and "?" in result.output          # 不能編碼的字元變成替代字元
    for expected in (f"模型：{MODEL}", "毫秒", "輸入 5、輸出 6", "結束原因：stop"):
        assert expected in result.output, expected


def test_a_utf8_console_still_shows_the_reply_unchanged():
    result = run(make_app(FakeTransport(payload=ok_payload(f"海風{UNENCODABLE}"))))
    assert result.exit_code == 0 and f"海風{UNENCODABLE}" in result.output


def test_the_reasoning_setting_is_shown_and_defaults_to_off():
    transport = FakeTransport(payload=ok_payload("好。"))
    result = run(make_app(transport))
    assert result.exit_code == 0 and "推理：關閉" in result.output
    assert transport.calls[0]["body"]["reasoning"] == {"enabled": False}


def test_with_reasoning_enabled_the_probe_says_so_and_sends_no_reasoning_parameter():
    transport = FakeTransport(payload=ok_payload("好。"))
    config = load_config(environ={"APP_ENV": "testing", **RPG, "OPENROUTER_REASONING": "true"})
    app = create_app(config, rpg_llm_provider=OpenAICompatibleProvider(KEY, transport=transport, reasoning=True))
    Base.metadata.create_all(app.extensions["db_engine"])
    result = run(app)
    assert result.exit_code == 0 and "推理：開啟" in result.output
    assert "reasoning" not in transport.calls[0]["body"]


# ---- 結構化選項 ----------------------------------------------------------------------------------------

def test_the_structured_option_sends_the_fixed_schema_and_reports_a_pass():
    transport = FakeTransport(payload=ok_payload(json.dumps({"title": "晨霧小鎮", "mood": "calm"}, ensure_ascii=False)))
    result = run(make_app(transport), "--structured")
    assert result.exit_code == 0, result.output
    assert "結構化驗證" in result.output and "通過" in result.output and "未通過" not in result.output
    system = transport.calls[0]["body"]["messages"][0]["content"]
    assert json.dumps(PROBE_SCHEMA, ensure_ascii=False) in system            # 預設退化做法：結構描述附在系統指令


@pytest.mark.parametrize(
    "reply, reason",
    [("好的，標題是晨霧小鎮。", "not_json"), (json.dumps({"title": "x"}), "schema"), (json.dumps({"title": "x", "mood": "angry"}), "schema")],
)
def test_a_reply_that_fails_validation_is_reported_with_a_reason_category_and_a_nonzero_exit(reply, reason):
    result = run(make_app(FakeTransport(payload=ok_payload(reply))), "--structured")
    assert result.exit_code == 1
    assert "未通過" in result.output and f"原因分類：{reason}" in result.output


# ---- 環境與設定 ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("env_name", ["staging", "production"])
def test_staging_and_production_refuse_to_run_and_send_nothing(env_name):
    transport = FakeTransport(payload=ok_payload())
    config = load_config(environ=production_like_env(env_name, **RPG))
    app = create_app(config, rpg_llm_provider=OpenAICompatibleProvider(KEY, transport=transport))
    Base.metadata.create_all(app.extensions["db_engine"])
    result = run(app)
    assert result.exit_code != 0 and "development 與 testing" in result.output
    assert transport.calls == []


def test_without_the_rpg_purpose_it_says_so_and_fails():
    config = load_config(environ={"APP_ENV": "testing"})
    app = create_app(config)
    Base.metadata.create_all(app.extensions["db_engine"])
    result = run(app)
    assert result.exit_code != 0 and "RPG 用途尚未設定" in result.output and "OPENROUTER_API_KEY" in result.output


# ---- 失敗：只印分類，不印供應商的錯誤本文 ----------------------------------------------------------------

@pytest.mark.parametrize(
    "status, expected",
    [(429, "暫時性錯誤：rate_limited"), (500, "暫時性錯誤：server_error"), (402, "設定錯誤（狀態碼 402）"), (404, "設定錯誤（狀態碼 404）")],
)
def test_a_provider_failure_prints_only_the_category_and_status_never_the_provider_body(status, expected):
    result = run(make_app(FakeTransport(status=status, payload={"error": {"message": ECHO}})))
    assert result.exit_code != 0 and expected in result.output
    assert "SECRET-ECHO" not in result.output and KEY not in result.output


def test_a_content_filtered_reply_is_reported_as_a_block_without_any_text():
    result = run(make_app(FakeTransport(payload=ok_payload("不該被印出的部分文字", finish="content_filter"))))
    assert result.exit_code != 0 and "內容安全過濾阻擋" in result.output and "不該被印出" not in result.output


def test_a_timeout_and_a_connection_failure_are_reported_as_transient():
    for error, kind in ((TimeoutError(), "timeout"), (OSError("boom"), "network")):
        result = run(make_app(FakeTransport(error=error)))
        assert result.exit_code != 0 and f"暫時性錯誤：{kind}" in result.output


def test_the_command_touches_no_table_even_when_the_call_fails():
    app = make_app(FakeTransport(status=500, payload={}))
    run(app)
    assert row_counts(app) == {name: 0 for name in row_counts(app)}
    with Session(app.extensions["db_engine"]) as session:
        assert session.scalar(select(func.count()).select_from(LlmCallLog)) == 0 and session.scalar(select(func.count()).select_from(User)) == 0
