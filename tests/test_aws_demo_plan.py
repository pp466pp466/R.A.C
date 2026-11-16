"""aws-demo-environment 規格：展示環境的佈建計畫（純函式）——區域與名稱守門、標籤、變數歸類、資源步驟與費用護欄。

只比對 scripts/aws_demo_plan.py 產生的資料，不呼叫 AWS 指令，也不可能在測試中誤建資源。
"""
import inspect
import json
import re
from datetime import date

import pytest

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


def target(**overrides):
    values = dict(account_id=ACCOUNT, notify_emails=(EMAIL,))
    values.update(overrides)
    return plan.Target(**values)


def full_plan(accept_cost=True, **overrides):
    return plan.build_plan(target(**overrides), accept_cost=accept_cost)


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
    assert body["BackupRetentionPeriod"] == 7 and body["MultiAZ"] is False
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
        used = placeholders(before)
        assert used <= captured, f"{step.id} 用到尚未擷取的佔位符：{sorted(used - captured)}"
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
             "wait-rds-available", "get-rds-endpoint", "put-parameter-DATABASE_URL", "create-ec2-status-alarm"]
    positions = [ids.index(step_id) for step_id in order]
    assert positions == sorted(positions) and len(set(positions)) == len(order)
    assert ids.index("create-rds-instance") < ids.index("launch-ec2") < ids.index("wait-rds-available")
    assert ids.index("create-db-sg") > ids.index("create-host-sg")             # 資料庫群組的來源是主機群組
    assert ids.index("put-parameter-RDS_MASTER_PASSWORD") < ids.index("create-rds-instance")


def test_wait_steps_always_have_a_timeout_and_a_polling_interval():
    polls = [s for s in full_plan() if s.poll]
    assert {s.id for s in polls} == {"wait-instance-profile", "wait-ec2-running", "wait-ssm-online", "wait-rds-available"}
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
