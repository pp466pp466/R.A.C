"""llm-provider 規格「依用途指定模型供應商」：客服與摘要走 Gemini，RPG 敘事走 OpenAI 相容端點，彼此獨立。

用假傳輸（不連網）驗證：兩個供應商的組裝、兩把金鑰互不混用、設定的旗標確實傳到轉接器、帳本記錄實際使用的供應商與模型，
以及 RPG 用途持續失敗時客服的呼叫、備援與斷路完全不受影響。
"""
import json
from contextlib import contextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.db.session import get_session
from app.llm.gemini import GeminiProvider
from app.llm.openai_compatible import DEFAULT_BASE_URL, PROVIDER_NAME, OpenAICompatibleProvider
from app.llm.types import LLMContent, LLMRequest
from app.models import Base, LlmCallLog
from app.models.llm_call_log import KIND_CHAT, KIND_SUMMARY
from app.services.registry import get_llm_service, get_rpg_llm_service
from tests.support.auth import FakeClock
from tests.support.llm import FakeLLMProvider
from tests.test_openai_compatible_provider import FakeTransport, ok_payload

GEMINI_KEY = "AIzaSy-gemini-key-for-tests-0123456789"
RPG_KEY = "sk-or-v1-rpg-key-for-tests-0123456789"
RPG_MODEL = "vendor/narrator:free"
BOTH = {"GEMINI_API_KEY": GEMINI_KEY, "OPENROUTER_API_KEY": RPG_KEY, "OPENROUTER_MODEL": RPG_MODEL}
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def make_app(environ: dict | None = None, **injected):
    config = load_config(environ={"APP_ENV": "testing", **(environ or {})})
    application = create_app(config, clock=FakeClock(), **injected)
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


@contextmanager
def request_scope(app):
    """一個真正的請求範圍：`before_request` 會建立請求級的資料庫工作階段（組裝層的 `get_session()` 需要它）。"""
    with app.test_request_context():
        app.preprocess_request()
        yield


def request(model="m") -> LLMRequest:
    return LLMRequest(
        model=model, system_instruction="指令", contents=(LLMContent("user", ("你好",)),), response_schema=None,
        timeout_seconds=20.0, max_output_tokens=512,
    )


def ledger(app) -> list[LlmCallLog]:
    with Session(app.extensions["db_engine"]) as session:
        return list(session.scalars(select(LlmCallLog).order_by(LlmCallLog.id)))


def run_rpg(app, **kwargs):
    with request_scope(app):
        service = get_rpg_llm_service()
        outcome = service.run_structured(
            kind=KIND_SUMMARY, user_id=None, conversation_id=None, system_instruction="你是敘事者。",
            contents=[LLMContent("user", ("開場",))], schema=SCHEMA, **kwargs,
        )
        get_session().commit()                                    # 最後一筆帳本只寫入、不提交，交給呼叫端
        return outcome


# ---- 組裝 ------------------------------------------------------------------------------------------------

def test_with_only_gemini_configured_the_rpg_purpose_is_unavailable_and_chat_is_untouched():
    app = make_app({"GEMINI_API_KEY": GEMINI_KEY})
    assert isinstance(app.extensions["llm_provider"], GeminiProvider) and app.extensions["rpg_llm_provider"] is None
    with request_scope(app):
        assert get_rpg_llm_service() is None and get_llm_service() is not None


def test_with_nothing_configured_both_purposes_are_unavailable_and_the_app_still_starts():
    app = make_app()
    assert app.extensions["llm_provider"] is None and app.extensions["rpg_llm_provider"] is None
    with request_scope(app):
        assert get_rpg_llm_service() is None and get_llm_service() is None


def test_with_both_configured_each_purpose_gets_its_own_kind_of_provider():
    app = make_app(BOTH)
    assert isinstance(app.extensions["llm_provider"], GeminiProvider)
    assert isinstance(app.extensions["rpg_llm_provider"], OpenAICompatibleProvider)
    with request_scope(app):
        assert get_llm_service() is not None and get_rpg_llm_service() is not None


