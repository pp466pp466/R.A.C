"""app-configuration 規格的測試：環境選擇、必要機密、不安全設定、.env 來源、機密遮罩。"""
import logging

import pytest

from app.config import AppConfig, ConfigError, Secret, load_config
from tests.support.config_env import AUTH_ENV, FAKE_GEMINI_KEY, FAKE_SMTP_PASSWORD

FAKE_PASSWORD = "S3cretPw-for-tests"
FAKE_GOOGLE_SECRET = "GOCSPX-fake-secret-for-tests"
GOOD_URL = (
    f"mssql+pyodbc://app_user:{FAKE_PASSWORD}@db.example.com/project"
    "?driver=ODBC+Driver+18+for+SQL+Server"
)
SA_URL = f"mssql+pyodbc://sa:{FAKE_PASSWORD}@localhost/project?driver=ODBC+Driver+18+for+SQL+Server"
TRUST_URL = GOOD_URL + "&TrustServerCertificate=yes"


def prod_env(**overrides: str | None) -> dict[str, str]:
    """能通過驗證的 production 環境變數；值為 None 的覆寫代表「移除這個變數」。"""
    env: dict[str, str | None] = {
        "APP_ENV": "production", "SECRET_KEY": "k" * 32, "DATABASE_URL": GOOD_URL, "GEMINI_API_KEY": FAKE_GEMINI_KEY, **AUTH_ENV,
    }
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


def dev_env(**overrides: str | None) -> dict[str, str]:
    env: dict[str, str | None] = {"APP_ENV": "development", "SECRET_KEY": "k", "DATABASE_URL": GOOD_URL}
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


def load_dev(tmp_path, **overrides: str | None) -> AppConfig:
    return load_config(environ=dev_env(**overrides), dotenv_path=tmp_path / "missing.env")


# ---- 執行環境必須明確指定 ----------------------------------------------------

def test_unset_app_env_refuses_to_start():
    with pytest.raises(ConfigError) as exc:
        load_config(environ={})
    assert "APP_ENV" in str(exc.value)
    assert "development" in str(exc.value)


@pytest.mark.parametrize("value", ["prod", "Production", "dev", "stagingx"])
def test_invalid_app_env_is_rejected(value):
    with pytest.raises(ConfigError) as exc:
        load_config(environ={"APP_ENV": value})
    assert "合法值" in str(exc.value)


# ---- staging 與 production 缺少必要機密時啟動失敗 -----------------------------

def test_production_missing_database_url_fails_and_names_variable():
    env = prod_env()
    del env["DATABASE_URL"]
    with pytest.raises(ConfigError) as exc:
        load_config(environ=env)
    assert "DATABASE_URL" in str(exc.value)


def test_staging_missing_secret_key_fails():
    env = prod_env(APP_ENV="staging")
    del env["SECRET_KEY"]
    with pytest.raises(ConfigError) as exc:
        load_config(environ=env)
    assert "SECRET_KEY" in str(exc.value)


def test_missing_secret_error_does_not_leak_other_secret_values():
    env = prod_env()
    del env["SECRET_KEY"]
    with pytest.raises(ConfigError) as exc:
        load_config(environ=env)
    assert FAKE_PASSWORD not in str(exc.value)


def test_empty_secret_counts_as_missing():
    with pytest.raises(ConfigError):
        load_config(environ=prod_env(SECRET_KEY="   "))


def test_development_without_secret_key_uses_temporary_key(tmp_path, caplog):
    env = {"APP_ENV": "development", "DATABASE_URL": TRUST_URL}
    with caplog.at_level(logging.WARNING, logger="app.config"):
        cfg = load_config(environ=env, dotenv_path=tmp_path / "missing.env")
    assert cfg.secret_key.reveal()
    assert any("臨時金鑰" in r.message for r in caplog.records)


