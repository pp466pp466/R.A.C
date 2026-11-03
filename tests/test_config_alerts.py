"""app-configuration 規格「告警收件人設定」：`ALERT_EMAIL_TO` 的格式、上限、必填與寄信設定的相依。"""
import pytest

from app.config import MAX_ALERT_RECIPIENTS, ConfigError, load_config
from tests.support.config_env import ALERT_ENV, AUTH_ENV, production_like_env

MAIL_ONLY = {k: v for k, v in AUTH_ENV.items() if k.startswith("MAIL_")}


def env_for_testing(**overrides: str) -> dict[str, str]:
    return {"APP_ENV": "testing", **overrides}


# ---- 必填與選填 ---------------------------------------------------------------------

@pytest.mark.parametrize("env", ["staging", "production"])
def test_production_like_environments_refuse_to_start_without_recipients_and_name_only_the_variable(env):
    values = production_like_env(env)
    del values["ALERT_EMAIL_TO"]
    with pytest.raises(ConfigError) as exc:
        load_config(environ=values)
    assert "ALERT_EMAIL_TO" in str(exc.value) and env in str(exc.value)


@pytest.mark.parametrize("blank", ["", "   ", " , ,"])
def test_a_blank_value_counts_as_missing_in_production(blank):
    with pytest.raises(ConfigError, match="ALERT_EMAIL_TO"):
        load_config(environ=production_like_env(ALERT_EMAIL_TO=blank))


@pytest.mark.parametrize("env", ["development", "testing"])
def test_development_and_testing_may_leave_the_recipients_unset(env, tmp_path):
    config = load_config(environ={"APP_ENV": env, "SECRET_KEY": "k", "DATABASE_URL": "sqlite+pysqlite:///:memory:"}, dotenv_path=tmp_path / "none.env")
    assert config.alerts.recipients == () and config.mail is None


# ---- 寄信設定的相依 -------------------------------------------------------------------

def test_recipients_require_the_mail_settings():
    with pytest.raises(ConfigError) as exc:
        load_config(environ=env_for_testing(ALERT_EMAIL_TO="ops@example.com"))
    message = str(exc.value)
    assert "ALERT_EMAIL_TO" in message and "MAIL_SMTP_HOST" in message and "MAIL_SMTP_PORT" in message and "MAIL_FROM" in message
    assert "ops@example.com" not in message                                       # 不回顯設定的值


def test_recipients_with_complete_mail_settings_work():
    config = load_config(environ=env_for_testing(ALERT_EMAIL_TO="ops@example.com", **MAIL_ONLY))
    assert config.alerts.recipients == ("ops@example.com",) and config.mail is not None


def test_staging_needs_the_mail_settings_for_the_alerts():
    values = production_like_env("staging")
    for name in MAIL_ONLY:
        values.pop(name, None)
    with pytest.raises(ConfigError, match="MAIL_SMTP_HOST"):
        load_config(environ=values)


# ---- 格式 ----------------------------------------------------------------------------

def test_several_recipients_are_trimmed_and_deduplicated_ignoring_case():
    config = load_config(environ=production_like_env(ALERT_EMAIL_TO=" a@example.com, b@example.com,A@Example.com ,,"))
    assert config.alerts.recipients == ("a@example.com", "b@example.com")


def test_exactly_the_maximum_is_accepted_and_one_more_is_not():
    addresses = [f"ops{n}@example.com" for n in range(MAX_ALERT_RECIPIENTS + 1)]
    assert len(load_config(environ=production_like_env(ALERT_EMAIL_TO=",".join(addresses[:MAX_ALERT_RECIPIENTS]))).alerts.recipients) == 5
    with pytest.raises(ConfigError, match="5"):
        load_config(environ=production_like_env(ALERT_EMAIL_TO=",".join(addresses)))


def test_duplicates_do_not_count_toward_the_maximum():
    addresses = ",".join(["a@example.com"] * 9)
    assert load_config(environ=production_like_env(ALERT_EMAIL_TO=addresses)).alerts.recipients == ("a@example.com",)


@pytest.mark.parametrize(
    "bad",
    ["zzname", "Zz Team <zz@zz-host.example>", "a b@zz-host.example", "a@zz-host.example\r\nBcc: x@y.z", "zzuser@", "@zz-host.example", "a@b@zz-host.example", "zz@zz-host.example;x@zz-host.example"],
)
def test_an_address_that_is_not_plain_fails_without_echoing_the_value(bad):
    with pytest.raises(ConfigError) as exc:
        load_config(environ=production_like_env(ALERT_EMAIL_TO=bad))
    assert "ALERT_EMAIL_TO" in str(exc.value) and bad.strip() not in str(exc.value)


def test_the_alert_settings_are_part_of_the_immutable_config_and_not_secret_values():
    config = load_config(environ=production_like_env(**ALERT_ENV))
    assert config.alerts.recipients == ("ops@example.com",)
    with pytest.raises(Exception):
        config.alerts.recipients = ()                                             # type: ignore[misc]
