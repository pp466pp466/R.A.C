"""aws-demo-environment 規格：執行器（scripts/aws_demo.py）——預設預覽、逐步確認、單步執行、機密不經命令列、
輪詢與逾時、DuckDNS 權杖不外洩、拆除先列出快照並要求明確的例外回覆。

全部以假的 runner 驗證（不呼叫任何 AWS 指令，也不連網路）；步驟的內容與順序由 tests/test_aws_demo_plan.py 負責。
"""
import io
import json
import os
import re
import stat
import subprocess
import tempfile
import urllib.error
from contextlib import redirect_stderr
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from app import config as app_config
from app.config import PROJECT_ROOT
from tests.support.scripts import load_script

plan = load_script("aws_demo_plan")
demo = load_script("aws_demo")

ACCOUNT = "123456789012"
EMAIL = "ops@example.com"
TAG = "abc1234"
TODAY = date(2026, 11, 20)
DB = "rac-demo-staging-db"
SNAP_A = f"{DB}-pre-aaaaaaa"
SNAP_B = f"{DB}-pre-bbbbbbb"
SNAP_FINAL = f"{DB}-final-20261001"
SECRET_VALUE = "Never-Appears-S3cret-0042-value"
DUCKDNS_TOKEN = "11111111-2222-3333-4444-555555555555"

BASE = ["--account-id", ACCOUNT]
PROVISION = ["provision", *BASE, "--notify-email", EMAIL]
DEPLOY = ["deploy", *BASE, "--tag", TAG]
ROLLBACK = ["rollback", *BASE, "--tag", TAG]
TEARDOWN = ["teardown", *BASE]
DUCKDNS = ["duckdns", *BASE]

DESTRUCTIVE_VERBS = ("delete-", "terminate-", "release-", "detach-", "remove-", "rb")


@pytest.fixture(autouse=True)
def spool(tmp_path, monkeypatch):
    """所有暫存檔都落在這個目錄：測試結束前可以檢查沒有殘留（機密暫存檔用後一定刪除）。"""
    directory = tmp_path / "spool"
    directory.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(directory))
    return directory


class Clock:
    """假的時鐘：`sleep` 讓時間前進，所以輪詢的逾時不需要真的等待。"""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeAws:
    """依規則回覆的假 runner。規則是「argv 串起來後含有全部的字串」，第一條符合的生效；沒有符合的就成功且沒有輸出。
    `out` 給清單時依序回覆（最後一個重複）。記錄每次呼叫，以及呼叫當下 `file://` 暫存檔的內容。"""

    def __init__(self):
        self.calls = []
        self.rules = []
        self.bodies = []
        self.events = []

    def when(self, *tokens, out="", rc=0, err="", first=False):
        rule = {"tokens": tokens, "queue": [out] if isinstance(out, str) else list(out), "rc": rc, "err": err}
        self.rules.insert(0, rule) if first else self.rules.append(rule)
        return self

    def with_identity(self):
        return self.when("sts", "get-caller-identity", out=ACCOUNT)

    def __call__(self, argv):
        assert isinstance(argv, tuple) and argv and all(isinstance(part, str) for part in argv), "指令必須是字串引數的 tuple（不經 shell）"
        self.calls.append(argv)
        self.events.append(("run", argv))
        for part in argv:
            if part.startswith("file://"):
                try:
                    self.bodies.append(Path(part[len("file://"):]).read_text(encoding="utf-8"))
                except OSError:
                    self.bodies.append(None)
        joined = " ".join(argv)
        for rule in self.rules:
            if all(token in joined for token in rule["tokens"]):
                queue = rule["queue"]
                text = queue.pop(0) if len(queue) > 1 else queue[0]
                return demo.RunResult(rule["rc"], text, rule["err"])
        return demo.RunResult(0, "")

    def matching(self, *tokens):
        return [call for call in self.calls if all(token in " ".join(call) for token in tokens)]

    def ran(self, *tokens):
        return bool(self.matching(*tokens))

    def destructive(self):
        return [call for call in self.calls if len(call) > 2 and call[0] == "aws" and (call[2].startswith(DESTRUCTIVE_VERBS) or call[1] == "s3" and call[2] == "rb")]


class HookedAws(FakeAws):
    """FakeAws，但每次呼叫之前先執行 hook（可檢查暫存檔、丟例外）；hook 回傳 RunResult 就用它當回覆。"""

    def __init__(self, hook):
        super().__init__()
        self.hook = hook

    def __call__(self, argv):
        override = self.hook(argv)
        result = super().__call__(argv)
        return override or result


def put_runner(runner=None):
    """建立 SSM 參數的步驟需要的回覆：參數還不存在，建立後的類型驗證是 SecureString。"""
    runner = runner or FakeAws()
    runner.with_identity()
    runner.when("get-parameter", "Parameter.Name", rc=254)
    runner.when("describe-parameters", out="SecureString")
    return runner


class Result:
    def __init__(self, code, lines, err, runner, prompts, asked):
        self.code = code
        self.text = "\n".join(lines)
        self.err = err
        self.runner = runner
        self.prompts = prompts
        self.asked = asked


def invoke(argv, *, runner=None, answers=None, replies=(), clock=None, environ=None, fetch=None, today=TODAY, tag=TAG, project_root=None):
    """執行 `demo.main`。`answers=None` 表示每次確認都回 y；給清單時依序回覆，用完就回 n。`replies` 是 `ask` 的回覆（用完回空字串）。"""
    runner = runner if runner is not None else FakeAws()
    events = runner.events if hasattr(runner, "events") else []
    lines, prompts, asked = [], [], []
    answer_iter = None if answers is None else iter(answers)
    reply_iter = iter(replies)

    def confirm(prompt):
        events.append(("confirm", prompt))
        prompts.append(prompt)
        return True if answer_iter is None else bool(next(answer_iter, False))

    def ask(prompt):
        events.append(("ask", prompt))
        asked.append(prompt)
        return next(reply_iter, "")

    clock = clock or Clock()
    err = io.StringIO()
    with redirect_stderr(err):
        code = demo.main(
            argv, runner=runner, confirm=confirm, ask=ask, out=lines.append, sleep=clock.sleep, clock=clock,
            environ={} if environ is None else environ, fetch=fetch or (lambda url: (200, "OK")), tag_default=lambda: tag,
            today_fn=lambda: today, project_root=project_root)
    return Result(code, lines, err.getvalue(), runner, prompts, asked)


def make_executor(runner, steps=(), *, confirm=None, clock=None, secrets=None, project_root=None):
    target = plan.Target(account_id=ACCOUNT, notify_emails=(EMAIL,))
    redactor = demo.Redactor()
    out = []
    clock = clock or Clock()
    executor = demo.Executor(
        target=target, catalog=list(steps), runner=runner, confirm=confirm or (lambda prompt: True), out=out.append,
        sleep=clock.sleep, clock=clock, redactor=redactor, secrets=demo.SecretStore(target, secrets or {}, runner, redactor),
        today=TODAY, project_root=project_root or PROJECT_ROOT)
    return executor, out


def step(step_id, argv=(), **fields):
    return plan.Step(step_id, "test", f"測試步驟 {step_id}", tuple(argv), **fields)


def sample_target():
    return plan.Target(account_id=ACCOUNT, notify_emails=(EMAIL,))


# ---- 2.1 預設預覽 ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [PROVISION, DEPLOY, ROLLBACK, TEARDOWN, DUCKDNS, [*TEARDOWN, "--final-snapshot"]], ids=lambda a: a[0] + str(len(a)))
def test_without_the_execute_flag_nothing_is_ever_run(argv):
    result = invoke(argv)
    assert result.code == 0 and result.runner.calls == [] and result.prompts == [] and result.asked == []
    assert "這是預覽" in result.text and "--execute" in result.text


def test_the_preview_prints_the_commands_with_their_region():
    result = invoke(PROVISION)
    assert "aws ec2 create-security-group" in result.text and "--region=ap-southeast-2" in result.text
    assert "[1/" in result.text and "目標：帳號 123456789012" in result.text


@pytest.mark.parametrize("region", ["ap-northeast-1", "us-east-1", ""])
def test_any_other_region_is_refused_before_anything_runs(region):
    runner = FakeAws().with_identity()
    result = invoke([*PROVISION, "--region", region, "--execute"], runner=runner)
    assert result.code == demo.EXIT_USAGE and runner.calls == [] and "ap-southeast-2" in result.err and result.text == ""


def test_the_account_id_is_required_and_must_be_twelve_digits(capsys):
    with pytest.raises(SystemExit) as exit_info:
        demo.main(["provision", "--notify-email", EMAIL], runner=FakeAws())
    assert exit_info.value.code != 0 and "--account-id" in capsys.readouterr().err
    result = invoke(["provision", "--account-id", "12345", "--notify-email", EMAIL])
    assert result.code == demo.EXIT_USAGE and "12 位數字" in result.err


def test_provisioning_without_notification_emails_is_refused():
    result = invoke(["provision", *BASE])
    assert result.code == demo.EXIT_USAGE and "通知信箱" in result.err and result.text == ""


def test_only_and_yes_rules_are_enforced_before_anything_runs():
    runner = FakeAws().with_identity()
    assert invoke([*PROVISION, "--yes"], runner=runner).code == demo.EXIT_USAGE                       # --yes 不能用在整份計畫
    unknown = invoke([*PROVISION, "--only", "no-such-step", "--yes"], runner=runner)
    assert unknown.code == demo.EXIT_USAGE and "create-host-sg" in unknown.err                      # 列出可用的步驟代碼
    both = invoke([*PROVISION, "--only", "create-host-sg", "--group", "network", "--yes"], runner=runner)
    assert both.code == demo.EXIT_USAGE and runner.calls == []


