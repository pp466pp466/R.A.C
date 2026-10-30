"""deployment-plan 規格：部署計畫（純函式）——設定的對應完整且不含機密值、順序、排程、環境隔離。"""
import re

import pytest

from app import cli
from app.config import PROJECT_ROOT
from tests.support.scripts import load_script

plan = load_script("launch_plan")

SECRET_SENTINEL = "never-appears-S3cr3t-value"
PLAIN = {
    "BASE_URL": "https://cs.example.com", "MAIL_SMTP_HOST": "smtp.example.com", "MAIL_SMTP_PORT": "587",
    "MAIL_FROM": "service@example.com", "ALERT_EMAIL_TO": "ops@example.com,backup@example.com",
}


def target(env="staging", **overrides):
    values = dict(project="demo-project", region="asia-east1", env=env, sql_instance=f"cs-sql-{env}", image_tag="abc1234", notify_emails=("ops@example.com",))
    values.update(overrides)
    return plan.Target(**values)


def build(env="staging", plain=None, **overrides):
    return plan.build_plan(target(env, **overrides), PLAIN if plain is None else plain)


def joined(steps) -> str:
    return " ".join(" ".join(step.argv) for step in steps)


def example_variables() -> set[str]:
    text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8-sig")
    return {m.group(1) for m in re.finditer(r"^([A-Z][A-Z0-9_]*)=", text, re.M)}


# ---- 設定的對應完整且不含機密值 ----------------------------------------------------------


def test_every_variable_in_env_example_is_in_exactly_one_class():
    classes = {"secret": set(plan.SECRET_VARS), "plain": set(plan.PLAIN_VARS), "not_deployed": set(plan.NOT_DEPLOYED)}
    for variable in example_variables():
        owners = [name for name, members in classes.items() if variable in members]
        assert len(owners) == 1, f"{variable} 屬於 {owners or '沒有任何類別'}"
    classified = set().union(*classes.values())
    assert classified <= example_variables(), f"計畫列了 .env.example 沒有的變數：{sorted(classified - example_variables())}"


def test_a_variable_added_to_env_example_without_classification_would_be_caught(monkeypatch):
    monkeypatch.setattr(plan, "PLAIN_VARS", tuple(v for v in plan.PLAIN_VARS if v != "LOG_LEVEL"))
    classified = set(plan.SECRET_VARS) | set(plan.PLAIN_VARS) | set(plan.NOT_DEPLOYED)
    assert example_variables() - classified == {"LOG_LEVEL"}


def test_secrets_appear_only_as_names_and_version_references():
    steps = build(with_secrets=("OPENROUTER_API_KEY", "MAIL_SMTP_PASSWORD"))
    everything = joined(steps)
    assert SECRET_SENTINEL not in everything
    for var in ("SECRET_KEY", "DATABASE_URL", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "MAIL_SMTP_PASSWORD"):
        assert f"{var}=cs-staging-{var.lower().replace('_', '-')}:latest" in everything
    versions = [step for step in steps if step.id.startswith("add-secret-version-")]
    assert len(versions) == 5
    for step in versions:   # 沒有任何一步把值放在指令的參數裡
        assert step.stdin_from and step.stdin_from.startswith("prompt:")
        assert "--data-file=-" in step.argv and len(step.argv) < 10


def test_secret_values_never_come_through_the_plain_settings_file():
    with pytest.raises(plan.PlanError) as error:
        build(plain={**PLAIN, "GEMINI_API_KEY": SECRET_SENTINEL})
    assert "GEMINI_API_KEY" in str(error.value) and SECRET_SENTINEL not in str(error.value)


@pytest.mark.parametrize("variable", ["TEST_SQLSERVER_URL", "FLASK_DEBUG"])
def test_development_only_variables_are_refused_and_never_deployed(variable):
    with pytest.raises(plan.PlanError):
        build(plain={**PLAIN, variable: "x"})
    assert variable not in joined(build())


def test_app_env_is_the_environment_name_and_cannot_be_overridden():
    assert "APP_ENV=staging" in joined(build("staging"))
    assert "APP_ENV=production" in joined(build("production"))
    with pytest.raises(plan.PlanError):
        build("staging", plain={**PLAIN, "APP_ENV": "production"})


@pytest.mark.parametrize("missing", ["ALERT_EMAIL_TO", "MAIL_SMTP_HOST", "MAIL_FROM", "BASE_URL"])
def test_required_plain_settings_are_checked_before_anything_is_printed(missing):
    plain = {k: v for k, v in PLAIN.items() if k != missing}
    with pytest.raises(plan.PlanError) as error:
        build(plain=plain)
    assert missing in str(error.value)


def test_production_cannot_turn_email_verification_off_but_staging_can():
    with pytest.raises(plan.PlanError):
        build("production", plain={**PLAIN, "EMAIL_VERIFICATION_REQUIRED": "false"})
    staging_plain = {k: v for k, v in PLAIN.items() if k != "BASE_URL"} | {"EMAIL_VERIFICATION_REQUIRED": "false"}
    assert build("staging", plain=staging_plain)


def test_unknown_variables_and_unknown_secrets_are_refused():
    with pytest.raises(plan.PlanError):
        build(plain={**PLAIN, "SOMETHING_ELSE": "1"})
    with pytest.raises(plan.PlanError):
        build(with_secrets=("NOT_A_SECRET",))


def test_values_with_commas_are_passed_with_a_custom_delimiter_and_survive_intact():
    flag = next(a for step in build() for a in step.argv if a.startswith("--set-env-vars="))
    delimiter = flag[len("--set-env-vars=^"):][0]
    body = flag.split("^", 2)[2]
    parsed = dict(item.split("=", 1) for item in body.split(delimiter))
    assert parsed["ALERT_EMAIL_TO"] == "ops@example.com,backup@example.com" and parsed["APP_ENV"] == "staging"


