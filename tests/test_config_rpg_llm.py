"""RPG 用途的 OpenAI 相容端點與緊急煞車環境開關的設定驗證（openai-compatible-provider、content-tier-modes）。

對應規格：llm-provider（依用途指定供應商、模型名稱、金鑰）、llm-openai-compatible（基底網址必須安全）、
content-tier-modes（緊急煞車只能以環境設定啟用、舊的等級 3 變數與 `ADULT_MODE_*` 已淘汰且被忽略）。
所有錯誤訊息只能指出變數名稱，不得含它的值。
"""
import pytest

from app.config import ConfigError, load_config
from tests.support.config_env import ALERT_ENV, AUTH_ENV, FAKE_GEMINI_KEY

FAKE_OPENROUTER_KEY = "sk-or-v1-fake-key-for-tests-0123456789"
FREE_MODEL = "cognitivecomputations/dolphin-mistral-24b-venice-edition:free"
PAID_MODEL = "cognitivecomputations/dolphin-mistral-24b-venice-edition"
RPG_ENV = {"OPENROUTER_API_KEY": FAKE_OPENROUTER_KEY, "OPENROUTER_MODEL": FREE_MODEL}


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
    assert (rpg.base_url, rpg.zero_retention, rpg.supports_json_schema, rpg.reasoning) == (None, False, False, False)
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

@pytest.mark.parametrize(
    "variable, field",
    [("OPENROUTER_ZDR", "zero_retention"), ("OPENROUTER_JSON_SCHEMA", "supports_json_schema"), ("OPENROUTER_REASONING", "reasoning")],
)
@pytest.mark.parametrize("value, expected", [("true", True), ("TRUE", True), (" false ", False), ("", False)])
def test_the_boolean_rpg_settings_accept_only_true_and_false(tmp_path, variable, field, value, expected):
    assert getattr(load_dev(tmp_path, **RPG_ENV, **{variable: value}).rpg_llm, field) is expected


@pytest.mark.parametrize("variable", ["OPENROUTER_ZDR", "OPENROUTER_JSON_SCHEMA", "OPENROUTER_REASONING"])
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


# ---- 緊急煞車（SAFE_MODE_FORCED）-----------------------------------------------------------------------

def test_the_brake_is_off_by_default_with_or_without_the_rpg_purpose(tmp_path):
    for env in ({}, RPG_ENV):
        assert load_dev(tmp_path, **env).content.safe_mode_forced is False


@pytest.mark.parametrize(("value", "expected"), [("true", True), ("TRUE", True), ("false", False), ("False", False), ("", False)])
def test_the_brake_accepts_only_true_and_false_and_has_no_other_precondition(tmp_path, value, expected):
    # 不需要 RPG 用途、模型清單、書面許可或任何其他條件：它是營運出事時全站停用「關閉安全模式」的開關，不是使用門檻
    assert load_dev(tmp_path, SAFE_MODE_FORCED=value).content.safe_mode_forced is expected


@pytest.mark.parametrize("value", ["maybe", "1", "yes", "on", "enabled"])
def test_the_brake_must_be_a_real_boolean_and_the_error_never_echoes_the_value(tmp_path, value):
    with pytest.raises(ConfigError) as caught:
        load_dev(tmp_path, SAFE_MODE_FORCED=value)
    message = str(caught.value)
    assert "SAFE_MODE_FORCED" in message and value not in message.replace("true、false", "")


@pytest.mark.parametrize("env_name", ["development", "testing", "staging", "production"])
def test_every_environment_can_set_the_brake_and_the_default_is_off(env_name):
    if env_name in ("staging", "production"):
        base = prod_env(APP_ENV=env_name)
    else:
        base = {"APP_ENV": env_name, "SECRET_KEY": "k", "DATABASE_URL": "sqlite+pysqlite:///:memory:"}
    assert load_config(environ=base).content.safe_mode_forced is False
    assert load_config(environ={**base, "SAFE_MODE_FORCED": "true"}).content.safe_mode_forced is True
    assert load_config(environ={**base, "SAFE_MODE_FORCED": "false"}).content.safe_mode_forced is False