def test_only_without_execute_previews_just_that_step_and_runs_nothing():
    result = invoke([*PROVISION, "--only", "create-host-sg"])
    assert result.code == 0 and result.runner.calls == [] and "[1/1]" in result.text and "create-security-group" in result.text
    assert "authorize-host-ingress" not in result.text and "create-db-sg" not in result.text


# ---- 2.1 逐步確認、拒絕即停止、單步執行 ------------------------------------------------------------------------


def group_runner():
    runner = FakeAws().with_identity()
    runner.when("describe-vpcs", out="vpc-0abc")
    runner.when("describe-tags", "'Project'", out="rac-demo")
    runner.when("describe-tags", "'Environment'", out="staging")
    runner.when("create-security-group", out=["sg-0host", "sg-0db"])
    return runner


def test_every_step_asks_first_and_declining_stops_everything_after():
    runner = group_runner()
    result = invoke([*PROVISION, "--group", "network", "--execute"], runner=runner, answers=[True, True, False])
    assert result.code == demo.EXIT_DECLINED and "已停止" in result.text
    assert len(result.prompts) == 3
    assert runner.ran("create-security-group") and not runner.ran("authorize-security-group-ingress", "sg-0host", "IpPermissions")
    assert not runner.ran("authorize-security-group-ingress")     # 第三步（開放 80、443）被拒絕：它與之後的步驟都沒有執行


def test_declining_the_very_first_step_runs_nothing_after_the_identity_check():
    runner = group_runner()
    result = invoke([*PROVISION, "--group", "network", "--execute"], runner=runner, answers=[False])
    assert result.code == demo.EXIT_DECLINED
    assert [call[1:3] for call in runner.calls if call[1] != "sts"] == []


def test_a_single_step_runs_without_asking_and_touches_no_neighbour():
    runner = group_runner()
    result = invoke([*PROVISION, "--only", "create-host-sg", "--yes"], runner=runner)
    assert result.code == 0 and result.prompts == []
    assert len(runner.matching("create-security-group")) == 1
    for other in ("authorize-security-group-ingress", "create-db-instance", "run-instances", "put-parameter", "create-budget"):
        assert not runner.ran(other)
    assert result.text.count("[1/1]") == 1


def test_only_with_execute_but_without_yes_still_asks():
    runner = group_runner()
    result = invoke([*PROVISION, "--only", "create-host-sg", "--execute"], runner=runner, answers=[False])
    assert result.code == demo.EXIT_DECLINED and not runner.ran("create-security-group")
    assert len(result.prompts) == 1


def test_the_account_is_checked_before_anything_is_created_and_a_mismatch_stops_everything():
    runner = FakeAws().when("sts", "get-caller-identity", out="999999999999")
    result = invoke([*PROVISION, "--only", "create-host-sg", "--yes"], runner=runner)
    assert result.code == demo.EXIT_FAILED and "驗證 1/1 不符" in result.err and "999999999999" in result.err
    assert not runner.ran("create-security-group") and not runner.ran("describe-vpcs")


def test_the_identity_step_is_not_duplicated_when_the_plan_already_starts_with_it():
    runner = FakeAws().with_identity()
    result = invoke([*PROVISION, "--group", "preflight", "--execute"], runner=runner, answers=[True])
    assert result.code == 0 and "帳號確認（唯讀）" not in result.text


def test_a_step_that_already_exists_is_skipped_and_its_value_is_used_by_later_checks():
    runner = group_runner()
    runner.when("describe-security-groups", "group-name", out="sg-0existing")
    result = invoke([*PROVISION, "--only", "create-host-sg", "--yes"], runner=runner)
    assert result.code == 0 and "已存在，略過" in result.text and not runner.ran("create-security-group")
    assert runner.ran("describe-tags", "Values=sg-0existing")


def test_a_failing_command_stops_the_run_and_shows_only_the_last_error_line():
    runner = group_runner()
    runner.when("create-security-group", rc=254, err="Traceback (most recent call last)\nAn error occurred (UnauthorizedOperation) when calling X", first=True)
    result = invoke([*PROVISION, "--group", "network", "--execute"], runner=runner)
    assert result.code == demo.EXIT_FAILED and "失敗" in result.text and "UnauthorizedOperation" in result.text
    assert "Traceback" not in result.text and not runner.ran("authorize-security-group-ingress")


def test_a_verification_mismatch_fails_the_step():
    runner = group_runner()
    runner.when("describe-tags", "'Environment'", out="production", first=True)
    result = invoke([*PROVISION, "--only", "create-host-sg", "--yes"], runner=runner)
    assert result.code == demo.EXIT_FAILED and "不符" in result.text and "預期 staging" in result.text


def test_commands_are_always_argument_tuples_and_never_a_shell_string():
    runner = group_runner()
    invoke([*PROVISION, "--group", "network", "--execute"], runner=runner)
    create = runner.matching("create-security-group")[0]            # FakeAws 另外斷言每次呼叫都是字串 tuple
    assert "rac demo host: web ingress only" in create             # 含空白的值是同一個引數，沒有被拆開（沒有 shell）
    assert any(part.startswith('[{"ResourceType":"security-group"') for part in create)


def test_the_source_never_uses_a_shell_or_dynamic_evaluation():
    source = (PROJECT_ROOT / "scripts" / "aws_demo.py").read_text(encoding="utf-8")
    for forbidden in ("shell=True", "os.system", "os.popen", "eval(", "exec(", "pickle"):
        assert forbidden not in source
    for call in re.findall(r"\b(?:open|fdopen)\([^)]*\)", source):
        assert "encoding=" in call or "'w'" not in call, call


# ---- 2.1 費用護欄與被擋下的步驟 -------------------------------------------------------------------------------


def test_without_accept_cost_billing_commands_are_never_printed_and_the_cost_is_shown():
    result = invoke([*PROVISION, "--group", "rds", "--group", "ec2"])
    assert "create-db-instance" not in result.text and "run-instances" not in result.text and "allocate-address" not in result.text
    assert "--accept-cost" in result.text and "預估費用" in result.text and "合計約 US$" in result.text and "2026-12-31" in result.text


def test_with_accept_cost_the_billing_commands_are_printed_after_the_cost():
    result = invoke([*PROVISION, "--group", "rds", "--group", "ec2", "--accept-cost"])
    assert "create-db-instance" in result.text and "run-instances" in result.text
    assert result.text.index("預估費用") < result.text.index("create-db-instance")


def test_a_blocked_step_stops_the_run_and_never_executes_a_command():
    runner = FakeAws().with_identity()
    result = invoke([*PROVISION, "--only", "create-rds-instance", "--yes"], runner=runner)
    assert result.code == demo.EXIT_BLOCKED and "被擋下" in result.text
    assert not runner.ran("create-db-instance") and result.prompts == []


def test_a_blocked_step_in_the_middle_of_a_group_stops_there_and_the_rest_is_not_run():
    runner = FakeAws().with_identity()
    runner.when("describe-vpcs", out="vpc-0abc")
    runner.when("describe-subnets", out='["subnet-a","subnet-b"]')
    runner.when("describe-db-subnet-groups", rc=254)
    result = invoke([*PROVISION, "--group", "rds", "--execute"], runner=runner)
    assert result.code == demo.EXIT_BLOCKED and "注意：這些步驟被擋下" in result.text
    assert not runner.ran("create-db-instance")


def test_with_accept_cost_the_rds_step_runs_with_the_master_password_read_back_from_ssm():
    master = "Master-Pw-0123456789abcdefghijkl"
    runner = FakeAws().with_identity()
    runner.when("get-parameter", "--with-decryption", out=master)
    runner.when("describe-security-groups", out="sg-0db")
    runner.when("describe-vpcs", out="vpc-0abc")
    runner.when("describe-db-instances", rc=254)
    result = invoke([*PROVISION, "--only", "create-rds-instance", "--accept-cost", "--yes"], runner=runner)
    assert result.code == 0 and "預估費用" in result.text
    body = json.loads(runner.bodies[-1])
    assert body["MasterUserPassword"] == master and body["VpcSecurityGroupIds"] == ["sg-0db"] and body["PubliclyAccessible"] is False
    assert not any(master in " ".join(call) for call in runner.calls) and master not in result.text and master not in result.err


# ---- 2.1 佔位符、擷取與取回 ----------------------------------------------------------------------------------


def test_a_capture_feeds_the_next_step_and_values_with_several_ids_become_separate_arguments():
    runner = FakeAws()
    runner.when("describe-things", out="i-aaa\ti-bbb")
    first = step("find", ("aws", "ec2", "describe-things"), capture="ids")
    second = step("use", ("aws", "ec2", "terminate-instances", "--instance-ids", "{{ids}}", "--flag={{ids}}"))
    executor, _ = make_executor(runner, [first, second])
    assert executor.run([first, second], assume_yes=True) == 0
    assert runner.calls[-1] == ("aws", "ec2", "terminate-instances", "--instance-ids", "i-aaa", "i-bbb", "--flag=i-aaa\ti-bbb")


