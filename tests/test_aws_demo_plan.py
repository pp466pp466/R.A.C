"""aws-demo-environment 規格：展示環境的佈建計畫（純函式）——區域與名稱守門、標籤、變數歸類、資源步驟與費用護欄。

只比對 scripts/aws_demo_plan.py 產生的資料，不呼叫 AWS 指令，也不可能在測試中誤建資源。
"""
import base64
import hashlib
import inspect
import json
import re
import shlex
import shutil
import subprocess
from datetime import date

import pytest

from app import cli
from app.config import PROJECT_ROOT
from tests.support.scripts import load_script

plan = load_script("aws_demo_plan")
launch_plan = load_script("launch_plan")

ACCOUNT = "123456789012"
EMAIL = "ops@example.com"
SECRET_SENTINEL = "never-appears-S3cr3t-value"
PLAIN = {
    "MAIL_SMTP_HOST": "smtp.example.com", "MAIL_SMTP_PORT": "587", "MAIL_FROM": "service@example.com",
    "ALERT_EMAIL_TO": "ops@example.com", "LOG_LEVEL": "INFO",
}
HOST_INIT_IDS = ("host-init-system", "host-init-ca", "host-init-files", "host-init-units", "host-init-secrets", "host-init-sshd")


def target(**overrides):
    values = dict(account_id=ACCOUNT, notify_emails=(EMAIL,))
    values.update(overrides)
    return plan.Target(**values)


def full_plan(accept_cost=True, plain=PLAIN, **overrides):
    return plan.build_plan(target(**overrides), accept_cost=accept_cost, plain=plain)


def by_id(steps):
    return {step.id: step for step in steps}


def commands(step):
    """步驟內所有會被執行的指令引數（主指令、已存在檢查、輪詢、驗證）。"""
    result = []
    if step.argv:
        result.append(step.argv)
    if step.exists_argv:
        result.append(step.exists_argv)
    if step.poll:
        result.append(step.poll.argv)
    result.extend(check.argv for check in step.verify)
    return result


def all_strings(step):
    """步驟內所有文字（含說明、JSON 範本、預期輸出），用來掃描機密與佔位符。"""
    texts = [step.id, step.title, step.input_json or ""]
    for argv in commands(step):
        texts.extend(argv)
    if step.poll:
        texts.append(step.poll.expect)
    texts.extend(check.expect for check in step.verify)
    return texts


def json_args(step):
    found = []
    for arg in step.argv:
        if arg[:1] in ("{", "["):
            try:
                found.append(json.loads(arg))
            except json.JSONDecodeError:
                pass                    # 例如 {{subnet-ids}}：佔位符，不是 JSON
    if step.input_json:
        found.append(json.loads(step.input_json))
    return found


def tags_in(value):
    if isinstance(value, dict):
        if "Key" in value and "Value" in value:
            yield value["Key"], value["Value"]
        for inner in value.values():
            yield from tags_in(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from tags_in(inner)


def argument_after(argv, flag):
    return argv[list(argv).index(flag) + 1]


# ---- 1.1 區域與名稱守門、標籤 --------------------------------------------------------------------


@pytest.mark.parametrize("region", ["ap-northeast-1", "us-east-1", "ap-southeast-1", ""])
def test_any_region_other_than_sydney_is_rejected(region):
    with pytest.raises(plan.PlanError, match="ap-southeast-2"):
        plan.Target(account_id=ACCOUNT, region=region)


def test_the_region_guard_runs_before_any_step_can_be_built():
    """區域錯誤在 Target 建立時就被拒，所以沒有任何步驟（也就沒有任何 AWS 指令）能被產生。"""
    with pytest.raises(plan.PlanError):
        plan.build_plan(plan.Target(account_id=ACCOUNT, region="ap-northeast-1", notify_emails=(EMAIL,)))
    assert plan.Target(account_id=ACCOUNT).region == "ap-southeast-2"


@pytest.mark.parametrize("account", ["", "12345", "12345678901a", "1234567890123"])
def test_account_must_be_twelve_digits(account):
    with pytest.raises(plan.PlanError, match="account_id"):
        plan.Target(account_id=account)


def test_every_aws_command_names_the_sydney_region_explicitly():
    for step in full_plan():
        for argv in commands(step):
            assert argv[0] == "aws"
            assert "--region=ap-southeast-2" in argv, (step.id, argv[:3])
            assert not any(arg.startswith("--region=") and arg != "--region=ap-southeast-2" for arg in argv)


@pytest.mark.parametrize("name", ["rac-production-db", "Production", "demo-PRODUCTION-host"])
def test_names_containing_production_are_rejected(name):
    with pytest.raises(plan.PlanError, match="production"):
        plan.assert_name(name)


@pytest.mark.parametrize("domain", ["production.example.com", "rac.production.duckdns.org", "Prod-PRODUCTION.org"])
def test_a_domain_containing_production_is_rejected(domain):
    with pytest.raises(plan.PlanError, match="production"):
        target(domain=domain)


@pytest.mark.parametrize("domain", ["", "https://rac001.duckdns.org", "rac001.duckdns.org/path", "no_dots", "UPPER.example.com"])
def test_a_malformed_domain_is_rejected(domain):
    with pytest.raises(plan.PlanError):
        target(domain=domain)


def test_a_prefix_containing_production_would_fail_every_name(monkeypatch):
    monkeypatch.setattr(plan, "NAME_PREFIX", "rac-production")
    with pytest.raises(plan.PlanError, match="production"):
        plan.names(target())


def test_all_resource_names_follow_the_naming_rule_and_never_contain_production():
    n = plan.names(target())
    for field_name, value in n.__dict__.items():
        assert "production" not in value.lower(), field_name
        if field_name != "ssm_prefix":
            assert plan.NAME_PATTERN.match(value), (field_name, value)
            assert value.startswith("rac-demo-staging"), (field_name, value)
    assert n.bucket == f"rac-demo-staging-{ACCOUNT}-transfer" and len(n.bucket) <= 63
    assert n.ssm_prefix == "/rac-demo/staging"


def test_nothing_in_the_whole_plan_mentions_production():
    text = " ".join(" ".join(all_strings(step)) for step in full_plan()).lower()
    assert "production" not in text


def test_every_created_resource_carries_project_and_environment_tags():
    steps = full_plan()
    created = [step for step in steps if step.resource]
    assert created
    for step in created:
        if step.resource == "s3-bucket":
            continue                    # create-bucket 不接受標籤，由下一個 tag-bucket 步驟加上（下面單獨檢查）
        tags = set()
        for value in json_args(step):
            tags |= set(tags_in(value))
        assert ("Project", "rac-demo") in tags and ("Environment", "staging") in tags, step.id
    bucket_tags = set()
    for value in json_args(by_id(steps)["tag-bucket"]):
        bucket_tags |= set(tags_in(value))
    assert {("Project", "rac-demo"), ("Environment", "staging")} <= bucket_tags


def test_step_ids_are_unique_and_groups_are_known_and_ordered():
    steps = full_plan()
    ids = [step.id for step in steps]
    assert len(ids) == len(set(ids))
    assert {step.group for step in steps} == set(plan.GROUPS)
    positions = [plan.GROUPS.index(step.group) for step in steps]
    assert positions == sorted(positions)


def test_groups_can_be_selected_without_changing_the_order():
    steps = plan.build_plan(target(), groups=("network", "iam"))
    assert {step.group for step in steps} == {"network", "iam"}
    assert [s.id for s in steps if s.group == "network"][0] == "get-default-vpc"
    with pytest.raises(plan.PlanError, match="bogus"):
        plan.build_plan(target(), groups=("bogus",))


def test_the_plan_module_is_pure():
    source = inspect.getsource(plan)
    for forbidden in ("subprocess", "os.system", "os.environ", "urllib", "socket", "open("):
        assert forbidden not in source, forbidden


# ---- 1.2 變數歸類與機密 ------------------------------------------------------------------------


def example_variables() -> set[str]:
    text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8-sig")
    return {m.group(1) for m in re.finditer(r"^([A-Z][A-Z0-9_]*)=", text, re.M)}


def test_variable_classes_are_imported_from_launch_plan_not_copied():
    for name in ("SECRET_VARS", "PLAIN_VARS", "NOT_DEPLOYED", "OBSOLETE_VARS", "REQUIRED_SECRETS", "REQUIRED_PLAIN"):
        assert getattr(plan, name) is getattr(launch_plan, name), name


def test_every_variable_in_env_example_is_in_exactly_one_class():
    classes = {"secret": set(plan.SECRET_VARS), "plain": set(plan.PLAIN_VARS), "not_deployed": set(plan.NOT_DEPLOYED)}
    for variable in example_variables():
        owners = [name for name, members in classes.items() if variable in members]
        assert len(owners) == 1, f"{variable} 屬於 {owners or '沒有任何類別'}"
    assert set().union(*classes.values()) <= example_variables()


def test_a_variable_removed_from_the_classes_would_be_caught(monkeypatch):
    monkeypatch.setattr(plan, "PLAIN_VARS", tuple(v for v in plan.PLAIN_VARS if v != "LOG_LEVEL"))
    classified = set(plan.SECRET_VARS) | set(plan.PLAIN_VARS) | set(plan.NOT_DEPLOYED)
    assert example_variables() - classified == {"LOG_LEVEL"}


def test_secret_references_are_names_only_and_never_values():
    every_secret = tuple(v for v in plan.SECRET_VARS if v not in plan.COMPOSED_SECRETS)
    steps = full_plan(with_secrets=every_secret)
    references = []
    for step in steps:
        references.extend(plan.SECRET_REFERENCE.findall(" ".join(all_strings(step))))
        assert not plan.SECRET_REFERENCE.search(" ".join(step.argv)), f"{step.id}：機密參照只能在 JSON 範本內，不能在命令列"
    assert references, "至少要有機密以參照的形式出現"
    known = set(plan.SECRET_VARS) | set(plan.DB_ONLY_SECRETS)
    assert set(references) <= known


def test_json_bodies_hold_only_references_for_every_secret_field():
    steps = full_plan(with_secrets=("OPENROUTER_API_KEY", "MAIL_SMTP_PASSWORD"))
    checked = 0
    for step in steps:
        if not step.input_json:
            continue
        body = json.loads(step.input_json)
        for key in ("Value", "MasterUserPassword"):
            if key in body:
                assert re.fullmatch(r"\{\{secret:[A-Z][A-Z0-9_]*\}\}", body[key]), (step.id, key)
                checked += 1
    assert checked >= 5      # SECRET_KEY、GEMINI_API_KEY、OPENROUTER_API_KEY、MAIL_SMTP_PASSWORD、兩個資料庫密碼、DATABASE_URL、RDS 主要密碼


def test_the_plan_output_has_no_secret_value_flag_or_credential_shape():
    settings = plan.plain_settings(target(), PLAIN)
    text = " ".join(" ".join(all_strings(step)) for step in full_plan(with_secrets=("OPENROUTER_API_KEY",)))
    assert not set(settings) & set(plan.SECRET_VARS)
    assert not re.search(r"AKIA[0-9A-Z]{16}|--master-user-password|--password|--value\b|--secret-string", text)
    assert SECRET_SENTINEL not in text + json.dumps(settings)


@pytest.mark.parametrize("var", sorted(set(launch_plan.SECRET_VARS)))
def test_a_secret_variable_in_plain_settings_is_rejected_without_echoing_its_value(var):
    with pytest.raises(plan.PlanError) as caught:
        plan.plain_settings(target(), {**PLAIN, var: SECRET_SENTINEL})
    assert var in str(caught.value) and SECRET_SENTINEL not in str(caught.value)


def test_plain_settings_fill_app_env_and_base_url_and_reject_conflicts():
    settings = plan.plain_settings(target(), PLAIN)
    assert settings["APP_ENV"] == "staging" and settings["BASE_URL"] == "https://rac001.duckdns.org"
    assert list(settings) == sorted(settings)
    with pytest.raises(plan.PlanError, match="APP_ENV"):
        plan.plain_settings(target(), {**PLAIN, "APP_ENV": "production"})
    with pytest.raises(plan.PlanError, match="BASE_URL"):
        plan.plain_settings(target(), {**PLAIN, "BASE_URL": "https://other.example.com"})
    assert plan.plain_settings(target(), {**PLAIN, "BASE_URL": "https://rac001.duckdns.org"})["BASE_URL"] == "https://rac001.duckdns.org"


def test_plain_settings_reject_obsolete_not_deployed_unknown_and_missing_variables():
    with pytest.raises(plan.PlanError, match="EMAIL_VERIFICATION_REQUIRED"):
        plan.plain_settings(target(), {**PLAIN, "EMAIL_VERIFICATION_REQUIRED": "false"})
    with pytest.raises(plan.PlanError, match="TRUST_PROXY_HEADERS"):
        plan.plain_settings(target(), {**PLAIN, "TRUST_PROXY_HEADERS": "1"})
    with pytest.raises(plan.PlanError, match="NOT_A_REAL_SETTING"):
        plan.plain_settings(target(), {**PLAIN, "NOT_A_REAL_SETTING": "x"})
    with pytest.raises(plan.PlanError, match="MAIL_SMTP_HOST") as caught:
        plan.plain_settings(target(), {k: v for k, v in PLAIN.items() if k != "MAIL_SMTP_HOST"})
    assert "smtp.example.com" not in str(caught.value)


def test_app_unit_parameters_never_include_the_rds_master_password():
    t = target(with_secrets=("OPENROUTER_API_KEY",))
    app = plan.unit_parameter_names(t, "app")
    assert app == ("SECRET_KEY", "DATABASE_URL", "GEMINI_API_KEY", "OPENROUTER_API_KEY")
    for forbidden in plan.DB_ONLY_SECRETS:
        assert forbidden not in app
    assert plan.unit_parameter_names(t, "bootstrap") == ("RDS_MASTER_PASSWORD", "DB_APP_PASSWORD")
    assert not set(plan.unit_parameter_names(t, "bootstrap")) & set(app)
    with pytest.raises(plan.PlanError, match="batch"):
        plan.unit_parameter_names(t, "batch")


def test_parameter_names_cover_every_app_secret_and_both_database_passwords_once():
    names = plan.parameter_names(target(with_secrets=("GEMINI_API_KEY", "MAIL_SMTP_PASSWORD")))   # GEMINI_API_KEY 重複指定
    assert len(names) == len(set(names))
    assert set(names) == {"SECRET_KEY", "DATABASE_URL", "GEMINI_API_KEY", "MAIL_SMTP_PASSWORD", "RDS_MASTER_PASSWORD", "DB_APP_PASSWORD"}


def test_secret_sources_distinguish_generated_composed_and_provided():
    assert plan.secret_source("SECRET_KEY") == "generated"
    assert plan.secret_source("RDS_MASTER_PASSWORD") == "generated"
    assert plan.secret_source("DB_APP_PASSWORD") == "generated"
    assert plan.secret_source("DATABASE_URL") == "composed"
    assert plan.secret_source("GEMINI_API_KEY") == "provided"
    with pytest.raises(plan.PlanError):
        plan.secret_source("NOT_A_SECRET")


def test_unknown_or_composed_secrets_cannot_be_requested():
    with pytest.raises(plan.PlanError, match="NOT_A_SECRET"):
        target(with_secrets=("NOT_A_SECRET",))
    with pytest.raises(plan.PlanError, match="DATABASE_URL"):
        target(with_secrets=("DATABASE_URL",))


def test_parameters_live_under_the_project_path_as_secure_strings():
    steps = [s for s in full_plan() if s.id.startswith("put-parameter-")]
    assert {s.id for s in steps} == {f"put-parameter-{v}" for v in plan.parameter_names(target())}
    for step in steps:
        body = json.loads(step.input_json)
        assert body["Type"] == "SecureString"
        assert body["Name"].startswith("/rac-demo/staging/")
        assert step.argv[:3] == ("aws", "ssm", "put-parameter") and "file://{{input_file}}" in step.argv
        assert "--value" not in step.argv, "值不得出現在命令列"
        assert step.exists_argv and "--with-decryption" not in step.exists_argv, "存在檢查不得讀出明文"


def test_database_url_is_written_only_after_the_rds_endpoint_is_known():
    steps = full_plan()
    ids = [s.id for s in steps]
    url_step = by_id(steps)["put-parameter-DATABASE_URL"]
    assert url_step.group == "rds-ready"
    assert ids.index("get-rds-endpoint") < ids.index("put-parameter-DATABASE_URL")
    assert "put-parameter-DATABASE_URL" not in [s.id for s in steps if s.group == "secrets"]


def test_the_existing_database_url_can_be_overwritten_by_a_step_that_never_shows_or_reads_the_value():
    """design D7 修訂：既有的 SSM `DATABASE_URL` 仍是舊型態（帶 ServerCertificate），`put-parameter-…` 對已存在的參數會略過，所以要有覆寫步驟。"""
    steps = full_plan()
    ids = [s.id for s in steps]
    refresh = by_id(steps)["refresh-parameter-DATABASE_URL"]
    assert refresh.group == "rds-ready" and ids.index("put-parameter-DATABASE_URL") < ids.index(refresh.id)
    assert refresh.argv[:3] == ("aws", "ssm", "put-parameter") and "file://{{input_file}}" in refresh.argv
    body = json.loads(refresh.input_json)
    assert body == {"Name": "/rac-demo/staging/DATABASE_URL", "Type": "SecureString", "Value": "{{secret:DATABASE_URL}}", "Overwrite": True}
    assert "Tags" not in body, "AWS 不接受覆寫同時指定標籤；原有的標籤保留"
    assert "--value" not in refresh.argv and "--overwrite" not in refresh.argv and "--with-decryption" not in " ".join(refresh.argv)
    assert refresh.exists_argv is None and refresh.resource is None and not refresh.billing and not refresh.interactive, "不建立資源，沒有已存在就略過"
    assert len(refresh.verify) == 1 and "--with-decryption" not in refresh.verify[0].argv, "驗證只看名稱與型別，不讀出明文"
    assert refresh.verify[0].expect == '["/rac-demo/staging/DATABASE_URL","SecureString"]'
    assert "Parameters[0].[Name,Type]" in " ".join(refresh.verify[0].argv)


def test_only_composed_secrets_may_be_overwritten():
    """隨機產生的機密（密碼、SECRET_KEY）重新產生會讓既有的資源與參數不一致，不提供覆寫。"""
    assert plan._refresh_parameter_step(target(), "DATABASE_URL", "rds-ready").id == "refresh-parameter-DATABASE_URL"
    for var in ("SECRET_KEY", "RDS_MASTER_PASSWORD", "DB_APP_PASSWORD", "GEMINI_API_KEY"):
        with pytest.raises(plan.PlanError, match=var):
            plan._refresh_parameter_step(target(), var, "rds-ready")
    assert [s.id for s in full_plan() if s.id.startswith("refresh-parameter-")] == ["refresh-parameter-DATABASE_URL"]


# ---- 1.3 資源步驟 ------------------------------------------------------------------------------


def test_resources_are_exactly_the_allowed_types_and_no_other_service_is_used():
    steps = full_plan()
    assert {s.resource for s in steps if s.resource} == set(plan.ALLOWED_RESOURCES)
    services = {argv[1] for step in steps for argv in commands(step)}
    assert services <= {"sts", "budgets", "sns", "ec2", "iam", "s3api", "ssm", "rds", "cloudwatch"}
    text = " ".join(" ".join(argv) for step in steps for argv in commands(step))
    for forbidden in ("elbv2", "elb ", "create-nat-gateway", "create-vpc", "create-key-pair", "authorize-security-group-egress", "secretsmanager"):
        assert forbidden not in text, forbidden
    run_instances = [s for s in steps if s.argv[1:3] == ("ec2", "run-instances")]
    assert len(run_instances) == 1 and argument_after(run_instances[0].argv, "--count") == "1"


def test_every_resource_creating_step_can_be_skipped_when_it_exists_or_is_natively_idempotent():
    steps = full_plan()
    # create-topic 回傳既有的主題、put-metric-alarm 覆寫同名告警，兩者本身就是冪等的
    assert {s.id for s in steps if s.resource and not s.exists_argv} == {"create-sns-topic", "create-ec2-status-alarm"}


def test_host_security_group_opens_only_80_and_443_and_never_22():
    steps = by_id(full_plan())
    rules = json.loads(argument_after(steps["authorize-host-ingress"].argv, "--ip-permissions"))
    assert sorted(rule["FromPort"] for rule in rules) == [80, 443]
    for rule in rules:
        assert rule["FromPort"] == rule["ToPort"] and rule["IpProtocol"] == "tcp"
        assert rule["IpRanges"] == [{"CidrIp": "0.0.0.0/0"}] and "UserIdGroupPairs" not in rule
    assert 22 not in [rule["FromPort"] for rule in rules]


def test_database_security_group_allows_only_the_host_group_with_no_cidr():
    steps = by_id(full_plan())
    rules = json.loads(argument_after(steps["authorize-db-ingress"].argv, "--ip-permissions"))
    assert len(rules) == 1
    rule = rules[0]
    assert rule["FromPort"] == rule["ToPort"] == 1433
    assert rule["UserIdGroupPairs"] == [{"GroupId": "{{host-sg-id}}"}]
    assert "IpRanges" not in rule and "Ipv6Ranges" not in rule
    assert argument_after(steps["authorize-db-ingress"].argv, "--group-id") == "{{db-sg-id}}"


def test_security_group_descriptions_are_ascii_because_ec2_rejects_other_characters():
    for step in full_plan():
        if step.argv[1:3] == ("ec2", "create-security-group"):
            argument_after(step.argv, "--description").encode("ascii")


def test_host_role_policy_reads_only_the_project_parameter_path_and_the_transfer_bucket():
    document = plan.role_policy_document(target())
    actions = [action for statement in document["Statement"] for action in
               (statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]])]
    assert set(actions) == {"ssm:GetParameter", "ssm:GetParameters", "s3:GetObject", "s3:ListBucket"}
    for statement in document["Statement"]:
        assert statement["Effect"] == "Allow" and "*" != statement["Resource"]
        resource = statement["Resource"]
        if any(a.startswith("ssm:") for a in statement["Action"]):
            assert resource == f"arn:aws:ssm:ap-southeast-2:{ACCOUNT}:parameter/rac-demo/staging/*"
        else:
            assert resource.startswith(f"arn:aws:s3:::rac-demo-staging-{ACCOUNT}-transfer")
    assert not any(a.startswith(("iam:", "kms:", "ec2:", "rds:")) for a in actions)


