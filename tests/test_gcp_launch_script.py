"""deployment-plan 規格：部署腳本預設只預覽、缺少專案就拒絕、逐步確認、migration 失敗即停止、輸出不含機密。"""
import pytest

from tests.support.scripts import load_script

launch = load_script("gcp_launch")
plan = load_script("launch_plan")

SECRET_VALUE = "super-secret-value-that-must-never-be-printed-0042"
BASE_ARGS = ["--project", "demo-project", "--region", "asia-east1", "--env", "staging", "--sql-instance", "cs-sql-staging", "--image-tag", "abc1234", "--notify-email", "ops@example.com"]


@pytest.fixture()
def vars_file(tmp_path):
    path = tmp_path / "staging.vars"
    path.write_text(
        "BASE_URL=https://cs.example.com\nMAIL_SMTP_HOST=smtp.example.com\nMAIL_SMTP_PORT=587\n"
        "MAIL_FROM=service@example.com\nALERT_EMAIL_TO=ops@example.com\n", encoding="utf-8")
    return str(path)


class FakeRunner:
    def __init__(self, fail_on=None, exists=()):
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self.fail_on = fail_on          # argv 含這段字串就失敗
        self.exists = exists            # exists_argv 含這些字串之一就視為已存在

    def __call__(self, argv, stdin_text):
        self.calls.append((tuple(argv), stdin_text))
        joined = " ".join(argv)
        if self.fail_on and self.fail_on in joined:
            return launch.RunResult(7, "", "ERROR: boom")
        if "describe" in argv and "services" in argv:
            return launch.RunResult(0, "https://cs-web-staging-abc.a.run.app\n")
        if "--format=value(name)" in argv:
            if "create" in argv:                       # 建立通知管道或外部探測：回傳新資源的完整名稱
                return launch.RunResult(0, f"projects/demo/resources/id-{len(self.calls)}\n")
            listed = any(token in joined for token in self.exists)
            return launch.RunResult(0, "projects/demo/resources/existing-1\n" if listed else "")
        if "describe" in argv:
            return launch.RunResult(0 if any(token in joined for token in self.exists) else 1)
        return launch.RunResult(0)


def run(extra, *, runner=None, answers=("y",), status=200, secrets=lambda spec: SECRET_VALUE, args=None):
    out: list[str] = []
    answer_iter = iter(answers)
    code = launch.main(
        [*(args or BASE_ARGS), *extra], runner=runner or FakeRunner(), confirm=lambda prompt: next(answer_iter, "n") == "y",
        secret_reader=secrets, status_of=lambda url: status, out=out.append, image_tag_default=lambda: "fromgit",
    )
    return code, "\n".join(out)


def test_without_the_execute_flag_nothing_is_ever_run_and_the_commands_are_printed(vars_file):
    runner = FakeRunner()
    code, output = run(["plan", "--vars-file", vars_file], runner=runner)
    assert code == 0 and runner.calls == []
    assert "gcloud run deploy cs-web-staging" in output and "alembic" in output and "這是預覽" in output


def test_a_missing_project_exits_nonzero_and_prints_no_command(vars_file, capsys):
    runner = FakeRunner()
    args = [a for a in BASE_ARGS if a not in ("--project", "demo-project")]
    with pytest.raises(SystemExit) as exit_info:
        launch.main(["plan", *args, "--vars-file", vars_file], runner=runner, out=lambda text: pytest.fail(f"不該有輸出：{text}"))
    assert exit_info.value.code != 0 and "--project" in capsys.readouterr().err and runner.calls == []


def test_an_invalid_plan_exits_with_a_usage_error_and_no_command_is_printed(tmp_path, capsys):
    path = tmp_path / "bad.vars"
    path.write_text(f"GEMINI_API_KEY={SECRET_VALUE}\n", encoding="utf-8")
    code, output = run(["plan", "--vars-file", str(path)])
    assert code == launch.EXIT_USAGE and output == ""
    err = capsys.readouterr().err
    assert "GEMINI_API_KEY" in err and SECRET_VALUE not in err