def test_development_without_database_url_fails(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_config(environ={"APP_ENV": "development"}, dotenv_path=tmp_path / "missing.env")
    assert "DATABASE_URL" in str(exc.value)


# ---- 預備與正式環境禁用不安全的開發設定 ---------------------------------------

def test_production_rejects_sa_account():
    with pytest.raises(ConfigError) as exc:
        load_config(environ=prod_env(DATABASE_URL=SA_URL))
    assert "sa" in str(exc.value)
    assert FAKE_PASSWORD not in str(exc.value)


def test_production_rejects_sa_in_odbc_connect_string():
    url = "mssql+pyodbc:///?odbc_connect=DRIVER%3D%7BODBC+Driver+18%7D%3BSERVER%3Dh%3BUID%3DSA%3BPWD%3Dx"
    with pytest.raises(ConfigError):
        load_config(environ=prod_env(DATABASE_URL=url))


def test_staging_rejects_trust_server_certificate():
    with pytest.raises(ConfigError) as exc:
        load_config(environ=prod_env(APP_ENV="staging", DATABASE_URL=TRUST_URL))
    assert "TrustServerCertificate" in str(exc.value)


def test_trust_server_certificate_key_is_case_insensitive():
    url = GOOD_URL + "&trustservercertificate=Yes"
    with pytest.raises(ConfigError):
        load_config(environ=prod_env(DATABASE_URL=url))


def test_production_rejects_debug_mode():
    with pytest.raises(ConfigError) as exc:
        load_config(environ=prod_env(FLASK_DEBUG="1"))
    assert "除錯" in str(exc.value)


def test_development_allows_sa_and_trust_server_certificate(tmp_path):
    env = {"APP_ENV": "development", "SECRET_KEY": "k", "DATABASE_URL": SA_URL + "&TrustServerCertificate=yes"}
    cfg = load_config(environ=env, dotenv_path=tmp_path / "missing.env")
    assert cfg.env == "development"
    assert not cfg.is_production_like


def test_valid_production_config_is_accepted_and_trusts_proxy():
    cfg = load_config(environ=prod_env())
    assert cfg.env == "production"
    assert cfg.debug is False
    assert cfg.trust_proxy is True
    assert cfg.is_production_like


def test_development_does_not_trust_proxy(tmp_path):
    env = {"APP_ENV": "development", "SECRET_KEY": "k", "DATABASE_URL": GOOD_URL}
    assert load_config(environ=env, dotenv_path=tmp_path / "missing.env").trust_proxy is False


# ---- 機密的注入來源依環境區分 -------------------------------------------------

def test_development_loads_dotenv_and_process_env_wins(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        f"SECRET_KEY=from-file\nDATABASE_URL={GOOD_URL}\nLOG_LEVEL=DEBUG\n", encoding="utf-8"
    )
    cfg = load_config(
        environ={"APP_ENV": "development", "LOG_LEVEL": "WARNING"}, dotenv_path=dotenv
    )
    assert cfg.secret_key.reveal() == "from-file"      # 來自 .env
    assert cfg.log_level == "WARNING"                  # 行程環境變數優先
    assert cfg.database_url.reveal() == GOOD_URL


def test_dotenv_with_bom_is_read(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"SECRET_KEY=bom-ok\nDATABASE_URL={GOOD_URL}\n", encoding="utf-8-sig")
    cfg = load_config(environ={"APP_ENV": "development"}, dotenv_path=dotenv)
    assert cfg.secret_key.reveal() == "bom-ok"


def test_production_ignores_dotenv_file(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"SECRET_KEY=from-file\nDATABASE_URL={GOOD_URL}\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(environ={"APP_ENV": "production"}, dotenv_path=dotenv)
    assert "DATABASE_URL" in str(exc.value)


# ---- 機密不得外洩 -------------------------------------------------------------

def test_secret_wrapper_masks_repr_and_str():
    secret = Secret(FAKE_PASSWORD)
    assert FAKE_PASSWORD not in repr(secret)
    assert FAKE_PASSWORD not in str(secret)
    assert FAKE_PASSWORD not in f"{secret}"
    assert secret.reveal() == FAKE_PASSWORD


def test_config_repr_does_not_contain_secrets():
    cfg = load_config(environ=prod_env())
    text = repr(cfg) + str(cfg)
    assert FAKE_PASSWORD not in text
    assert "k" * 32 not in text


def test_masked_database_url_hides_password():
    cfg = load_config(environ=prod_env())
    assert FAKE_PASSWORD not in cfg.database_url_masked
    assert "app_user" in cfg.database_url_masked


# ---- 測試環境與外部資源隔離 ---------------------------------------------------

def test_testing_defaults_to_in_memory_sqlite_without_secrets():
    cfg = load_config(environ={"APP_ENV": "testing"})
    assert cfg.database_url.reveal().startswith("sqlite")
    assert ":memory:" in cfg.database_url.reveal()
    assert cfg.secret_key.reveal()


def test_testing_does_not_read_dotenv(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"DATABASE_URL={SA_URL}\n", encoding="utf-8")
    cfg = load_config(environ={"APP_ENV": "testing"}, dotenv_path=dotenv)
    assert ":memory:" in cfg.database_url.reveal()


def test_invalid_log_level_is_rejected():
    with pytest.raises(ConfigError):
        load_config(environ=prod_env(LOG_LEVEL="LOUD"))


def test_app_config_is_immutable():
    cfg = load_config(environ={"APP_ENV": "testing"})
    assert isinstance(cfg, AppConfig)
    with pytest.raises(Exception):
        cfg.env = "production"  # type: ignore[misc]


# =============================================================================
# 會員驗證相關設定（member-auth：app-configuration 與 google-sign-in 規格）
# =============================================================================

# ---- 信箱驗證開關 -------------------------------------------------------------

def test_production_with_verification_off_refuses_to_start():
    with pytest.raises(ConfigError) as exc:
        load_config(environ=prod_env(EMAIL_VERIFICATION_REQUIRED="false"))
    assert "信箱驗證" in str(exc.value) and "production" in str(exc.value)


def test_staging_can_explicitly_turn_verification_off_without_any_mail_settings():
    env = prod_env(
        APP_ENV="staging", EMAIL_VERIFICATION_REQUIRED="false",
        BASE_URL=None, MAIL_SMTP_HOST=None, MAIL_SMTP_PORT=None, MAIL_FROM=None,
        MAIL_SMTP_USER=None, MAIL_SMTP_PASSWORD=None,
    )
    cfg = load_config(environ=env)
    assert cfg.email_verification_required is False
    assert cfg.mail is None


def test_development_without_the_variable_means_off_and_needs_no_mail_settings(tmp_path):
    cfg = load_dev(tmp_path)
    assert cfg.email_verification_required is False
    assert cfg.mail is None and cfg.base_url is None


def test_testing_without_the_variable_means_off():
    assert load_config(environ={"APP_ENV": "testing"}).email_verification_required is False


@pytest.mark.parametrize("env_name", ["staging", "production"])
def test_production_like_defaults_to_verification_on(env_name):
    cfg = load_config(environ=prod_env(APP_ENV=env_name))
    assert cfg.email_verification_required is True
    assert cfg.mail is not None


@pytest.mark.parametrize("value", ["maybe", "1", "yes", "on", "no"])
def test_verification_value_must_be_true_or_false(value, tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_dev(tmp_path, EMAIL_VERIFICATION_REQUIRED=value)
    assert "true" in str(exc.value) and "false" in str(exc.value)


@pytest.mark.parametrize("value,expected", [("TRUE", True), ("False", False), (" true ", True)])
def test_verification_value_is_case_and_space_insensitive(value, expected, tmp_path):
    overrides = dict(EMAIL_VERIFICATION_REQUIRED=value, BASE_URL="http://localhost:5000", **_dev_mail())
    assert load_dev(tmp_path, **overrides).email_verification_required is expected


def _dev_mail(**overrides: str) -> dict[str, str]:
    values = {"MAIL_SMTP_HOST": "localhost", "MAIL_SMTP_PORT": "1025", "MAIL_FROM": "service@example.com", "MAIL_SMTP_SECURITY": "none"}
    values.update(overrides)
    return values


# ---- 寄信與對外網址設定 -------------------------------------------------------

def test_verification_on_without_mail_host_names_the_missing_variable():
    with pytest.raises(ConfigError) as exc:
        load_config(environ=prod_env(MAIL_SMTP_HOST=None))
    assert "MAIL_SMTP_HOST" in str(exc.value)
    assert "MAIL_FROM" not in str(exc.value)  # 只列出真正缺少的


def test_verification_on_lists_every_missing_mail_variable():
    with pytest.raises(ConfigError) as exc:
        load_config(environ=prod_env(MAIL_SMTP_HOST=None, MAIL_SMTP_PORT=None, MAIL_FROM=None))
    for name in ("MAIL_SMTP_HOST", "MAIL_SMTP_PORT", "MAIL_FROM"):
        assert name in str(exc.value)


def test_verification_on_without_base_url_fails():
    with pytest.raises(ConfigError) as exc:
        load_config(environ=prod_env(BASE_URL=None))
    assert "BASE_URL" in str(exc.value)


def test_production_base_url_must_be_https():
    with pytest.raises(ConfigError) as exc:
        load_config(environ=prod_env(BASE_URL="http://cs.example.com"))
    assert "https" in str(exc.value)


def test_staging_base_url_must_be_https_but_development_may_use_http(tmp_path):
    with pytest.raises(ConfigError):
        load_config(environ=prod_env(APP_ENV="staging", BASE_URL="http://cs.example.com"))
    assert load_dev(tmp_path, BASE_URL="http://localhost:5000").base_url == "http://localhost:5000"


@pytest.mark.parametrize(
    "value",
    ["cs.example.com", "ftp://cs.example.com", "https://cs.example.com/app", "https://cs.example.com?a=1",
     "https://cs.example.com#frag", "https://user:pw@cs.example.com", "https://cs.example.com:99999", "https://"],
)
def test_invalid_base_url_is_rejected(value, tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_dev(tmp_path, BASE_URL=value)
    assert "BASE_URL" in str(exc.value)


def test_base_url_trailing_slash_is_removed(tmp_path):
    assert load_dev(tmp_path, BASE_URL="http://localhost:5000/").base_url == "http://localhost:5000"


def test_mail_username_without_password_fails_and_does_not_echo_values():
    with pytest.raises(ConfigError) as exc:
        load_config(environ=prod_env(MAIL_SMTP_PASSWORD=None))
    assert "MAIL_SMTP_USER" in str(exc.value) and "MAIL_SMTP_PASSWORD" in str(exc.value)
    assert "mailer" not in str(exc.value)


def test_mail_password_without_username_fails_and_does_not_echo_values():
    with pytest.raises(ConfigError) as exc:
        load_config(environ=prod_env(MAIL_SMTP_USER=None))
    assert FAKE_SMTP_PASSWORD not in str(exc.value)


def test_mail_without_credentials_is_allowed(tmp_path):
    cfg = load_dev(tmp_path, EMAIL_VERIFICATION_REQUIRED="true", BASE_URL="http://localhost:5000", **_dev_mail())
    assert cfg.mail.username is None and cfg.mail.password is None


@pytest.mark.parametrize("env_name", ["staging", "production"])
def test_production_like_rejects_unencrypted_mail_connection(env_name):
    with pytest.raises(ConfigError) as exc:
        load_config(environ=prod_env(APP_ENV=env_name, MAIL_SMTP_SECURITY="none"))
    assert "MAIL_SMTP_SECURITY" in str(exc.value)


def test_development_and_testing_allow_unencrypted_mail_connection(tmp_path):
    overrides = dict(EMAIL_VERIFICATION_REQUIRED="true", BASE_URL="http://localhost:5000", **_dev_mail())
    assert load_dev(tmp_path, **overrides).mail.security == "none"
    testing = {"APP_ENV": "testing", "EMAIL_VERIFICATION_REQUIRED": "true", "BASE_URL": "http://localhost", **_dev_mail()}
    assert load_config(environ=testing).mail.security == "none"


def test_mail_security_defaults_to_starttls_and_rejects_unknown_modes():
    assert load_config(environ=prod_env()).mail.security == "starttls"
    assert load_config(environ=prod_env(MAIL_SMTP_SECURITY="SSL")).mail.security == "ssl"
    with pytest.raises(ConfigError):
        load_config(environ=prod_env(MAIL_SMTP_SECURITY="tls13"))


@pytest.mark.parametrize("port", ["abc", "0", "70000", "-1"])
def test_invalid_mail_port_is_rejected(port):
    with pytest.raises(ConfigError) as exc:
        load_config(environ=prod_env(MAIL_SMTP_PORT=port))
    assert "MAIL_SMTP_PORT" in str(exc.value)


@pytest.mark.parametrize("address", ["service", "a b@example.com", "Service <a@example.com>", "a@b@example.com", "a@example.com\r\nBcc: x@y.z"])
def test_invalid_mail_from_is_rejected(address):
    with pytest.raises(ConfigError) as exc:
        load_config(environ=prod_env(MAIL_FROM=address))
    assert "MAIL_FROM" in str(exc.value)


def test_mail_settings_are_parsed():
    mail = load_config(environ=prod_env()).mail
    assert (mail.host, mail.port, mail.from_address, mail.username) == ("smtp.example.com", 587, "service@example.com", "mailer")
    assert mail.password.reveal() == FAKE_SMTP_PASSWORD


def test_printing_the_config_masks_the_mail_password_and_google_secret():
    cfg = load_config(environ=prod_env(GOOGLE_OAUTH_CLIENT_ID="id.apps.googleusercontent.com", GOOGLE_OAUTH_CLIENT_SECRET=FAKE_GOOGLE_SECRET))
    text = repr(cfg) + str(cfg) + repr(cfg.mail) + repr(cfg.google)
    assert FAKE_SMTP_PASSWORD not in text
    assert FAKE_GOOGLE_SECRET not in text


# ---- 簽章金鑰的強度與輪替 -----------------------------------------------------

@pytest.mark.parametrize("env_name", ["staging", "production"])
def test_production_like_requires_a_long_secret_key_and_never_echoes_it(env_name):
    short = "s" * 16
    with pytest.raises(ConfigError) as exc:
        load_config(environ=prod_env(APP_ENV=env_name, SECRET_KEY=short))
    assert "SECRET_KEY" in str(exc.value) and "32" in str(exc.value)
    assert short not in str(exc.value)


def test_secret_key_of_exactly_32_characters_is_accepted_and_development_has_no_minimum(tmp_path):
    assert load_config(environ=prod_env(SECRET_KEY="k" * 32)).secret_key.reveal() == "k" * 32
    assert load_dev(tmp_path, SECRET_KEY="short").secret_key.reveal() == "short"


def test_secret_key_fallbacks_are_parsed_and_masked(tmp_path):
    cfg = load_dev(tmp_path, SECRET_KEY_FALLBACKS="old-one, old-two ,,")
    assert [s.reveal() for s in cfg.secret_key_fallbacks] == ["old-one", "old-two"]
    assert "old-one" not in repr(cfg)
    assert load_dev(tmp_path).secret_key_fallbacks == ()


# ---- Google 登入為選用 --------------------------------------------------------

def test_google_is_disabled_when_nothing_is_configured(tmp_path):
    cfg = load_dev(tmp_path)
    assert cfg.google is None and cfg.google_enabled is False


def test_google_is_enabled_when_both_values_are_set_and_needs_base_url(tmp_path):
    values = dict(GOOGLE_OAUTH_CLIENT_ID="cid.apps.googleusercontent.com", GOOGLE_OAUTH_CLIENT_SECRET=FAKE_GOOGLE_SECRET)
    cfg = load_dev(tmp_path, BASE_URL="http://localhost:5000", **values)
    assert cfg.google_enabled and cfg.google.client_id == "cid.apps.googleusercontent.com"
    assert cfg.google.client_secret.reveal() == FAKE_GOOGLE_SECRET
    with pytest.raises(ConfigError) as exc:
        load_dev(tmp_path, **values)  # 沒有 BASE_URL
    assert "BASE_URL" in str(exc.value) and "Google" in str(exc.value)
    assert FAKE_GOOGLE_SECRET not in str(exc.value)


def test_google_with_only_the_client_id_fails_and_names_the_missing_variable(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_dev(tmp_path, GOOGLE_OAUTH_CLIENT_ID="cid.apps.googleusercontent.com")
    assert "GOOGLE_OAUTH_CLIENT_SECRET" in str(exc.value)


def test_google_with_only_the_secret_fails_without_echoing_it(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_dev(tmp_path, GOOGLE_OAUTH_CLIENT_SECRET=FAKE_GOOGLE_SECRET)
    assert "GOOGLE_OAUTH_CLIENT_ID" in str(exc.value)
    assert FAKE_GOOGLE_SECRET not in str(exc.value)


def test_google_and_base_url_are_read_from_the_dotenv_file_in_development(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        f"DATABASE_URL={GOOD_URL}\nBASE_URL=http://localhost:5000\n"
        f"GOOGLE_OAUTH_CLIENT_ID=cid.apps.googleusercontent.com\nGOOGLE_OAUTH_CLIENT_SECRET={FAKE_GOOGLE_SECRET}\n",
        encoding="utf-8",
    )
    cfg = load_config(environ={"APP_ENV": "development", "SECRET_KEY": "k"}, dotenv_path=dotenv)
    assert cfg.google_enabled and cfg.base_url == "http://localhost:5000"


# ---- 模型呼叫設定（cs-chat-core：llm-provider 與 app-configuration 規格）---------

def test_llm_defaults_when_nothing_is_set(tmp_path):
    llm = load_dev(tmp_path, GEMINI_API_KEY=FAKE_GEMINI_KEY).llm
    assert (llm.primary_model, llm.fallback_model) == ("gemini-3.8-flash", "gemini-3.5-flash-lite")
    assert llm.summary_model == llm.primary_model                      # 未指定時與主要模型相同
    assert (llm.call_timeout_seconds, llm.request_budget_seconds, llm.breaker_seconds) == (20, 30, 60)
    assert llm.max_output_tokens == 4096 and llm.thinking_budget is None
    assert llm.enabled and llm.api_key.reveal() == FAKE_GEMINI_KEY
    assert llm.frontend_timeout_seconds == 35                           # 後端總預算加 5 秒


def test_models_can_be_chosen_per_purpose(tmp_path):
    llm = load_dev(tmp_path, GEMINI_MODEL="m-main.1", GEMINI_FALLBACK_MODEL="m_fb", GEMINI_SUMMARY_MODEL="m-sum").llm
    assert (llm.primary_model, llm.fallback_model, llm.summary_model) == ("m-main.1", "m_fb", "m-sum")
    assert load_dev(tmp_path, GEMINI_MODEL="m-main").llm.summary_model == "m-main"   # 摘要預設跟著主要模型


@pytest.mark.parametrize("name", ["GEMINI_MODEL", "GEMINI_FALLBACK_MODEL", "GEMINI_SUMMARY_MODEL"])
@pytest.mark.parametrize("bad", ["models/x", "a b", "x?key=1", "ｍｏｄ", "m" * 65])
def test_an_invalid_model_name_fails_and_names_the_variable_without_its_value(tmp_path, name, bad):
    with pytest.raises(ConfigError) as exc:
        load_dev(tmp_path, **{name: bad})
    assert name in str(exc.value) and bad not in str(exc.value)


def test_a_model_name_of_exactly_64_characters_is_accepted(tmp_path):
    assert load_dev(tmp_path, GEMINI_MODEL="m" * 64).llm.primary_model == "m" * 64


@pytest.mark.parametrize(
    ("overrides", "variable"),
    [
        ({"LLM_CALL_TIMEOUT_SECONDS": "40", "LLM_REQUEST_BUDGET_SECONDS": "30"}, "LLM_CALL_TIMEOUT_SECONDS"),   # 單次大於總預算
        ({"LLM_REQUEST_BUDGET_SECONDS": "55"}, "LLM_REQUEST_BUDGET_SECONDS"),                                    # 必須小於 55
        ({"LLM_REQUEST_BUDGET_SECONDS": "60"}, "LLM_REQUEST_BUDGET_SECONDS"),
        ({"LLM_CALL_TIMEOUT_SECONDS": "0"}, "LLM_CALL_TIMEOUT_SECONDS"),
        ({"LLM_CALL_TIMEOUT_SECONDS": "-3"}, "LLM_CALL_TIMEOUT_SECONDS"),
        ({"LLM_CALL_TIMEOUT_SECONDS": "abc"}, "LLM_CALL_TIMEOUT_SECONDS"),
        ({"LLM_REQUEST_BUDGET_SECONDS": "nan"}, "LLM_REQUEST_BUDGET_SECONDS"),
        ({"LLM_REQUEST_BUDGET_SECONDS": "inf"}, "LLM_REQUEST_BUDGET_SECONDS"),
        ({"LLM_BREAKER_SECONDS": "0"}, "LLM_BREAKER_SECONDS"),
        ({"LLM_MAX_OUTPUT_TOKENS": "100"}, "LLM_MAX_OUTPUT_TOKENS"),
        ({"LLM_MAX_OUTPUT_TOKENS": "32769"}, "LLM_MAX_OUTPUT_TOKENS"),
        ({"LLM_MAX_OUTPUT_TOKENS": "many"}, "LLM_MAX_OUTPUT_TOKENS"),
        ({"GEMINI_THINKING_BUDGET": "-1"}, "GEMINI_THINKING_BUDGET"),
        ({"GEMINI_THINKING_BUDGET": "x"}, "GEMINI_THINKING_BUDGET"),
    ],
)
def test_contradictory_or_invalid_llm_numbers_fail_and_name_the_variable(tmp_path, overrides, variable):
    with pytest.raises(ConfigError) as exc:
        load_dev(tmp_path, **overrides)
    assert variable in str(exc.value)


def test_valid_llm_numbers_are_parsed_and_the_frontend_timeout_follows_the_budget(tmp_path):
    llm = load_dev(
        tmp_path, LLM_CALL_TIMEOUT_SECONDS="25.5", LLM_REQUEST_BUDGET_SECONDS="40", LLM_BREAKER_SECONDS="90",
        LLM_MAX_OUTPUT_TOKENS="256", GEMINI_THINKING_BUDGET="0",
    ).llm
    assert (llm.call_timeout_seconds, llm.request_budget_seconds, llm.breaker_seconds) == (25.5, 40, 90)
    assert llm.max_output_tokens == 256 and llm.thinking_budget == 0       # 0 是合法值（關閉思考），不能被當成未設定
    assert llm.frontend_timeout_seconds == 45


def test_a_call_timeout_equal_to_the_budget_is_accepted(tmp_path):
    assert load_dev(tmp_path, LLM_CALL_TIMEOUT_SECONDS="30").llm.call_timeout_seconds == 30


@pytest.mark.parametrize("env_name", ["staging", "production"])
def test_production_like_without_the_model_key_fails_and_names_only_the_variable(env_name):
    env = prod_env(APP_ENV=env_name, GEMINI_API_KEY=None)
    with pytest.raises(ConfigError) as exc:
        load_config(environ=env)
    assert "GEMINI_API_KEY" in str(exc.value)
    assert FAKE_PASSWORD not in str(exc.value) and FAKE_GEMINI_KEY not in str(exc.value)


def test_an_empty_model_key_counts_as_missing_in_production():
    with pytest.raises(ConfigError):
        load_config(environ=prod_env(GEMINI_API_KEY="   "))


def test_development_without_the_key_starts_with_the_feature_off(tmp_path):
    llm = load_dev(tmp_path).llm
    assert llm.api_key is None and llm.enabled is False


def test_testing_without_the_key_needs_nothing():
    assert load_config(environ={"APP_ENV": "testing"}).llm.enabled is False


def test_the_model_key_never_appears_in_the_config_string_forms(tmp_path):
    cfg = load_dev(tmp_path, GEMINI_API_KEY=FAKE_GEMINI_KEY)
    assert FAKE_GEMINI_KEY not in repr(cfg) and FAKE_GEMINI_KEY not in str(cfg) and FAKE_GEMINI_KEY not in repr(cfg.llm)


def test_the_model_key_is_read_from_the_dotenv_file_in_development_only(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"DATABASE_URL={GOOD_URL}\nGEMINI_API_KEY={FAKE_GEMINI_KEY}\n", encoding="utf-8")
    cfg = load_config(environ={"APP_ENV": "development", "SECRET_KEY": "k"}, dotenv_path=dotenv)
    assert cfg.llm.api_key.reveal() == FAKE_GEMINI_KEY
    testing = load_config(environ={"APP_ENV": "testing"}, dotenv_path=dotenv)       # testing 不讀 .env
    assert testing.llm.api_key is None