def test_a_fake_provider_can_be_injected_for_the_rpg_purpose_even_without_any_rpg_configuration():
    fake = FakeLLMProvider()
    app = make_app(rpg_llm_provider=fake)
    assert app.extensions["rpg_llm_provider"] is fake
    with request_scope(app):
        assert get_rpg_llm_service() is not None


def test_the_rpg_service_uses_the_rpg_models_and_the_chat_service_keeps_the_gemini_ones():
    app = make_app({**BOTH, "GEMINI_MODEL": "gemini-x", "OPENROUTER_FALLBACK_MODEL": "vendor/fallback"})
    with request_scope(app):
        assert (get_rpg_llm_service()._settings.primary_model, get_rpg_llm_service()._settings.fallback_model) == (RPG_MODEL, "vendor/fallback")
        assert get_llm_service()._settings.primary_model == "gemini-x"


# ---- 兩把金鑰互不混用、設定的旗標確實傳到轉接器 ---------------------------------------------------------------

def capture_transports(monkeypatch):
    gemini, rpg = FakeTransport(payload={"candidates": [{"content": {"parts": [{"text": "{}"}]}, "finishReason": "STOP"}]}), FakeTransport(payload=ok_payload())
    monkeypatch.setattr("app.llm.gemini.UrllibTransport", lambda: gemini)
    monkeypatch.setattr("app.llm.openai_compatible.UrllibTransport", lambda: rpg)
    return gemini, rpg


def test_the_two_keys_never_mix_each_request_carries_only_its_own_key(monkeypatch):
    gemini, rpg = capture_transports(monkeypatch)
    app = make_app(BOTH)
    app.extensions["llm_provider"].generate(request("gemini-model"))
    app.extensions["rpg_llm_provider"].generate(request(RPG_MODEL))
    gemini_call, rpg_call = gemini.calls[0], rpg.calls[0]
    assert GEMINI_KEY in gemini_call["headers"].values() and RPG_KEY not in str(gemini_call)
    assert f"Bearer {RPG_KEY}" in rpg_call["headers"].values() and GEMINI_KEY not in str(rpg_call)
    assert rpg_call["url"] == f"{DEFAULT_BASE_URL}/chat/completions" and rpg_call["body"]["model"] == RPG_MODEL


def test_the_configured_base_url_and_flags_reach_the_adapter(monkeypatch):
    _, rpg = capture_transports(monkeypatch)
    app = make_app({
        **BOTH, "OPENROUTER_BASE_URL": "http://localhost:11434/v1", "OPENROUTER_ZDR": "true", "OPENROUTER_JSON_SCHEMA": "true",
    })
    app.extensions["rpg_llm_provider"].generate(LLMRequest(RPG_MODEL, "指令", (LLMContent("user", ("你好",)),), SCHEMA, 20.0, 512))
    call = rpg.calls[0]
    assert call["url"] == "http://localhost:11434/v1/chat/completions"
    assert call["body"]["provider"] == {"zdr": True, "data_collection": "deny"} and "response_format" in call["body"]


def test_without_the_flags_the_adapter_sends_neither_preference_nor_schema_parameter(monkeypatch):
    _, rpg = capture_transports(monkeypatch)
    app = make_app(BOTH)
    app.extensions["rpg_llm_provider"].generate(LLMRequest(RPG_MODEL, "指令", (LLMContent("user", ("你好",)),), SCHEMA, 20.0, 512))
    body = rpg.calls[0]["body"]
    assert "provider" not in body and "response_format" not in body


# ---- 帳本記錄實際使用的供應商與模型 --------------------------------------------------------------------------------

def test_an_rpg_call_is_recorded_in_the_ledger_with_the_openai_compatible_provider_and_the_actual_model():
    transport = FakeTransport(payload=ok_payload(json.dumps({"ok": True}), usage={"prompt_tokens": 7, "completion_tokens": 3}))
    app = make_app(
        {"OPENROUTER_API_KEY": RPG_KEY, "OPENROUTER_MODEL": RPG_MODEL}, rpg_llm_provider=OpenAICompatibleProvider(RPG_KEY, transport=transport)
    )
    outcome = run_rpg(app)
    assert outcome.ok and outcome.value == {"ok": True} and outcome.model == RPG_MODEL and outcome.attempts == 1
    (row,) = ledger(app)
    assert (row.provider, row.model, row.status, row.kind) == (PROVIDER_NAME, RPG_MODEL, "ok", KIND_SUMMARY)
    assert (row.prompt_tokens, row.completion_tokens, row.thinking_tokens) == (7, 3, None)
    assert row.provider != "gemini"
    assert transport.calls[0]["body"]["model"] == RPG_MODEL