def test_role_trust_is_ec2_only_and_the_managed_policy_is_just_ssm_core():
    steps = by_id(full_plan())
    trust = json.loads(argument_after(steps["create-host-role"].argv, "--assume-role-policy-document"))
    assert [s["Principal"] for s in trust["Statement"]] == [{"Service": "ec2.amazonaws.com"}]
    attached = [s.argv for s in steps.values() if s.argv[1:3] == ("iam", "attach-role-policy")]
    assert [argument_after(argv, "--policy-arn") for argv in attached] == ["arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"]
    assert steps["wait-instance-profile"].poll is not None


def test_transfer_bucket_blocks_public_access_and_is_encrypted():
    steps = by_id(full_plan())
    block = json.loads(argument_after(steps["block-bucket-public-access"].argv, "--public-access-block-configuration"))
    assert block == {"BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True}
    sse = json.loads(argument_after(steps["encrypt-bucket"].argv, "--server-side-encryption-configuration"))
    assert sse["Rules"][0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"
    assert "LocationConstraint=ap-southeast-2" in steps["create-bucket"].argv


def test_rds_is_private_encrypted_backed_up_and_reachable_only_through_the_database_group():
    steps = by_id(full_plan())
    body = json.loads(steps["create-rds-instance"].input_json)
    assert body["PubliclyAccessible"] is False and body["StorageEncrypted"] is True
    # Free plan 的 AWS 驗證拒絕 7 天（FreeTierRestrictionError，2026-11-16 實測）；規格只要求大於 0
    assert body["BackupRetentionPeriod"] == 1 and body["BackupRetentionPeriod"] > 0 and body["MultiAZ"] is False
    assert body["VpcSecurityGroupIds"] == ["{{db-sg-id}}"] and body["DBSubnetGroupName"] == "rac-demo-staging-db-subnets"
    assert body["Engine"] == "sqlserver-ex" and body["DBInstanceClass"] == "db.t3.micro" and body["LicenseModel"] == "license-included"
    assert body["MasterUsername"] not in ("sa", "admin") and body["MasterUserPassword"] == "{{secret:RDS_MASTER_PASSWORD}}"
    assert "DBName" not in body, "SQL Server 不接受 DBName；資料庫由初始化腳本建立"
    assert "MasterUserPassword" not in " ".join(steps["create-rds-instance"].argv)
    assert "--master-user-password" not in steps["create-rds-instance"].argv
    assert "file://{{input_file}}" in steps["create-rds-instance"].argv


def test_ec2_enforces_imdsv2_uses_gp3_20gb_and_has_no_key_pair():
    launch = by_id(full_plan())["launch-ec2"]
    assert argument_after(launch.argv, "--metadata-options") == "HttpTokens=required,HttpEndpoint=enabled,HttpPutResponseHopLimit=1"
    assert argument_after(launch.argv, "--instance-type") == "t3.small"
    assert "--key-name" not in launch.argv
    device = json.loads(argument_after(launch.argv, "--block-device-mappings"))[0]["Ebs"]
    assert device["VolumeType"] == "gp3" and device["VolumeSize"] == 20 and device["Encrypted"] is True
    assert argument_after(launch.argv, "--security-group-ids") == "{{host-sg-id}}"
    assert argument_after(launch.argv, "--iam-instance-profile") == "Name=rac-demo-staging-host-profile"
    specs = json.loads(argument_after(launch.argv, "--tag-specifications"))
    assert {spec["ResourceType"] for spec in specs} == {"instance", "volume"}
    assert launch.capture == "instance-id"


def test_billing_steps_print_no_command_without_accept_cost():
    steps = by_id(full_plan(accept_cost=False))
    for step_id in ("create-rds-instance", "launch-ec2", "allocate-eip"):
        step = steps[step_id]
        assert step.billing and step.blocked
        assert step.argv == () and step.input_json is None and step.exists_argv is None and step.verify == ()
        assert "--accept-cost" in step.title
    text = " ".join(" ".join(argv) for step in steps.values() for argv in commands(step))
    for command in ("create-db-instance", "run-instances", "allocate-address"):
        assert command not in text, command
    assert not any(s.blocked for s in steps.values() if not s.billing)


def test_billing_steps_print_their_commands_with_accept_cost():
    steps = by_id(full_plan(accept_cost=True))
    for step_id, command in (("create-rds-instance", "create-db-instance"), ("launch-ec2", "run-instances"), ("allocate-eip", "allocate-address")):
        assert steps[step_id].billing and not steps[step_id].blocked and command in steps[step_id].argv
    assert {s.id for s in steps.values() if s.billing} == {"create-rds-instance", "launch-ec2", "allocate-eip"}


def test_blocked_steps_keep_their_capture_so_later_placeholders_still_resolve():
    for accept in (False, True):
        assert_placeholders_resolve(full_plan(accept_cost=accept))


def placeholders(texts):
    found = set()
    for text in texts:
        found |= set(plan.PLACEHOLDER.findall(text))
    found.discard("input_file")
    return found


def assert_placeholders_resolve(steps):
    """指令、已存在檢查、輪詢與 JSON 範本只能用「更早的步驟」擷取的名稱；驗證在本步驟之後執行，所以也可用本步驟自己的擷取。"""
    captured = set()
    for step in steps:
        before = [step.input_json or "", *step.argv, *(step.exists_argv or ())]
        if step.poll:
            before.extend([*step.poll.argv, step.poll.expect])
        # 拆除步驟（run_if_found）：capture 存的是查詢的輸出，指令可以用它；查詢本身仍只能用更早的擷取
        own = {step.capture} if step.run_if_found and step.capture else set()
        used = placeholders(before)
        assert used <= captured | own, f"{step.id} 用到尚未擷取的佔位符：{sorted(used - captured - own)}"
        assert placeholders(step.exists_argv or ()) <= captured, f"{step.id} 的查詢用到尚未擷取的佔位符"
        assert ("{{input_file}}" in " ".join(step.argv)) == (step.input_json is not None), step.id
        if step.capture:
            captured.add(step.capture)
        after = [text for check in step.verify for text in (*check.argv, check.expect)]
        assert placeholders(after) <= captured, f"{step.id} 的驗證用到尚未擷取的佔位符"


def test_every_placeholder_is_captured_by_an_earlier_step():
    assert_placeholders_resolve(full_plan())


def test_budget_has_two_thresholds_and_notifies_only_the_given_addresses():
    budget = by_id(full_plan())["create-budget"]
    notifications = json.loads(argument_after(budget.argv, "--notifications-with-subscribers"))
    assert [n["Notification"]["Threshold"] for n in notifications] == [60, 90]
    for entry in notifications:
        assert entry["Notification"]["ThresholdType"] == "PERCENTAGE" and entry["Notification"]["NotificationType"] == "ACTUAL"
        assert entry["Subscribers"] == [{"SubscriptionType": "EMAIL", "Address": EMAIL}]
    limit = json.loads(argument_after(budget.argv, "--budget"))
    assert limit["BudgetLimit"] == {"Amount": "75", "Unit": "USD"} and limit["TimeUnit"] == "QUARTERLY"
    assert limit["CostTypes"]["IncludeCredit"] is False, "預算要對照實際花費，抵用金不能抵銷它"
    assert argument_after(budget.argv, "--account-id") == ACCOUNT
    assert budget.verify[0].expect == "2"


def test_notification_addresses_have_no_default_and_are_required_for_budget_and_alarm_groups():
    assert plan.Target(account_id=ACCOUNT).notify_emails == ()
    for groups in (("budget",), ("alarms",), plan.GROUPS):
        with pytest.raises(plan.PlanError, match="通知信箱"):
            plan.build_plan(plan.Target(account_id=ACCOUNT), groups=groups)
    assert plan.build_plan(plan.Target(account_id=ACCOUNT), groups=("network", "iam", "storage", "secrets"))


@pytest.mark.parametrize("emails", [("not-an-email",), ("a@example.com", "a@example.com"), ("Name <a@example.com>",), ("a@example.com\nb@example.com",),
                                    ("a@example.com\n",), tuple(f"u{i}@example.com" for i in range(11))])
def test_invalid_notification_addresses_are_rejected_without_echoing_them(emails):
    with pytest.raises(plan.PlanError) as caught:
        plan.build_plan(plan.Target(account_id=ACCOUNT, notify_emails=emails), groups=("budget",))
    for email in emails:
        assert email not in str(caught.value)


def test_each_notification_address_gets_its_own_subscription_and_a_manual_confirmation_step():
    steps = full_plan(notify_emails=("a@example.com", "b@example.com"))
    ids = [s.id for s in steps]
    assert "subscribe-sns-0" in ids and "subscribe-sns-1" in ids and "subscribe-sns-2" not in ids
    confirm = by_id(steps)["confirm-sns-subscriptions"]
    assert confirm.interactive and confirm.argv == ()
    assert confirm.verify[0].expect == "0"


def test_the_instance_status_alarm_notifies_the_sns_topic_and_is_tagged():
    alarm = by_id(full_plan())["create-ec2-status-alarm"]
    assert argument_after(alarm.argv, "--alarm-actions") == f"arn:aws:sns:ap-southeast-2:{ACCOUNT}:rac-demo-staging-alerts"
    assert argument_after(alarm.argv, "--metric-name") == "StatusCheckFailed" and "Name=InstanceId,Value={{instance-id}}" in alarm.argv


def test_deployment_order_overlaps_ec2_with_the_rds_wait():
    ids = [s.id for s in full_plan()]
    order = ["check-identity", "create-budget", "create-host-sg", "create-db-sg", "create-host-role", "wait-instance-profile", "create-bucket",
             "put-parameter-RDS_MASTER_PASSWORD", "create-rds-instance", "launch-ec2", "allocate-eip", "wait-ssm-online",
             "wait-rds-available", "get-rds-endpoint", "put-parameter-DATABASE_URL", "refresh-parameter-DATABASE_URL", "create-ec2-status-alarm"]
    positions = [ids.index(step_id) for step_id in order]
    assert positions == sorted(positions) and len(set(positions)) == len(order)
    assert ids.index("create-rds-instance") < ids.index("launch-ec2") < ids.index("wait-rds-available")
    assert ids.index("create-db-sg") > ids.index("create-host-sg")             # 資料庫群組的來源是主機群組
    assert ids.index("put-parameter-RDS_MASTER_PASSWORD") < ids.index("create-rds-instance")


def test_wait_steps_always_have_a_timeout_and_a_polling_interval():
    polls = [s for s in full_plan() if s.poll]
    host_init_waits = {f"wait-{step_id}" for step_id in HOST_INIT_IDS}
    assert {s.id for s in polls} == {"wait-instance-profile", "wait-ec2-running", "wait-ssm-online", "wait-rds-available"} | host_init_waits
    for step in polls:
        assert step.argv == () and step.timeout_seconds and step.interval_seconds
        assert 0 < step.interval_seconds < step.timeout_seconds
    assert by_id(full_plan())["wait-rds-available"].timeout_seconds >= 25 * 60, "RDS 建立最久約 25 分鐘"


def test_first_step_checks_the_account_and_stops_on_mismatch():
    first = full_plan()[0]
    assert first.id == "check-identity" and not first.billing and not first.resource
    assert first.verify[0].expect == ACCOUNT


# ---- 費用預估 ----------------------------------------------------------------------------------


def test_cost_estimate_fits_inside_the_credit_and_states_that_prices_change():
    daily = plan.estimated_daily_usd()
    assert 1.5 <= daily <= 1.8
    total = plan.estimated_total_usd(date(2026, 11, 16))
    assert 70 <= total <= 80 < 100
    assert plan.BUDGET_LIMIT_USD < 100 and abs(plan.BUDGET_LIMIT_USD - total) <= 5
    assert plan.estimated_total_usd(date(2027, 1, 1)) == 0
    lines = "\n".join(plan.cost_lines(date(2026, 11, 16)))
    assert "價格會變" in lines and "帳單頁" in lines and "2026-12-31" in lines
    for label, _ in plan.COST_ITEMS:
        assert label in lines


# ---- 1.4 主機檔案：Caddyfile、rac-* 單元、機密取出腳本 ----------------------------------------------------


def host_files(**overrides) -> dict:
    return {f.path: f for f in plan.host_files(target(**overrides), PLAIN)}


def exec_tokens(unit_text: str, key: str = "ExecStart") -> list[str]:
    """單元檔裡某個指令行的引數（反斜線接續的行先合併）。"""
    joined = unit_text.replace("\\\n", " ")
    line = next(l for l in joined.splitlines() if l.startswith(key + "="))
    return shlex.split(line[len(key) + 1:])


def option_values(tokens: list[str], flag: str) -> list[str]:
    return [t.split("=", 1)[1] for t in tokens if t.startswith(flag + "=")]


def pool_units() -> dict:
    return {pool.container: plan.pool_unit(pool) for pool in plan.POOLS}


def app_container_units() -> dict:
    """所有跑應用程式映像的單元：兩個池與每個排程工作的服務。"""
    units = dict(pool_units())
    units.update({name: text for name, text in plan.scheduled_units().items() if name.endswith(".service")})
    return units


def test_host_files_are_utf8_lf_without_bom_and_have_unique_absolute_paths():
    files = plan.host_files(target(), PLAIN)
    assert len({f.path for f in files}) == len(files)
    for f in files:
        assert f.path.startswith("/") and f.mode in ("0644", "0755"), f.path
        assert "\r" not in f.content and f.content.endswith("\n") and not f.content.startswith("\ufeff"), f.path
        f.content.encode("utf-8")           # 不得含無法編碼的字元（例如孤立代理字元）


def test_host_files_list_the_caddyfile_env_scripts_and_every_unit():
    files = host_files()
    jobs = {f"/etc/systemd/system/rac-job-{c}.{kind}" for c in cli.SCHEDULED_COMMANDS for kind in ("service", "timer")}
    assert set(files) == {
        "/etc/rac/Caddyfile", "/etc/rac/plain.env",
        "/usr/local/sbin/rac-render-env-app", "/usr/local/sbin/rac-render-env-bootstrap",
        "/etc/systemd/system/rac-cs.service", "/etc/systemd/system/rac-rpg.service", "/etc/systemd/system/rac-caddy.service",
    } | jobs
    assert "/etc/rac/release.env" not in files, "版本標籤在部署時才寫入"
    assert files["/usr/local/sbin/rac-render-env-app"].mode == "0755" and files["/etc/rac/Caddyfile"].mode == "0644"


def test_host_directories_keep_certificates_on_a_private_persistent_path():
    modes = dict(plan.HOST_DIRECTORIES)
    assert modes["/var/lib/rac/caddy"] == "0700"
    assert modes["/etc/rac"] == "0755"
    assert not any(path.startswith("/run") or path.startswith("/tmp") for path in modes)


def test_caddyfile_routes_game_paths_to_the_rpg_pool_and_the_rest_to_the_customer_service_pool():
    text = plan.caddyfile(target())
    assert re.search(r"^rac001\.duckdns\.org \{$", text, re.M)
    game = re.search(r"@game path /api/game/\*\n\s+handle @game \{\n(.*?)\n    \}\n", text, re.S)
    assert game, "RPG 路徑必須有自己的分流區塊"
    assert re.findall(r"reverse_proxy (\S+)", game.group(1)) == ["127.0.0.1:8082"]
    assert "flush_interval -1" in game.group(1)
    rest = text.split("handle {", 1)[1]
    assert re.findall(r"reverse_proxy (\S+)", rest) == ["127.0.0.1:8081"]
    assert "flush_interval" not in rest, "只有串流的池關閉緩衝"
    assert len(re.findall(r"reverse_proxy", text)) == 2


def test_caddyfile_does_not_compress_buffer_or_time_out_the_stream_and_keeps_app_headers():
    text = plan.caddyfile(target())
    assert not re.search(r"\b(encode|timeouts?|read_timeout|write_timeout|response_header_timeout|transport)\b", text), text
    assert not re.search(r"\bheader(_up|_down)?\b", text), "入口不得改寫或移除應用程式送出的 HSTS 與安全標頭"
    assert not re.search(r"\b(tls internal|auto_https off|http_port|https_port)\b", text), "憑證自動簽發與標準連接埠不得被關閉"


def test_caddyfile_uses_the_target_domain_and_a_malformed_domain_cannot_reach_it():
    assert plan.caddyfile(target(domain="demo.example.org")).count("demo.example.org") == 1
    for bad in ("rac001.duckdns.org\n", "rac001.duckdns.org {\n}", "rac001.duckdns.org\r\n", "a b.example.com", "{evil}.example.com"):
        with pytest.raises(plan.PlanError):
            target(domain=bad)


def test_resource_names_and_account_ids_reject_a_trailing_newline():
    with pytest.raises(plan.PlanError):
        plan.assert_name("rac-name\n")
    with pytest.raises(plan.PlanError, match="account_id"):
        target(account_id=ACCOUNT + "\n")


def test_each_pool_runs_its_own_gunicorn_config_in_its_own_container():
    units = pool_units()
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8-sig")
    for container, config, other in (("rac-cs", "gunicorn.conf.py", "gunicorn.rpg.conf.py"), ("rac-rpg", "gunicorn.rpg.conf.py", "gunicorn.conf.py")):
        tokens = exec_tokens(units[container])
        assert tokens[-4:] == ["gunicorn", "--config", config, "wsgi:app"], container
        assert other not in units[container].replace(config, ""), "兩個池不得共用設定"
        assert (PROJECT_ROOT / config).is_file() and config in dockerfile, f"{config} 必須存在且被映像帶入"
        assert option_values(tokens, "--name") == [container]


def test_application_ports_are_published_on_loopback_only_and_match_the_proxy_targets():
    published = []
    for container, text in pool_units().items():
        tokens = exec_tokens(text)
        mappings = option_values(tokens, "--publish")
        assert len(mappings) == 1 and "-p" not in tokens and "--publish-all" not in tokens and "-P" not in tokens, container
        host, host_port, container_port = mappings[0].split(":")
        assert host == "127.0.0.1", f"{container}：應用程式埠只能綁回送位址"
        assert container_port == "8080", "映像的 PORT 預設是 8080"
        published.append(host_port)
        assert "0.0.0.0" not in text
    assert sorted(published) == ["8081", "8082"] and len(set(published)) == 2
    assert sorted(re.findall(r"reverse_proxy 127\.0\.0\.1:(\d+)", plan.caddyfile(target()))) == sorted(published)
    for text in app_container_units().values():
        assert "--network=host" not in text and "--network host" not in text


def test_app_containers_are_hardened_run_as_the_image_user_and_keep_secrets_off_the_command_line():
    for name, text in app_container_units().items():
        tokens = exec_tokens(text)
        assert tokens[:3] == ["/usr/bin/docker", "run", "--rm"], name
        for required in ("--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pull=never"):
            assert required in tokens, (name, required)
        # 沒有覆寫使用者（沿用映像的非 root）、沒有額外權限或命名空間共用、沒有經 -e／--env 把值放進命令列
        assert not any(t.startswith(("--user", "-u", "--privileged", "--cap-add", "--env=", "--env", "-e", "--pid", "--ipc", "--uts",
                                     "--security-opt=seccomp", "--security-opt=apparmor", "--device", "--volume", "-v"))
                       for t in tokens if t != "--env-file" and not t.startswith("--env-file=")), (name, tokens)
        assert option_values(tokens, "--env-file") == ["/etc/rac/plain.env", "/run/rac/app.env"], "機密只經記憶體檔案系統上的環境檔"
        assert option_values(tokens, "--log-opt") == ["max-size=10m", "max-file=3"] and option_values(tokens, "--log-driver") == ["json-file"]
        assert any(t.startswith("--tmpfs=/dev/shm:") for t in tokens), "gunicorn 的 worker_tmp_dir 是 /dev/shm"
        assert tokens.index("rac-app:${RAC_IMAGE_TAG}") > tokens.index("--read-only"), "選項都在映像名稱之前"


def test_app_containers_use_the_tag_from_release_env_and_mount_no_certificate():
    """design D7：RDS 的 CA 在建置時加進映像的系統憑證庫，容器不再掛載它（掛載是已否決的 ServerCertificate 方案）。"""
    for name, text in app_container_units().items():
        assert "EnvironmentFile=/etc/rac/release.env" in text, name
        assert "rac-app:${RAC_IMAGE_TAG}" in exec_tokens(text)
        assert option_values(exec_tokens(text), "--mount") == [], name
        assert "--volume" not in text and "rds-ca-bundle" not in text and "ServerCertificate" not in text, name
    assert plan.RDS_CA_BUNDLE_PATH == "/etc/rac/rds-ca-bundle.pem", "它只是建置輸入（主機初始化 B 段放的位置）"


def test_one_off_containers_mount_no_certificate_either():
    for tag_lines in (plan._migrate_lines(TAG), plan._seed_lines(TAG), plan._migration_head_lines(TAG)):
        text = "\n".join(tag_lines)
        assert "--mount" not in text and "rds-ca-bundle" not in text and "--volume" not in text


def test_release_env_file_holds_only_a_valid_image_tag():
    assert plan.release_env_file("a1b2c3d") == "RAC_IMAGE_TAG=a1b2c3d\n"
    assert plan.release_env_file("rollback-2026.11.16") == "RAC_IMAGE_TAG=rollback-2026.11.16\n"
    for bad in ("", "Bad Tag", "x\ny", "-start", "a:b", "A1B2", "a" * 129, "a b", "$(id)"):
        with pytest.raises(plan.PlanError, match="映像標籤"):
            plan.release_env_file(bad)


def test_pool_and_caddy_services_restart_always_start_after_docker_and_are_independent():
    units = {**pool_units(), "rac-caddy": plan.caddy_unit()}
    for name, text in units.items():
        assert "Restart=always" in text and "RestartSec=5" in text, name
        assert "Requires=docker.service" in text and "WantedBy=multi-user.target" in text, name
        assert "--restart" not in text, "重啟只由 systemd 負責，不與 Docker 的重啟政策並存"
    for name in ("rac-cs", "rac-rpg"):
        other = "rac-rpg" if name == "rac-cs" else "rac-cs"
        assert other not in units[name], f"{name} 不得依賴另一個池：停止一個池不能影響另一個"
    caddy = units["rac-caddy"]
    assert "Wants=network-online.target rac-cs.service rac-rpg.service" in caddy
    assert not re.search(r"^(Requires|BindsTo|PartOf)=.*rac-(cs|rpg)", caddy, re.M), "入口只是弱依賴：池停止時入口不跟著停"


def test_pool_services_wait_long_enough_for_gunicorn_to_finish_in_flight_requests():
    for pool, graceful in ((plan.POOLS[0], 30), (plan.POOLS[1], 60)):
        text = plan.pool_unit(pool)
        stop = int(re.search(r"docker stop --time=(\d+)", text).group(1))
        timeout = int(re.search(r"^TimeoutStopSec=(\d+)$", text, re.M).group(1))
        assert stop > graceful and timeout > stop, pool.container
    for config, graceful in (("gunicorn.conf.py", 30), ("gunicorn.rpg.conf.py", 60)):
        assert f"graceful_timeout = {graceful}" in (PROJECT_ROOT / config).read_text(encoding="utf-8-sig")


def test_caddy_runs_on_the_host_network_with_a_single_capability_and_persistent_certificate_storage():
    text = plan.caddy_unit()
    tokens = exec_tokens(text)
    assert "--network=host" in tokens and "--read-only" in tokens and "--cap-drop=ALL" in tokens
    assert option_values(tokens, "--cap-add") == ["NET_BIND_SERVICE"]
    assert "--security-opt=no-new-privileges" in tokens and "--privileged" not in tokens
    assert "type=bind,source=/etc/rac/Caddyfile,target=/etc/caddy/Caddyfile,readonly" in option_values(tokens, "--mount")
    assert "type=bind,source=/var/lib/rac/caddy,target=/data" in option_values(tokens, "--mount"), "憑證與帳戶資料在持久路徑，容器重建不重新簽發"
    assert tokens[-1].startswith("caddy:") and not any(t.startswith("--publish") for t in tokens)
    assert option_values(tokens, "--log-opt") == ["max-size=10m", "max-file=3"]


def test_no_host_file_disables_certificate_verification_or_widens_trust_or_leaks_a_secret():
    text = "\n".join(f.content for f in plan.host_files(target(with_secrets=("OPENROUTER_API_KEY",)), PLAIN))
    for forbidden in ("TrustServerCertificate", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "TRUST_PROXY_HEADERS", "--privileged", "0.0.0.0",
                      SECRET_SENTINEL):
        assert forbidden not in text, forbidden
    assert not re.search(r"AKIA[0-9A-Z]{16}|--password|--value\b|--secret-string", text)


def test_plain_env_file_has_only_plain_settings_and_never_a_secret_name():
    text = plan.plain_env_file(target(), PLAIN)
    lines = text.splitlines()
    assert lines == sorted(lines) and "APP_ENV=staging" in lines and "BASE_URL=https://rac001.duckdns.org" in lines
    assert not {line.split("=", 1)[0] for line in lines} & (set(plan.SECRET_VARS) | set(plan.DB_ONLY_SECRETS))
    with pytest.raises(plan.PlanError, match="換行"):
        plan.plain_env_file(target(), {**PLAIN, "LOG_LEVEL": "INFO\nSECRET_KEY=x"})
    with pytest.raises(plan.PlanError, match="OPENROUTER_API_KEY"):
        plan.plain_env_file(target(), {**PLAIN, "OPENROUTER_API_KEY": SECRET_SENTINEL})


def names_block(script: str) -> list[str]:
    return re.search(r"^NAMES=\(\n(.*?)\n\)$", script, re.S | re.M).group(1).split()


def test_the_app_unit_script_fetches_only_app_parameters_and_never_the_rds_master_password():
    t = target(with_secrets=("OPENROUTER_API_KEY",))
    app = plan.render_env_script(t, "app")
    assert names_block(app) == list(plan.unit_parameter_names(t, "app"))
    for forbidden in plan.DB_ONLY_SECRETS:
        assert forbidden not in app
    bootstrap = plan.render_env_script(t, "bootstrap")
    assert names_block(bootstrap) == list(plan.DB_ONLY_SECRETS)
    assert not set(names_block(bootstrap)) & set(names_block(app))
    assert 'OUT="/run/rac/app.env"' in app and 'OUT="/run/rac/bootstrap.env"' in bootstrap
    with pytest.raises(plan.PlanError, match="batch"):
        plan.render_env_script(t, "batch")
    with pytest.raises(plan.PlanError, match="batch"):
        plan.render_env_script_path("batch")


def test_the_script_reads_the_same_ssm_path_the_plan_creates_and_the_role_policy_allows():
    t = target(with_secrets=("OPENROUTER_API_KEY",))
    script = plan.render_env_script(t, "app")
    prefix = re.search(r'^PREFIX="([^"]+)"$', script, re.M).group(1)
    assert prefix == plan.SSM_PREFIX == "/rac-demo/staging"
    assert re.search(r'^REGION="ap-southeast-2"$', script, re.M)
    for var in names_block(script):
        assert plan.parameter_name(var) == f"{prefix}/{var}"
    allowed = plan.role_policy_document(t)["Statement"][0]["Resource"]
    assert allowed == f"arn:aws:ssm:ap-southeast-2:{ACCOUNT}:parameter{prefix}/*"
    assert "--with-decryption" in script and "--region" in script


def test_the_script_writes_secrets_only_to_a_private_file_on_the_memory_filesystem_and_prints_no_values():
    script = plan.render_env_script(target(), "app")
    assert script.startswith("#!/bin/bash\n") and "set -euo pipefail" in script and "umask 077" in script
    assert 'chmod 0600 "$tmp"' in script and 'install -d -m 0700 -o root -g root "$RUN_DIR"' in script
    assert re.search(r'stat -f -c %T "\$RUN_DIR"\)" != "tmpfs"', script), "不是記憶體檔案系統就必須中止"
    assert 'mktemp "$RUN_DIR/' in script and 'mv -f "$tmp" "$OUT"' in script, "先寫暫存檔再原子改名，避免另一個單元讀到半個檔案"
    assert not re.search(r"set -[a-z]*x|set -o xtrace|\bxtrace\b", script), "不得開啟指令追蹤（會印出值）"
    redirections = re.findall(r">>?\s*(\S+)", script)
    assert set(redirections) <= {'"$tmp"', "&2", "&1"}, f"只能寫到暫存檔與標準錯誤：{redirections}"
    for line in script.splitlines():
        if "$value" in line:
            assert re.search(r'^\s*(if !? ?value=|if \[ -z|case "\$value"|printf \'%s=%s\\n\' "\$name" "\$value" >> "\$tmp")', line), f"值只能用於判斷與寫入暫存檔：{line}"
        if "echo" in line:
            assert ">&2" in line and "$value" not in line
    code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
    for path in ("/var/", "/home", "/root", "/etc/", "/opt", "/tmp"):
        assert path not in code, f"機密不得寫到持久路徑：{path}"


def usable_bash() -> str | None:
    path = shutil.which("bash")
    if not path:
        return None
    try:
        probe = subprocess.run([path, "-c", "echo ok"], capture_output=True, encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return path if probe.returncode == 0 and probe.stdout.strip() == "ok" else None


@pytest.mark.parametrize("unit", plan.UNITS)
def test_the_render_scripts_pass_a_bash_syntax_check(unit):
    bash = usable_bash()
    if bash is None:
        pytest.skip("這台機器沒有可用的 bash，無法做語法檢查（不算通過）")
    result = subprocess.run([bash, "-n"], input=plan.render_env_script(target(), unit).encode("utf-8"), capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")


# ---- 1.5 排程：由 SCHEDULED_COMMANDS 產生 systemd 計時器 ---------------------------------------------------


@pytest.mark.parametrize("cron, expected", [
    ("*/5 * * * *", "*-*-* *:0/5:00 Asia/Taipei"),
    ("*/1 * * * *", "*-*-* *:0/1:00 Asia/Taipei"),
    ("*/59 * * * *", "*-*-* *:0/59:00 Asia/Taipei"),
    ("0 * * * *", "*-*-* *:00:00 Asia/Taipei"),
    ("0 3 * * *", "*-*-* 03:00:00 Asia/Taipei"),
    ("20 3 * * *", "*-*-* 03:20:00 Asia/Taipei"),
    ("40 3 * * *", "*-*-* 03:40:00 Asia/Taipei"),
    ("59 23 * * *", "*-*-* 23:59:00 Asia/Taipei"),
    ("0 0 * * *", "*-*-* 00:00:00 Asia/Taipei"),
])
def test_the_three_supported_cron_forms_become_on_calendar_in_taipei_time(cron, expected):
    assert plan.cron_to_on_calendar(cron) == expected


@pytest.mark.parametrize("cron", [
    "", "@daily", "*/0 * * * *", "*/60 * * * *", "*/5 * * * 1", "*/5 1 * * *", "0 3 1 * *", "0 3 * 1 *", "0 3 * * 1",
    "0 */2 * * *", "5 * * * *", "0,30 * * * *", "1-5 * * * *", "60 3 * * *", "0 24 * * *", "0 3 * * * ", " 0 3 * * *", "0 3 * *",
    "*/5 * * * *\n", "0 3 * * *\n",
])
def test_any_other_cron_form_is_rejected_instead_of_being_converted_wrongly(cron):
    with pytest.raises(plan.PlanError, match="cron"):
        plan.cron_to_on_calendar(cron)


def timers() -> dict:
    return {name: text for name, text in plan.scheduled_units().items() if name.endswith(".timer")}


def services() -> dict:
    return {name: text for name, text in plan.scheduled_units().items() if name.endswith(".service")}


def test_generated_schedule_matches_scheduled_commands_item_by_item():
    assert set(timers()) == {f"rac-job-{c}.timer" for c in cli.SCHEDULED_COMMANDS}
    assert set(services()) == {f"rac-job-{c}.service" for c in cli.SCHEDULED_COMMANDS}
    for command, cron in cli.SCHEDULED_COMMANDS.items():
        timer, service = timers()[f"rac-job-{command}.timer"], services()[f"rac-job-{command}.service"]
        assert f"OnCalendar={plan.cron_to_on_calendar(cron)}\n" in timer, command
        assert f"Unit=rac-job-{command}.service\n" in timer, command
        assert exec_tokens(service)[-4:] == ["flask", "--app", "wsgi", command], command
        assert f"cron {cron}" in timer.splitlines()[1], "說明列寫出原本的 cron 運算式，方便對照"


def test_no_manual_command_is_scheduled():
    generated = " ".join(plan.scheduled_units())
    for manual in cli.MANUAL_COMMANDS:
        assert f"rac-job-{manual}" not in generated, manual
    for forbidden in ("create-admin", "replay-deletions", "seed-scripts"):
        assert forbidden not in "\n".join(plan.scheduled_units().values())
    with pytest.raises(plan.PlanError, match="人工類"):
        plan.scheduled_units({"create-admin": "0 3 * * *"})


def test_an_unsupported_expression_in_the_list_fails_generation():
    with pytest.raises(plan.PlanError, match="cron"):
        plan.scheduled_units({"check-alerts": "*/5 * * * *", "weekly-thing": "0 3 * * 1"})


def test_a_command_added_to_the_list_gets_a_schedule_without_changing_the_plan(monkeypatch):
    monkeypatch.setattr(plan, "SCHEDULED_COMMANDS", {**cli.SCHEDULED_COMMANDS, "new-nightly-job": "15 4 * * *"})
    units = plan.scheduled_units()
    assert "OnCalendar=*-*-* 04:15:00 Asia/Taipei\n" in units["rac-job-new-nightly-job.timer"]
    assert exec_tokens(units["rac-job-new-nightly-job.service"])[-4:] == ["flask", "--app", "wsgi", "new-nightly-job"]
    assert {f.path for f in plan.host_files(target(), PLAIN)} >= {"/etc/systemd/system/rac-job-new-nightly-job.timer"}


def test_timers_use_taipei_time_and_catch_up_after_the_host_was_off():
    for name, text in timers().items():
        assert re.search(r"^OnCalendar=.* Asia/Taipei$", text, re.M), name
        assert "Persistent=true" in text and "WantedBy=timers.target" in text, name
    daily = [text for text in timers().values() if re.search(r"OnCalendar=\*-\*-\* \d\d:\d\d:00", text)]
    assert len(daily) == 3 and all("OnCalendar=*-*-* 03:" in text for text in daily), "每日工作在台灣時間凌晨 3 點"


def test_scheduled_jobs_run_once_with_the_same_image_environment_and_hardening_as_the_pools():
    pool_tokens = exec_tokens(plan.pool_unit(plan.POOLS[0]))
    for name, text in services().items():
        tokens = exec_tokens(text)
        assert "Type=oneshot" in text and "Restart" not in text and "[Install]" not in text, name
        assert "TimeoutStartSec=900" in text and "ExecStartPre=/usr/local/sbin/rac-render-env-app" in text, name
        assert not any(t.startswith("--publish") for t in tokens), "排程工作不對外開任何連接埠"
        assert option_values(tokens, "--env-file") == option_values(pool_tokens, "--env-file"), name
        for shared in ("--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pull=never", "rac-app:${RAC_IMAGE_TAG}"):
            assert shared in tokens, (name, shared)
        assert option_values(tokens, "--name")[0].startswith("rac-job-")


def test_job_names_pass_the_name_guard_and_never_contain_production():
    with pytest.raises(plan.PlanError, match="production"):
        plan.job_unit_name("production-cleanup")
    with pytest.raises(plan.PlanError):
        plan.job_unit_name("Bad_Name")
    assert all("production" not in name for name in plan.scheduled_units())


# ---- 1.6 部署、回復與拆除 ----------------------------------------------------------------------------------

TAG = "abc1234"
OTHER_TAG = "def5678"
DEPLOY_PLAN_IDS = (
    "deploy-find-host", "snapshot-db", "wait-snapshot-db", "archive-source", "upload-source", "build-image", "wait-build-image",
    "check-image", "wait-check-image", "migrate-db", "wait-migrate-db", "check-migration-head", "wait-check-migration-head",
    "seed-scripts", "wait-seed-scripts", "switch-release", "wait-switch-release", "check-pools", "wait-check-pools",
)


DB_INIT_IDS = (
    "deploy-find-db-endpoint", "upload-ops-bootstrap-db", "upload-ops-probe-db", "bootstrap-db", "wait-bootstrap-db", "probe-db", "wait-probe-db",
)


def deploy_steps(tag=TAG, **overrides):
    """例行部署的整份計畫（不含只在第一次部署執行的 db-init）。"""
    return plan.deploy_plan(target(**overrides), tag)


def all_deploy_steps(tag=TAG, **overrides):
    """含 db-init 的完整目錄（執行器的 catalog 與第一次部署分組執行用）。"""
    return plan.deploy_plan(target(**overrides), tag, groups=plan.DEPLOY_GROUPS)


def rollback_steps(tag=OTHER_TAG, **overrides):
    return plan.rollback_plan(target(**overrides), tag)


def teardown_steps(final_snapshot=True, **overrides):
    return plan.teardown_plan(target(**overrides), final_snapshot=final_snapshot, today=date(2026, 12, 31))


def script_lines(step):
    """SSM 命令要在主機上執行的腳本（一行一個元素）。"""
    return json.loads(step.input_json)["Parameters"]["commands"]


def script_text(step):
    return "\n".join(script_lines(step))


def is_host_command(step):
    return step.argv[1:3] == ("ssm", "send-command")


def host_scripts(steps):
    return {step.id: script_text(step) for step in steps if is_host_command(step)}


def positions(steps, *ids):
    ordered = [s.id for s in steps]
    return [ordered.index(step_id) for step_id in ids]


def assert_in_order(steps, *ids):
    found = positions(steps, *ids)
    assert found == sorted(found) and len(set(found)) == len(ids), dict(zip(ids, found))


def whole_plan_text(steps):
    """每個步驟的說明、指令、JSON 範本與預期輸出（含主機腳本）串成一段文字，用來掃描禁止出現的字樣。"""
    parts = []
    for step in steps:
        parts.extend(all_strings(step))
        parts.extend(" ".join(argv) for argv in commands(step))
    return " ".join(parts)


def deleting_steps(steps):
    verbs = ("delete", "terminate", "release", "remove", "detach", "rb")
    return [s for s in steps if s.argv and s.argv[0] == "aws" and any(s.argv[2].startswith(verb) for verb in verbs)]


def test_the_deploy_plan_has_the_expected_steps_in_the_five_phases_snapshot_build_migrate_restart_verify():
    steps = deploy_steps()
    assert tuple(s.id for s in steps) == DEPLOY_PLAN_IDS
    phases = [s.group for s in steps]
    assert phases == sorted(phases, key=plan.DEPLOY_GROUPS.index)
    assert set(phases) == set(plan.ROUTINE_DEPLOY_GROUPS) and "db-init" not in phases, "例行部署不含只在第一次部署執行的資料庫初始化"
    assert_in_order(steps, "snapshot-db", "wait-snapshot-db", "archive-source", "upload-source", "build-image", "migrate-db",
                    "wait-migrate-db", "seed-scripts", "switch-release", "check-pools")
    assert steps[-1].id == "wait-check-pools", "驗證在最後"


def test_the_snapshot_is_available_before_any_migration_and_the_migration_succeeds_before_any_restart():
    steps = deploy_steps()
    assert_in_order(steps, "snapshot-db", "wait-snapshot-db", "migrate-db")
    assert_in_order(steps, "wait-migrate-db", "wait-check-migration-head", "wait-seed-scripts", "switch-release")
    assert_in_order(steps, "wait-switch-release", "check-pools")
    restarting = [s.id for s in steps if is_host_command(s) and "systemctl restart" in script_text(s)]
    assert restarting == ["switch-release"], "重啟服務的只有這一步，且在遷移與種子成功之後"


def test_deploy_groups_can_be_selected_and_the_host_lookup_comes_along_when_needed():
    only_snapshot = plan.deploy_plan(target(), TAG, groups=("snapshot",))
    assert [s.id for s in only_snapshot] == ["snapshot-db", "wait-snapshot-db"]
    migrate_only = plan.deploy_plan(target(), TAG, groups=("migrate",))
    assert migrate_only[0].id == "deploy-find-host" and {s.group for s in migrate_only} == {"prepare", "migrate"}
    with pytest.raises(plan.PlanError, match="bogus"):
        plan.deploy_plan(target(), TAG, groups=("bogus",))


@pytest.mark.parametrize("tag", ["", "latest", "ABC1234", "abc123", "abc1234\n", "abc1234;rm", "abc 1234", "g" * 8, "a" * 41, None, 1234567])
def test_the_image_tag_must_be_a_commit_short_code(tag):
    with pytest.raises(plan.PlanError, match="提交短碼"):
        plan.deploy_plan(target(), tag)
    with pytest.raises(plan.PlanError, match="提交短碼"):
        plan.rollback_plan(target(), tag)


def test_every_on_host_command_goes_through_ssm_run_command_with_ascii_only_and_no_secret():
    sends = [s for s in all_deploy_steps() + rollback_steps() if is_host_command(s)]
    assert len(sends) == 9 + 2, "部署 9 個（建置、映像檢查、資料庫初始化、探測、遷移、版本檢查、種子、切換、池檢查）與回復 2 個（切換、池檢查）"
    for step in sends:
        body = json.loads(step.input_json)
        assert body["DocumentName"] == "AWS-RunShellScript" and body["InstanceIds"] == ["{{instance-id}}"], step.id
        assert "file://{{input_file}}" in step.argv and "--cli-input-json" in step.argv
        assert step.capture == f"{step.id}-command-id"
        assert all(ord(char) < 128 for char in step.input_json), f"{step.id}：傳給 AWS 的 JSON 只能用 ASCII（Windows 以系統地區編碼讀 file://）"
        text = script_text(step)
        assert "--with-decryption" not in text and not any(var in text for var in plan.SECRET_VARS), step.id
        assert not plan.SECRET_REFERENCE.search(step.input_json), step.id


def test_the_host_lookup_needs_the_name_both_tags_and_the_running_state_and_exactly_one_match():
    for step in (by_id(deploy_steps())["deploy-find-host"], by_id(rollback_steps())["rollback-find-host"]):
        find = " ".join(step.argv)
        for required in ("Name=tag:Name,Values=rac-demo-staging-host", "Name=tag:Project,Values=rac-demo",
                         "Name=tag:Environment,Values=staging", "Name=instance-state-name,Values=running"):
            assert required in find, (step.id, required)
        assert step.capture == "instance-id" and step.group == "prepare"
        assert "length(Reservations[].Instances[])" in " ".join(step.verify[0].argv) and step.verify[0].expect == "1"


def test_each_on_host_command_is_followed_by_a_wait_step_that_fails_fast_on_a_failed_command():
    steps = all_deploy_steps() + rollback_steps()
    sends = [s for s in steps if is_host_command(s)]
    waits = {s.id: s for s in steps if s.poll and s.poll.argv[1:3] == ("ssm", "get-command-invocation")}
    assert set(waits) == {f"wait-{send.id}" for send in sends}
    for send in sends:
        wait = waits[f"wait-{send.id}"]
        assert "{{" + send.capture + "}}" in wait.poll.argv and "{{instance-id}}" in wait.poll.argv
        assert wait.poll.expect == "Success" and wait.argv == ()
        assert {"Failed", "Cancelled", "TimedOut"} <= set(wait.poll_fail)
        assert 0 < wait.interval_seconds < wait.timeout_seconds
        execution = int(json.loads(send.input_json)["Parameters"]["executionTimeout"][0])
        assert wait.timeout_seconds > execution, f"{send.id}：等待要比命令本身的逾時久，否則命令還在跑就先放棄"
    assert waits["wait-build-image"].timeout_seconds >= 25 * 60, "建置映像要給足時間"


def test_the_snapshot_is_named_per_tag_tagged_skipped_when_it_exists_and_waited_for():
    steps = by_id(deploy_steps())
    snapshot = steps["snapshot-db"]
    name = f"rac-demo-staging-db-pre-{TAG}"
    assert plan.snapshot_name(target(), TAG) == name
    assert argument_after(snapshot.argv, "--db-snapshot-identifier") == name
    assert argument_after(snapshot.argv, "--db-instance-identifier") == "rac-demo-staging-db"
    assert {("Project", "rac-demo"), ("Environment", "staging")} <= set(tags_in(json.loads(argument_after(snapshot.argv, "--tags"))))
    assert snapshot.exists_argv and snapshot.exists_nonempty and snapshot.resource in plan.DEPLOY_RESOURCES
    wait = steps["wait-snapshot-db"]
    assert wait.poll.expect == "available" and "failed" in wait.poll_fail and name in wait.poll.argv
    assert wait.timeout_seconds >= 20 * 60
    assert plan.snapshot_name(target(), OTHER_TAG) != name


def test_the_source_comes_from_the_commit_not_the_working_tree_and_is_uploaded_encrypted_to_the_transfer_bucket():
    steps = by_id(deploy_steps())
    archive = steps["archive-source"]
    assert archive.argv[:3] == ("git", "archive", "--format=tar.gz") and archive.argv[-1] == TAG, "從提交取出，不是工作目錄：未提交的檔案與 .env 不會進來"
    output = option_values(list(archive.argv), "--output")[0]
    assert output == f"{plan.LOCAL_ARCHIVE_DIR}/source-{TAG}.tar.gz" and plan.LOCAL_ARCHIVE_DIR.startswith("build/")
    assert "build/" in (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8-sig").splitlines()
    upload = steps["upload-source"]
    bucket = plan.names(target()).bucket
    assert upload.argv[:3] == ("aws", "s3", "cp") and upload.argv[3] == output
    assert upload.argv[4] == f"s3://{bucket}/source/{TAG}.tar.gz" and argument_after(upload.argv, "--sse") == "AES256"
    assert upload.verify[0].expect == "AES256"


def test_the_image_is_built_on_the_host_from_that_object_tagged_with_the_commit_and_older_images_are_kept():
    text = script_text(by_id(deploy_steps())["build-image"])
    bucket = plan.names(target()).bucket
    assert f"s3://{bucket}/source/{TAG}.tar.gz" in text and f"--tag rac-app:{TAG}" in text and f"--tag rac-app-base:{TAG}" in text
    assert "--region ap-southeast-2" in text, "主機上的 aws 指令也明確指定雪梨區域"
    assert "mktemp -d /var/tmp" in text and "trap" in text, "建置用的暫存目錄在持久磁碟（/tmp 是記憶體）並在結束時清除"
    for forbidden in ("rmi", "prune", "docker rm ", "--no-cache"):
        assert forbidden not in text, f"前一版映像要保留供回復：{forbidden}"


def test_the_image_check_expects_a_non_root_user_and_no_env_or_git_directory_in_the_image():
    steps = by_id(deploy_steps())
    assert steps["wait-check-image"].verify[0].expect == f"{plan.IMAGE_USER_ID} clean"
    text = script_text(steps["check-image"])
    assert "--pull=never" in text and f"rac-app:{TAG}" in text and "-e /app/.env" in text and "-e /app/.git" in text
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8-sig")
    assert f"--uid {plan.IMAGE_USER_ID}" in dockerfile and f"WORKDIR {plan.APP_DIR}\n" in dockerfile and "USER app" in dockerfile


# ---- 憑證方案（design D7，2026-11-17 修訂）：衍生映像層，連線字串只有 Encrypt=yes ------------------------------------

ALLOWED_DERIVED_INSTRUCTIONS = ("FROM", "USER", "COPY", "RUN")


def test_the_derived_dockerfile_only_adds_the_rds_trust_anchors_on_top_of_the_base_image_and_returns_to_the_non_root_user():
    text = plan.derived_dockerfile(TAG)
    assert text.isascii() and text.endswith("\n") and "\r" not in text
    lines = text.splitlines()
    assert lines == [
        f"FROM rac-app-base:{TAG}",
        "USER root",
        "COPY rds-ca-bundle.crt /usr/local/share/ca-certificates/rds-ca-bundle.crt",
        "RUN update-ca-certificates",
        "USER app",
    ]
    assert {line.split()[0] for line in lines} <= set(ALLOWED_DERIVED_INSTRUCTIONS)
    assert lines[-1] == "USER app", "最後一個 USER 必須回到映像原本的非 root 使用者"
    assert "/usr/local/share/ca-certificates/" in text and lines[2].split()[1].endswith(".crt"), "update-ca-certificates 只收 .crt"


def test_the_derived_dockerfile_has_no_secret_network_fetch_or_trust_relaxation():
    text = plan.derived_dockerfile(TAG)
    for forbidden in ("ARG", "ENV", "ADD", "curl", "wget", "http", "apt", "pip", "TrustServerCertificate", "ServerCertificate", "SSL_CERT_FILE",
                      "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "--privileged", "PASSWORD", "SECRET", "rm "):
        assert forbidden not in text, forbidden
    with pytest.raises(plan.PlanError, match="映像標籤"):
        plan.derived_dockerfile("Bad Tag")
    with pytest.raises(plan.PlanError, match="映像標籤"):
        plan.derived_dockerfile("abc1234\nUSER root")


def test_the_shared_dockerfile_is_not_touched_by_the_certificate_scheme():
    shared = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8-sig")
    for forbidden in ("rds", "update-ca-certificates", "ca-certificates.crt", "ServerCertificate"):
        assert forbidden not in shared, f"共用的 Dockerfile 不為單一展示環境而改（design D7）：{forbidden}"


def test_the_build_step_builds_the_base_image_first_then_the_derived_layer_from_the_host_bundle():
    text = script_text(by_id(deploy_steps())["build-image"])
    base_build = text.index(f"docker build --tag rac-app-base:{TAG} ")
    derived_build = text.index(f"docker build --tag rac-app:{TAG} ")
    assert base_build < text.index("install -m 0644 /etc/rac/rds-ca-bundle.pem") < text.index("<<'RAC_DERIVED_DOCKERFILE'") < derived_build
    assert text.count("docker build") == 2
    assert "rds-ca-bundle.crt" in text and plan.derived_dockerfile(TAG).strip() in text
    assert text.isascii()
    assert "TrustServerCertificate" not in text and "SSL_CERT_FILE" not in text


def fake_docker_script(body: str) -> list[str]:
    """在 bash 內定義一個假的 docker 函式（`/usr/bin/docker` 之後會被代換成它）。`LOG` 與 `BUNDLE` 是呼叫端設定的 shell 變數。"""
    return ["dockerfake() {", *("  " + line for line in body.splitlines()), "}"]


def test_the_build_script_runs_both_builds_in_order_with_a_context_holding_only_the_dockerfile_and_the_bundle():
    bash = usable_bash()
    if bash is None:
        pytest.skip("這台機器沒有可用的 bash，無法執行腳本（不算通過）")
    fake = fake_docker_script(
        'echo "ARGS: $*" >> "$LOG"\n'
        'if [ "$1" = build ]; then\n'
        '  for last; do :; done\n'
        '  echo "CTX: $(ls -1 "$last" | tr "\\n" " ")" >> "$LOG"\n'
        '  if [ -f "$last/Dockerfile" ]; then echo DOCKERFILE_BEGIN >> "$LOG"; cat "$last/Dockerfile" >> "$LOG"; echo DOCKERFILE_END >> "$LOG"; fi\n'
        '  if [ -f "$last/rds-ca-bundle.crt" ] && cmp -s "$last/rds-ca-bundle.crt" "$BUNDLE"; then echo BUNDLE_SAME >> "$LOG"; fi\n'
        "fi")
    lines = script_lines(by_id(deploy_steps())["build-image"])
    body = "\n".join(lines)
    body = body.replace("/usr/bin/docker", "dockerfake").replace("/var/tmp", "$d").replace("/etc/rac/rds-ca-bundle.pem", "$BUNDLE")
    script = "\n".join([
        'd="$(mktemp -d)"',
        'LOG="$d/log"; BUNDLE="$d/bundle.pem"; : > "$LOG"',
        'printf "%s\\n" "-----BEGIN CERTIFICATE-----" "AAA" "-----END CERTIFICATE-----" "-----BEGIN CERTIFICATE-----" "BBB" "-----END CERTIFICATE-----" > "$BUNDLE"',
        "aws() { :; }", "tar() { :; }",
        *fake,
        body,
        'cat "$LOG"', 'rm -rf "$d"',
    ])
    result = subprocess.run([bash], input=script.encode("utf-8"), capture_output=True, timeout=60)
    out = result.stdout.decode("utf-8", "replace")
    assert result.returncode == 0, (out, result.stderr.decode("utf-8", "replace"))
    builds = [line for line in out.splitlines() if line.startswith("ARGS: build")]
    assert len(builds) == 2 and f"--tag rac-app-base:{TAG}" in builds[0] and f"--tag rac-app:{TAG}" in builds[1]
    assert out.count("CTX: ") == 2
    ctx = [line for line in out.splitlines() if line.startswith("CTX: ")]
    assert ctx[1].split() == ["CTX:", "Dockerfile", "rds-ca-bundle.crt"], "衍生映像的建置內容只有 Dockerfile 與 CA 套件（不含原始碼與其他檔案）"
    dockerfile = out[out.index("DOCKERFILE_BEGIN\n") + len("DOCKERFILE_BEGIN\n"):out.index("DOCKERFILE_END")].replace("\r\n", "\n")
    assert dockerfile == plan.derived_dockerfile(TAG), "heredoc 寫出的 Dockerfile 就是計畫模組產生的那一份"
    assert "BUNDLE_SAME" in out, "放進建置內容的就是主機上的套件，沒有被改動"


@pytest.mark.parametrize(("base", "derived", "passes"), [
    (140, 142, True),           # 套件 2 張（測試用的假套件）：基底 140 + 2 = 142
    (140, 140, False),          # 憑證沒有加進去
    (140, 141, False),          # 只加了一張
    (140, 144, False),          # 多了不明的憑證
    (140, 139, False),          # 少了原有的憑證（移除了信任錨）
])
def test_the_image_check_script_requires_the_certificate_store_to_grow_by_exactly_the_bundle(base, derived, passes):
    bash = usable_bash()
    if bash is None:
        pytest.skip("這台機器沒有可用的 bash，無法執行腳本（不算通過）")
    fake = fake_docker_script(
        'case "$*" in\n'
        '  *"--entrypoint id"*) echo 10001;;\n'
        '  *"--entrypoint sh"*) echo clean;;\n'
        '  *"--entrypoint grep rac-app-base:"*) echo "$BASE";;\n'
        '  *"--entrypoint grep rac-app:"*) echo "$DERIVED";;\n'
        "esac")
    body = "\n".join(plan._image_check_lines(TAG)).replace("/usr/bin/docker", "dockerfake").replace("/etc/rac/rds-ca-bundle.pem", "$BUNDLE")
    script = "\n".join([
        'd="$(mktemp -d)"', 'BUNDLE="$d/bundle.pem"', f"BASE={base}", f"DERIVED={derived}",
        'printf "%s\\n" "-----BEGIN CERTIFICATE-----" "AAA" "-----END CERTIFICATE-----" "-----BEGIN CERTIFICATE-----" "BBB" "-----END CERTIFICATE-----" > "$BUNDLE"',
        *fake,
        body,
        'rm -rf "$d"',
    ])
    result = subprocess.run([bash], input=script.encode("utf-8"), capture_output=True, timeout=60)
    out, err = result.stdout.decode("utf-8", "replace").strip(), result.stderr.decode("utf-8", "replace")
    if passes:
        assert result.returncode == 0 and out == f"{plan.IMAGE_USER_ID} clean", (out, err)
    else:
        assert result.returncode != 0 and "10001 clean" not in out and "ca store mismatch" in err, (out, err)
        assert f"base={base}" in err and f"image={derived}" in err and "bundle=2" in err


def test_the_image_check_compares_the_system_certificate_store_of_the_base_and_the_derived_image_with_the_host_bundle():
    text = script_text(by_id(deploy_steps())["check-image"])
    assert text.count(plan.CA_STORE_FILE) == 2 and plan.CA_STORE_FILE == "/etc/ssl/certs/ca-certificates.crt"
    assert f"rac-app-base:{TAG}" in text and f"rac-app:{TAG}" in text and plan.RDS_CA_BUNDLE_PATH in text
    assert "--pull=never" in text and text.isascii()
    assert "$((base_certs + bundle_certs))" in text, "憑證庫只新增套件的憑證：基底張數加套件張數"


def test_the_derived_image_scheme_adds_no_certificate_trust_through_environment_variables_or_flags():
    for steps in (all_deploy_steps(), full_plan()):
        text = whole_plan_text(steps)
        for forbidden in ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "TrustServerCertificate=yes", "ServerCertificate="):
            assert forbidden not in text, forbidden


# 資料庫初始化與探測（task 5.3 現場執行後回寫）：`db-init` 組，只在第一次部署以 `--group db-init` 執行

OPS_STEPS = (("bootstrap-db", plan.BOOTSTRAP_SCRIPT, "bootstrap"), ("probe-db", plan.PROBE_SCRIPT, "app"))


def test_the_db_init_group_sits_between_the_build_and_the_migration_and_is_not_part_of_a_routine_deploy():
    steps = all_deploy_steps()
    assert [s.id for s in steps if s.group == "db-init"] == list(DB_INIT_IDS)
    assert_in_order(steps, "wait-check-image", *DB_INIT_IDS, "migrate-db")
    assert plan.FIRST_DEPLOY_GROUPS == ("db-init",)
    assert plan.ROUTINE_DEPLOY_GROUPS == tuple(g for g in plan.DEPLOY_GROUPS if g != "db-init")
    assert not set(DB_INIT_IDS) & {s.id for s in deploy_steps()}, "例行部署不用主要使用者重做資料庫初始化"
    assert [s.id for s in plan.deploy_plan(target(), TAG, groups=plan.ROUTINE_DEPLOY_GROUPS)] == [s.id for s in deploy_steps()]


def test_selecting_only_the_db_init_group_brings_the_host_lookup_along_and_nothing_else():
    selected = plan.deploy_plan(target(), TAG, groups=("db-init",))
    assert [s.id for s in selected] == ["deploy-find-host", *DB_INIT_IDS]
    assert_placeholders_resolve(selected)


def test_the_database_endpoint_is_looked_up_read_only_and_only_the_bootstrap_script_uses_it():
    steps = by_id(all_deploy_steps())
    find = steps["deploy-find-db-endpoint"]
    assert find.argv[1:3] == ("rds", "describe-db-instances") and "rac-demo-staging-db" in find.argv
    assert find.capture == plan.DB_ENDPOINT_CAPTURE == "db-endpoint" and not find.input_json and not find.resource
    assert "--host {{db-endpoint}}" in script_text(steps["bootstrap-db"])
    assert "{{db-endpoint}}" not in script_text(steps["probe-db"]) and "--host" not in script_text(steps["probe-db"])


@pytest.mark.parametrize(("step_id", "name", "unit"), OPS_STEPS)
def test_the_ops_scripts_are_uploaded_encrypted_and_the_host_checks_their_sha256_before_using_them(step_id, name, unit):
    steps = by_id(all_deploy_steps())
    upload = steps[f"upload-ops-{name.removeprefix('aws_demo_').removesuffix('.py').replace('_', '-')}"]
    bucket = f"rac-demo-staging-{ACCOUNT}-transfer"
    assert upload.argv[1:3] == ("s3", "cp") and f"scripts/{name}" in upload.argv and f"s3://{bucket}/ops/{name}" in upload.argv
    assert upload.argv[upload.argv.index("--sse") + 1] == "AES256"
    assert upload.verify[0].expect == "AES256" and f"ops/{name}" in " ".join(upload.verify[0].argv)
    digest = hashlib.sha256((PROJECT_ROOT / "scripts" / name).read_bytes()).hexdigest()
    assert plan.ops_script_sha256(name) == digest
    lines = script_lines(steps[step_id])
    check = lines.index(f'echo "{digest}  $work/{name}" | sha256sum -c --status -')
    download = next(i for i, line in enumerate(lines) if line.startswith(f'aws s3 cp "s3://{bucket}/ops/{name}"'))
    render = lines.index(plan.render_env_script_path(unit))
    run = next(i for i, line in enumerate(lines) if "docker run" in line)
    assert download < check < render < run, "下載、核對雜湊都在取出機密與啟動容器之前；雜湊不符時機密根本不會被取出"


@pytest.mark.parametrize("name", ["", "aws_demo_plan.py", "../app/config.py", "scripts/aws_demo_probe_db.py", "AWS_DEMO_PROBE_DB.PY", None])
def test_only_the_two_ops_scripts_can_be_hashed(name):
    with pytest.raises(plan.PlanError, match="維運腳本"):
        plan.ops_script_sha256(name)


@pytest.mark.parametrize(("step_id", "name", "unit"), OPS_STEPS)
def test_the_ops_containers_have_the_pools_hardening_and_no_certificate_mount_or_secret_on_the_command_line(step_id, name, unit):
    steps = by_id(all_deploy_steps())
    text = script_text(steps[step_id])
    assert text.isascii() and not plan.SECRET_REFERENCE.search(steps[step_id].input_json)
    for required in ("--rm", "--pull=never", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", f"rac-app:{TAG}"):
        assert required in text, required
    assert "rac-app-base" not in text, "一次性容器用含 RDS CA 的映像，不是基底映像"
    assert text.count("--mount=") == 1 and f'--mount=type=bind,source="$work/{name}",target=/ops/{name},readonly' in text
    for forbidden in ("rds-ca-bundle", "/etc/ssl", "SSL_CERT_FILE", "TrustServerCertificate", "ServerCertificate", "--with-decryption", "--env DATABASE_URL"):
        assert forbidden not in text, forbidden
    assert steps[f"wait-{step_id}"].verify[0].expect == "docker-exit=0"
    assert int(json.loads(steps[step_id].input_json)["Parameters"]["executionTimeout"][0]) >= 300


def test_the_bootstrap_gets_the_master_user_environment_and_the_probe_only_the_application_one():
    steps = by_id(all_deploy_steps())
    bootstrap, probe = script_text(steps["bootstrap-db"]), script_text(steps["probe-db"])
    assert f"{plan.SBIN_DIR}/rac-render-env-bootstrap" in bootstrap and "--env-file=/run/rac/bootstrap.env" in bootstrap
    assert "app.env" not in bootstrap and "plain.env" not in bootstrap
    assert f"{plan.SBIN_DIR}/rac-render-env-app" in probe and "--env-file=/etc/rac/plain.env --env-file=/run/rac/app.env" in probe
    assert "bootstrap" not in probe.replace("aws_demo_bootstrap_db", ""), "探測（應用程式登入）取不到 RDS 主要使用者的密碼"
    assert "/run/rac/bootstrap.env" in next(line for line in script_lines(steps["bootstrap-db"]) if line.startswith("trap "))
    assert "/run/rac/app.env" in next(line for line in script_lines(steps["probe-db"]) if line.startswith("trap "))


def run_ops_script(tmp_path, step_id, *, tamper=False, docker_rc=0):
    """在 bash 內以假的 aws、docker 與機密取出腳本執行 db-init 的主機腳本；回傳結束代碼、標準輸出與錯誤、呼叫紀錄與殘留檔案數。"""
    bash = usable_bash()
    if bash is None or shutil.which("sha256sum") is None:
        pytest.skip("這台機器沒有可用的 bash 或 sha256sum，無法執行腳本（不算通過）")
    _, name, unit = next(item for item in OPS_STEPS if item[0] == step_id)
    body = "\n".join(script_lines(by_id(all_deploy_steps())[step_id]))
    body = (body.replace("{{db-endpoint}}", "db.example.internal").replace("/usr/bin/docker", "dockerfake").replace("/var/tmp", "$d")
            .replace(f"{plan.SBIN_DIR}/rac-render-env-{unit}", "renderfake").replace("/run/rac", "$d/run").replace("/etc/rac/plain.env", "$d/plain.env"))
    real = shlex.quote((PROJECT_ROOT / "scripts" / name).as_posix())
    script = "\n".join([
        'd="$(mktemp -d)"', 'mkdir "$d/run"', 'LOG="$d/log"; : > "$LOG"',
        f'FIXTURE="$d/fixture"; cp {real} "$FIXTURE"', 'echo "# tampered" >> "$FIXTURE"' if tamper else ":",
        f"DOCKER_RC={docker_rc}",
        'aws() { echo "AWS: $*" >> "$LOG"; cp "$FIXTURE" "$4"; }',
        f'renderfake() {{ echo "RENDER {unit}" >> "$LOG"; : > "$d/run/{unit}.env"; }}',
        'dockerfake() { echo "DOCKER: $*" >> "$LOG"; if [ "$1" = run ]; then echo container-out; echo container-err >&2; return "$DOCKER_RC"; fi; }',
        "(", body, ') > "$d/out" 2> "$d/err"',
        'echo "rc=$?"', 'echo "left_env=$(ls -A "$d/run" | wc -l)"', 'echo "left_work=$(ls -d "$d"/rac-ops.* 2>/dev/null | wc -l)"',
        'echo "---out"; cat "$d/out"', 'echo "---err"; cat "$d/err"', 'echo "---log"; cat "$LOG"', 'rm -rf "$d"',
    ])
    result = subprocess.run([bash], input=script.encode("utf-8"), capture_output=True, timeout=60)
    text = result.stdout.decode("utf-8", "replace").replace("\r\n", "\n")
    assert result.returncode == 0, (text, result.stderr.decode("utf-8", "replace"))
    head, rest = text.split("---out\n", 1)
    out, rest = rest.split("---err\n", 1)
    err, log = rest.split("---log\n", 1)
    values = dict(line.split("=", 1) for line in head.strip().splitlines())
    return {"rc": int(values["rc"]), "left_env": int(values["left_env"]), "left_work": int(values["left_work"]), "out": out, "err": err, "log": log}


@pytest.mark.parametrize(("step_id", "name", "unit"), OPS_STEPS)
def test_a_successful_ops_script_prints_only_the_exit_line_on_stdout_sends_the_container_output_to_stderr_and_cleans_up(tmp_path, step_id, name, unit):
    result = run_ops_script(tmp_path, step_id)
    assert result["rc"] == 0 and result["out"].strip() == "docker-exit=0"
    assert "container-out" in result["err"] and "container-err" in result["err"], "容器的輸出（含繁體中文的進度）在標準錯誤，不干擾固定文字的驗證"
    assert (result["left_env"], result["left_work"]) == (0, 0), "機密環境檔與暫存目錄結束時一定刪除"
    log = result["log"].splitlines()
    run = [line for line in log if line.startswith("DOCKER: run")]
    assert len(run) == 1 and f"rac-app:{TAG} python /ops/{name}" in run[0]
    assert log.index(f"RENDER {unit}") < log.index(run[0]), "環境檔在容器啟動之前取出"
    assert ("--host db.example.internal" in run[0]) == (step_id == "bootstrap-db")


@pytest.mark.parametrize("step_id", ["bootstrap-db", "probe-db"])
def test_an_ops_script_whose_hash_does_not_match_stops_before_fetching_any_secret_or_starting_a_container(tmp_path, step_id):
    result = run_ops_script(tmp_path, step_id, tamper=True)
    assert result["rc"] != 0 and "docker-exit" not in result["out"]
    assert "RENDER" not in result["log"] and "DOCKER: run" not in result["log"]
    assert (result["left_env"], result["left_work"]) == (0, 0)


@pytest.mark.parametrize("step_id", ["bootstrap-db", "probe-db"])
def test_a_failing_container_fails_the_host_command_with_the_same_exit_code_and_still_cleans_up(tmp_path, step_id):
    result = run_ops_script(tmp_path, step_id, docker_rc=3)
    assert result["rc"] == 3 and result["out"].strip() == "docker-exit=3", "非零結束讓 SSM 狀態為 Failed，等待步驟立即停止"
    assert (result["left_env"], result["left_work"]) == (0, 0)


def test_migration_runs_in_a_one_off_container_with_the_pools_hardening_and_never_downgrades():
    steps = by_id(deploy_steps())
    migrate = script_lines(steps["migrate-db"])
    assert migrate[2] == "/usr/local/sbin/rac-render-env-app" and f"RAC_IMAGE_TAG={TAG}" in migrate
    run = next(line for line in migrate if line.startswith("/usr/bin/docker run"))
    tokens = shlex.split(run)
    pool_tokens = exec_tokens(plan.pool_unit(plan.POOLS[0]))
    assert tokens[-3:] == ["alembic", "upgrade", "head"] and "rac-app:${RAC_IMAGE_TAG}" in tokens
    assert option_values(tokens, "--env-file") == option_values(pool_tokens, "--env-file")
    for shared in ("--rm", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pull=never"):
        assert shared in tokens, shared
    assert not any(t.startswith("--publish") for t in tokens)
    assert "downgrade" not in whole_plan_text(deploy_steps()).lower()
    head = script_text(steps["check-migration-head"])
    assert steps["wait-check-migration-head"].verify[0].expect == "1"
    assert "alembic current" in head and "(head)" in head
    seed = script_text(steps["seed-scripts"])
    assert "flask --app wsgi seed-scripts" in seed and "alembic" not in seed


def test_switching_the_release_checks_the_image_first_keeps_the_previous_tag_and_restarts_only_the_two_pools():
    lines = script_lines(by_id(deploy_steps())["switch-release"])
    text = "\n".join(lines)
    assert lines[0] == "set -eu"
    inspect_at = next(i for i, line in enumerate(lines) if f"image inspect rac-app:{TAG}" in line)
    write_at = next(i for i, line in enumerate(lines) if "release.env.new" in line and ">" in line)
    assert inspect_at < write_at, "映像不存在時，在改任何檔案之前就要失敗"
    assert "cp -p release.env release.previous.env" in text and f"grep -qx 'RAC_IMAGE_TAG={TAG}' release.env" in text
    assert "mv -f release.env.new release.env" in text, "原子替換"
    assert lines[-1] == "systemctl restart rac-cs.service rac-rpg.service"
    assert "rac-caddy" not in text and "rac-job" not in text and "alembic" not in text
    assert plan.PREVIOUS_RELEASE_ENV_PATH == "/etc/rac/release.previous.env" and plan.RELEASE_ENV_PATH == "/etc/rac/release.env"


def test_the_pool_check_probes_both_loopback_ports_and_expects_the_new_image_in_both_containers():
    steps = by_id(deploy_steps())
    text = script_text(steps["check-pools"])
    for pool in plan.POOLS:
        assert str(pool.port) in text and f"inspect --format '{{{{.Config.Image}}}}' {pool.container}" in text
    assert "http://127.0.0.1:$port/readyz" in text and "curl -fsS" in text and "exit 1" in text
    assert steps["wait-check-pools"].verify[0].expect == f"rac-app:{TAG} rac-app:{TAG}"
    assert "0.0.0.0" not in text


def test_the_new_plans_use_only_aws_and_one_git_command_and_always_name_the_sydney_region():
    steps = deploy_steps() + rollback_steps() + teardown_steps()
    for step in steps:
        for argv in commands(step):
            if argv[0] == "git":
                assert step.id == "archive-source"
                continue
            assert argv[0] == "aws" and "--region=ap-southeast-2" in argv, (step.id, argv[:3])
    services = {argv[1] for step in steps for argv in commands(step) if argv[0] == "aws"}
    assert services <= {"sts", "budgets", "sns", "ec2", "iam", "s3", "s3api", "ssm", "rds", "cloudwatch", "resourcegroupstaggingapi"}


def test_step_ids_are_unique_across_the_build_deploy_rollback_and_teardown_plans():
    ids = [s.id for s in (*full_plan(), *all_deploy_steps(), *rollback_steps(), *teardown_steps())]
    assert len(ids) == len(set(ids))


def test_every_placeholder_in_the_new_plans_is_captured_by_an_earlier_step():
    for steps in (deploy_steps(), all_deploy_steps(), rollback_steps(), teardown_steps(True), teardown_steps(False), teardown_steps(None)):
        assert_placeholders_resolve(steps)


def test_nothing_in_the_new_plans_mentions_production_or_a_secret_value():
    for steps in (all_deploy_steps(), rollback_steps(), teardown_steps()):
        text = whole_plan_text(steps)
        assert "production" not in text.lower()
        assert not re.search(r"AKIA[0-9A-Z]{16}|--master-user-password|--password|--value\b|--secret-string|--with-decryption", text)


@pytest.mark.parametrize("make", [all_deploy_steps, rollback_steps])
def test_the_host_scripts_pass_a_bash_syntax_check(make):
    bash = usable_bash()
    if bash is None:
        pytest.skip("這台機器沒有可用的 bash，無法做語法檢查（不算通過）")
    scripts = host_scripts(make())
    assert scripts
    for step_id, text in scripts.items():
        result = subprocess.run([bash, "-n"], input=(text + "\n").encode("ascii"), capture_output=True, timeout=60)
        assert result.returncode == 0, (step_id, result.stderr.decode("utf-8", "replace"))


# 回復


def test_rollback_only_switches_the_image_tag_and_restarts_the_pools_without_touching_the_database():
    steps = rollback_steps()
    assert [s.id for s in steps] == ["rollback-find-host", "rollback-switch-release", "wait-rollback-switch-release",
                                      "rollback-check-pools", "wait-rollback-check-pools"]
    assert [s.group for s in steps] == ["prepare", "restart", "restart", "verify", "verify"]
    text = whole_plan_text(steps).lower()
    for forbidden in ("alembic", "downgrade", "migrate", "seed-scripts", "snapshot", "restore", "docker build", "s3 cp", "delete", "rds"):
        assert forbidden not in text, forbidden
    assert script_lines(by_id(steps)["rollback-switch-release"]) == plan._switch_release_lines(OTHER_TAG)
    assert by_id(steps)["wait-rollback-check-pools"].verify[0].expect == f"rac-app:{OTHER_TAG} rac-app:{OTHER_TAG}"


def test_rollback_to_an_image_that_is_not_on_the_host_fails_before_any_file_changes():
    lines = script_lines(by_id(rollback_steps())["rollback-switch-release"])
    assert lines[0] == "set -eu" and lines[1].endswith(f"image inspect rac-app:{OTHER_TAG} >/dev/null")
    assert not any("release.env" in line for line in lines[:2])


def test_deploy_and_rollback_switch_the_release_with_the_same_script():
    assert script_lines(by_id(deploy_steps(OTHER_TAG))["switch-release"]) == script_lines(by_id(rollback_steps(OTHER_TAG))["rollback-switch-release"])


# 拆除


def test_teardown_requires_an_explicit_choice_about_the_final_snapshot_and_has_no_default():
    parameter = inspect.signature(plan.teardown_plan).parameters["final_snapshot"]
    assert parameter.default is inspect.Parameter.empty and parameter.kind is inspect.Parameter.KEYWORD_ONLY
    with pytest.raises(TypeError):
        plan.teardown_plan(target())


def test_without_a_choice_the_rds_deletion_step_prints_no_command_and_no_other_step_deletes_the_database():
    steps = teardown_steps(None)
    rds = by_id(steps)["td-delete-rds"]
    assert rds.blocked and rds.argv == () and rds.exists_argv is None and "最終快照" in rds.title
    assert "delete-db-instance" not in whole_plan_text(steps)


def test_choosing_to_keep_the_final_snapshot_names_it_by_date_and_never_skips_it():
    steps = teardown_steps(True)
    rds = by_id(steps)["td-delete-rds"]
    assert argument_after(rds.argv, "--final-db-snapshot-identifier") == "rac-demo-staging-db-final-20261231"
    assert "--skip-final-snapshot" not in rds.argv and argument_after(rds.argv, "--db-instance-identifier") == "rac-demo-staging-db"
    assert not rds.blocked


def test_choosing_not_to_keep_it_skips_the_final_snapshot_and_names_none():
    argv = by_id(teardown_steps(False))["td-delete-rds"].argv
    assert "--skip-final-snapshot" in argv and "--final-db-snapshot-identifier" not in argv


@pytest.mark.parametrize("choice", [0, 1, "yes", "", "True", [], 2.0])
def test_an_ambiguous_final_snapshot_choice_is_rejected(choice):
    with pytest.raises(plan.PlanError, match="final_snapshot"):
        plan.teardown_plan(target(), final_snapshot=choice)


def test_keeping_a_final_snapshot_needs_a_date_so_the_name_is_unique():
    with pytest.raises(plan.PlanError, match="日期"):
        plan.teardown_plan(target(), final_snapshot=True)
    assert plan.teardown_plan(target(), final_snapshot=False)           # 不保留時不需要日期


def test_every_deleting_step_looks_the_resource_up_by_tag_first_and_is_skipped_when_nothing_matches():
    steps = teardown_steps(False)
    deleting = deleting_steps(steps)
    # 告警、主機、Elastic IP、RDS、子網路群組、兩個安全群組、五個 IAM 步驟、儲存貯體、每個機密參數、SNS 主題、預算
    assert len(deleting) == 3 + 1 + 1 + 2 + 5 + 1 + len(plan.parameter_names(target())) + 2
    for step in deleting:
        assert step.run_if_found and step.exists_nonempty and step.exists_argv, step.id
        find = " ".join(step.exists_argv)
        by_filter = "Name=tag:Project,Values=rac-demo" in find and "Name=tag:Environment,Values=staging" in find
        by_query = "Key=='Project' && Value=='rac-demo'" in find and "Key=='Environment' && Value=='staging'" in find
        assert by_filter or by_query, (step.id, find)
        assert not any(verb in find for verb in ("delete", "terminate", "release", "remove", "detach", "create")), f"{step.id}：查詢必須是唯讀的"
    assert {s.id for s in steps if s.run_if_found} == {s.id for s in deleting}, "只有刪除步驟走「查到才執行」"


def test_the_ownership_query_needs_both_tags_to_match():
    assert plan._owned("Tags") == "Tags[?Key=='Environment' && Value=='staging'] && Tags[?Key=='Project' && Value=='rac-demo'].Value"
    assert plan._owned("Role.Tags").startswith("Role.Tags[?Key=='Environment'") and " && Role.Tags[?Key=='Project'" in plan._owned("Role.Tags")


def test_teardown_deletes_only_names_from_the_plan_and_never_uses_wildcards_or_broad_commands():
    steps = teardown_steps(True)
    n = plan.names(target())
    text = " ".join(command for step in steps for command in (" ".join(argv) for argv in commands(step)))      # 只掃指令，不掃步驟 id 與說明
    for field_name, value in n.__dict__.items():
        if field_name not in ("ssm_prefix", "eip", "host"):          # 主機與 Elastic IP 以標籤查詢，不靠名稱
            assert value in text, f"拆除計畫沒有提到建立過的資源 {field_name}={value}"
    for var in plan.parameter_names(target()):
        assert plan.parameter_name(var) in text
    for step in deleting_steps(steps):
        assert "*" not in " ".join(step.argv), step.id
    for forbidden in ("delete-vpc", "delete-subnet ", "delete-snapshot", "delete-db-snapshot", "delete-db-cluster", "delete-parameters",
                      "delete-bucket", "delete-key-pair", "delete-launch-template", "deregister", "delete-security-group-rules"):
        assert forbidden not in text, forbidden
    assert next(s for s in steps if s.argv[1:3] == ("s3", "rb")).argv[3] == f"s3://{n.bucket}"


def test_teardown_covers_every_resource_type_the_build_plan_creates():
    built = {s.resource for s in full_plan() if s.resource}
    operations = {(s.argv[1], s.argv[2]) for s in deleting_steps(teardown_steps(False))}
    expected = {
        "budget": ("budgets", "delete-budget"), "sns-topic": ("sns", "delete-topic"), "security-group": ("ec2", "delete-security-group"),
        "iam-role": ("iam", "delete-role"), "instance-profile": ("iam", "delete-instance-profile"), "s3-bucket": ("s3", "rb"),
        "ssm-parameter": ("ssm", "delete-parameter"), "db-subnet-group": ("rds", "delete-db-subnet-group"),
        "rds-instance": ("rds", "delete-db-instance"), "ec2-instance": ("ec2", "terminate-instances"),
        "elastic-ip": ("ec2", "release-address"), "cloudwatch-alarm": ("cloudwatch", "delete-alarms"),
    }
    assert set(expected) == built == set(plan.ALLOWED_RESOURCES)
    assert set(expected.values()) <= operations


def test_teardown_order_respects_dependencies_and_removes_the_budget_last():
    steps = teardown_steps(True)
    assert_in_order(steps, "td-delete-ec2-alarm", "td-terminate-ec2", "td-wait-ec2-terminated", "td-release-eip", "td-delete-rds",
                    "td-wait-rds-deleted", "td-delete-db-subnet-group", "td-delete-db-sg", "td-delete-host-sg")
    assert_in_order(steps, "td-wait-ec2-terminated", "td-delete-host-sg")
    assert_in_order(steps, "td-wait-rds-deleted", "td-delete-db-sg")
    assert_in_order(steps, "td-remove-role-from-profile", "td-delete-instance-profile", "td-delete-role-policy", "td-detach-ssm-policy", "td-delete-role")
    assert_in_order(steps, "td-delete-ec2-alarm", "td-delete-sns-topic", "td-delete-budget", "td-list-remaining-snapshots", "td-wait-tagged-resources-gone")
    assert [s.group for s in steps] == sorted((s.group for s in steps), key=plan.TEARDOWN_GROUPS.index)
    last_delete = max(positions(steps, *(s.id for s in deleting_steps(steps))))
    assert positions(steps, "td-delete-budget")[0] == last_delete, "預算最後刪，拆除期間費用告警仍有效"
    parameter_ids = [s.id for s in steps if s.id.startswith("td-delete-parameter-")]
    assert parameter_ids and len(parameter_ids) == len(plan.parameter_names(target()))


def test_the_final_listing_shows_what_is_left_and_the_final_check_ignores_snapshots():
    steps = by_id(teardown_steps(True))
    listing = steps["td-list-remaining-snapshots"]
    assert listing == plan.snapshot_listing_step(target()), "拆除最後的列出與執行器事先列出的是同一個步驟"
    assert listing.capture == "remaining-snapshots" and not listing.run_if_found and "--snapshot-type" in listing.argv
    wait = steps["td-wait-tagged-resources-gone"]
    assert wait.poll.expect == "0" and "':snapshot:'" in " ".join(wait.poll.argv) and wait.timeout_seconds and wait.interval_seconds
    assert len(wait.verify) >= 6 and all(check.expect == "0" for check in wait.verify)


def test_teardown_waits_for_the_host_and_the_database_to_disappear_with_a_timeout():
    steps = by_id(teardown_steps(True))
    for step_id, minimum in (("td-wait-ec2-terminated", 300), ("td-wait-rds-deleted", 25 * 60)):
        step = steps[step_id]
        assert step.argv == () and step.poll.expect == "0" and step.timeout_seconds >= minimum and 0 < step.interval_seconds < step.timeout_seconds


# ---- 1.8 拆除時的部署前快照（design D14）------------------------------------------------------------------

PRE_A = "rac-demo-staging-db-pre-abc1234"
PRE_B = "rac-demo-staging-db-pre-def5678"
PRE_C = "rac-demo-staging-db-pre-0123456789ab"
FINAL_OLD = "rac-demo-staging-db-final-20261201"
FINAL_NEW = "rac-demo-staging-db-final-20261231"


def snapshot_teardown(delete_snapshots, final_snapshot=True):
    return plan.teardown_plan(target(), final_snapshot=final_snapshot, today=date(2026, 12, 31), delete_snapshots=delete_snapshots)


def snapshot_delete_steps(steps):
    return [s for s in steps if s.id.startswith("td-delete-snapshot-")]


def test_until_the_exceptions_are_chosen_the_snapshot_step_is_a_placeholder_with_no_command():
    for final_snapshot in (True, False, None):
        steps = by_id(snapshot_teardown(None, final_snapshot))
        placeholder = steps["td-delete-snapshots"]
        assert placeholder.blocked and placeholder.argv == () and placeholder.exists_argv is None and placeholder.group == "snapshots"
        assert "例外" in placeholder.title and "最終快照不會被刪" in placeholder.title
        assert not snapshot_delete_steps(steps.values())
        assert "delete-db-snapshot" not in whole_plan_text(list(steps.values()))
    assert [s.id for s in teardown_steps(False) if s.group == "snapshots"] == ["td-delete-snapshots"], "預設（沒給名單）就是尚未決定"


def test_each_chosen_snapshot_gets_a_tag_guarded_delete_step_after_the_database_is_gone():
    steps = snapshot_teardown((PRE_A, PRE_B))
    assert "td-delete-snapshots" not in {s.id for s in steps}
    deleting = snapshot_delete_steps(steps)
    assert [s.id for s in deleting] == [f"td-delete-snapshot-{PRE_A}", f"td-delete-snapshot-{PRE_B}"]
    for step, name in zip(deleting, (PRE_A, PRE_B)):
        assert step.group == "snapshots" and not step.blocked
        assert step.argv == ("aws", "rds", "delete-db-snapshot", "--db-snapshot-identifier", name, "--region=ap-southeast-2")
        assert step.run_if_found and step.exists_nonempty
        find = " ".join(step.exists_argv)
        assert f"arn:aws:rds:ap-southeast-2:{ACCOUNT}:snapshot:{name}" in find and "list-tags-for-resource" in find
        assert "Key=='Project' && Value=='rac-demo'" in find and "Key=='Environment' && Value=='staging'" in find
    assert_in_order(steps, "td-wait-rds-deleted", "td-delete-db-subnet-group", f"td-delete-snapshot-{PRE_A}", f"td-delete-snapshot-{PRE_B}",
                    "td-delete-db-sg", "td-list-remaining-snapshots")
    assert [s.group for s in steps] == sorted((s.group for s in steps), key=plan.TEARDOWN_GROUPS.index)


@pytest.mark.parametrize("final_snapshot", [True, False, None])
def test_the_pre_deploy_snapshots_can_be_deleted_whichever_final_snapshot_choice_was_made(final_snapshot):
    steps = snapshot_teardown((PRE_A,), final_snapshot)
    assert [s.id for s in snapshot_delete_steps(steps)] == [f"td-delete-snapshot-{PRE_A}"]
    assert_placeholders_resolve(steps)


def test_an_empty_choice_means_nothing_to_delete_and_adds_no_placeholder():
    steps = snapshot_teardown(())
    assert not [s for s in steps if s.group == "snapshots"]
    assert "delete-db-snapshot" not in whole_plan_text(steps)


@pytest.mark.parametrize("name", [
    FINAL_NEW, FINAL_OLD,                                   # 最終快照一律不刪（本次保留的、先前拆除留下的）
    "rac-demo-staging-other-db-pre-abc1234", "other-db-pre-abc1234", "rac-demo-staging-db-pre-", "rac-demo-staging-db-pre-ABC1234",
    "rac-demo-staging-db-pre-abc123", "rac-demo-staging-db-pre-abc1234\n", "rac-demo-staging-db-pre-abc1234 ; rm", "rac-demo-staging-db",
    "", "*", "rac-demo-staging-db-pre-abc1234,rac-demo-staging-db-pre-def5678", "automated:rac-demo-staging-db-2026-12-31",
])
def test_only_this_databases_pre_deploy_snapshots_are_accepted_and_the_final_snapshot_never(name):
    with pytest.raises(plan.PlanError, match="部署前快照"):
        snapshot_teardown((PRE_A, name))
    assert not plan.is_predeploy_snapshot(target(), name)
    assert plan.is_predeploy_snapshot(target(), PRE_A)


@pytest.mark.parametrize("bad", ["rac-demo-staging-db-pre-abc1234", {PRE_A}, 5, b"x", object()])
def test_the_snapshot_choice_must_be_none_or_a_tuple_or_list_of_names(bad):
    with pytest.raises(plan.PlanError, match="delete_snapshots"):
        snapshot_teardown(bad)
    assert snapshot_teardown([PRE_A])             # list 可以


def test_a_snapshot_cannot_be_listed_twice_and_non_strings_are_rejected():
    with pytest.raises(plan.PlanError, match="重複"):
        snapshot_teardown((PRE_A, PRE_A))
    with pytest.raises(plan.PlanError, match="部署前快照"):
        snapshot_teardown((PRE_A, 123))


def test_the_delete_list_is_the_listed_pre_deploy_snapshots_minus_the_exceptions_and_never_a_final_snapshot():
    listed = (PRE_A, FINAL_OLD, PRE_B, PRE_C, FINAL_NEW, "rac-demo-staging-db-pre-abc1234", "someone-elses-snapshot")
    assert plan.snapshots_to_delete(target(), listed) == (PRE_A, PRE_B, PRE_C), "沒有例外：部署前快照全刪，最終快照與別人的快照不在內，順序不變、不重複"
    assert plan.snapshots_to_delete(target(), listed, keep=(PRE_B,)) == (PRE_A, PRE_C)
    assert plan.snapshots_to_delete(target(), listed, keep=(PRE_A, PRE_B, PRE_C)) == ()
    assert plan.snapshots_to_delete(target(), listed, keep=(FINAL_OLD,)) == (PRE_A, PRE_B, PRE_C), "指定保留最終快照無妨，它本來就不會被刪"
    assert plan.snapshots_to_delete(target(), (), ()) == ()
    assert plan.snapshots_to_delete(target(), [PRE_A, PRE_B], [PRE_A]) == (PRE_B,)


def test_an_exception_that_is_not_in_the_listing_is_an_error_instead_of_deleting_everything():
    with pytest.raises(plan.PlanError, match="不在列出的清單內"):
        plan.snapshots_to_delete(target(), (PRE_A, PRE_B), keep=("rac-demo-staging-db-pre-aaaaaaa",))
    with pytest.raises(plan.PlanError, match="不在列出的清單內"):
        plan.snapshots_to_delete(target(), (), keep=(PRE_A,))


def test_what_the_executor_derives_always_passes_the_plan_validation():
    listed = (PRE_A, FINAL_OLD, PRE_B, "automated:rac-demo-staging-db-2026-12-31")
    for keep in ((), (PRE_A,), (PRE_B,), (PRE_A, PRE_B)):
        names_to_delete = plan.snapshots_to_delete(target(), listed, keep)
        steps = snapshot_teardown(names_to_delete)
        assert {s.id for s in snapshot_delete_steps(steps)} == {f"td-delete-snapshot-{name}" for name in names_to_delete}
        assert not set(keep) & {s.argv[4] for s in snapshot_delete_steps(steps)}


def test_the_listing_step_is_read_only_and_covers_both_kinds_of_manual_snapshot():
    step = plan.snapshot_listing_step(target())
    assert step.id == "td-list-remaining-snapshots" and step.capture == "remaining-snapshots" and not step.run_if_found and not step.blocked
    assert step.argv[:3] == ("aws", "rds", "describe-db-snapshots") and "--snapshot-type" in step.argv and "manual" in step.argv
    assert "--region=ap-southeast-2" in step.argv
    query = argument_after(step.argv, "--query")
    assert "starts_with(DBSnapshotIdentifier, 'rac-demo-staging-db-')" in query
    assert not any(verb in " ".join(step.argv) for verb in ("delete", "create", "copy", "modify"))


def test_every_snapshot_deleting_command_is_aws_in_sydney_without_wildcards_and_the_ids_stay_unique():
    steps = snapshot_teardown((PRE_A, PRE_B, PRE_C))
    for step in snapshot_delete_steps(steps):
        for argv in commands(step):
            assert argv[0] == "aws" and "--region=ap-southeast-2" in argv and "*" not in " ".join(argv)
    ids = [s.id for s in (*full_plan(), *deploy_steps(), *rollback_steps(), *steps)]
    assert len(ids) == len(set(ids))
    assert "production" not in whole_plan_text(steps).lower()


def test_deleting_snapshots_does_not_touch_the_other_teardown_guards():
    base = {s.id: s for s in teardown_steps(True)}
    with_snapshots = {s.id: s for s in snapshot_teardown((PRE_A,))}
    assert set(with_snapshots) - set(base) == {f"td-delete-snapshot-{PRE_A}"}
    assert set(base) - set(with_snapshots) == {"td-delete-snapshots"}
    for step_id in set(base) & set(with_snapshots):
        assert base[step_id] == with_snapshots[step_id], step_id
    rds = with_snapshots["td-delete-rds"]
    assert argument_after(rds.argv, "--final-db-snapshot-identifier") == FINAL_NEW, "保留最終快照的選擇不受影響"


# ---- 4.5 主機初始化（host-init）：4.4 在主機上現場撰寫並驗證過，這裡是回寫後的計畫 -----------------------------------------


def host_init_steps(*, plain=PLAIN, accept_cost=True, **overrides):
    return plan.build_plan(target(**overrides), accept_cost=accept_cost, groups=("host-init",), plain=plain)


def host_init_sends(**kwargs):
    return {s.id: s for s in host_init_steps(**kwargs) if is_host_command(s)}


def put_lines(step):
    """放置檔案那一步的每個 `put 權限 路徑 sha256 base64`。"""
    return [line.split(" ") for line in script_lines(step) if line.startswith("put ")]


def test_host_init_is_the_last_group_and_looks_up_the_host_then_has_six_ssm_steps_each_followed_by_its_wait_step_in_a_fixed_order():
    assert plan.GROUPS[-1] == "host-init"
    steps = host_init_steps()
    expected = ["host-init-find-host", *(name for step_id in HOST_INIT_IDS for name in (step_id, f"wait-{step_id}"))]
    assert [s.id for s in steps] == expected and {s.group for s in steps} == {"host-init"}
    assert [s.id for s in steps if is_host_command(s)] == list(HOST_INIT_IDS)


def test_host_init_finds_the_host_by_name_and_tags_with_its_own_capture_so_it_never_needs_the_billing_steps():
    steps = host_init_steps(accept_cost=False)
    find = steps[0]
    assert find.id == "host-init-find-host" and find.capture == "host-instance-id" == plan.HOST_INSTANCE_CAPTURE
    text = " ".join(find.argv)
    for required in ("Name=tag:Name,Values=rac-demo-staging-host", "Name=tag:Project,Values=rac-demo",
                     "Name=tag:Environment,Values=staging", "Name=instance-state-name,Values=running"):
        assert required in text
    assert "length(Reservations[].Instances[])" in " ".join(find.verify[0].argv) and find.verify[0].expect == "1", "必須剛好一台"
    assert not find.resource and not find.billing and not find.input_json and not find.exists_argv, "純查詢：單步執行時可以安全重跑"
    assert "{{instance-id}}" not in "".join(" ".join(all_strings(s)) for s in steps), "不依賴建立主機的步驟（那一步需要 --accept-cost）"
    assert_placeholders_resolve(steps)


def test_host_init_needs_no_cost_acceptance_and_runs_after_the_database_url_exists_so_the_secrets_check_can_succeed():
    assert not any(s.blocked or s.billing for s in host_init_steps(accept_cost=False))
    assert_in_order(full_plan(), "wait-ssm-online", "put-parameter-DATABASE_URL", "create-ec2-status-alarm", "host-init-find-host", "host-init-system", "host-init-secrets")


def test_without_the_general_settings_only_the_file_placing_step_is_blocked_and_keeps_its_capture():
    steps = by_id(host_init_steps(plain=None))
    blocked = [s for s in steps.values() if s.blocked]
    assert [s.id for s in blocked] == ["host-init-files"]
    step = blocked[0]
    assert step.argv == () and step.input_json is None and step.verify == () and "--vars-file" in step.title
    assert step.capture == "host-init-files-command-id"
    assert "{{host-init-files-command-id}}" in steps["wait-host-init-files"].poll.argv, "之後的等待步驟仍有擷取來源"
    assert_placeholders_resolve(full_plan(plain=None))


def test_a_general_settings_file_with_a_secret_or_an_unknown_name_is_rejected_without_echoing_the_value():
    with pytest.raises(plan.PlanError) as error:
        host_init_steps(plain={**PLAIN, "GEMINI_API_KEY": SECRET_SENTINEL})
    assert "GEMINI_API_KEY" in str(error.value) and SECRET_SENTINEL not in str(error.value)
    with pytest.raises(plan.PlanError, match="NOT_A_REAL_SETTING"):
        host_init_steps(plain={**PLAIN, "NOT_A_REAL_SETTING": "x"})
    with pytest.raises(plan.PlanError, match="MAIL_SMTP_HOST"):
        host_init_steps(plain={k: v for k, v in PLAIN.items() if k != "MAIL_SMTP_HOST"})
    host_init_steps(plain=None)        # 沒有一般設定時只是擋下放置檔案的那一步，其餘照常


def test_every_host_init_script_goes_through_ssm_with_ascii_only_and_no_secret_value_or_name():
    sends = host_init_sends()
    assert set(sends) == set(HOST_INIT_IDS)
    for step in sends.values():
        body = json.loads(step.input_json)
        assert body["DocumentName"] == "AWS-RunShellScript" and body["InstanceIds"] == ["{{host-instance-id}}"], step.id
        assert "file://{{input_file}}" in step.argv and "--cli-input-json" in step.argv and step.capture == f"{step.id}-command-id"
        assert all(ord(char) < 128 for char in step.input_json), f"{step.id}：傳給 AWS 的 JSON 只能用 ASCII（Windows 以系統地區編碼讀 file://）"
        text = script_text(step)
        assert "--with-decryption" not in text and not plan.SECRET_REFERENCE.search(step.input_json), step.id
        assert not any(var in text for var in plan.SECRET_VARS), f"{step.id}：腳本不得出現機密變數名稱"
        assert plan.SSM_PREFIX not in text and "AKIA" not in text
        assert int(body["Parameters"]["executionTimeout"][0]) >= 120
        assert script_lines(step)[:2] == ["set -eu", "export HOME=/root"], f"{step.id}：任何一行失敗就停止，且 aws CLI 需要 HOME"


def test_each_host_init_wait_step_fails_fast_and_checks_one_fixed_output_line():
    steps = by_id(host_init_steps())
    expected = {
        "host-init-system": "docker=active swap=1 dirs=755,700",
        "host-init-ca": "ca-bundle=644",
        "host-init-files": "files=17",
        "host-init-units": "enabled=8",
        "host-init-secrets": "secrets=3 fs=tmpfs mode=700/600",
        "host-init-sshd": "sshd=inactive port22=0 ssm=active",
    }
    assert len(plan.host_files(target(), PLAIN)) == 17 and len(plan.unit_parameter_names(target(), "app")) == 3
    for step_id, line in expected.items():
        send, wait = steps[step_id], steps[f"wait-{step_id}"]
        assert "{{" + send.capture + "}}" in wait.poll.argv and "{{host-instance-id}}" in wait.poll.argv
        assert wait.poll.expect == "Success" and {"Failed", "Cancelled", "TimedOut"} <= set(wait.poll_fail)
        assert [c.expect for c in wait.verify] == [line], step_id
        assert "StandardOutputContent" in " ".join(wait.verify[0].argv)
        assert wait.timeout_seconds > int(json.loads(send.input_json)["Parameters"]["executionTimeout"][0]) and 0 < wait.interval_seconds < wait.timeout_seconds
        key = line.split("=")[0]
        assert f'echo "{key}=' in script_text(send), f"{step_id}：腳本最後要印出被驗證的那一行（{key}=…）"


def test_the_system_step_makes_the_directories_a_two_gigabyte_swap_file_and_docker_and_never_leaves_a_half_written_swap_file():
    text = script_text(host_init_sends()["host-init-system"])
    for path, mode in plan.HOST_DIRECTORIES:
        assert f"install -d -m {mode} -o root -g root {path}" in text
    assert plan.SWAP_MB == 2048 and "dd if=/dev/zero of=/swapfile.new bs=1M count=2048" in text
    assert text.index("chmod 600 /swapfile.new") < text.index("mkswap /swapfile.new") < text.index("mv /swapfile.new /swapfile") < text.index("swapon /swapfile\n")
    assert "if [ ! -f /swapfile ]" in text and "swapon --show=NAME --noheadings | grep -qx /swapfile" in text, "做過的不重做"
    assert "grep -q '^/swapfile ' /etc/fstab ||" in text, "fstab 只寫一次"
    assert text.index("dnf install -y -q docker") < text.index("systemctl enable --now docker")


def test_the_ca_step_downloads_only_over_https_checks_for_a_certificate_and_installs_it_where_the_connection_string_expects():
    text = script_text(host_init_sends()["host-init-ca"])
    assert "url=https://truststore.pki.rds.amazonaws.com/ap-southeast-2/ap-southeast-2-bundle.pem" in text
    assert plan.rds_ca_bundle_url(target()) == "https://truststore.pki.rds.amazonaws.com/ap-southeast-2/ap-southeast-2-bundle.pem"
    assert "--proto '=https'" in text and "--tlsv1.2" in text and "curl -fsS" in text
    assert not re.search(r"\s-k\b|--insecure|http://", text)
    assert text.index("grep -c 'BEGIN CERTIFICATE'") < text.index('[ "$count" -ge 1 ]') < text.index("openssl x509") < text.index("install -m 0644 -o root -g root"), "沒有憑證或無法解析就不放置"
    assert f'"$tmp" {plan.RDS_CA_BUNDLE_PATH}' in text, "放到連線字串的 ServerCertificate 使用的路徑"
    assert "trap 'rm -f \"$tmp\"' EXIT" in text, "暫存檔不留下"


def test_the_files_step_places_exactly_the_host_files_with_their_hashes_modes_and_contents():
    files = plan.host_files(target(), PLAIN)
    lines = put_lines(host_init_sends()["host-init-files"])
    assert plan.host_file_count() == len(files) == len(lines) == 17
    assert [(m, p) for _, m, p, *_ in lines] == [(f.mode, f.path) for f in files], "順序與權限和 host_files() 相同"
    for (_, mode, path, digest, payload), item in zip(lines, files):
        raw = base64.b64decode(payload, validate=True)
        assert raw.decode("utf-8") == item.content, path
        assert digest == hashlib.sha256(raw).hexdigest() and len(digest) == 64, path
    assert any(not f.content.isascii() for f in files), "有繁體中文內容的檔案（取出腳本的註解）：證明內容是以 base64 傳，不是直接放進 JSON"


def test_the_files_step_carries_secret_names_only_inside_the_encoded_files_and_no_secret_value():
    step = host_init_sends(with_secrets=("OPENROUTER_API_KEY",))["host-init-files"]
    assert "{{secret:" not in step.input_json
    decoded = "\n".join(base64.b64decode(line[4]).decode("utf-8") for line in put_lines(step))
    assert "OPENROUTER_API_KEY" in decoded and SECRET_SENTINEL not in decoded
    assert {line[2] for line in put_lines(step)} >= {plan.render_env_script_path("app"), plan.render_env_script_path("bootstrap")}, "兩支取出腳本都放得上去"
    assert plan.RDS_CA_BUNDLE_PATH not in decoded, "沒有任何主機檔案參照 CA 套件：它只是建置輸入（design D7）"


def test_the_files_script_stays_within_the_size_the_cloud_run_proved():
    step = host_init_sends()["host-init-files"]
    assert len(script_text(step)) < 32 * 1024, "雲端驗證過約 19 KB 的單一命令；超過 32 KB 之前要改成分批放置"


@pytest.mark.parametrize("tamper", [True, False])
def test_the_files_script_verifies_the_hash_before_installing_and_stops_on_a_mismatch(tamper):
    """在 bash 內以假的 install 執行 put()：雜湊不符時在安裝之前就失敗；雜湊相符時才會呼叫 install。"""
    bash = usable_bash()
    if bash is None:
        pytest.skip("這台機器沒有可用的 bash，無法執行腳本（不算通過）")
    content = "hello 中文\n".encode("utf-8")
    payload = base64.b64encode(content).decode("ascii")
    digest = hashlib.sha256(content).hexdigest()
    if tamper:
        digest = ("0" if digest[0] != "0" else "1") + digest[1:]
    head = plan._HOST_FILES_HEAD.replace("/var/tmp", "$d")
    report = "trap 'echo \"installed=$([ -f \"$d/mark\" ] && echo yes || echo no)\"; rm -rf \"$d\"' EXIT"
    script = "\n".join([
        'd="$(mktemp -d)"',
        'install() { echo called >> "$d/mark"; cp "$7" "$8"; }',
        head,
        report,
        f'put 0644 "$d/target" {digest} {payload}',
        'echo "files=$n"',
    ])
    result = subprocess.run([bash], input=script.encode("utf-8"), capture_output=True, timeout=60)
    out = result.stdout.decode("utf-8", "replace")
    if tamper:
        assert result.returncode != 0 and "installed=no" in out and "files=" not in out, out
    else:
        assert "installed=yes" in out, (out, result.stderr.decode("utf-8", "replace"))


def test_the_files_script_checks_the_hash_again_after_installing_and_the_mode_and_counts_the_files():
    head = plan._HOST_FILES_HEAD
    first, last = head.index("sha256sum -c - >/dev/null"), head.rindex("sha256sum -c - >/dev/null")
    assert first < head.index('install -m "$1" -o root -g root "$tmp" "$2"') < last
    assert '[ "$(stat -c %a "$2")" = "${1#0}" ]' in head
    assert script_lines(host_init_sends()["host-init-files"])[-1] == 'echo "files=$n"'


def test_unsafe_host_file_paths_and_modes_are_refused_when_building_the_script():
    item = plan.HostFile("/etc/rac/ok", "0644", "x\n")
    assert plan._host_files_lines((item,))[-2].startswith("put 0644 /etc/rac/ok ")
    for path in ("/etc/rac/a b", "/etc/rac/a;b", "etc/rac/a", "/etc/rac/$x", "/etc/rac/a\n", "/etc/rac/'a'"):
        with pytest.raises(plan.PlanError, match="路徑或權限"):
            plan._host_files_lines((plan.HostFile(path, "0644", "x\n"),))
    for mode in ("644", "0999", "0644 ", "06440", "u+x"):
        with pytest.raises(plan.PlanError, match="路徑或權限"):
            plan._host_files_lines((plan.HostFile("/etc/rac/ok", mode, "x\n"),))


def test_the_units_step_enables_every_unit_the_host_files_define_and_starts_nothing():
    text = script_text(host_init_sends()["host-init-units"])
    units = plan.host_enabled_units()
    paths = {f.path for f in plan.host_files(target(), PLAIN)}
    assert len(units) == 8 and len(set(units)) == 8
    assert all(f"{plan.UNIT_DIR}/{unit}" in paths for unit in units), "啟用的單元都是這次放置的檔案"
    assert {"rac-cs.service", "rac-rpg.service", "rac-caddy.service"} <= set(units)
    timers = {path.rsplit("/", 1)[1] for path in paths if path.endswith(".timer")}
    assert {unit for unit in units if unit.endswith(".timer")} == timers, "每個計時器都啟用"
    assert not any(unit.startswith("rac-job-") and unit.endswith(".service") for unit in units), "排程的 .service 由計時器觸發，不單獨啟用"
    assert f"systemctl enable {' '.join(units)} >/dev/null" in text
    assert text.index("systemctl daemon-reload") < text.index("systemctl enable")
    assert not re.search(r"systemctl (start|restart|reload-or-restart|try-restart)|--now", text), "啟動留給部署與排程階段"


def test_no_host_init_script_starts_a_unit_of_this_project():
    for step_id, text in host_scripts(host_init_steps()).items():
        assert not re.search(r"systemctl\s+(start|restart)\s+\S*rac-", text), step_id
        for line in text.splitlines():
            if "--now" in line:
                assert re.search(r"systemctl (enable|disable) --now (docker|sshd\.service) >/dev/null$", line), f"{step_id}：只有 docker 與 sshd 可以用 --now"


def test_the_secrets_check_renders_to_memory_prints_only_counts_and_deletes_the_file():
    text = script_text(host_init_sends()["host-init-secrets"])
    assert plan.render_env_script_path("app") in {f.path for f in plan.host_files(target(), PLAIN)}
    assert text.index("trap 'rm -f /run/rac/app.env' EXIT") < text.index(plan.render_env_script_path("app")), "渲染之前就設好刪除"
    assert "stat -f -c %T /run/rac" in text and f"{plan.RUN_DIR}/app.env" in text
    assert not re.search(r"\bcat\b|\bcut\b|\bsed\b|\bawk\b|echo[^\n]*\$value|--with-decryption|/dev/stdout", text), "不印出環境檔的內容"
    assert text.splitlines()[-1] == 'echo "secrets=$count fs=$fs mode=$mode"'
    chosen = ("OPENROUTER_API_KEY", "GOOGLE_OAUTH_CLIENT_SECRET")
    assert script_text(host_init_sends(with_secrets=chosen)["host-init-secrets"]) == text, "腳本本身不依機密數量改變；數量只在預期輸出"
    assert by_id(host_init_steps(with_secrets=chosen))["wait-host-init-secrets"].verify[0].expect == "secrets=5 fs=tmpfs mode=700/600"


def test_the_sshd_step_disables_only_sshd_and_keeps_the_ssm_agent_checked():
    text = script_text(host_init_sends()["host-init-sshd"])
    assert "systemctl disable --now sshd.service" in text and text.count("systemctl disable") == 1
    assert "if systemctl list-unit-files sshd.service" in text, "沒有 sshd 的映像也不會失敗"
    assert "amazon-ssm-agent" in text and "disable --now amazon-ssm-agent" not in text and "stop amazon-ssm-agent" not in text
    assert "grep -c ':22$'" in text


@pytest.mark.parametrize("plain", [PLAIN, None])
def test_the_host_init_scripts_pass_a_bash_syntax_check(plain):
    bash = usable_bash()
    if bash is None:
        pytest.skip("這台機器沒有可用的 bash，無法做語法檢查（不算通過）")
    scripts = host_scripts(host_init_steps(plain=plain))
    assert len(scripts) == (5 if plain is None else 6), "沒有一般設定時放置檔案的那一步沒有腳本"
    for step_id, text in scripts.items():
        result = subprocess.run([bash, "-n"], input=(text + "\n").encode("ascii"), capture_output=True, timeout=60)
        assert result.returncode == 0, (step_id, result.stderr.decode("utf-8", "replace"))


def test_a_template_with_a_missing_value_or_a_non_ascii_character_is_refused():
    with pytest.raises(plan.PlanError, match="沒有代入"):
        plan._script("echo @MISSING@")
    with pytest.raises(plan.PlanError, match="ASCII"):
        plan._script("echo 中文")
    assert plan._script("echo @A@ @A@", A="x") == ["echo x x"]


def test_the_host_init_ids_do_not_collide_with_the_other_plans_and_nothing_mentions_production():
    ids = [s.id for s in (*full_plan(), *deploy_steps(), *rollback_steps(), *teardown_steps())]
    assert len(ids) == len(set(ids))
    text = whole_plan_text(host_init_steps())
    assert "production" not in text.lower() and not re.search(r"AKIA[0-9A-Z]{16}|--master-user-password|--password|--secret-string", text)