@pytest.mark.parametrize("output", ["", "None", "  \n"])
def test_a_capture_that_comes_back_empty_or_none_fails_instead_of_continuing(output):
    runner = FakeAws().when("describe-vpcs", out=output)
    first = step("get-vpc", ("aws", "ec2", "describe-vpcs"), capture="vpc-id")
    second = step("use", ("aws", "ec2", "create-thing", "{{vpc-id}}"))
    executor, out = make_executor(runner, [first, second])
    assert executor.run([first, second], assume_yes=True) == demo.EXIT_FAILED
    assert not runner.ran("create-thing") and any("沒有取得要擷取的值" in line for line in out)


def test_run_if_found_skips_on_error_or_empty_and_runs_with_the_query_output_when_found():
    delete = step("td", ("aws", "ec2", "terminate-instances", "--instance-ids", "{{found}}"),
                  exists_argv=("aws", "ec2", "describe-instances"), exists_nonempty=True, run_if_found=True, capture="found")
    for outcome in (demo.RunResult(254, "", "boom"), demo.RunResult(0, ""), demo.RunResult(0, "None")):
        runner = FakeAws()
        runner.rules.append({"tokens": ("describe-instances",), "queue": [outcome.stdout], "rc": outcome.returncode, "err": outcome.stderr})
        executor, out = make_executor(runner, [delete])
        assert executor.run([delete], assume_yes=True) == 0 and not runner.ran("terminate-instances") and any("略過" in line for line in out)
    runner = FakeAws().when("describe-instances", out="i-0123")
    executor, _ = make_executor(runner, [delete])
    assert executor.run([delete], assume_yes=True) == 0
    assert runner.calls[-1] == ("aws", "ec2", "terminate-instances", "--instance-ids", "i-0123")


def test_an_existence_check_that_fails_means_not_found_and_the_step_runs():
    runner = FakeAws().when("describe-budget", rc=254, err="NotFoundException")
    create = step("create", ("aws", "budgets", "create-budget"), exists_argv=("aws", "budgets", "describe-budget"))
    executor, _ = make_executor(runner, [create])
    assert executor.run([create], assume_yes=True) == 0 and runner.ran("create-budget")


def test_interactive_steps_always_ask_even_with_yes_and_run_their_verification():
    runner = FakeAws().when("list-subscriptions", out="0")
    pause = step("confirm-subscriptions", interactive=True, verify=(plan.Check(("aws", "sns", "list-subscriptions"), "0"),))
    answers = iter([False, True])
    executor, out = make_executor(runner, [pause], confirm=lambda prompt: next(answers))
    assert executor.run([pause], assume_yes=True) == demo.EXIT_DECLINED and runner.calls == []
    assert executor.run([pause], assume_yes=True) == 0 and runner.ran("list-subscriptions")
    assert any("人工步驟" in line for line in out)


def test_missing_captures_are_recovered_read_only_from_the_producer_not_by_repeating_a_creation():
    runner = FakeAws().with_identity()
    runner.when("describe-vpcs", out="vpc-0abc")
    runner.when("to_string(sort_by", out='[[80,80,"0.0.0.0/0",1],[443,443,"0.0.0.0/0",1]]')
    runner.when("describe-security-groups", "--group-ids", out="")                    # 這個群組還沒有任何規則
    runner.when("describe-security-groups", "group-name", out="sg-0host")
    result = invoke([*PROVISION, "--only", "authorize-host-ingress", "--yes"], runner=runner)
    assert result.code == 0, result.text + result.err
    assert "取回先前步驟的結果：host-sg-id" in result.text
    # 取回只用唯讀查詢：沒有建立任何東西；真正執行的只有開放 80、443 這一個寫入
    writes = [call for call in runner.calls if call[2].startswith(("create-", "authorize-"))]
    assert [call[2] for call in writes] == ["authorize-security-group-ingress"]


def test_a_value_that_cannot_be_recovered_safely_asks_for_the_producing_step():
    runner = FakeAws().with_identity()
    result = invoke([*DEPLOY, "--only", "wait-build-image", "--yes"], runner=runner)
    assert result.code == demo.EXIT_FAILED and "build-image" in result.text and "無法安全地重新取得" in result.text
    assert not runner.ran("send-command")


def test_recovery_refuses_a_cycle_instead_of_looping():
    a = step("a", ("aws", "x", "describe-a", "{{b}}"), capture="a")
    b = step("b", ("aws", "x", "describe-b", "{{a}}"), capture="b")
    runner = FakeAws()
    executor, out = make_executor(runner, [a, b])
    assert executor.run([step("use", ("aws", "x", "create", "{{a}}"))], assume_yes=True) == demo.EXIT_FAILED
    assert any("找不到先前步驟擷取的" in line for line in out) and not runner.ran("create")


def test_deploy_groups_include_the_host_lookup_and_the_archive_directory_is_created(tmp_path):
    runner = FakeAws().with_identity()
    runner.when("describe-instances", out="i-0host")
    root = tmp_path / "project"
    root.mkdir()
    result = invoke([*DEPLOY, "--only", "archive-source", "--yes"], runner=runner, project_root=root)
    assert result.code == 0 and (root / "build" / "aws-demo").is_dir()
    assert runner.calls[-1][:3] == ("git", "archive", "--format=tar.gz") and runner.calls[-1][-1] == TAG
    narrowed = invoke([*DEPLOY, "--group", "build"])
    assert "找出主機" in narrowed.text and "git archive" in narrowed.text and "alembic" not in narrowed.text


def test_the_default_deploy_tag_comes_from_git_and_a_bad_tag_is_refused():
    result = invoke(["deploy", *BASE], tag="deadbee")
    assert result.code == 0 and "deadbee" in result.text
    refused = invoke(["deploy", *BASE, "--tag", "latest"])
    assert refused.code == demo.EXIT_USAGE and "提交短碼" in refused.err
    assert invoke(["deploy", *BASE], tag="").code == demo.EXIT_USAGE


def test_rollback_changes_only_the_tag_and_never_migrates():
    result = invoke(ROLLBACK)
    assert "switch" in result.text and "alembic" not in result.text and "create-db-snapshot" not in result.text


# ---- 2.3 輪詢、逾時與診斷 ------------------------------------------------------------------------------------


def poll_step(**overrides):
    fields = dict(poll=plan.Check(("aws", "rds", "describe-db-instances"), "available"), timeout_seconds=100, interval_seconds=10)
    fields.update(overrides)
    return step("wait-rds", **fields)


def test_polling_waits_until_the_expected_value_and_sleeps_between_tries():
    runner = FakeAws().when("describe-db-instances", out=["creating", "creating", "backing-up", "available"])
    clock = Clock()
    executor, out = make_executor(runner, clock=clock)
    assert executor.run([poll_step()], assume_yes=True) == 0
    assert len(runner.calls) == 4 and clock.sleeps == [10, 10, 10]
    assert sum("等待中" in line for line in out) == 2                    # 只在狀態改變時才印，長時間等待不會洗版


def test_polling_times_out_with_a_message_instead_of_waiting_forever():
    runner = FakeAws().when("describe-db-instances", out="creating")
    clock = Clock()
    executor, out = make_executor(runner, clock=clock)
    assert executor.run([poll_step(timeout_seconds=35)], assume_yes=True) == demo.EXIT_FAILED
    assert clock.now <= 35 + 10 and any("逾時" in line and "35 秒" in line and "creating" in line for line in out)


def test_a_failure_value_stops_polling_immediately():
    runner = FakeAws().when("get-command-invocation", out="Failed")
    clock = Clock()
    waiting = poll_step(poll=plan.Check(("aws", "ssm", "get-command-invocation", "--command-id", "c-1", "--query", "Status", "--output", "text"), "Success"),
                        poll_fail=("Failed", "TimedOut"))
    executor, out = make_executor(runner, clock=clock)
    assert executor.run([waiting], assume_yes=True) == demo.EXIT_FAILED
    assert clock.sleeps == [] and any("失敗狀態（Failed）" in line for line in out)


def test_a_failed_ssm_command_prints_its_error_output_truncated():
    runner = FakeAws()
    runner.when("StandardErrorContent", out="E" * 900)
    runner.when("get-command-invocation", out="Failed")
    waiting = poll_step(poll=plan.Check(("aws", "ssm", "get-command-invocation", "--command-id", "c-1", "--instance-id", "i-1", "--query", "Status", "--output", "text"), "Success"),
                        poll_fail=("Failed",))
    executor, out = make_executor(runner)
    assert executor.run([waiting], assume_yes=True) == demo.EXIT_FAILED
    shown = "".join(line.strip() for line in out if set(line.strip()) == {"E"})
    assert shown == "E" * 500
    diagnostic = runner.calls[-1]
    assert "StandardErrorContent" in diagnostic and "Status" not in diagnostic and diagnostic.count("--query") == 1


def test_a_command_that_errors_while_polling_counts_as_not_ready_yet():
    sequence = iter([demo.RunResult(254, "", "InvocationDoesNotExist"), demo.RunResult(254, "", "InvocationDoesNotExist"), demo.RunResult(0, "Success\n")])
    clock = Clock()
    waiting = poll_step(poll=plan.Check(("aws", "ssm", "get-command-invocation"), "Success"), poll_fail=("Failed",))
    executor, out = make_executor(lambda argv: next(sequence), clock=clock)
    assert executor.run([waiting], assume_yes=True) == 0 and len(clock.sleeps) == 2


# ---- 2.2 機密：產生、機密檔、暫存檔、遮蔽 ----------------------------------------------------------------------


