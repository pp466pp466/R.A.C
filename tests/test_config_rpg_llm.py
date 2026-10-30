"""RPG 用途的 OpenAI 相容端點與等級 3 環境開關的設定驗證（openai-compatible-provider）。

對應規格：llm-provider（依用途指定供應商、模型名稱、金鑰）、llm-openai-compatible（基底網址必須安全）、
age-confirmation（等級 3 預設關閉且只能以環境設定開啟、須記錄許可涵蓋的模型）。
所有錯誤訊息只能指出變數名稱，不得含它的值。
"""
import pytest

from app.config import ConfigError, load_config
from tests.support.config_env import ALERT_ENV, AUTH_ENV, FAKE_GEMINI_KEY

FAKE_OPENROUTER_KEY = "sk-or-v1-fake-key-for-tests-0123456789"
FREE_MODEL = "cognitivecomputations/dolphin-mistral-24b-venice-edition:free"
PAID_MODEL = "cognitivecomputations/dolphin-mistral-24b-venice-edition"
RPG_ENV = {"OPENROUTER_API_KEY": FAKE_OPENROUTER_KEY, "OPENROUTER_MODEL": FREE_MODEL}
LEVEL3_ON = {"SEXUAL_CONTENT_LEVEL3_ENABLED": "true", "SEXUAL_CONTENT_LEVEL3_MODELS": f"{FREE_MODEL},{PAID_MODEL}"}


def prod_env(**overrides: str) -> dict[str, str]:
    env = {
        "APP_ENV": "production", "SECRET_KEY": "k" * 32, "DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "GEMINI_API_KEY": FAKE_GEMINI_KEY, **AUTH_ENV, **ALERT_ENV,
    }
    env.update(overrides)
    return env


def load_dev(tmp_path, **overrides: str):
    env = {"APP_ENV": "development", "SECRET_KEY": "k", "DATABASE_URL": "sqlite+pysqlite:///:memory:", **overrides}
    return load_config(environ=env, dotenv_path=tmp_path / "missing.env")


# ---- 依用途指定供應商：RPG 用途要嘛完整、要嘛完全沒有 ---------------------------------------------------

def test_without_any_openrouter_variable_the_rpg_purpose_is_simply_not_configured(tmp_path):
    config = load_dev(tmp_path, GEMINI_API_KEY=FAKE_GEMINI_KEY)
    assert config.rpg_llm is None and config.llm.enabled              # 客服與摘要完全不受影響


def test_a_complete_rpg_setting_is_parsed_and_shares_the_timeouts_but_not_the_gemini_only_settings(tmp_path):
    config = load_dev(tmp_path, GEMINI_API_KEY=FAKE_GEMINI_KEY, GEMINI_THINKING_BUDGET="128", LLM_CALL_TIMEOUT_SECONDS="15", **RPG_ENV)
    rpg = config.rpg_llm
    assert rpg.llm.api_key.reveal() == FAKE_OPENROUTER_KEY and rpg.llm.enabled
    assert (rpg.llm.primary_model, rpg.llm.fallback_model, rpg.llm.summary_model) == (FREE_MODEL,) * 3    # 沒設備援 = 主要模型
    assert rpg.llm.call_timeout_seconds == 15 and rpg.llm.request_budget_seconds == 30                    # 沿用 LLM_*
    assert rpg.llm.thinking_budget is None and config.llm.thinking_budget == 128                          # 思考預算是 Gemini 專屬
    assert (rpg.base_url, rpg.zero_retention, rpg.supports_json_schema) == (None, False, False)
    assert config.llm.api_key.reveal() == FAKE_GEMINI_KEY                                                 # 兩把金鑰互不混用


def test_a_fallback_model_can_be_set_and_the_gemini_models_are_untouched(tmp_path):
    config = load_dev(tmp_path, OPENROUTER_FALLBACK_MODEL="vendor/other:free", GEMINI_MODEL="gemini-x", **RPG_ENV)
    assert (config.rpg_llm.llm.primary_model, config.rpg_llm.llm.fallback_model) == (FREE_MODEL, "vendor/other:free")
    assert config.llm.primary_model == "gemini-x"