# ---- 順序 -------------------------------------------------------------------------------


def index(steps, step_id):
    return next(i for i, step in enumerate(steps) if step.id == step_id)


def test_backup_comes_before_migration_and_migration_before_the_service():
    steps = build()
    assert index(steps, "backup-database") < index(steps, "deploy-migrate-job") < index(steps, "run-migrate-job") < index(steps, "deploy-service")
    assert index(steps, "build-image") < index(steps, "backup-database")
    assert index(steps, "deploy-service") < index(steps, "verify-service") < index(steps, "schedule-check-alerts")


def test_migration_runs_alembic_upgrade_head_once_without_retries():
    steps = {step.id: step for step in build()}
    job = steps["deploy-migrate-job"].argv
    assert "--command=alembic" in job and "--args=upgrade,head" in job and "--max-retries=0" in job
    assert "--wait" in steps["run-migrate-job"].argv


def test_the_service_itself_does_not_run_migrations():
    service = next(step for step in build() if step.id == "deploy-service").argv
    assert not any("alembic" in part for part in service)


def test_the_plan_groups_keep_their_relative_order():
    only = plan.build_plan(target(), PLAIN, groups=("deploy", "migrate"))
    ids = [step.id for step in only]
    assert ids.index("backup-database") < ids.index("deploy-service")


# ---- 排程 -------------------------------------------------------------------------------


def test_one_job_and_one_scheduler_per_scheduled_command_with_the_declared_frequency():
    steps = build()
    schedulers = {step.id.removeprefix("schedule-"): step for step in steps if step.id.startswith("schedule-")}
    assert set(schedulers) == set(cli.SCHEDULED_COMMANDS)
    for command, cron in cli.SCHEDULED_COMMANDS.items():
        step = schedulers[command]
        assert f"--schedule={cron}" in step.argv and "--time-zone=Asia/Taipei" in step.argv
        assert step.exists_argv and step.update_argv and step.update_argv[3] == "update"
    assert "--schedule=*/5 * * * *" in schedulers["check-alerts"].argv


def test_jobs_use_the_same_image_secrets_and_a_single_run_that_fails_on_nonzero_exit():
    steps = {step.id: step for step in build()}
    service = steps["deploy-service"].argv
    for command in cli.SCHEDULED_COMMANDS:
        job = steps[f"deploy-job-{command}"].argv
        assert next(a for a in job if a.startswith("--image=")) == next(a for a in service if a.startswith("--image="))
        assert next(a for a in job if a.startswith("--set-secrets=")) == next(a for a in service if a.startswith("--set-secrets="))
        assert "--max-retries=0" in job and "--command=flask" in job and f"--args=--app,wsgi,{command}" in job


# ---- 環境隔離 ---------------------------------------------------------------------------


def resource_names(steps) -> set[str]:
    found = set()
    for step in steps:
        for part in step.argv:
            found.update(re.findall(r"cs-[a-z0-9-]+", part))
    return found


def test_staging_and_production_share_no_resource_name():
    staging, production = resource_names(build("staging")), resource_names(build("production"))
    assert staging and production and staging.isdisjoint(production)


def test_missing_project_region_or_instance_is_refused_without_producing_steps():
    for field in ("project", "region", "sql_instance", "image_tag"):
        with pytest.raises(plan.PlanError):
            build(**{field: ""})
    with pytest.raises(plan.PlanError):
        build("development")


def test_the_plan_does_not_create_database_instances():
    assert not [step for step in build() if step.argv[:3] == ("gcloud", "sql", "instances")]
    assert [step.argv[:4] for step in build() if step.argv[:2] == ("gcloud", "sql")] == [("gcloud", "sql", "backups", "create")]


def test_a_database_instance_name_without_the_environment_name_is_refused_so_environments_cannot_share_one():
    with pytest.raises(plan.PlanError):
        build("production", sql_instance="cs-sql-staging")
    with pytest.raises(plan.PlanError):
        build("staging", sql_instance="shared-db")


def test_vpc_flags_are_added_only_when_both_network_and_subnet_are_given():
    assert "--network=" not in joined(build())
    with_vpc = joined(build(network="default", subnet="default"))
    assert "--network=default" in with_vpc and "--subnet=default" in with_vpc


# ---- 輪替 -------------------------------------------------------------------------------


def test_rotation_writes_new_versions_for_the_named_secrets_only_and_nothing_else():
    steps = plan.build_plan(target(rotate=("GEMINI_API_KEY", "OPENROUTER_API_KEY")), PLAIN, groups=("secrets",))
    assert [step.id for step in steps] == ["add-secret-version-cs-staging-gemini-api-key", "add-secret-version-cs-staging-openrouter-api-key"]
    for step in steps:
        assert step.argv[:4] == ("gcloud", "secrets", "versions", "add") and "--data-file=-" in step.argv
        assert step.stdin_from.startswith("prompt:") and step.exists_argv is None
    assert "service-accounts" not in joined(steps) and "secrets create" not in joined(steps) and "add-iam-policy-binding" not in joined(steps)


def test_rotating_an_unknown_variable_is_refused():
    with pytest.raises(plan.PlanError):
        plan.build_plan(target(rotate=("SOMETHING",)), PLAIN, groups=("secrets",))


def test_without_rotate_the_secrets_group_is_the_first_time_preparation():
    ids = [step.id for step in plan.build_plan(target(), PLAIN, groups=("secrets",))]
    assert "create-service-account" in ids and any(step_id.startswith("create-secret-") for step_id in ids)