def test_generated_passwords_satisfy_the_rds_and_sql_server_rules():
    seen = set()
    for _ in range(200):
        password = demo.generate_password()
        seen.add(password)
        assert len(password) == demo.PASSWORD_LENGTH and 8 <= len(password) <= 128
        assert re.search(r"[A-Z]", password) and re.search(r"[a-z]", password) and re.search(r"\d", password) and re.search(r"[-_]", password)
        assert not any(char in password for char in '/"@ \'\\:;,%&=+?#{}[]()$`!*<>|^~')       # RDS 禁用字元與 URL、連線字串的特殊字元
    assert len(seen) == 200
    key = demo.generate_secret_key()
    assert re.fullmatch(r"[0-9a-f]{64}", key) and key != demo.generate_secret_key()
    with pytest.raises(demo.DemoError):
        demo.generate_password(8)


def test_a_generated_secret_goes_to_ssm_only_through_the_temporary_file(spool):
    runner = put_runner()
    result = invoke([*PROVISION, "--only", "put-parameter-SECRET_KEY", "--yes"], runner=runner)
    assert result.code == 0, result.text + result.err
    body = json.loads(runner.bodies[-1])
    assert body["Name"] == "/rac-demo/staging/SECRET_KEY" and body["Type"] == "SecureString" and re.fullmatch(r"[0-9a-f]{64}", body["Value"])
    value = body["Value"]
    assert not any(value in " ".join(call) for call in runner.calls), "機密值不得出現在任何命令列"
    assert value not in result.text and value not in result.err
    put_call = runner.matching("put-parameter")[0]
    assert put_call[:3] == ("aws", "ssm", "put-parameter") and put_call[3] == "--cli-input-json" and put_call[4].startswith("file://")
    assert list(spool.iterdir()) == []


def test_an_existing_parameter_is_never_overwritten_and_no_value_is_generated(monkeypatch):
    monkeypatch.setattr(demo, "generate_secret_key", lambda: pytest.fail("已存在的參數不得重新產生值"))
    runner = FakeAws().with_identity()
    runner.when("get-parameter", "Parameter.Name", out="/rac-demo/staging/SECRET_KEY")
    runner.when("describe-parameters", out="SecureString")
    result = invoke([*PROVISION, "--only", "put-parameter-SECRET_KEY", "--yes"], runner=runner)
    assert result.code == 0, result.text + result.err
    assert "已存在，略過" in result.text and not runner.ran("put-parameter") and runner.bodies == []


def test_the_temporary_file_is_removed_after_success_after_a_failed_command_and_after_an_exception(spool):
    during = []

    def hook_for(mode):
        def hook(argv):
            if argv[1:3] != ("ssm", "put-parameter"):
                return None
            files = list(spool.iterdir())
            during.append(files)
            assert len(files) == 1 and "Value" in files[0].read_text(encoding="utf-8")
            if mode == "raise":
                raise RuntimeError("runner crashed")
            return demo.RunResult(254, "", "denied") if mode == "fail" else None
        return hook

    for mode in ("ok", "fail", "raise"):
        runner = put_runner(HookedAws(hook_for(mode)))
        args = [*PROVISION, "--only", "put-parameter-SECRET_KEY", "--yes"]
        if mode == "raise":
            with pytest.raises(RuntimeError):
                invoke(args, runner=runner)
        else:
            assert invoke(args, runner=runner).code == (0 if mode == "ok" else demo.EXIT_FAILED)
        assert list(spool.iterdir()) == [], f"{mode}：暫存檔必須刪除"
    assert len(during) == 3


@pytest.mark.skipif(os.name != "posix", reason="Windows 的檔案權限以 ACL 表示，無法用 mode 位元驗證")
def test_the_temporary_file_is_readable_by_its_owner_only(spool):
    modes = []

    def hook(argv):
        if argv[1:3] == ("ssm", "put-parameter"):
            modes.extend(stat.S_IMODE(path.stat().st_mode) for path in spool.iterdir())

    invoke([*PROVISION, "--only", "put-parameter-SECRET_KEY", "--yes"], runner=put_runner(HookedAws(hook)))
    assert modes and all(mode & 0o077 == 0 for mode in modes)


def test_provided_secrets_come_from_the_secrets_file_with_no_variable_interpolation(tmp_path):
    secrets_file = tmp_path / "aws-demo.secrets"
    secrets_file.write_text("﻿GEMINI_API_KEY=AIza$HOME-${NOPE}-tail\n", encoding="utf-8")        # 含 BOM 與 $：不插值
    runner = put_runner()
    result = invoke([*PROVISION, "--secrets-file", str(secrets_file), "--only", "put-parameter-GEMINI_API_KEY", "--yes"], runner=runner)
    assert result.code == 0, result.text + result.err
    assert json.loads(runner.bodies[-1])["Value"] == "AIza$HOME-${NOPE}-tail"
    assert "AIza$HOME" not in result.text and not any("AIza" in " ".join(call) for call in runner.calls)


def test_json_special_characters_in_a_secret_cannot_break_or_inject_into_the_template(monkeypatch):
    nasty = 'a"b\\c\nd","Type":"String","x":"é'
    monkeypatch.setattr(demo, "read_secrets_file", lambda path: {"GEMINI_API_KEY": nasty})
    runner = put_runner()
    result = invoke([*PROVISION, "--secrets-file", "ignored.secrets", "--only", "put-parameter-GEMINI_API_KEY", "--yes"], runner=runner)
    assert result.code == 0, result.text + result.err
    body = json.loads(runner.bodies[-1])
    assert body["Value"] == nasty and body["Type"] == "SecureString" and "x" not in body
    assert runner.bodies[-1].isascii()


def test_a_missing_provided_secret_fails_at_its_own_step_with_the_name_only():
    runner = put_runner()
    result = invoke([*PROVISION, "--only", "put-parameter-GEMINI_API_KEY", "--yes"], runner=runner)
    assert result.code == demo.EXIT_FAILED and "GEMINI_API_KEY" in result.text and not runner.ran("put-parameter")


def test_the_secrets_file_is_checked_for_names_and_completeness_before_anything_runs(tmp_path):
    def check(content, *extra):
        path = tmp_path / "x.secrets"
        path.write_text(content, encoding="utf-8")
        runner = FakeAws().with_identity()
        result = invoke([*PROVISION, "--secrets-file", str(path), *extra], runner=runner)
        return result, runner

    result, runner = check("GEMINI_API_KEY=value-one-aaaaaa\nMAIL_FROM=x@y.co\n")
    assert result.code == demo.EXIT_USAGE and "MAIL_FROM" in result.err and "value-one-aaaaaa" not in result.err and runner.calls == []
    result, _ = check("GEMINI_API_KEY=value-one-aaaaaa\nDATABASE_URL=mssql://x\nRDS_MASTER_PASSWORD=pw-aaaaaaaa\n")
    assert result.code == demo.EXIT_USAGE and "DATABASE_URL" in result.err and "RDS_MASTER_PASSWORD" in result.err and "pw-aaaaaaaa" not in result.err
    result, _ = check("SECRET_KEY=" + "k" * 40 + "\n")
    assert result.code == demo.EXIT_USAGE and "GEMINI_API_KEY" in result.err and "k" * 40 not in result.err
    result, _ = check("GEMINI_API_KEY=value-one-aaaaaa\n", "--group", "network")        # 這次的步驟用不到機密：不要求齊全
    assert result.code == 0 and "名稱與完整性檢查通過" in result.text
    result, _ = check("GEMINI_API_KEY=value-one-aaaaaa\n")
    assert result.code == 0 and "value-one-aaaaaa" not in result.text
    missing = invoke([*PROVISION, "--secrets-file", str(tmp_path / "nope.secrets")])
    assert missing.code == demo.EXIT_USAGE and "找不到機密檔" in missing.err


def test_with_secret_adds_that_secret_to_what_the_file_must_hold(tmp_path):
    path = tmp_path / "x.secrets"
    path.write_text("GEMINI_API_KEY=value-one-aaaaaa\n", encoding="utf-8")
    result = invoke([*PROVISION, "--with-secret", "OPENROUTER_API_KEY", "--secrets-file", str(path)])
    assert result.code == demo.EXIT_USAGE and "OPENROUTER_API_KEY" in result.err


def test_the_rds_master_password_is_never_generated_for_an_existing_database():
    runner = FakeAws().with_identity()
    runner.when("get-parameter", "--with-decryption", rc=254, err="ParameterNotFound")
    runner.when("describe-security-groups", out="sg-0db")
    runner.when("describe-vpcs", out="vpc-0abc")
    runner.when("describe-db-instances", rc=254)
    result = invoke([*PROVISION, "--only", "create-rds-instance", "--accept-cost", "--yes"], runner=runner)
    assert result.code == demo.EXIT_FAILED and "RDS_MASTER_PASSWORD" in result.text and "還沒有建立" in result.text
    assert not runner.ran("create-db-instance") and runner.bodies == []