@pytest.mark.parametrize(
    "overrides, missing",
    [({"OPENROUTER_API_KEY": FAKE_OPENROUTER_KEY}, "OPENROUTER_MODEL"), ({"OPENROUTER_MODEL": FREE_MODEL}, "OPENROUTER_API_KEY")],
)
def test_a_partial_rpg_setting_fails_to_start_naming_the_missing_variable_without_any_value(tmp_path, overrides, missing):
    with pytest.raises(ConfigError) as caught:
        load_dev(tmp_path, **overrides)
    assert missing in str(caught.value)
    assert FAKE_OPENROUTER_KEY not in str(caught.value) and FREE_MODEL not in str(caught.value)


def test_the_rpg_key_is_masked_in_every_string_form_of_the_config(tmp_path):
    config = load_dev(tmp_path, **RPG_ENV)
    assert FAKE_OPENROUTER_KEY not in repr(config) and FAKE_OPENROUTER_KEY not in str(config.rpg_llm)


def test_the_rpg_key_is_not_a_required_secret_in_production():
    assert load_config(environ=prod_env()).rpg_llm is None            # RPG 尚未實作：選用
    assert load_config(environ=prod_env(**RPG_ENV)).rpg_llm.llm.primary_model == FREE_MODEL


# ---- 模型名稱 ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", [FREE_MODEL, PAID_MODEL, "vendor/model-name:free", "a", "m" * 64, "vendor/model_1.5:beta"])
def test_openai_compatible_model_identifiers_may_contain_slashes_and_colons(tmp_path, name):
    assert load_dev(tmp_path, OPENROUTER_API_KEY=FAKE_OPENROUTER_KEY, OPENROUTER_MODEL=name).rpg_llm.llm.primary_model == name


@pytest.mark.parametrize("name", ["vendor model", 'vendor/"model"', "vendor/模型", "m" * 65, "a;b", "vendor\\model", "x?y=1"])
@pytest.mark.parametrize("variable", ["OPENROUTER_MODEL", "OPENROUTER_FALLBACK_MODEL"])
def test_an_invalid_openai_compatible_model_name_fails_naming_the_variable_but_not_the_value(tmp_path, variable, name):
    with pytest.raises(ConfigError) as caught:
        load_dev(tmp_path, **{**RPG_ENV, variable: name})
    assert variable in str(caught.value) and name not in str(caught.value)


def test_the_gemini_model_name_rule_is_unchanged_and_still_rejects_slashes(tmp_path):
    with pytest.raises(ConfigError) as caught:
        load_dev(tmp_path, GEMINI_MODEL="vendor/model")
    assert "GEMINI_MODEL" in str(caught.value)


# ---- 兩個布林設定 --------------------------------------------------------------------------------------

@pytest.mark.parametrize("variable, field", [("OPENROUTER_ZDR", "zero_retention"), ("OPENROUTER_JSON_SCHEMA", "supports_json_schema")])
@pytest.mark.parametrize("value, expected", [("true", True), ("TRUE", True), (" false ", False), ("", False)])
def test_the_two_boolean_rpg_settings_accept_only_true_and_false(tmp_path, variable, field, value, expected):
    assert getattr(load_dev(tmp_path, **RPG_ENV, **{variable: value}).rpg_llm, field) is expected


@pytest.mark.parametrize("variable", ["OPENROUTER_ZDR", "OPENROUTER_JSON_SCHEMA"])
def test_an_invalid_boolean_fails_naming_the_variable_but_not_the_value(tmp_path, variable):
    with pytest.raises(ConfigError) as caught:
        load_dev(tmp_path, **RPG_ENV, **{variable: "maybe-so"})
    assert variable in str(caught.value) and "maybe-so" not in str(caught.value)


# ---- 基底網址：金鑰會隨它一起送出，所以必須安全 ----------------------------------------------------------

@pytest.mark.parametrize(
    "url, expected", [("https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1"), ("https://example.com/v1/", "https://example.com/v1")]
)
def test_an_https_base_url_is_accepted_and_a_trailing_slash_is_removed(tmp_path, url, expected):
    assert load_dev(tmp_path, OPENROUTER_BASE_URL=url, **RPG_ENV).rpg_llm.base_url == expected