@pytest.mark.parametrize(
    "name", ["cognitivecomputations/dolphin-mistral-24b-venice-edition:free", "v" * 32 + "/" + "m" * 31],        # 後者剛好 64 字元
)
def test_a_model_name_with_slashes_and_colons_is_kept_verbatim_in_the_ledger_even_at_the_maximum_length(name):
    """設定允許的最長名稱必須能完整寫進帳本的 `model` 欄位，否則 SQL Server 會截斷或出錯，斷路也會查不到。"""
    assert len(name) <= LlmCallLog.model.type.length
    transport = FakeTransport(payload=ok_payload(json.dumps({"ok": True})))
    app = make_app({"OPENROUTER_API_KEY": RPG_KEY, "OPENROUTER_MODEL": name}, rpg_llm_provider=OpenAICompatibleProvider(RPG_KEY, transport=transport))
    run_rpg(app)
    assert ledger(app)[0].model == name


def test_the_longest_model_name_the_config_accepts_is_exactly_what_the_ledger_column_can_hold():
    """上限不能比帳本欄位寬：設定接受的名稱，帳本一定寫得下。"""
    from app.config import ConfigError

    limit = LlmCallLog.model.type.length
    accepted = load_config(environ={"APP_ENV": "testing", "OPENROUTER_API_KEY": RPG_KEY, "OPENROUTER_MODEL": "m" * limit})
    assert len(accepted.rpg_llm.llm.primary_model) == limit
    with pytest.raises(ConfigError):
        load_config(environ={"APP_ENV": "testing", "OPENROUTER_API_KEY": RPG_KEY, "OPENROUTER_MODEL": "m" * (limit + 1)})


# ---- RPG 用途持續失敗，客服不受影響 -----------------------------------------------------------------------------

def test_a_failing_rpg_provider_does_not_open_the_chat_breaker_or_change_which_model_chat_uses():
    rpg_transport = FakeTransport(status=500, payload={"error": {"message": "boom"}})
    chat_provider = FakeLLMProvider().script(json.dumps({"ok": True}))
    app = make_app(
        {**BOTH}, llm_provider=chat_provider, rpg_llm_provider=OpenAICompatibleProvider(RPG_KEY, transport=rpg_transport),
    )
    for _ in range(3):                                            # RPG 用途連續失敗：每次都寫入「伺服器錯誤」的帳本
        assert run_rpg(app).ok is False
    assert [r.status for r in ledger(app)] == ["server_error"] * 3 and {r.model for r in ledger(app)} == {RPG_MODEL}

    with request_scope(app):
        outcome = get_llm_service().run_structured(
            kind=KIND_CHAT, user_id=None, conversation_id=None, system_instruction="客服", contents=[LLMContent("user", ("您好",))], schema=SCHEMA,
        )
        get_session().commit()
    assert outcome.ok and outcome.attempts == 1
    assert outcome.model == app.extensions["app_config"].llm.primary_model          # 沒有因為 RPG 的失敗而改用備援
    assert [call.model for call in chat_provider.calls] == [outcome.model]


def test_the_rpg_breaker_is_keyed_by_the_rpg_model_so_chat_models_never_look_unhealthy():
    rpg_transport = FakeTransport(status=500, payload={})
    app = make_app({**BOTH}, rpg_llm_provider=OpenAICompatibleProvider(RPG_KEY, transport=rpg_transport))
    run_rpg(app)
    chat_model = app.extensions["app_config"].llm.primary_model
    assert chat_model != RPG_MODEL
    with request_scope(app):
        rpg_service = get_rpg_llm_service()
        assert rpg_service._breaker_open(RPG_MODEL) is True and rpg_service._breaker_open(chat_model) is False