def test_every_step_asks_for_confirmation_and_declining_stops_everything_after(vars_file):
    runner = FakeRunner()
    code, output = run(["migrate", "--vars-file", vars_file, "--execute"], runner=runner, answers=("y", "n"))
    assert code == launch.EXIT_DECLINED and "已停止" in output
    assert len(runner.calls) == 1 and runner.calls[0][0][:3] == ("gcloud", "sql", "backups")


def test_the_migration_failure_stops_before_the_service_is_deployed(vars_file):
    runner = FakeRunner(fail_on="jobs execute")
    code, output = run(["plan", "--vars-file", vars_file, "--execute"], runner=runner, answers=["y"] * 200)
    assert code == launch.EXIT_FAILED and "失敗" in output
    executed = [" ".join(argv) for argv, _ in runner.calls]
    assert any("jobs execute" in call for call in executed)
    assert not any("run deploy" in call for call in executed), "migration 失敗後不得部署新版程式"


def test_secret_values_go_to_stdin_only_and_never_into_arguments_or_output(vars_file):
    runner = FakeRunner()
    code, output = run(["secrets", "--execute"], runner=runner, answers=["y"] * 100)
    assert code == 0
    versions = [(argv, stdin) for argv, stdin in runner.calls if argv[:4] == ("gcloud", "secrets", "versions", "add")]
    assert len(versions) == 3 and all(stdin == SECRET_VALUE for _, stdin in versions)
    for argv, _ in runner.calls:
        assert SECRET_VALUE not in " ".join(argv)
    assert SECRET_VALUE not in output


def test_a_resource_that_already_exists_is_skipped_or_updated_not_recreated(vars_file):
    runner = FakeRunner(exists=("cs-sched-check-alerts-staging", "secrets describe", "service-accounts describe"))
    code, output = run(["plan", "--vars-file", vars_file, "--execute"], runner=runner, answers=["y"] * 300)
    assert code == 0
    calls = [" ".join(argv) for argv, _ in runner.calls]
    assert not any("service-accounts create" in call for call in calls) and not any("secrets create" in call for call in calls)
    assert any("scheduler jobs update http cs-sched-check-alerts-staging" in call for call in calls)
    assert any("scheduler jobs create http cs-sched-execute-account-deletions-staging" in call for call in calls)


def test_the_deployment_is_verified_with_healthz_and_readyz_and_fails_if_either_is_not_200(vars_file):
    ok_code, ok_output = run(["deploy", "--vars-file", vars_file, "--execute"], answers=["y"] * 10)
    assert ok_code == 0 and "/healthz → 200" in ok_output and "/readyz → 200" in ok_output
    bad_code, bad_output = run(["deploy", "--vars-file", vars_file, "--execute"], answers=["y"] * 10, status=503)
    assert bad_code == launch.EXIT_FAILED and "沒有回 200" in bad_output


def test_the_image_tag_defaults_to_the_git_commit_and_the_output_has_no_values_of_secrets(vars_file):
    args = [a for a in BASE_ARGS if a not in ("--image-tag", "abc1234")]
    code, output = run(["plan", "--vars-file", vars_file], args=args)
    assert code == 0 and ":fromgit" in output
    assert SECRET_VALUE not in output


def test_each_group_can_be_previewed_on_its_own(vars_file):
    for group, expected in (("secrets", "secrets create"), ("build", "builds submit"), ("migrate", "backups create"),
                            ("deploy", "run deploy"), ("schedule", "scheduler jobs create http")):
        code, output = run([group, "--vars-file", vars_file])
        assert code == 0 and expected in output, group


def test_groups_that_need_no_settings_do_not_require_a_settings_file():
    for group in ("secrets", "build"):
        code, _ = run([group])
        assert code == 0
    code, _ = run(["deploy"])
    assert code == launch.EXIT_USAGE