@pytest.mark.parametrize("url", ["http://localhost:11434/v1", "http://127.0.0.1:8080", "http://[::1]:8080/v1", "http://LOCALHOST/v1"])
def test_development_may_use_http_for_a_loopback_local_model(tmp_path, url):
    assert load_dev(tmp_path, OPENROUTER_BASE_URL=url, **RPG_ENV).rpg_llm.base_url == url.rstrip("/")


def test_testing_may_also_use_http_for_a_loopback_address():
    env = {"APP_ENV": "testing", "OPENROUTER_BASE_URL": "http://127.0.0.1:9/v1", **RPG_ENV}
    assert load_config(environ=env).rpg_llm.base_url == "http://127.0.0.1:9/v1"


@pytest.mark.parametrize("env_name", ["staging", "production"])
@pytest.mark.parametrize("url", ["http://openrouter.example.com/v1", "http://localhost:11434/v1", "http://127.0.0.1/v1"])
def test_staging_and_production_never_allow_http_even_for_loopback(env_name, url):
    with pytest.raises(ConfigError) as caught:
        load_config(environ=prod_env(APP_ENV=env_name, OPENROUTER_BASE_URL=url, **RPG_ENV))
    assert "OPENROUTER_BASE_URL" in str(caught.value) and url not in str(caught.value)


@pytest.mark.parametrize("url", ["http://example.com/v1", "http://192.168.1.5:11434/v1", "http://localhost.evil.com/v1"])
def test_development_rejects_http_to_anything_but_a_loopback_address(tmp_path, url):
    with pytest.raises(ConfigError) as caught:
        load_dev(tmp_path, OPENROUTER_BASE_URL=url, **RPG_ENV)
    assert "OPENROUTER_BASE_URL" in str(caught.value) and url not in str(caught.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@example.com/v1", "https://user@example.com/v1", "https://example.com/v1?key=abc", "https://example.com/v1#frag",
        "ftp://example.com/v1", "example.com/v1", "https:///v1", "https://example.com:notaport/v1", "//example.com/v1",
    ],
)
def test_a_base_url_with_credentials_a_query_a_fragment_or_a_bad_shape_is_rejected_without_echoing_it(tmp_path, url):
    with pytest.raises(ConfigError) as caught:
        load_dev(tmp_path, OPENROUTER_BASE_URL=url, **RPG_ENV)
    message = str(caught.value)
    assert "OPENROUTER_BASE_URL" in message and "pass" not in message and "abc" not in message and url not in message


# ---- 等級 3 的環境開關 ---------------------------------------------------------------------------------

def test_level_3_is_off_by_default_even_when_the_rpg_purpose_is_configured(tmp_path):
    for config in (load_dev(tmp_path), load_dev(tmp_path, **RPG_ENV)):
        assert config.content.level3_enabled is False and config.content.level3_models == ()


def test_development_can_enable_level_3_with_a_permission_list_that_covers_the_rpg_models(tmp_path):
    config = load_dev(tmp_path, **RPG_ENV, **LEVEL3_ON)
    assert config.content.level3_enabled is True
    assert config.content.level3_models == (FREE_MODEL, PAID_MODEL)


def test_testing_can_enable_level_3_too():
    env = {"APP_ENV": "testing", **RPG_ENV, **LEVEL3_ON}
    assert load_config(environ=env).content.level3_enabled is True


@pytest.mark.parametrize("value", ["maybe", "1", "yes", "on", "enabled"])
def test_the_switch_must_be_a_real_boolean_and_the_error_never_echoes_the_value(tmp_path, value):
    with pytest.raises(ConfigError) as caught:
        load_dev(tmp_path, SEXUAL_CONTENT_LEVEL3_ENABLED=value)
    message = str(caught.value)
    assert "SEXUAL_CONTENT_LEVEL3_ENABLED" in message and value not in message.replace("true、false", "")


@pytest.mark.parametrize("env_name", ["staging", "production"])
def test_staging_and_production_refuse_to_start_with_level_3_on_until_adult_verification_exists(env_name):
    with pytest.raises(ConfigError) as caught:
        load_config(environ=prod_env(APP_ENV=env_name, **RPG_ENV, **LEVEL3_ON))      # 其他條件都齊備也一樣
    assert "SEXUAL_CONTENT_LEVEL3_ENABLED" in str(caught.value) and "成年驗證" in str(caught.value)