@pytest.mark.parametrize("env_name", ["staging", "production"])
def test_an_invalid_brake_value_stops_a_production_like_start_naming_only_the_variable(env_name):
    with pytest.raises(ConfigError) as caught:
        load_config(environ=prod_env(APP_ENV=env_name, SAFE_MODE_FORCED="maybe"))
    assert "SAFE_MODE_FORCED" in str(caught.value) and "maybe" not in str(caught.value)


def test_the_content_settings_have_no_adult_mode_switch_or_model_list_any_more(tmp_path):
    settings = load_dev(tmp_path, **RPG_ENV).content
    assert not hasattr(settings, "adult_mode_enabled") and not hasattr(settings, "adult_models")


# ---- 已淘汰的變數：忽略、只警告、不會改變任何東西 ---------------------------------------------------------

OBSOLETE_LEVEL3 = ("SEXUAL_CONTENT_LEVEL3_ENABLED", "SEXUAL_CONTENT_LEVEL3_MODELS")
OBSOLETE_ADULT_MODE = ("ADULT_MODE_ENABLED", "ADULT_MODE_MODELS")
OBSOLETE_ALL = OBSOLETE_LEVEL3 + OBSOLETE_ADULT_MODE


def obsolete_warnings(caplog, name):
    return [record.getMessage() for record in caplog.records if name in record.getMessage()]


@pytest.mark.parametrize("names", [OBSOLETE_LEVEL3, OBSOLETE_ADULT_MODE])
def test_an_old_switch_set_to_on_changes_nothing_with_one_warning_naming_only_the_variable(tmp_path, caplog, names):
    values = {names[0]: "true", names[1]: FREE_MODEL}
    with caplog.at_level("WARNING", logger="app.config"):
        config = load_dev(tmp_path, **values, **RPG_ENV)
    assert config.content.safe_mode_forced is False                       # 不會讓緊急煞車啟用，也不會改變玩家可用的模式
    for name in names:
        warnings = obsolete_warnings(caplog, name)
        assert len(warnings) == 1 and "已淘汰" in warnings[0]
        assert FREE_MODEL not in warnings[0] and "true" not in warnings[0].replace(name, "")


@pytest.mark.parametrize("env_name", ["staging", "production"])
@pytest.mark.parametrize("value", ["true", "false", "maybe"])
def test_a_leftover_old_variable_never_stops_a_production_like_start_and_its_value_is_not_echoed(env_name, value, caplog):
    with caplog.at_level("WARNING", logger="app.config"):
        config = load_config(environ=prod_env(APP_ENV=env_name, **{name: value for name in OBSOLETE_ALL}, **RPG_ENV))
    assert config.content.safe_mode_forced is False
    for name in OBSOLETE_ALL:
        warnings = obsolete_warnings(caplog, name)
        assert len(warnings) == 1 and value not in warnings[0].replace(name, "")


def test_the_old_variables_do_not_switch_the_brake_off_when_it_is_on(tmp_path):
    config = load_dev(tmp_path, SAFE_MODE_FORCED="true", ADULT_MODE_ENABLED="true", ADULT_MODE_MODELS=FREE_MODEL, **RPG_ENV)
    assert config.content.safe_mode_forced is True


def test_without_the_old_variables_there_is_no_obsolete_warning(tmp_path, caplog):
    with caplog.at_level("WARNING", logger="app.config"):
        load_dev(tmp_path, SAFE_MODE_FORCED="true", **RPG_ENV)
    assert not [record for record in caplog.records if any(name in record.getMessage() for name in OBSOLETE_ALL)]


def test_reasoning_is_off_by_default_and_the_flag_is_independent_of_the_other_two(tmp_path):
    """OPENROUTER_REASONING 預設關閉；開啟它不影響零保存與結構描述兩個旗標。"""
    default = load_dev(tmp_path, **RPG_ENV).rpg_llm
    assert default.reasoning is False
    on = load_dev(tmp_path, OPENROUTER_REASONING="true", **RPG_ENV).rpg_llm
    assert (on.reasoning, on.zero_retention, on.supports_json_schema) == (True, False, False)