def test_database_url_is_composed_from_the_endpoint_and_the_stored_application_password():
    endpoint = "rac-demo-staging-db.abcdefg.ap-southeast-2.rds.amazonaws.com"
    app_password = "App-Pw_0123456789abcdefghijklmn"
    runner = put_runner()
    runner.when("get-parameter", "--with-decryption", out=app_password, first=True)
    runner.when("describe-db-instances", "Endpoint.Address", out=endpoint)
    result = invoke([*PROVISION, "--only", "put-parameter-DATABASE_URL", "--yes"], runner=runner)
    assert result.code == 0, result.text + result.err
    assert "取回先前步驟的結果：rds-endpoint" in result.text
    url = json.loads(runner.bodies[-1])["Value"]
    parsed = make_url(url)
    assert (parsed.username, parsed.password, parsed.host, parsed.port, parsed.database) == ("rac_app", app_password, endpoint, 1433, "rac_demo")
    assert dict(parsed.query) == {"driver": "ODBC Driver 18 for SQL Server", "Encrypt": "yes"}, "design D7：只有 Encrypt=yes，憑證由映像的系統憑證庫驗證"
    assert "TrustServerCertificate" not in url and "ServerCertificate" not in url and app_password not in result.text and url not in result.text
    assert not any(app_password in " ".join(call) for call in runner.calls)


def test_refreshing_database_url_overwrites_it_with_the_new_shape_and_shows_neither_the_value_nor_the_password():
    """既有的 SSM 值仍帶舊的 ServerCertificate：覆寫步驟重新組成連線字串（密碼從 SSM 讀回，不重新產生），值只經暫存檔、輸出與命令列都沒有。"""
    endpoint = "rac-demo-staging-db.abcdefg.ap-southeast-2.rds.amazonaws.com"
    app_password = "App-Pw_0123456789abcdefghijklmn"
    runner = put_runner()
    runner.when("describe-parameters", out='["/rac-demo/staging/DATABASE_URL","SecureString"]', first=True)
    runner.when("get-parameter", "--with-decryption", out=app_password, first=True)
    runner.when("describe-db-instances", "Endpoint.Address", out=endpoint)
    result = invoke([*PROVISION, "--only", "refresh-parameter-DATABASE_URL", "--yes"], runner=runner)
    assert result.code == 0, result.text + result.err
    body = json.loads(runner.bodies[-1])
    assert body["Overwrite"] is True and "Tags" not in body and body["Name"] == "/rac-demo/staging/DATABASE_URL" and body["Type"] == "SecureString"
    parsed = make_url(body["Value"])
    assert (parsed.username, parsed.password, parsed.host, parsed.database) == ("rac_app", app_password, endpoint, "rac_demo")
    assert dict(parsed.query) == {"driver": "ODBC Driver 18 for SQL Server", "Encrypt": "yes"}
    assert body["Value"] not in result.text and app_password not in result.text and app_password not in result.err
    assert not any(body["Value"] in " ".join(call) or app_password in " ".join(call) for call in runner.calls)
    put_call = runner.matching("put-parameter")[0]
    assert put_call[:5] == ("aws", "ssm", "put-parameter", "--cli-input-json", put_call[4]) and put_call[4].startswith("file://")
    assert not runner.matching("get-parameter", "Parameter.Name"), "覆寫不先查是否已存在（它的目的就是取代既有的值）"
    assert "驗證通過" in result.text


def test_refreshing_never_generates_a_new_password_or_secret_key(monkeypatch):
    monkeypatch.setattr(demo, "generate_password", lambda *a, **k: pytest.fail("覆寫不得重新產生密碼"))
    monkeypatch.setattr(demo, "generate_secret_key", lambda: pytest.fail("覆寫不得重新產生 SECRET_KEY"))
    runner = put_runner()
    runner.when("get-parameter", "--with-decryption", rc=254, err="ParameterNotFound", first=True)
    runner.when("describe-db-instances", "Endpoint.Address", out="db.example.ap-southeast-2.rds.amazonaws.com")
    result = invoke([*PROVISION, "--only", "refresh-parameter-DATABASE_URL", "--yes"], runner=runner)
    assert result.code == demo.EXIT_FAILED and "DB_APP_PASSWORD" in result.text and "還沒有建立" in result.text
    assert not runner.ran("put-parameter") and runner.bodies == []


def test_a_refresh_step_for_a_secret_that_is_not_composed_reads_the_stored_value_instead_of_generating_a_new_one(monkeypatch, spool):
    """計畫模組本來就不產生這樣的步驟；執行器再守一道（縱深）：就算有人造出 refresh-parameter-SECRET_KEY，也只會把 SSM 內既有的值讀回來寫回去。"""
    monkeypatch.setattr(demo, "generate_secret_key", lambda: pytest.fail("覆寫不得重新產生 SECRET_KEY"))
    stored = "stored-secret-key-0123456789abcdef"
    runner = FakeAws().when("get-parameter", "--with-decryption", out=stored)
    synthetic = step("refresh-parameter-SECRET_KEY", ("aws", "ssm", "put-parameter", "--cli-input-json", "file://{{input_file}}"),
                     input_json='{"Name":"/rac-demo/staging/SECRET_KEY","Type":"SecureString","Value":"{{secret:SECRET_KEY}}","Overwrite":true}')
    executor, out = make_executor(runner, [synthetic])
    assert executor.run([synthetic], assume_yes=True) == 0
    assert json.loads(runner.bodies[-1])["Value"] == stored and stored not in "\n".join(out)


def test_the_refresh_step_needs_the_users_confirmation_and_does_nothing_when_declined():
    runner = put_runner()
    result = invoke([*PROVISION, "--only", "refresh-parameter-DATABASE_URL", "--execute"], runner=runner, answers=[False])
    assert result.code == demo.EXIT_DECLINED and len(result.prompts) == 1
    assert not runner.ran("put-parameter") and runner.bodies == [], "沒有確認就不覆寫"


@pytest.mark.parametrize("password", ["p@ss/w:rd?#%&=+ x", "a\\b\"c'd", "ünï-密碼"])
def test_the_composed_url_round_trips_any_password_and_passes_the_apps_staging_checks(password):
    url = demo.compose_database_url(endpoint="db.example.ap-southeast-2.rds.amazonaws.com", password=password)
    parsed = make_url(url)
    assert parsed.password == password and parsed.username == "rac_app"
    assert app_config._url_username(parsed) != "sa" and not app_config._url_trusts_server_certificate(parsed)


@pytest.mark.parametrize("endpoint", ["", "host;Encrypt=no", "host/db", "host name", "host\n", "-bad", "a@b", "x:1433"])
def test_a_suspicious_endpoint_is_not_put_into_a_connection_string(endpoint):
    with pytest.raises(demo.DemoError):
        demo.compose_database_url(endpoint=endpoint, password="x" * 20)
    with pytest.raises(demo.DemoError):
        demo.compose_database_url(endpoint="db.example.com", password="")


def test_a_secret_reference_in_a_command_line_is_refused():
    leaky = step("leaky", ("aws", "ssm", "put-parameter", "--value", "{{secret:SECRET_KEY}}"))
    runner = FakeAws()
    executor, out = make_executor(runner, [leaky])
    assert executor.run([leaky], assume_yes=True) == demo.EXIT_FAILED and runner.calls == []


def test_captured_values_are_json_escaped_and_never_reinterpreted_as_templates(spool):
    runner = FakeAws().when("describe-things", out='he said "hi" {{secret:SECRET_KEY}}')
    first = step("find", ("aws", "x", "describe-things"), capture="odd")
    second = step("put", ("aws", "x", "put", "--cli-input-json", "file://{{input_file}}"), input_json='{"Value":"{{odd}}"}')
    executor, _ = make_executor(runner, [first, second])
    assert executor.run([first, second], assume_yes=True) == 0
    assert json.loads(runner.bodies[-1]) == {"Value": 'he said "hi" {{secret:SECRET_KEY}}'}
    assert list(spool.iterdir()) == []


def test_every_output_masks_known_secret_values_even_when_a_command_echoes_one():
    leaked = "Leaked-From-Aws-Error-0042-zzzz"
    runner = put_runner()
    runner.when("put-parameter", rc=254, err=f"An error occurred: invalid value {leaked}", first=True)
    result = invoke([*PROVISION, "--only", "put-parameter-GEMINI_API_KEY", "--yes"], runner=runner)
    redactor = demo.Redactor()
    redactor.add(leaked)
    assert redactor(f"x {leaked} y") == "x *** y"
    redactor.add("ab")
    assert redactor("ab") == "ab"                      # 太短的字串不遮蔽（會把一般輸出弄亂）
    assert result.code == demo.EXIT_FAILED


def test_the_provided_secret_values_are_masked_if_a_command_error_echoes_them(tmp_path, monkeypatch):
    monkeypatch.setattr(demo, "read_secrets_file", lambda path: {"GEMINI_API_KEY": SECRET_VALUE})
    runner = put_runner()
    runner.when("put-parameter", rc=254, err=f"ValidationException: bad value {SECRET_VALUE}", first=True)
    result = invoke([*PROVISION, "--secrets-file", "x.secrets", "--only", "put-parameter-GEMINI_API_KEY", "--yes"], runner=runner)
    assert result.code == demo.EXIT_FAILED and SECRET_VALUE not in result.text and SECRET_VALUE not in result.err and "***" in result.text


def test_the_preview_with_a_secrets_file_shows_secret_names_and_never_their_values(monkeypatch):
    monkeypatch.setattr(demo, "read_secrets_file", lambda path: {"GEMINI_API_KEY": SECRET_VALUE})
    result = invoke([*PROVISION, "--secrets-file", "x.secrets", "--accept-cost"])
    assert result.code == 0 and "{{secret:GEMINI_API_KEY}}" in result.text and "{{secret:RDS_MASTER_PASSWORD}}" in result.text
    assert SECRET_VALUE not in result.text and SECRET_VALUE not in result.err


