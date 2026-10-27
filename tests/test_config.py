"""app-configuration 規格的測試：環境選擇、必要機密、不安全設定、.env 來源、機密遮罩。"""
import logging

import pytest

from app.config import AppConfig, ConfigError, Secret, load_config

FAKE_PASSWORD = "S3cretPw-for-tests"
GOOD_URL = (
    f"mssql+pyodbc://app_user:{FAKE_PASSWORD}@db.example.com/project"
    "?driver=ODBC+Driver+18+for+SQL+Server"
)
SA_URL = f"mssql+pyodbc://sa:{FAKE_PASSWORD}@localhost/project?driver=ODBC+Driver+18+for+SQL+Server"
TRUST_URL = GOOD_URL + "&TrustServerCertificate=yes"


def prod_env(**overrides: str) -> dict[str, str]:
    env = {"APP_ENV": "production", "SECRET_KEY": "k" * 32, "DATABASE_URL": GOOD_URL}
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


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
