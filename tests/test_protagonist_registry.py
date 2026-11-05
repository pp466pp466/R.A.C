"""主角相關服務的組裝（registry）：只用 RPG 用途的模型、沒有設定時仍可建立並回報不可用、標記字串一致。

用假傳輸（不連網）；沿用 `test_llm_providers_by_purpose.py` 的組裝輔助。
"""
import pytest

from app.llm.gemini import GeminiProvider
from app.llm.openai_compatible import OpenAICompatibleProvider
from app.services.protagonist_prompt import protagonist_marker
from app.services.registry import (
    get_llm_service, get_protagonist_review_service, get_protagonist_service, get_rpg_llm_service, get_session_service,
)
from tests.support.llm import FakeLLMProvider
from tests.test_llm_providers_by_purpose import BOTH, GEMINI_KEY, RPG_KEY, make_app, request_scope
from tests.test_openai_compatible_provider import FakeTransport


def test_without_an_rpg_provider_the_services_still_build_and_report_the_model_as_unavailable():
    app = make_app({"GEMINI_API_KEY": GEMINI_KEY})
    with request_scope(app):
        assert get_protagonist_service()._llm is None
        assert get_protagonist_review_service()._llm is None
        assert get_session_service() is not None                                        # 開存檔的其餘部分仍可組裝


def test_the_protagonist_services_use_the_rpg_provider_and_never_the_customer_service_one():
    rpg = FakeLLMProvider()
    app = make_app({**BOTH}, rpg_llm_provider=rpg)
    with request_scope(app):
        assert get_protagonist_service()._llm._provider is rpg
        assert get_protagonist_review_service()._llm._provider is rpg
        assert get_protagonist_service()._llm._provider is get_rpg_llm_service()._provider
        assert get_protagonist_service()._llm._provider is not get_llm_service()._provider
        assert isinstance(get_llm_service()._provider, GeminiProvider)


def test_a_real_adapter_for_the_rpg_purpose_is_the_openai_compatible_one():
    app = make_app({**BOTH}, rpg_llm_provider=OpenAICompatibleProvider(RPG_KEY, transport=FakeTransport(status=200, payload={})))
    with request_scope(app):
        assert isinstance(get_protagonist_review_service()._llm._provider, OpenAICompatibleProvider)


def test_the_generation_review_and_start_share_one_marker_derived_from_the_secret_key():
    app = make_app({**BOTH}, rpg_llm_provider=FakeLLMProvider())
    secret = app.extensions["app_config"].secret_key.reveal()
    with request_scope(app):
        expected = protagonist_marker(secret)
        assert get_protagonist_service()._marker == expected
        assert get_protagonist_review_service()._marker == expected
        assert get_session_service()._marker == expected


@pytest.mark.parametrize("name", ["get_protagonist_service", "get_protagonist_review_service"])
def test_the_services_read_the_global_budget_through_the_usage_service(name):
    app = make_app({**BOTH}, rpg_llm_provider=FakeLLMProvider())
    with request_scope(app):
        service = {"get_protagonist_service": get_protagonist_service, "get_protagonist_review_service": get_protagonist_review_service}[name]()
        assert service._budget_exhausted() is False                                      # 沒有用量時預算未用盡（實際上限由用量服務判斷）