def test_secrets_file_names_are_ignored_by_git():
    ignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "*.secrets" in ignore.splitlines()
    done = subprocess.run(["git", "check-ignore", "-q", f"{demo.SECRETS_FILE_HINT}"], cwd=PROJECT_ROOT, check=False)
    assert done.returncode == 0, "建議的機密檔名必須被 .gitignore 忽略"


# ---- 外部指令的呼叫方式 --------------------------------------------------------------------------------------


def test_the_real_runner_passes_an_argument_list_a_utf8_file_encoding_and_no_duckdns_token(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

    monkeypatch.setenv(demo.DUCKDNS_TOKEN_ENV, DUCKDNS_TOKEN)
    monkeypatch.setattr(subprocess, "run", fake_run)
    result = demo.subprocess_runner(("aws", "sts", "get-caller-identity"))
    assert result == demo.RunResult(0, "ok\n", "") and seen["argv"] == ["aws", "sts", "get-caller-identity"]
    assert not seen.get("shell") and seen["env"]["AWS_CLI_FILE_ENCODING"] == "UTF-8" and demo.DUCKDNS_TOKEN_ENV not in seen["env"]
    assert seen["timeout"] == demo.COMMAND_TIMEOUT_SECONDS and seen["capture_output"] is True and seen["encoding"] == "utf-8"


def test_the_real_runner_turns_a_timeout_and_a_missing_program_into_results(monkeypatch):
    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(subprocess, "run", timeout)
    assert demo.subprocess_runner(("aws", "x")).returncode == 124

    def missing(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(subprocess, "run", missing)
    result = demo.subprocess_runner(("aws", "x"))
    assert result.returncode == 127 and "aws" in result.stderr


# ---- 2.3 DuckDNS --------------------------------------------------------------------------------------------


def duckdns_runner(address="203.0.113.7"):
    runner = FakeAws().with_identity()
    runner.when("describe-addresses", out=address)
    return runner


def test_duckdns_preview_runs_nothing_and_needs_no_token():
    result = invoke(DUCKDNS)
    assert result.code == 0 and result.runner.calls == [] and "DUCKDNS_TOKEN" in result.text and "rac001.duckdns.org" in result.text


def test_duckdns_without_a_token_in_the_environment_refuses_before_any_call():
    runner = duckdns_runner()
    result = invoke([*DUCKDNS, "--execute"], runner=runner, environ={})
    assert result.code == demo.EXIT_USAGE and "DUCKDNS_TOKEN" in result.err and runner.calls == []


def test_duckdns_updates_after_confirmation_and_never_shows_the_token():
    urls = []
    runner = duckdns_runner()
    result = invoke([*DUCKDNS, "--execute"], runner=runner, environ={"DUCKDNS_TOKEN": DUCKDNS_TOKEN}, fetch=lambda url: urls.append(url) or (200, "OK\n"))
    assert result.code == 0 and "203.0.113.7" in result.text and "已更新" in result.text
    assert len(urls) == 1 and urls[0].startswith("https://www.duckdns.org/update?") and f"token={DUCKDNS_TOKEN}" in urls[0]
    assert "domains=rac001" in urls[0] and "ip=203.0.113.7" in urls[0] and "duckdns.org.duckdns" not in urls[0]
    assert DUCKDNS_TOKEN not in result.text and DUCKDNS_TOKEN not in result.err
    assert not any(DUCKDNS_TOKEN in " ".join(call) for call in runner.calls)
    assert "203.0.113.7" in result.prompts[0]


def test_duckdns_does_nothing_when_the_user_declines():
    urls = []
    result = invoke([*DUCKDNS, "--execute"], runner=duckdns_runner(), answers=[False], environ={"DUCKDNS_TOKEN": DUCKDNS_TOKEN},
                    fetch=lambda url: urls.append(url) or (200, "OK"))
    assert result.code == demo.EXIT_DECLINED and urls == []


@pytest.mark.parametrize("address", ["None", "", "999.1.1.1", "not-an-ip", "203.0.113.7; rm -rf /"])
def test_duckdns_refuses_when_the_elastic_ip_cannot_be_found_or_is_malformed(address):
    urls = []
    result = invoke([*DUCKDNS, "--execute"], runner=duckdns_runner(address), environ={"DUCKDNS_TOKEN": DUCKDNS_TOKEN},
                    fetch=lambda url: urls.append(url) or (200, "OK"))
    assert result.code == demo.EXIT_FAILED and urls == [] and "Elastic IP" in result.err


@pytest.mark.parametrize("reply", [(200, "KO"), (200, ""), (500, "OK"), (403, "OK")])
def test_duckdns_failures_report_the_status_without_the_token(reply):
    result = invoke([*DUCKDNS, "--execute"], runner=duckdns_runner(), environ={"DUCKDNS_TOKEN": DUCKDNS_TOKEN}, fetch=lambda url: reply)
    assert result.code == demo.EXIT_FAILED and DUCKDNS_TOKEN not in result.err and DUCKDNS_TOKEN not in result.text


@pytest.mark.parametrize("domain", ["example.com", "a.b.duckdns.org", ".duckdns.org", "-x.duckdns.org", "rac001.duckdns.org.evil.com"])
def test_duckdns_only_updates_a_single_label_duckdns_subdomain(domain):
    with pytest.raises(demo.DemoError):
        demo.duckdns_label(domain)
    assert demo.duckdns_label("rac001.duckdns.org") == "rac001"


def test_the_real_fetch_never_leaks_the_url_or_token_through_errors_or_exception_chains(monkeypatch):
    url = f"https://www.duckdns.org/update?domains=rac001&token={DUCKDNS_TOKEN}&ip=203.0.113.7"

    def refuse(request_url, timeout=None):
        raise urllib.error.URLError(f"connection to {request_url} failed")

    monkeypatch.setattr("urllib.request.urlopen", refuse)
    with pytest.raises(demo.DemoError) as caught:
        demo.default_fetch(url)
    error = caught.value
    assert DUCKDNS_TOKEN not in str(error) and "URLError" in str(error)
    assert error.__cause__ is None and error.__context__ is None, "例外鏈不得帶著含權杖的網址"

    def explode(request_url, timeout=None):
        raise RuntimeError(request_url)

    monkeypatch.setattr("urllib.request.urlopen", explode)
    with pytest.raises(demo.DemoError) as caught:
        demo.default_fetch(url)
    assert DUCKDNS_TOKEN not in str(caught.value) and caught.value.__context__ is None


def test_the_real_fetch_reports_an_http_error_by_code_only(monkeypatch):
    def not_found(request_url, timeout=None):
        raise urllib.error.HTTPError(request_url, 403, "Forbidden", {}, io.BytesIO(b""))

    monkeypatch.setattr("urllib.request.urlopen", not_found)
    assert demo.default_fetch("https://www.duckdns.org/update?token=x") == (403, "")


@pytest.mark.parametrize("reply", [(200, "KO"), (500, "OK"), (200, "")])
def test_the_update_function_itself_never_puts_the_token_or_the_url_in_its_errors(reply):
    with pytest.raises(demo.DemoError) as caught:
        demo.update_duckdns("rac001", DUCKDNS_TOKEN, "203.0.113.7", fetch=lambda url: reply)
    assert DUCKDNS_TOKEN not in str(caught.value) and "duckdns.org/update" not in str(caught.value)


def test_a_secret_reference_in_a_command_line_never_reaches_the_runner_even_inside_a_longer_argument():
    for argument in ("{{secret:SECRET_KEY}}", "--value={{secret:SECRET_KEY}}"):
        runner = FakeAws()
        leaky = step("leaky", ("aws", "ssm", "put-parameter", argument))
        executor, out = make_executor(runner, [leaky])
        assert executor.run([leaky], assume_yes=True) == demo.EXIT_FAILED and runner.calls == []
        assert any("機密參照只能出現在暫存檔" in line for line in out)


def test_duckdns_update_asks_the_fetch_for_a_get_with_the_expected_query():
    seen = []
    demo.update_duckdns("rac001", DUCKDNS_TOKEN, "203.0.113.7", fetch=lambda url: seen.append(url) or (200, "OK"))
    query = dict(part.split("=") for part in seen[0].split("?", 1)[1].split("&"))
    assert query == {"domains": "rac001", "token": DUCKDNS_TOKEN, "ip": "203.0.113.7"}


# ---- 2.4 拆除：先列出部署前快照、要求明確的例外回覆 --------------------------------------------------------------


def teardown_runner(*, listed=(SNAP_A, SNAP_B, SNAP_FINAL), owned=(SNAP_A, SNAP_B), rds_owned=False):
    """假的 AWS：`length(` 的輪詢與驗證都回 0；帶標籤的資源（`owned`）查得到，其餘查不到。"""
    runner = FakeAws().with_identity()
    runner.when("describe-db-snapshots", out=" ".join(listed))
    for name in owned:
        runner.when("list-tags-for-resource", f"snapshot:{name}", out="rac-demo")
    if rds_owned:
        runner.when("list-tags-for-resource", f"db:{DB}", out="rac-demo")
    runner.when("length(", out="0")
    return runner


def run_teardown(*extra, runner=None, replies=(), answers=None, **kw):
    runner = runner or teardown_runner()
    return invoke([*TEARDOWN, *extra, "--execute"], runner=runner, replies=replies, answers=answers, **kw)


def test_without_choosing_a_final_snapshot_a_real_teardown_is_refused_before_any_call():
    runner = teardown_runner()
    result = run_teardown(runner=runner)
    assert result.code == demo.EXIT_USAGE and "--final-snapshot" in result.err and runner.calls == []


def test_the_two_final_snapshot_flags_are_mutually_exclusive(capsys):
    with pytest.raises(SystemExit):
        demo.main([*TEARDOWN, "--final-snapshot", "--no-final-snapshot"], runner=FakeAws())
    assert "not allowed with" in capsys.readouterr().err


def test_the_snapshots_are_listed_and_the_user_is_asked_before_anything_is_deleted():
    runner = teardown_runner(rds_owned=True)
    result = run_teardown("--no-final-snapshot", runner=runner, replies=[demo.NO_EXCEPTIONS_REPLY])
    assert result.code == 0
    kinds = [event[0] for event in runner.events]
    listing = next(i for i, event in enumerate(runner.events) if event[0] == "run" and event[1][2] == "describe-db-snapshots")
    asked = kinds.index("ask")
    first_destructive = next(i for i, event in enumerate(runner.events) if event[0] == "run" and event[1][2].startswith(DESTRUCTIVE_VERBS))
    assert listing < asked < first_destructive
    assert SNAP_A in result.text and SNAP_B in result.text and "不是部署前快照" in result.text and SNAP_FINAL in result.text


def test_no_exceptions_deletes_every_predeploy_snapshot_and_never_the_final_snapshot():
    runner = teardown_runner()
    result = run_teardown("--final-snapshot", runner=runner, replies=[demo.NO_EXCEPTIONS_REPLY])
    deleted = [call[call.index("--db-snapshot-identifier") + 1] for call in runner.matching("delete-db-snapshot")]
    assert result.code == 0 and sorted(deleted) == sorted([SNAP_A, SNAP_B])
    assert not runner.ran(SNAP_FINAL, "delete") and "將刪除：" + SNAP_A in result.text


def test_a_named_exception_is_kept_and_not_deleted():
    runner = teardown_runner()
    result = run_teardown("--no-final-snapshot", runner=runner, replies=[SNAP_A])
    deleted = [call[call.index("--db-snapshot-identifier") + 1] for call in runner.matching("delete-db-snapshot")]
    assert result.code == 0 and deleted == [SNAP_B] and f"將保留：{SNAP_A}" in result.text
    assert not runner.ran("delete-db-snapshot", SNAP_A)


def test_several_exceptions_may_be_separated_by_spaces_or_commas():
    runner = teardown_runner()
    result = run_teardown("--no-final-snapshot", runner=runner, replies=[f"{SNAP_A}， {SNAP_B}"])
    assert result.code == 0 and runner.matching("delete-db-snapshot") == []


@pytest.mark.parametrize("silence", ["", "   ", "\n"])
def test_silence_is_not_a_reply_and_after_three_tries_nothing_is_deleted(silence):
    runner = teardown_runner(rds_owned=True)
    result = run_teardown("--no-final-snapshot", runner=runner, replies=[silence, silence, silence])
    assert result.code == demo.EXIT_DECLINED and "沒有得到明確的回覆" in result.err
    assert len(result.asked) == demo.SNAPSHOT_REPLY_ATTEMPTS and result.text.count("沉默或直接按 Enter 不算回覆") == 3
    assert runner.destructive() == [] and result.prompts == []


def test_a_mistyped_name_is_rejected_with_the_reason_and_asked_again():
    runner = teardown_runner()
    result = run_teardown("--no-final-snapshot", runner=runner, replies=[f"{SNAP_A}-typo", demo.NO_EXCEPTIONS_REPLY])
    assert result.code == 0 and "不在列出的清單內" in result.text and len(result.asked) == 2
    assert len(runner.matching("delete-db-snapshot")) == 2


def test_the_no_exceptions_phrase_cannot_be_mixed_with_names():
    runner = teardown_runner()
    result = run_teardown("--no-final-snapshot", runner=runner, replies=[f"{demo.NO_EXCEPTIONS_REPLY} {SNAP_A}", demo.NO_EXCEPTIONS_REPLY])
    assert result.code == 0 and "不能和快照名稱一起輸入" in result.text and len(result.asked) == 2


def test_a_final_snapshot_name_given_as_an_exception_is_harmless_and_still_never_deleted():
    runner = teardown_runner()
    result = run_teardown("--no-final-snapshot", runner=runner, replies=[SNAP_FINAL])
    deleted = [call[call.index("--db-snapshot-identifier") + 1] for call in runner.matching("delete-db-snapshot")]
    assert result.code == 0 and sorted(deleted) == sorted([SNAP_A, SNAP_B])


def test_a_snapshot_without_the_project_tags_is_not_deleted_even_if_its_name_matches():
    runner = teardown_runner(owned=(SNAP_A,))                                  # SNAP_B 的標籤查詢查不到
    result = run_teardown("--no-final-snapshot", runner=runner, replies=[demo.NO_EXCEPTIONS_REPLY])
    deleted = [call[call.index("--db-snapshot-identifier") + 1] for call in runner.matching("delete-db-snapshot")]
    assert result.code == 0 and deleted == [SNAP_A] and "沒有符合的資源" in result.text


def test_nothing_is_deleted_when_no_resource_carries_the_project_tags():
    runner = teardown_runner(owned=())
    result = run_teardown("--no-final-snapshot", runner=runner, replies=[demo.NO_EXCEPTIONS_REPLY])
    assert result.code == 0 and runner.destructive() == []
    assert result.text.count("沒有符合的資源") >= 20


def test_every_destructive_command_is_preceded_by_a_successful_tagged_lookup():
    runner = FakeAws().with_identity()
    runner.when("describe-db-snapshots", out="")
    runner.when("length(", out="0")
    runner.when("describe-instances", out="i-0aaa\ti-0bbb")
    runner.when("describe-addresses", out="eipalloc-0abc")
    runner.when("describe-security-groups", out="sg-0xyz")
    runner.when("list-tags-for-resource", out="rac-demo")
    runner.when("get-role", out="rac-demo")
    runner.when("get-instance-profile", out="rac-demo")
    runner.when("get-bucket-tagging", out="rac-demo")
    result = run_teardown("--no-final-snapshot", runner=runner)
    assert result.code == 0
    terminate = runner.matching("terminate-instances")
    assert terminate == [("aws", "ec2", "terminate-instances", "--instance-ids", "i-0aaa", "i-0bbb", "--region=ap-southeast-2")]
    for index, call in enumerate(runner.calls):
        if call in runner.destructive():
            assert any(earlier[0] == "aws" and earlier[2].startswith(("describe-", "list-", "get-")) for earlier in runner.calls[:index]), call
    assert runner.matching("delete-db-instance")[0][-2:] == ("--skip-final-snapshot", "--delete-automated-backups") or "--skip-final-snapshot" in runner.matching("delete-db-instance")[0]


def test_keeping_a_final_snapshot_uses_the_taipei_date_for_its_name():
    runner = teardown_runner(rds_owned=True, listed=())
    result = run_teardown("--final-snapshot", runner=runner, today=date(2026, 12, 31))
    call = runner.matching("delete-db-instance")[0]
    assert result.code == 0 and call[call.index("--final-db-snapshot-identifier") + 1] == f"{DB}-final-20261231"


def test_with_no_predeploy_snapshots_there_is_nothing_to_ask_and_no_snapshot_is_deleted():
    runner = teardown_runner(listed=(SNAP_FINAL,), owned=())
    result = run_teardown("--final-snapshot", runner=runner)
    assert result.code == 0 and result.asked == [] and "沒有部署前快照" in result.text and runner.matching("delete-db-snapshot") == []


def test_if_the_snapshots_cannot_be_listed_the_teardown_stops_before_deleting_anything():
    runner = teardown_runner(rds_owned=True)
    runner.when("describe-db-snapshots", "manual", rc=254, err="AccessDenied", first=True)
    result = run_teardown("--no-final-snapshot", runner=runner, replies=[demo.NO_EXCEPTIONS_REPLY])
    assert result.code == demo.EXIT_FAILED and "無法列出快照" in result.err and runner.destructive() == [] and result.asked == []


def test_the_placeholder_step_is_never_executed_and_a_single_snapshot_still_goes_through_the_questions():
    runner = teardown_runner()
    placeholder = invoke([*TEARDOWN, "--only", "td-delete-snapshots", "--yes"], runner=runner, replies=[demo.NO_EXCEPTIONS_REPLY])
    assert placeholder.code == demo.EXIT_USAGE and "佔位步驟" in placeholder.err and runner.calls == []
    kept = invoke([*TEARDOWN, "--only", f"td-delete-snapshot-{SNAP_A}", "--yes"], runner=runner, replies=[SNAP_A])
    assert kept.code == demo.EXIT_USAGE and "沒有這個步驟" in kept.err and runner.matching("delete-db-snapshot") == []
    chosen = invoke([*TEARDOWN, "--only", f"td-delete-snapshot-{SNAP_A}", "--yes"], runner=runner, replies=[SNAP_B])
    assert chosen.code == 0 and [call[call.index("--db-snapshot-identifier") + 1] for call in runner.matching("delete-db-snapshot")] == [SNAP_A]


def test_yes_cannot_skip_the_exception_question():
    runner = teardown_runner()
    result = invoke([*TEARDOWN, "--only", f"td-delete-snapshot-{SNAP_A}", "--yes"], runner=runner, replies=["", "", ""])
    assert result.code == demo.EXIT_DECLINED and runner.matching("delete-db-snapshot") == []


def test_a_single_blocked_teardown_step_without_the_final_snapshot_choice_never_deletes_the_database():
    runner = teardown_runner(rds_owned=True)
    result = invoke([*TEARDOWN, "--only", "td-delete-rds", "--yes"], runner=runner)
    assert result.code == demo.EXIT_BLOCKED and not runner.ran("delete-db-instance")


def test_the_teardown_preview_lists_the_listing_command_and_the_blocked_placeholders():
    result = invoke([*TEARDOWN, "--group", "snapshots", "--group", "verify"])
    assert result.code == 0 and result.runner.calls == []
    assert "describe-db-snapshots" in result.text and "明確回覆要保留的例外" in result.text and "delete-db-snapshot" not in result.text


# ---- 4.5 主機初始化：--vars-file 與 host-init 組 -------------------------------------------------------------------

VARS_TEXT = "﻿MAIL_SMTP_HOST=smtp.example.com\nMAIL_SMTP_PORT=587\nMAIL_FROM=service@example.com\nALERT_EMAIL_TO=ops@example.com\nLOG_LEVEL=INFO\n"
HOST_INIT_OUTPUTS = [
    "docker=active swap=1 dirs=755,700", "ca-bundle=644", "files=17", "enabled=8", "secrets=3 fs=tmpfs mode=700/600",
    "sshd=inactive port22=0 ssm=active",
]


def write_vars(tmp_path, text=VARS_TEXT):
    path = tmp_path / "aws-demo.vars"
    path.write_text(text, encoding="utf-8")
    return str(path)


def host_runner(outputs=HOST_INIT_OUTPUTS):
    """主機初始化的假回覆：找得到剛好一台主機、命令送出後成功，各段的標準輸出依序為 `outputs`。"""
    runner = FakeAws().with_identity()
    runner.when("describe-instances", "length(Reservations", out="1")
    runner.when("describe-instances", out="i-0abc123")
    runner.when("send-command", out="cmd-1")
    runner.when("StandardOutputContent", out=[line + "\n" for line in outputs])
    runner.when("get-command-invocation", out="Success")
    return runner


def sent_bodies(runner):
    return [json.loads(body) for body in runner.bodies if body and '"DocumentName"' in body]


def test_the_vars_file_is_read_as_utf8_without_interpolation_and_none_means_not_given(tmp_path):
    path = write_vars(tmp_path, VARS_TEXT + "LOG_FORMAT=a$HOME-${NOPE}-b\n")
    values = demo.read_vars_file(path)
    assert values["MAIL_SMTP_HOST"] == "smtp.example.com" and values["LOG_FORMAT"] == "a$HOME-${NOPE}-b", "含 BOM、不做變數插值"
    assert demo.read_vars_file(None) is None and demo.read_vars_file("") is None
    with pytest.raises(demo.DemoError) as error:
        demo.read_vars_file(str(tmp_path / "nope.vars"))
    assert error.value.code == demo.EXIT_USAGE and "找不到一般設定檔" in str(error.value)


def test_with_a_vars_file_the_host_init_group_runs_all_six_ssm_steps_without_accept_cost_and_checks_each_output(tmp_path):
    runner = host_runner()
    result = invoke([*PROVISION, "--vars-file", write_vars(tmp_path), "--group", "host-init", "--execute"], runner=runner)
    assert result.code == 0 and "全部完成" in result.text and "一般設定檔：5 個設定" in result.text
    assert len(runner.matching("send-command")) == 6 and result.text.count("驗證通過") >= 6 + 1
    assert not runner.ran("run-instances") and not runner.ran("create-db-instance") and not runner.ran("allocate-address"), "不需要費用同意，也不碰計費資源"
    bodies = sent_bodies(runner)
    assert [body["Comment"] for body in bodies] == ["host-init-system", "host-init-ca", "host-init-files", "host-init-units", "host-init-secrets", "host-init-sshd"]
    assert all(body["InstanceIds"] == ["i-0abc123"] for body in bodies), "實例編號來自唯讀的查詢"
    commands = {body["Comment"]: "\n".join(body["Parameters"]["commands"]) for body in bodies}
    assert "install -d -m 0755 -o root -g root /etc/rac" in commands["host-init-system"]
    assert "put 0644 /etc/rac/Caddyfile " in commands["host-init-files"] and "put 0644 /etc/rac/plain.env " in commands["host-init-files"]
    assert SECRET_VALUE not in result.text


def test_a_wrong_output_line_fails_that_step_and_nothing_after_it_is_sent(tmp_path):
    runner = host_runner(["docker=active swap=0 dirs=755,700"])
    result = invoke([*PROVISION, "--vars-file", write_vars(tmp_path), "--group", "host-init", "--execute"], runner=runner)
    assert result.code == demo.EXIT_FAILED and "驗證 1/1 不符" in result.text and "swap=0" in result.text
    assert len(runner.matching("send-command")) == 1


def test_a_single_host_init_step_finds_the_host_read_only_and_needs_no_accept_cost(tmp_path):
    runner = host_runner(["sshd=inactive port22=0 ssm=active"])
    result = invoke([*PROVISION, "--only", "host-init-sshd", "--yes"], runner=runner)
    assert result.code == 0 and "唯讀取回先前步驟的結果：host-instance-id" in result.text
    assert not runner.ran("run-instances") and len(runner.matching("send-command")) == 1
    assert sent_bodies(runner)[0]["InstanceIds"] == ["i-0abc123"]


def test_without_a_vars_file_the_run_stops_at_the_file_placing_step_after_the_steps_before_it(tmp_path):
    runner = host_runner()
    result = invoke([*PROVISION, "--group", "host-init", "--execute"], runner=runner)
    assert result.code == demo.EXIT_BLOCKED and "注意：這些步驟被擋下" in result.text and "--vars-file" in result.text
    assert [body["Comment"] for body in sent_bodies(runner)] == ["host-init-system", "host-init-ca"], "放置檔案之前的步驟照常，之後的不執行"
    only = invoke([*PROVISION, "--only", "host-init-files", "--yes"], runner=host_runner())
    assert only.code == demo.EXIT_BLOCKED and not only.runner.ran("send-command")


def test_the_preview_with_a_vars_file_shows_every_file_and_runs_nothing(tmp_path):
    result = invoke([*PROVISION, "--vars-file", write_vars(tmp_path), "--only", "host-init-files"])
    assert result.code == 0 and result.runner.calls == []
    assert "put 0644 /etc/rac/Caddyfile" in result.text and "put 0755 /usr/local/sbin/rac-render-env-app" in result.text
    assert "一般設定檔：5 個設定，檢查通過（只檢查名稱，值不顯示）" in result.text


@pytest.mark.parametrize("extra, named", [("GEMINI_API_KEY=" + SECRET_VALUE + "\n", "GEMINI_API_KEY"), ("NOT_A_REAL_SETTING=zzz-value-zzz\n", "NOT_A_REAL_SETTING"),
                                          ("EMAIL_VERIFICATION_REQUIRED=true\n", "EMAIL_VERIFICATION_REQUIRED")])
def test_a_vars_file_with_a_secret_an_unknown_or_an_obsolete_variable_is_refused_before_anything_runs(tmp_path, extra, named):
    runner = FakeAws().with_identity()
    result = invoke([*PROVISION, "--vars-file", write_vars(tmp_path, VARS_TEXT + extra), "--group", "host-init", "--execute"], runner=runner)
    assert result.code == demo.EXIT_USAGE and named in result.err and runner.calls == []
    assert SECRET_VALUE not in result.err and "zzz-value-zzz" not in result.err


def test_a_missing_vars_file_and_missing_required_settings_are_refused_with_names_only(tmp_path):
    missing = invoke([*PROVISION, "--vars-file", str(tmp_path / "nope.vars")])
    assert missing.code == demo.EXIT_USAGE and "找不到一般設定檔" in missing.err and missing.runner.calls == []
    incomplete = invoke([*PROVISION, "--vars-file", write_vars(tmp_path, "LOG_LEVEL=INFO\n")])
    assert incomplete.code == demo.EXIT_USAGE and "MAIL_SMTP_HOST" in incomplete.err and incomplete.runner.calls == []


def test_the_vars_file_name_is_ignored_by_git_and_the_help_names_the_option():
    ignored = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "*.vars" in ignored
    assert demo.build_parser().parse_args([*PROVISION, "--vars-file", "x.vars"]).vars_file == "x.vars"
    assert demo.build_parser().parse_args(PROVISION).vars_file is None


# ---- 2.5 文件 -----------------------------------------------------------------------------------------------


def test_the_operations_document_describes_how_to_use_the_executor():
    text = (PROJECT_ROOT / "docs" / "營運與部署-operations.md").read_text(encoding="utf-8")
    section = text[text.index("## 8. AWS 展示環境"):]
    for needed in ("scripts/aws_demo.py", "provision", "deploy", "rollback", "teardown", "duckdns", "--execute", "--only", "--yes",
                   "--accept-cost", "--secrets-file", "--group", "--final-snapshot", "--no-final-snapshot", "DUCKDNS_TOKEN", "*.secrets",
                   "沒有例外"):
        assert needed in section, f"營運文件第 8 節沒有提到 {needed}"
    parser = demo.build_parser()
    commands = {action.dest: action for action in parser._actions if action.dest == "command"}["command"].choices
    for name in commands:
        assert f"aws_demo.py {name}" in section, f"營運文件沒有 {name} 的用法範例"