@pytest.mark.parametrize("env_name", ["staging", "production"])
def test_staging_and_production_start_normally_with_level_3_off_or_unset(env_name):
    assert load_config(environ=prod_env(APP_ENV=env_name, **RPG_ENV)).content.level3_enabled is False
    assert load_config(environ=prod_env(APP_ENV=env_name, SEXUAL_CONTENT_LEVEL3_ENABLED="false", **RPG_ENV)).content.level3_enabled is False


def test_level_3_on_without_the_rpg_purpose_fails_and_says_so(tmp_path):
    with pytest.raises(ConfigError) as caught:
        load_dev(tmp_path, **LEVEL3_ON)
    assert "RPG 用途尚未設定" in str(caught.value)


def test_level_3_on_with_an_empty_permission_list_fails_naming_the_list_variable(tmp_path):
    with pytest.raises(ConfigError) as caught:
        load_dev(tmp_path, SEXUAL_CONTENT_LEVEL3_ENABLED="true", **RPG_ENV)
    assert "SEXUAL_CONTENT_LEVEL3_MODELS" in str(caught.value)
    assert "沒有設定" in str(caught.value)            # 空清單要有專屬的明確訊息，而不是被「沒有涵蓋」的訊息順帶擋下


def test_level_3_on_fails_when_the_primary_model_is_not_in_the_list_and_never_echoes_the_names(tmp_path):
    with pytest.raises(ConfigError) as caught:
        load_dev(tmp_path, SEXUAL_CONTENT_LEVEL3_ENABLED="true", SEXUAL_CONTENT_LEVEL3_MODELS="vendor/something-else", **RPG_ENV)
    message = str(caught.value)
    assert "SEXUAL_CONTENT_LEVEL3_MODELS" in message and "OPENROUTER_MODEL" in message
    assert FREE_MODEL not in message and "vendor/something-else" not in message


def test_level_3_on_fails_when_only_the_fallback_model_is_missing_from_the_list(tmp_path):
    with pytest.raises(ConfigError) as caught:
        load_dev(
            tmp_path, SEXUAL_CONTENT_LEVEL3_ENABLED="true", SEXUAL_CONTENT_LEVEL3_MODELS=FREE_MODEL,
            OPENROUTER_FALLBACK_MODEL=PAID_MODEL, **RPG_ENV,
        )
    message = str(caught.value)
    assert "SEXUAL_CONTENT_LEVEL3_MODELS" in message and "OPENROUTER_FALLBACK_MODEL" in message
    assert "OPENROUTER_MODEL" not in message.replace("OPENROUTER_FALLBACK_MODEL", "")      # 主要模型有涵蓋，不該被點名


def test_the_permission_list_is_split_on_commas_and_trimmed(tmp_path):
    config = load_dev(
        tmp_path, SEXUAL_CONTENT_LEVEL3_ENABLED="true", SEXUAL_CONTENT_LEVEL3_MODELS=f"  {FREE_MODEL} ,, {PAID_MODEL},",
        OPENROUTER_FALLBACK_MODEL=PAID_MODEL, **RPG_ENV,
    )
    assert config.content.level3_models == (FREE_MODEL, PAID_MODEL)


def test_an_invalid_name_in_the_permission_list_is_rejected_when_the_switch_is_on(tmp_path):
    with pytest.raises(ConfigError) as caught:
        load_dev(tmp_path, SEXUAL_CONTENT_LEVEL3_ENABLED="true", SEXUAL_CONTENT_LEVEL3_MODELS=f"{FREE_MODEL},bad name", **RPG_ENV)
    assert "SEXUAL_CONTENT_LEVEL3_MODELS" in str(caught.value) and "bad name" not in str(caught.value)


def test_the_permission_list_has_no_effect_at_all_while_the_switch_is_off(tmp_path):
    """spec：清單不影響開關關閉時的行為，即使清單寫壞了也不能讓應用程式無法啟動。"""
    config = load_dev(tmp_path, SEXUAL_CONTENT_LEVEL3_MODELS="bad name;;", **RPG_ENV)
    assert config.content.level3_enabled is False
