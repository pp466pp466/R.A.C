"""AWS 展示環境的佈建計畫（見 openspec/changes/aws-demo-environment 的 design D1–D13 與四份規格）。

這個模組是**純函式**：只回傳步驟資料（`Step`），不呼叫任何程序、不讀環境變數、不含任何機密值。
`scripts/aws_demo.py`（執行器，之後的批次）負責印出與（在 `--execute` 時）逐步執行；測試只比對這裡產生的資料，
所以不需要 AWS CLI，也不可能在測試中誤建資源。

- 只接受雪梨區域 `ap-southeast-2`（`Target` 建立時就檢查，其他區域一律拒絕）；所有資源名稱與網域都不得含 `production`。
- 每個建立的資源都帶 `Project`、`Environment` 標籤（`teardown` 只選帶標籤的資源）。
- 環境變數的歸類（`SECRET_VARS`、`PLAIN_VARS`、`NOT_DEPLOYED`、`OBSOLETE_VARS`）**匯入自 `scripts/launch_plan.py`，不複製**，
  所以新增環境變數時兩條部署路徑同時被既有的守門測試覆蓋。
- 機密只以名稱出現：需要機密值的步驟把 JSON 寫成範本（`input_json`），值用 `{{secret:名稱}}` 代入，由執行器經暫存檔
  （權限限本人、用後刪除）交給 `file://{{input_file}}`，不進命令列、輸出與日誌。
- 建立計費資源的步驟（`billing=True`）在沒有 `accept_cost` 時不輸出指令（`blocked=True`），只留說明。
- 預設使用預設 VPC（Free plan 新帳號都有），不建立 NAT 閘道、負載平衡器或第二台主機。
- 主機檔案（Caddyfile、`rac-*` 單元、機密取出腳本、排程計時器；task 1.4、1.5）也是純函式：回傳字串（UTF-8、LF、不含 BOM），
  由執行器經 SSM 放到主機；字串內只有機密的**名稱**與 SSM 路徑，沒有任何機密值。

佔位符（`{{…}}`）：
- `{{名稱}}`：先前某個步驟用 `capture` 存下的標準輸出（例如 `{{host-sg-id}}`）；執行器在執行時代入。
- `{{input_file}}`：`input_json` 寫成的暫存檔路徑。
- `{{secret:名稱}}`：只出現在 `input_json` 內，由執行器代入機密值。
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
for _path in (str(SCRIPTS_DIR), str(PROJECT_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# 變數歸類沿用 launch_plan（design D2）：這裡只匯入，不複製。
from launch_plan import (  # noqa: E402 - 需要先把 scripts 與專案根目錄放進 sys.path
    EMAIL_PATTERN, NOT_DEPLOYED, OBSOLETE_VARS, PLAIN_VARS, REQUIRED_PLAIN, REQUIRED_SECRETS, SECRET_VARS,
)
# 排程清單的單一來源是 app.cli（launch_plan 也從這裡取）；人工類清單只用來擋下被誤放進排程的指令
from app.cli import MANUAL_COMMANDS, SCHEDULED_COMMANDS  # noqa: E402

__all__ = [
    "NOT_DEPLOYED", "OBSOLETE_VARS", "PLAIN_VARS", "REQUIRED_PLAIN", "REQUIRED_SECRETS", "SECRET_VARS",
]

# ---- 區域、命名與標籤 -------------------------------------------------------------------------

REGION = "ap-southeast-2"
PROJECT_TAG = "rac-demo"
ENV = "staging"                         # 展示環境就是 staging（APP_ENV=staging），沒有其他環境
NAME_PREFIX = f"{PROJECT_TAG}-{ENV}"    # 所有資源名稱的前綴
SSM_PREFIX = f"/{PROJECT_TAG}/{ENV}"    # 機密參數的路徑（design D6）
DEMO_DOMAIN = "rac001.duckdns.org"
DEMO_END = date(2026, 12, 31)           # 展示要撐到的日期（費用預估的終點）
FORBIDDEN_NAME_PART = "production"

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
DOMAIN_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
ACCOUNT_PATTERN = re.compile(r"^\d{12}$")

# ---- 費用護欄（design D12；牌價估算，價格會變，以 AWS 帳單頁為準）---------------------------------

COST_ITEMS = (                          # 每日美元（雪梨牌價的粗估）
    ("EC2 t3.small（Linux，隨需）", 0.63),
    ("RDS for SQL Server Express db.t3.micro（單一可用區）", 0.74),
    ("公有 IPv4（Elastic IP）", 0.12),
    ("EBS（20 GB gp3）與 RDS 儲存", 0.15),
)
COST_DISCLAIMER = "以上是牌價的粗估，價格會變，實際金額以 AWS 帳單頁為準；不含超額使用與資料傳輸。"
BUDGET_LIMIT_USD = 75                   # D12 的到 2026-12-31 預估（約 US$75），低於 US$100 的抵用金
BUDGET_THRESHOLDS_PERCENT = (60, 90)    # 兩個門檻：預估金額的 60% 與 90%（實際花費）
MAX_NOTIFY_EMAILS = 10                  # AWS Budgets 每個通知最多 10 位訂閱者

RDS_PORT = 1433
RDS_MASTER_USER = "racmaster"           # 不是 sa；密碼隨機產生、只存在 SSM（design D8）
DB_NAME = "rac_demo"                    # 專屬資料庫與登入的名稱（建立由 aws_demo_bootstrap_db.py 負責）
DB_APP_LOGIN = "rac_app"
RDS_BACKUP_RETENTION_DAYS = 7
RDS_STORAGE_GB = 20
EC2_INSTANCE_TYPE = "t3.small"
EC2_VOLUME_GB = 20
AMI_PARAMETER = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"

# 只在計畫內使用的三種機密來源（design D6、D8）
GENERATED_SECRETS = ("SECRET_KEY", "RDS_MASTER_PASSWORD", "DB_APP_PASSWORD")   # 由腳本隨機產生
DB_ONLY_SECRETS = ("RDS_MASTER_PASSWORD", "DB_APP_PASSWORD")                    # 不是應用程式的環境變數
COMPOSED_SECRETS = ("DATABASE_URL",)                                            # 由腳本用 RDS 端點與專屬登入組成
UNITS = ("app", "bootstrap")

# 規格允許建立的資源類型（aws-demo-environment 的「拓撲固定」）
ALLOWED_RESOURCES = (
    "budget", "sns-topic", "security-group", "iam-role", "instance-profile", "s3-bucket", "ssm-parameter",
    "db-subnet-group", "rds-instance", "ec2-instance", "elastic-ip", "cloudwatch-alarm",
)

GROUPS = ("preflight", "budget", "network", "iam", "storage", "secrets", "rds", "ec2", "rds-ready", "alarms")

PLACEHOLDER = re.compile(r"\{\{([a-z][a-z0-9_-]*)\}\}")
SECRET_REFERENCE = re.compile(r"\{\{secret:([A-Z][A-Z0-9_]*)\}\}")


class PlanError(ValueError):
    """計畫的輸入不合法（區域不對、名稱含 production、缺少參數、未知的變數…）。訊息只含名稱，不含機密值。"""


@dataclass(frozen=True)
class Check:
    """唯讀檢查：執行 `argv`，標準輸出去除前後空白後必須等於 `expect`（`expect` 可含 `{{擷取名稱}}`）。"""
    argv: tuple[str, ...]
    expect: str


@dataclass(frozen=True)
class Step:
    id: str
    group: str
    title: str
    argv: tuple[str, ...] = ()
    resource: str | None = None          # 這一步建立的資源類型（ALLOWED_RESOURCES 之一）；沒有建立資源的步驟為 None
    billing: bool = False                # 建立計費資源：沒有 accept_cost 時 build_plan 不輸出指令
    blocked: bool = False                # 因沒有 accept_cost 而被清空指令（只留說明）
    interactive: bool = False            # 需要人工動作（argv 可為空）；執行器等待使用者確認
    input_json: str | None = None        # 經暫存檔傳給指令的 JSON 範本（含 {{secret:名稱}}）；argv 以 file://{{input_file}} 參照
    exists_argv: tuple[str, ...] | None = None   # 先執行這個；成功（且 exists_nonempty 時輸出非空）代表資源已存在，略過本步
    exists_nonempty: bool = False
    capture: str | None = None           # 把標準輸出（去除前後空白）存成這個名稱；已存在時改存 exists_argv 的輸出
    poll: Check | None = None            # 輪詢：重複執行直到輸出等於 expect；必須同時有 timeout_seconds 與 interval_seconds
    timeout_seconds: int | None = None
    interval_seconds: int | None = None
    verify: tuple[Check, ...] = ()       # 執行後的唯讀驗證


@dataclass(frozen=True)
class Target:
    account_id: str
    region: str = REGION
    domain: str = DEMO_DOMAIN
    notify_emails: tuple[str, ...] = ()  # 預算與狀態告警的收件人，沒有預設值（budget 組與 alarms 組使用）
    budget_usd: int = BUDGET_LIMIT_USD
    with_secrets: tuple[str, ...] = ()   # 除必要機密之外要建立的機密名稱（SECRET_VARS 的子集）

    def __post_init__(self) -> None:
        validate_target(self)


@dataclass(frozen=True)
class Names:
    topic: str
    budget: str
    host_sg: str
    db_sg: str
    role: str
    instance_profile: str
    role_policy: str
    bucket: str
    subnet_group: str
    db_instance: str
    host: str
    eip: str
    alarm: str
    ssm_prefix: str


# ---- 驗證與命名 ------------------------------------------------------------------------------


def assert_name(name: str, *, what: str = "名稱") -> str:
    """資源名稱守門：小寫英數與連字號、長度合理、不得含 production。回傳原值。"""
    if FORBIDDEN_NAME_PART in name.lower():
        raise PlanError(f"{what}不得含 {FORBIDDEN_NAME_PART}：展示環境不是 production")
    if not NAME_PATTERN.fullmatch(name):          # fullmatch：`$` 會放行結尾多一個換行
        raise PlanError(f"{what}只能是小寫英數與連字號（2 至 63 字）")
    return name


def validate_target(target: Target) -> None:
    if target.region != REGION:
        raise PlanError(f"只接受雪梨區域 {REGION}（收到 {target.region or '空值'}）")
    if not ACCOUNT_PATTERN.fullmatch(target.account_id or ""):
        raise PlanError("account_id 必須是 12 位數字的 AWS 帳號編號")
    domain = target.domain or ""
    if FORBIDDEN_NAME_PART in domain.lower():
        raise PlanError(f"網域不得含 {FORBIDDEN_NAME_PART}：展示環境不是 production")
    if not DOMAIN_PATTERN.fullmatch(domain) or len(domain) > 253:   # 網域會原樣放進 Caddyfile，換行不得通過
        raise PlanError("網域必須是小寫的完整網域名稱（不含通訊協定與路徑）")
    if not isinstance(target.budget_usd, int) or isinstance(target.budget_usd, bool) or target.budget_usd < 1:
        raise PlanError("budget_usd 必須是正整數")
    for var in target.with_secrets:
        if var not in SECRET_VARS:
            raise PlanError(f"不是已知的機密變數：{var}")
        if var in COMPOSED_SECRETS:
            raise PlanError(f"{var} 由腳本組成，不能用 with_secrets 指定")
    names(target)                                # 所有資源名稱都走一遍守門


def validate_notify_emails(target: Target) -> None:
    """預算與狀態告警只通知電子郵件，收件人由呼叫者提供，沒有預設值；訊息不回顯不合法的值。"""
    if not target.notify_emails:
        raise PlanError("缺少通知信箱：預算與狀態告警的收件人沒有預設值")
    if len(set(target.notify_emails)) != len(target.notify_emails):
        raise PlanError("通知信箱有重複的地址")
    if len(target.notify_emails) > MAX_NOTIFY_EMAILS:
        raise PlanError(f"通知信箱最多 {MAX_NOTIFY_EMAILS} 個")
    for email in target.notify_emails:
        if not EMAIL_PATTERN.fullmatch(email):       # fullmatch：結尾多一個換行也要擋（`$` 會放行它）
            raise PlanError("通知信箱必須是單純的電子郵件地址（不含顯示名稱、空白與換行）")


def names(target: Target) -> Names:
    """所有資源名稱（單一來源）。每個名稱都通過 assert_name，所以沒有任何一個含 production。"""
    p = NAME_PREFIX
    values = Names(
        topic=f"{p}-alerts", budget=f"{p}-budget", host_sg=f"{p}-host", db_sg=f"{p}-db",
        role=f"{p}-host-role", instance_profile=f"{p}-host-profile", role_policy=f"{p}-read-project",
        bucket=f"{p}-{target.account_id}-transfer", subnet_group=f"{p}-db-subnets", db_instance=f"{p}-db",
        host=f"{p}-host", eip=f"{p}-eip", alarm=f"{p}-ec2-status-check", ssm_prefix=SSM_PREFIX,
    )
    for field_name, value in values.__dict__.items():
        if field_name != "ssm_prefix":
            assert_name(value, what=f"資源名稱（{field_name}）")
    return values


def sns_topic_arn(target: Target) -> str:
    return f"arn:aws:sns:{target.region}:{target.account_id}:{names(target).topic}"


def parameter_name(var: str) -> str:
    """機密參數的完整名稱：/rac-demo/staging/<名稱>。"""
    return f"{SSM_PREFIX}/{var}"


def tag_list(name: str | None = None) -> list[dict[str, str]]:
    tags = [{"Key": "Project", "Value": PROJECT_TAG}, {"Key": "Environment", "Value": ENV}]
    if name:
        tags.append({"Key": "Name", "Value": name})
    return tags


# ---- 費用預估 --------------------------------------------------------------------------------


def estimated_daily_usd() -> float:
    return round(sum(amount for _, amount in COST_ITEMS), 2)


def estimated_total_usd(start: date) -> float:
    """從 start 到 2026-12-31（含）的預估總額；start 已超過終點則為 0。"""
    days = max((DEMO_END - start).days + 1, 0)
    return round(estimated_daily_usd() * days, 2)


def cost_lines(start: date) -> list[str]:
    """執行器在建立計費資源前顯示的文字（含免責聲明）。"""
    lines = [f"  {label}：約 US${amount:.2f}／日" for label, amount in COST_ITEMS]
    days = max((DEMO_END - start).days + 1, 0)
    lines.append(f"  合計約 US${estimated_daily_usd():.2f}／日；從 {start.isoformat()} 到 {DEMO_END.isoformat()}（{days} 天）約 US${estimated_total_usd(start):.2f}")
    lines.append(f"  {COST_DISCLAIMER}")
    return lines


# ---- 機密與一般設定（task 1.2）-----------------------------------------------------------------


def secrets_in_use(target: Target) -> tuple[str, ...]:
    """應用程式需要的機密變數：必要機密加上 with_secrets（去重、保持順序）。"""
    chosen = list(REQUIRED_SECRETS)
    for var in target.with_secrets:
        if var not in chosen:
            chosen.append(var)
    return tuple(chosen)


def secret_source(var: str) -> str:
    """機密值的來源：generated（腳本隨機產生）、composed（腳本用 RDS 端點與專屬登入組成）、provided（--secrets-file 提供）。"""
    if var in COMPOSED_SECRETS:
        return "composed"
    if var in GENERATED_SECRETS:
        return "generated"
    if var in SECRET_VARS:
        return "provided"
    raise PlanError(f"不是已知的機密：{var}")


def unit_parameter_names(target: Target, unit: str) -> tuple[str, ...]:
    """某個單元的機密取出腳本要取的參數名稱（不含路徑）。應用程式單元**不含** RDS 主要使用者密碼（design D8）。"""
    if unit == "app":
        return secrets_in_use(target)
    if unit == "bootstrap":
        return DB_ONLY_SECRETS
    raise PlanError(f"不認得的單元：{unit}（只有 {'、'.join(UNITS)}）")


def parameter_names(target: Target) -> tuple[str, ...]:
    """要建立的全部 SSM 參數名稱：應用程式的機密加資料庫專用的兩個密碼。"""
    chosen = list(secrets_in_use(target))
    chosen.extend(name for name in DB_ONLY_SECRETS if name not in chosen)
    return tuple(chosen)


def validate_plain(target: Target, plain: dict[str, str]) -> None:
    """一般設定的檢查（與 launch_plan.validate 同一組歸類）；訊息只含變數名稱，不含值。"""
    leaked = sorted(name for name in plain if name in SECRET_VARS)
    if leaked:
        raise PlanError("一般設定不得含機密變數（機密只走 SSM 參數）：" + "、".join(leaked))
    obsolete = sorted(name for name in plain if name in OBSOLETE_VARS)
    if obsolete:
        raise PlanError("這些變數已淘汰，請自設定中移除：" + "、".join(obsolete))
    forbidden = sorted(name for name in plain if name in NOT_DEPLOYED)
    if forbidden:
        raise PlanError("這些變數不可部署：" + "、".join(forbidden))
    unknown = sorted(name for name in plain if name not in PLAIN_VARS)
    if unknown:
        raise PlanError("不認得的變數：" + "、".join(unknown))
    if "APP_ENV" in plain and plain["APP_ENV"] != ENV:
        raise PlanError(f"APP_ENV 必須是 {ENV}，且由計畫自動填入")
    if "BASE_URL" in plain and plain["BASE_URL"] != f"https://{target.domain}":
        raise PlanError("BASE_URL 必須是展示網域的 https 網址（不含路徑），且由計畫自動填入")
    missing = [name for name in REQUIRED_PLAIN if not plain.get(name)]
    if missing:
        raise PlanError("缺少必要的一般設定：" + "、".join(missing))


def plain_settings(target: Target, plain: dict[str, str]) -> dict[str, str]:
    """應用程式的一般設定（環境變數）：呼叫者提供的設定，加上由計畫填入的 APP_ENV 與 BASE_URL。不含任何機密。"""
    validate_plain(target, plain)
    merged = {**plain, "APP_ENV": ENV, "BASE_URL": f"https://{target.domain}"}
    return {name: merged[name] for name in sorted(merged)}


# ---- 指令組裝 --------------------------------------------------------------------------------


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _aws(target: Target, service: str, operation: str, *args: str) -> tuple[str, ...]:
    return ("aws", service, operation, *args, f"--region={target.region}")


def _text(*query: str) -> tuple[str, ...]:
    return ("--query", *query, "--output", "text")


def _ec2_tag_checks(target: Target, resource_id: str) -> tuple[Check, ...]:
    """EC2 類資源（安全群組、主機、Elastic IP）的標籤驗證：Project 與 Environment 都是預期值。"""
    def check(key: str, expected: str) -> Check:
        return Check(_aws(target, "ec2", "describe-tags", "--filters", f"Name=resource-id,Values={resource_id}",
                          *_text(f"Tags[?Key=='{key}'].Value|[0]")), expected)
    return (check("Project", PROJECT_TAG), check("Environment", ENV))


# ---- 各組步驟 --------------------------------------------------------------------------------


def _preflight_steps(target: Target) -> list[Step]:
    identity = _aws(target, "sts", "get-caller-identity", *_text("Account"))
    return [Step("check-identity", "preflight", "確認目前的 AWS 帳號就是預期的帳號（唯讀；帳號不符即停止）",
                 identity, verify=(Check(identity, target.account_id),))]


def _budget_steps(target: Target) -> list[Step]:
    validate_notify_emails(target)
    n = names(target)
    cost_types = {                       # 以實際花費（不扣抵用金與退款）對照預算，抵用金用完前就能看到用量
        "IncludeTax": True, "IncludeSubscription": True, "UseBlended": False, "IncludeRefund": False, "IncludeCredit": False,
        "IncludeUpfront": True, "IncludeRecurring": True, "IncludeOtherSubscription": True, "IncludeSupport": True,
        "IncludeDiscount": True, "UseAmortized": False,
    }
    budget = {"BudgetName": n.budget, "BudgetType": "COST", "TimeUnit": "QUARTERLY",
              "BudgetLimit": {"Amount": str(target.budget_usd), "Unit": "USD"}, "CostTypes": cost_types}
    subscribers = [{"SubscriptionType": "EMAIL", "Address": email} for email in target.notify_emails]
    notifications = [{"Notification": {"NotificationType": "ACTUAL", "ComparisonOperator": "GREATER_THAN",
                                       "Threshold": percent, "ThresholdType": "PERCENTAGE"},
                      "Subscribers": subscribers} for percent in BUDGET_THRESHOLDS_PERCENT]
    percents = "、".join(f"{p}%" for p in BUDGET_THRESHOLDS_PERCENT)
    steps = [
        Step("create-budget", "budget",
             f"建立預算 {n.budget}：US${target.budget_usd}（第四季，涵蓋到 {DEMO_END.isoformat()}），實際花費超過 {percents} 時寄信；價格會變，以 AWS 帳單頁為準",
             _aws(target, "budgets", "create-budget", "--account-id", target.account_id, "--budget", _json(budget),
                  "--notifications-with-subscribers", _json(notifications), "--resource-tags", _json(tag_list())),
             resource="budget",
             exists_argv=_aws(target, "budgets", "describe-budget", "--account-id", target.account_id, "--budget-name", n.budget),
             verify=(Check(_aws(target, "budgets", "describe-notifications-for-budget", "--account-id", target.account_id,
                                "--budget-name", n.budget, *_text("length(Notifications)")), str(len(BUDGET_THRESHOLDS_PERCENT))),)),
        Step("create-sns-topic", "budget", f"建立 SNS 主題 {n.topic}（EC2 狀態告警的通知管道；建立同名主題是冪等的）",
             _aws(target, "sns", "create-topic", "--name", n.topic, "--tags", _json(tag_list(n.topic)), *_text("TopicArn")),
             resource="sns-topic",
             verify=(Check(_aws(target, "sns", "list-tags-for-resource", "--resource-arn", sns_topic_arn(target),
                                *_text("Tags[?Key=='Project'].Value|[0]")), PROJECT_TAG),)),
    ]
    for index, email in enumerate(target.notify_emails):
        steps.append(Step(
            f"subscribe-sns-{index}", "budget", f"訂閱 SNS 主題 {n.topic}（收件人由呼叫者提供，需要收件人點確認信）",
            _aws(target, "sns", "subscribe", "--topic-arn", sns_topic_arn(target), "--protocol", "email", "--notification-endpoint", email),
            exists_argv=_aws(target, "sns", "list-subscriptions-by-topic", "--topic-arn", sns_topic_arn(target),
                             *_text(f"Subscriptions[?Endpoint=='{email}'].SubscriptionArn")),
            exists_nonempty=True))
    steps.append(Step(
        "confirm-sns-subscriptions", "budget", "人工：請每位收件人開啟 AWS 寄出的訂閱確認信並點擊確認（沒收到先看垃圾郵件匣）",
        interactive=True,
        verify=(Check(_aws(target, "sns", "list-subscriptions-by-topic", "--topic-arn", sns_topic_arn(target),
                           *_text("length(Subscriptions[?SubscriptionArn=='PendingConfirmation'])")), "0"),)))
    return steps


def _network_steps(target: Target) -> list[Step]:
    n = names(target)
    web_rules = [{"IpProtocol": "tcp", "FromPort": port, "ToPort": port, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]} for port in (80, 443)]
    db_rules = [{"IpProtocol": "tcp", "FromPort": RDS_PORT, "ToPort": RDS_PORT, "UserIdGroupPairs": [{"GroupId": "{{host-sg-id}}"}]}]

    def group_step(step_id: str, name: str, description: str, capture: str) -> Step:
        return Step(
            step_id, "network", f"建立安全群組 {name}（預設 VPC）",
            _aws(target, "ec2", "create-security-group", "--group-name", name, "--description", description, "--vpc-id", "{{vpc-id}}",
                 "--tag-specifications", _json([{"ResourceType": "security-group", "Tags": tag_list(name)}]), *_text("GroupId")),
            resource="security-group", capture=capture, exists_nonempty=True,
            exists_argv=_aws(target, "ec2", "describe-security-groups", "--filters", f"Name=group-name,Values={name}",
                             "Name=vpc-id,Values={{vpc-id}}", *_text("SecurityGroups[].GroupId")),
            verify=_ec2_tag_checks(target, "{{" + capture + "}}"))

    return [
        Step("get-default-vpc", "network", "取得預設 VPC 的編號（唯讀；沒有預設 VPC 時輸出為空，執行器應停止）",
             _aws(target, "ec2", "describe-vpcs", "--filters", "Name=is-default,Values=true", *_text("Vpcs[0].VpcId")), capture="vpc-id"),
        group_step("create-host-sg", n.host_sg, "rac demo host: web ingress only", "host-sg-id"),
        Step("authorize-host-ingress", "network", f"{n.host_sg}：只開放 80 與 443（沒有 22；管理經 SSM Session Manager）",
             _aws(target, "ec2", "authorize-security-group-ingress", "--group-id", "{{host-sg-id}}", "--ip-permissions", _json(web_rules)),
             exists_nonempty=True,
             exists_argv=_aws(target, "ec2", "describe-security-groups", "--group-ids", "{{host-sg-id}}", *_text("SecurityGroups[0].IpPermissions[].FromPort")),
             verify=(Check(_aws(target, "ec2", "describe-security-groups", "--group-ids", "{{host-sg-id}}",
                                *_text("to_string(sort_by(SecurityGroups[0].IpPermissions,&FromPort)[].[FromPort,ToPort,IpRanges[0].CidrIp,length(IpRanges)])")),
                           '[[80,80,"0.0.0.0/0",1],[443,443,"0.0.0.0/0",1]]'),)),
        group_step("create-db-sg", n.db_sg, "rac demo database: host access only", "db-sg-id"),
        Step("authorize-db-ingress", "network", f"{n.db_sg}：只允許主機安全群組連入 {RDS_PORT}（來源是安全群組，沒有任何 CIDR）",
             _aws(target, "ec2", "authorize-security-group-ingress", "--group-id", "{{db-sg-id}}", "--ip-permissions", _json(db_rules)),
             exists_nonempty=True,
             exists_argv=_aws(target, "ec2", "describe-security-groups", "--group-ids", "{{db-sg-id}}", *_text("SecurityGroups[0].IpPermissions[].FromPort")),
             verify=(Check(_aws(target, "ec2", "describe-security-groups", "--group-ids", "{{db-sg-id}}",
                                *_text("to_string(SecurityGroups[0].IpPermissions[].[FromPort,UserIdGroupPairs[0].GroupId,length(IpRanges)])")),
                           f'[[{RDS_PORT},"{{{{host-sg-id}}}}",0]]'),)),
    ]


def role_policy_document(target: Target) -> dict:
    """主機角色的內嵌政策：只讀本專案的機密參數路徑與傳檔儲存貯體（design D3、D4、D6）。"""
    n = names(target)
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "ReadProjectParameters", "Effect": "Allow", "Action": ["ssm:GetParameter", "ssm:GetParameters"],
         "Resource": f"arn:aws:ssm:{target.region}:{target.account_id}:parameter{SSM_PREFIX}/*"},
        {"Sid": "ReadTransferObjects", "Effect": "Allow", "Action": ["s3:GetObject"], "Resource": f"arn:aws:s3:::{n.bucket}/*"},
        {"Sid": "ListTransferBucket", "Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": f"arn:aws:s3:::{n.bucket}"},
    ]}


def _iam_steps(target: Target) -> list[Step]:
    n = names(target)
    trust = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    ssm_core = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
    profile_check = _aws(target, "iam", "get-instance-profile", "--instance-profile-name", n.instance_profile,
                         *_text("InstanceProfile.InstanceProfileName"))
    return [
        Step("create-host-role", "iam", f"建立主機角色 {n.role}（只有 EC2 可擔任）",
             _aws(target, "iam", "create-role", "--role-name", n.role, "--assume-role-policy-document", _json(trust),
                  "--tags", _json(tag_list(n.role))),
             resource="iam-role", exists_argv=_aws(target, "iam", "get-role", "--role-name", n.role),
             verify=(Check(_aws(target, "iam", "get-role", "--role-name", n.role, *_text("Role.Tags[?Key=='Project'].Value|[0]")), PROJECT_TAG),)),
        Step("attach-ssm-managed-policy", "iam", "附加 AmazonSSMManagedInstanceCore（讓主機能以 Session Manager 管理；重複附加是冪等的）",
             _aws(target, "iam", "attach-role-policy", "--role-name", n.role, "--policy-arn", ssm_core),
             verify=(Check(_aws(target, "iam", "list-attached-role-policies", "--role-name", n.role, *_text("AttachedPolicies[].PolicyArn")), ssm_core),)),
        Step("put-role-policy", "iam", f"內嵌政策 {n.role_policy}：只能讀 {SSM_PREFIX}/* 的參數與傳檔儲存貯體（重複寫入是覆蓋）",
             _aws(target, "iam", "put-role-policy", "--role-name", n.role, "--policy-name", n.role_policy,
                  "--policy-document", _json(role_policy_document(target))),
             verify=(Check(_aws(target, "iam", "get-role-policy", "--role-name", n.role, "--policy-name", n.role_policy,
                                *_text("PolicyDocument.Statement[0].Resource")),
                           f"arn:aws:ssm:{target.region}:{target.account_id}:parameter{SSM_PREFIX}/*"),)),
        Step("create-instance-profile", "iam", f"建立執行個體設定檔 {n.instance_profile}",
             _aws(target, "iam", "create-instance-profile", "--instance-profile-name", n.instance_profile, "--tags", _json(tag_list(n.instance_profile))),
             resource="instance-profile", exists_argv=_aws(target, "iam", "get-instance-profile", "--instance-profile-name", n.instance_profile)),
        Step("add-role-to-profile", "iam", f"把角色 {n.role} 放進設定檔",
             _aws(target, "iam", "add-role-to-instance-profile", "--instance-profile-name", n.instance_profile, "--role-name", n.role),
             exists_nonempty=True,
             exists_argv=_aws(target, "iam", "get-instance-profile", "--instance-profile-name", n.instance_profile, *_text("InstanceProfile.Roles[].RoleName")),
             verify=(Check(_aws(target, "iam", "get-instance-profile", "--instance-profile-name", n.instance_profile, *_text("InstanceProfile.Roles[0].RoleName")), n.role),)),
        Step("wait-instance-profile", "iam", "等待設定檔可被 EC2 使用（IAM 的變更需要幾秒傳播）",
             poll=Check(profile_check, n.instance_profile), timeout_seconds=120, interval_seconds=5),
    ]


def _storage_steps(target: Target) -> list[Step]:
    n = names(target)
    block_all = {"BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True}
    sse = {"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}
    return [
        Step("create-bucket", "storage", f"建立傳檔儲存貯體 {n.bucket}（只放原始碼壓縮檔；映像在主機上建置，design D4）",
             _aws(target, "s3api", "create-bucket", "--bucket", n.bucket, "--create-bucket-configuration", f"LocationConstraint={target.region}"),
             resource="s3-bucket", exists_argv=_aws(target, "s3api", "head-bucket", "--bucket", n.bucket)),
        Step("block-bucket-public-access", "storage", "封鎖儲存貯體的所有公開存取",
             _aws(target, "s3api", "put-public-access-block", "--bucket", n.bucket, "--public-access-block-configuration", _json(block_all)),
             verify=(Check(_aws(target, "s3api", "get-public-access-block", "--bucket", n.bucket,
                                *_text("to_string(PublicAccessBlockConfiguration.[BlockPublicAcls,IgnorePublicAcls,BlockPublicPolicy,RestrictPublicBuckets])")),
                           "[true,true,true,true]"),)),
        Step("encrypt-bucket", "storage", "預設加密（SSE-S3）",
             _aws(target, "s3api", "put-bucket-encryption", "--bucket", n.bucket, "--server-side-encryption-configuration", _json(sse)),
             verify=(Check(_aws(target, "s3api", "get-bucket-encryption", "--bucket", n.bucket,
                                *_text("ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault.SSEAlgorithm")), "AES256"),)),
        Step("tag-bucket", "storage", "加上專案與環境標籤",
             _aws(target, "s3api", "put-bucket-tagging", "--bucket", n.bucket, "--tagging", _json({"TagSet": tag_list(n.bucket)})),
             verify=(Check(_aws(target, "s3api", "get-bucket-tagging", "--bucket", n.bucket, *_text("TagSet[?Key=='Project'].Value|[0]")), PROJECT_TAG),)),
    ]


def _parameter_step(target: Target, var: str, group: str) -> Step:
    """建立一個 SecureString 參數。值不在計畫內：input_json 只有 {{secret:名稱}}，由執行器代入；已存在時略過（不覆寫既有的值）。"""
    name = parameter_name(var)
    body = {"Name": name, "Type": "SecureString", "Value": "{{secret:" + var + "}}", "Tags": tag_list()}
    source = {"generated": "腳本隨機產生", "composed": "腳本用 RDS 端點與專屬登入組成", "provided": "來自 --secrets-file"}[secret_source(var)]
    return Step(
        f"put-parameter-{var}", group, f"建立機密參數 {name}（SecureString；值{source}，經暫存檔傳入，不經命令列）",
        _aws(target, "ssm", "put-parameter", "--cli-input-json", "file://{{input_file}}"),
        resource="ssm-parameter", input_json=_json(body),
        exists_argv=_aws(target, "ssm", "get-parameter", "--name", name, *_text("Parameter.Name")),
        verify=(Check(_aws(target, "ssm", "describe-parameters", "--parameter-filters", f"Key=Name,Option=Equals,Values={name}",
                           *_text("Parameters[0].Type")), "SecureString"),))


def _secrets_steps(target: Target) -> list[Step]:
    """RDS 建立之前就能寫入的參數；DATABASE_URL 要等 RDS 端點，放在 rds-ready 組。"""
    return [_parameter_step(target, var, "secrets") for var in parameter_names(target) if var not in COMPOSED_SECRETS]


def _rds_steps(target: Target, *, group: str = "rds") -> list[Step]:
    n = names(target)
    instance = {
        "DBInstanceIdentifier": n.db_instance, "Engine": "sqlserver-ex", "LicenseModel": "license-included",
        "DBInstanceClass": "db.t3.micro", "AllocatedStorage": RDS_STORAGE_GB, "StorageType": "gp3", "StorageEncrypted": True,
        "MasterUsername": RDS_MASTER_USER, "MasterUserPassword": "{{secret:RDS_MASTER_PASSWORD}}",
        "DBSubnetGroupName": n.subnet_group, "VpcSecurityGroupIds": ["{{db-sg-id}}"], "PubliclyAccessible": False, "MultiAZ": False,
        "BackupRetentionPeriod": RDS_BACKUP_RETENTION_DAYS, "CopyTagsToSnapshot": True, "Tags": tag_list(n.db_instance),
    }
    return [
        Step("get-default-subnets", group, "取得預設 VPC 的子網路編號（唯讀；資料庫子網路群組需要至少兩個可用區）",
             _aws(target, "ec2", "describe-subnets", "--filters", "Name=vpc-id,Values={{vpc-id}}", *_text("to_string(Subnets[].SubnetId)")),
             capture="subnet-ids"),
        Step("create-db-subnet-group", group, f"建立資料庫子網路群組 {n.subnet_group}",
             _aws(target, "rds", "create-db-subnet-group", "--db-subnet-group-name", n.subnet_group,
                  "--db-subnet-group-description", "rac demo database subnets", "--subnet-ids", "{{subnet-ids}}",
                  "--tags", _json(tag_list(n.subnet_group))),
             resource="db-subnet-group",
             exists_argv=_aws(target, "rds", "describe-db-subnet-groups", "--db-subnet-group-name", n.subnet_group)),
        Step("create-rds-instance", group,
             f"建立 RDS {n.db_instance}（SQL Server Express db.t3.micro、{RDS_STORAGE_GB} GB gp3、加密、不公開、備份保留 {RDS_BACKUP_RETENTION_DAYS} 天、只允許主機安全群組；開始計費，建立約需 15 至 25 分鐘）",
             _aws(target, "rds", "create-db-instance", "--cli-input-json", "file://{{input_file}}"),
             resource="rds-instance", billing=True, input_json=_json(instance),
             exists_argv=_aws(target, "rds", "describe-db-instances", "--db-instance-identifier", n.db_instance)),
    ]


def _ec2_steps(target: Target) -> list[Step]:
    n = names(target)
    ebs = [{"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": EC2_VOLUME_GB, "VolumeType": "gp3", "Encrypted": True, "DeleteOnTermination": True}}]
    specs = [{"ResourceType": kind, "Tags": tag_list(n.host)} for kind in ("instance", "volume")]
    return [
        Step("get-ami", "ec2", "取得 Amazon Linux 2023（x86_64）的最新映像編號（AWS 公開參數，唯讀）",
             _aws(target, "ssm", "get-parameter", "--name", AMI_PARAMETER, *_text("Parameter.Value")), capture="ami-id"),
        Step("launch-ec2", "ec2",
             f"建立主機 {n.host}（{EC2_INSTANCE_TYPE}、IMDSv2 強制、gp3 {EC2_VOLUME_GB} GB、無金鑰對、無 22；開始計費）",
             _aws(target, "ec2", "run-instances", "--image-id", "{{ami-id}}", "--instance-type", EC2_INSTANCE_TYPE, "--count", "1",
                  "--security-group-ids", "{{host-sg-id}}", "--iam-instance-profile", f"Name={n.instance_profile}",
                  "--metadata-options", "HttpTokens=required,HttpEndpoint=enabled,HttpPutResponseHopLimit=1",
                  "--block-device-mappings", _json(ebs), "--tag-specifications", _json(specs), *_text("Instances[0].InstanceId")),
             resource="ec2-instance", billing=True, capture="instance-id", exists_nonempty=True,
             exists_argv=_aws(target, "ec2", "describe-instances",
                              "--filters", f"Name=tag:Name,Values={n.host}", "Name=instance-state-name,Values=pending,running,stopping,stopped",
                              *_text("Reservations[].Instances[].InstanceId")),
             verify=(Check(_aws(target, "ec2", "describe-instances", "--instance-ids", "{{instance-id}}",
                                *_text("to_string(Reservations[0].Instances[0].[MetadataOptions.HttpTokens,KeyName,InstanceType])")),
                           f'["required",null,"{EC2_INSTANCE_TYPE}"]'), *_ec2_tag_checks(target, "{{instance-id}}"))),
        Step("wait-ec2-running", "ec2", "等待主機進入 running",
             poll=Check(_aws(target, "ec2", "describe-instances", "--instance-ids", "{{instance-id}}", *_text("Reservations[0].Instances[0].State.Name")), "running"),
             timeout_seconds=300, interval_seconds=10),
        Step("allocate-eip", "ec2", f"配置 Elastic IP {n.eip}（固定公開位址；公有 IPv4 計費）",
             _aws(target, "ec2", "allocate-address", "--domain", "vpc",
                  "--tag-specifications", _json([{"ResourceType": "elastic-ip", "Tags": tag_list(n.eip)}]), *_text("AllocationId")),
             resource="elastic-ip", billing=True, capture="eip-allocation-id", exists_nonempty=True,
             exists_argv=_aws(target, "ec2", "describe-addresses", "--filters", f"Name=tag:Name,Values={n.eip}", *_text("Addresses[].AllocationId")),
             verify=_ec2_tag_checks(target, "{{eip-allocation-id}}")),
        Step("get-eip-address", "ec2", "取得 Elastic IP 的位址（DuckDNS 的 A 記錄要指向它）",
             _aws(target, "ec2", "describe-addresses", "--allocation-ids", "{{eip-allocation-id}}", *_text("Addresses[0].PublicIp")), capture="eip-public-ip"),
        Step("associate-eip", "ec2", "把 Elastic IP 綁到主機",
             _aws(target, "ec2", "associate-address", "--instance-id", "{{instance-id}}", "--allocation-id", "{{eip-allocation-id}}"),
             verify=(Check(_aws(target, "ec2", "describe-addresses", "--allocation-ids", "{{eip-allocation-id}}", *_text("Addresses[0].InstanceId")), "{{instance-id}}"),)),
        Step("wait-ssm-online", "ec2", "等待主機以 SSM 註冊並顯示 Online（之後的管理都經 Session Manager，沒有 SSH）",
             poll=Check(_aws(target, "ssm", "describe-instance-information", "--filters", "Key=InstanceIds,Values={{instance-id}}",
                             *_text("InstanceInformationList[0].PingStatus")), "Online"),
             timeout_seconds=600, interval_seconds=15),
    ]


def _rds_ready_steps(target: Target) -> list[Step]:
    n = names(target)
    status = _aws(target, "rds", "describe-db-instances", "--db-instance-identifier", n.db_instance, *_text("DBInstances[0].DBInstanceStatus"))
    return [
        Step("wait-rds-available", "rds-ready", f"等待 RDS {n.db_instance} 進入 available（建立約 15 至 25 分鐘；逾時即停止並回報）",
             poll=Check(status, "available"), timeout_seconds=2400, interval_seconds=30,
             verify=(Check(_aws(target, "rds", "describe-db-instances", "--db-instance-identifier", n.db_instance,
                                *_text("to_string(DBInstances[0].[PubliclyAccessible,StorageEncrypted,BackupRetentionPeriod,length(VpcSecurityGroups),VpcSecurityGroups[0].VpcSecurityGroupId])")),
                           f'[false,true,{RDS_BACKUP_RETENTION_DAYS},1,"{{{{db-sg-id}}}}"]'),)),
        Step("get-rds-endpoint", "rds-ready", "取得 RDS 的端點位址（唯讀；只在 VPC 內可連）",
             _aws(target, "rds", "describe-db-instances", "--db-instance-identifier", n.db_instance, *_text("DBInstances[0].Endpoint.Address")),
             capture="rds-endpoint"),
        _parameter_step(target, "DATABASE_URL", "rds-ready"),
    ]


def _alarm_steps(target: Target) -> list[Step]:
    validate_notify_emails(target)
    n = names(target)
    return [Step(
        "create-ec2-status-alarm", "alarms", f"建立 CloudWatch 告警 {n.alarm}：主機狀態檢查連續 2 分鐘失敗時通知 {n.topic}",
        _aws(target, "cloudwatch", "put-metric-alarm", "--alarm-name", n.alarm, "--namespace", "AWS/EC2", "--metric-name", "StatusCheckFailed",
             "--dimensions", "Name=InstanceId,Value={{instance-id}}", "--statistic", "Maximum", "--period", "60", "--evaluation-periods", "2",
             "--threshold", "1", "--comparison-operator", "GreaterThanOrEqualToThreshold", "--treat-missing-data", "notBreaching",
             "--alarm-actions", sns_topic_arn(target), "--tags", _json(tag_list(n.alarm))),
        resource="cloudwatch-alarm",
        verify=(Check(_aws(target, "cloudwatch", "describe-alarms", "--alarm-names", n.alarm, *_text("MetricAlarms[0].AlarmActions[0]")), sns_topic_arn(target)),))]


def build_plan(target: Target, *, accept_cost: bool = False, groups: tuple[str, ...] = GROUPS) -> list[Step]:
    """依固定順序產生步驟。`groups` 只取其中幾組（順序不變）。

    順序（design D1）：帳號確認 → 預算與 SNS → 網路 → 角色 → 傳檔儲存貯體 → 機密 → RDS（只建立，不等待）→
    EC2 與 Elastic IP（RDS 等待期間做）→ 等 RDS 可用並寫入 DATABASE_URL → 主機告警。
    沒有 `accept_cost` 時，計費步驟（RDS、EC2、Elastic IP）只留說明，不輸出指令。
    """
    unknown = [g for g in groups if g not in GROUPS]
    if unknown:
        raise PlanError("不認得的步驟組：" + "、".join(unknown))
    builders = {
        "preflight": _preflight_steps, "budget": _budget_steps, "network": _network_steps, "iam": _iam_steps,
        "storage": _storage_steps, "secrets": _secrets_steps, "rds": _rds_steps, "ec2": _ec2_steps,
        "rds-ready": _rds_ready_steps, "alarms": _alarm_steps,
    }
    steps: list[Step] = []
    for group in GROUPS:
        if group in groups:
            steps.extend(builders[group](target))
    if not accept_cost:
        steps = [_without_command(step) for step in steps]
    return steps


def _without_command(step: Step) -> Step:
    """沒有 accept_cost 時，計費步驟不輸出指令（也不輸出 JSON 範本與驗證）；保留 capture，使後面的佔位符仍有來源。"""
    if not step.billing:
        return step
    return replace(step, argv=(), input_json=None, exists_argv=None, verify=(), blocked=True,
                   title=f"{step.title}——需要 --accept-cost 才會輸出建立指令（執行器會先顯示預估費用）")


# ---- 主機檔案（task 1.4、1.5）：Caddyfile、rac-* 單元、機密取出腳本、排程計時器 -----------------------------
# 每個函式只回傳字串；寫到主機是執行器的事。字串內沒有機密值（機密只以名稱與 SSM 路徑出現）。

HOST_CONFIG_DIR = "/etc/rac"            # 非機密的設定：Caddyfile、一般設定、版本標籤、RDS 的 CA 套件
RUN_DIR = "/run/rac"                    # 記憶體檔案系統（tmpfs）：機密環境檔只存在這裡（design D6）
CADDY_DATA_DIR = "/var/lib/rac/caddy"   # Caddy 的憑證與帳戶資料（持久；容器重建不重新簽發）
SBIN_DIR = "/usr/local/sbin"
UNIT_DIR = "/etc/systemd/system"
CADDYFILE_PATH = f"{HOST_CONFIG_DIR}/Caddyfile"
PLAIN_ENV_PATH = f"{HOST_CONFIG_DIR}/plain.env"        # 一般設定（非機密，可放持久磁碟）
RELEASE_ENV_PATH = f"{HOST_CONFIG_DIR}/release.env"    # 目前使用的映像標籤；部署與回復只改這個檔案（task 1.6）
# design D7 候選 A：RDS 的 CA 套件放在主機、唯讀掛進容器；DATABASE_URL 的 ServerCertificate 指向容器內同一路徑
RDS_CA_BUNDLE_PATH = f"{HOST_CONFIG_DIR}/rds-ca-bundle.pem"
HOST_DIRECTORIES = ((HOST_CONFIG_DIR, "0755"), (CADDY_DATA_DIR, "0700"))   # 主機初始化要先建立的目錄（路徑、權限）

DOCKER = "/usr/bin/docker"
APP_IMAGE_REPO = "rac-app"
IMAGE_TAG_VAR = "RAC_IMAGE_TAG"
APP_IMAGE_REF = f"{APP_IMAGE_REPO}:${{{IMAGE_TAG_VAR}}}"     # rac-app:${RAC_IMAGE_TAG}，由 systemd 以 release.env 展開
IMAGE_TAG_PATTERN = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}")
CADDY_IMAGE = "caddy:2"                 # 官方映像的主版本標籤（展示環境接受浮動的次版本；要固定時改這個常數）
STREAM_PATH_PREFIX = "/api/game/"       # RPG 的 SSE 串流與其餘 RPG API（design D5）
SCHEDULE_TIMEZONE = "Asia/Taipei"
JOB_TIMEOUT_SECONDS = 900               # 排程工作單次最長 15 分鐘（check-alerts 每 5 分鐘一次，不應重疊）

_TMPFS_OPTIONS = "rw,nosuid,nodev,size=64m"
_LOG_OPTIONS = ("--log-driver=json-file", "--log-opt=max-size=10m", "--log-opt=max-file=3")   # 限制容器日誌大小，免得磁碟耗盡


@dataclass(frozen=True)
class HostFile:
    path: str
    mode: str           # 八進位權限字串，例如 "0644"
    content: str


@dataclass(frozen=True)
class Pool:
    key: str
    container: str
    port: int                    # 只發布在主機的回送位址
    gunicorn_config: str
    description: str
    stop_seconds: int            # docker stop 的等待秒數：大於該池 gunicorn 的 graceful_timeout（30／60）


POOLS = (
    Pool("cs", "rac-cs", 8081, "gunicorn.conf.py", "客服池", 45),
    Pool("rpg", "rac-rpg", 8082, "gunicorn.rpg.conf.py", "RPG 串流池", 75),
)


def _pool(key: str) -> Pool:
    return next(pool for pool in POOLS if pool.key == key)


def _text_file(lines: list[str]) -> str:
    """主機檔案一律 LF 換行、結尾有換行。"""
    return "\n".join(lines) + "\n"


def _exec_start(chunks: list[str]) -> str:
    """systemd 的 ExecStart：依用途分成幾行（以反斜線接續），沒有 shell；每一行內以空白分隔引數。"""
    return "ExecStart=" + " \\\n    ".join(chunks)


def _app_run_args(name: str, command: tuple[str, ...], *, publish: str | None = None) -> list[str]:
    """應用程式容器（兩個池與排程工作共用）：非 root（沿用映像的使用者）、唯讀根檔案系統、丟掉所有 capability、
    不得提權、限制日誌；機密環境檔來自記憶體檔案系統。`--pull=never`：標籤不存在時直接失敗，不去公開倉庫找同名映像。"""
    chunks = [f"{DOCKER} run --rm --name={name} --pull=never"]
    if publish is not None:
        chunks.append(f"--publish={publish}")
    chunks += [
        f"--env-file={PLAIN_ENV_PATH} --env-file={RUN_DIR}/app.env",
        f"--read-only --tmpfs=/dev/shm:{_TMPFS_OPTIONS} --tmpfs=/tmp:{_TMPFS_OPTIONS}",
        "--cap-drop=ALL --security-opt=no-new-privileges",
        " ".join(_LOG_OPTIONS),
        f"--mount=type=bind,source={RDS_CA_BUNDLE_PATH},target={RDS_CA_BUNDLE_PATH},readonly",
        f"{APP_IMAGE_REF} {' '.join(command)}",
    ]
    return chunks


def caddyfile(target: Target) -> str:
    """入口設定：`/api/game/*` 到 RPG 池且關閉回應緩衝（`flush_interval -1`），其餘到客服池。
    不對任何路徑壓縮（壓縮會緩衝串流）；Caddy 預設自動簽發憑證、啟用 HTTP/2、把 HTTP 導向 HTTPS，
    且預設不對上游設逾時（不會切斷長串流）、會覆寫（不信任）用戶端送來的 X-Forwarded-*，與應用程式的單層 ProxyFix 相符。
    `admin off`：入口用主機網路，關閉本機管理介面以縮小攻擊面（換設定以重啟單元為準）。"""
    rpg, cs = _pool("rpg"), _pool("cs")
    return _text_file([
        "# 由 scripts/aws_demo_plan.py 產生，不要手動修改。",
        "{",
        "    admin off",
        "}",
        "",
        f"{target.domain} {{",
        f"    @game path {STREAM_PATH_PREFIX}*",
        "    handle @game {",
        f"        reverse_proxy 127.0.0.1:{rpg.port} {{",
        "            flush_interval -1",
        "        }",
        "    }",
        "    handle {",
        f"        reverse_proxy 127.0.0.1:{cs.port}",
        "    }",
        "}",
    ])


def pool_unit(pool: Pool) -> str:
    """客服池或 RPG 池的 systemd 單元：只發布在回送位址（沒有任何對外的連接埠）、`Restart=always`。"""
    return _text_file([
        "[Unit]",
        f"Description=rac 展示環境：{pool.description}（{pool.gunicorn_config}）",
        "Requires=docker.service",
        "After=docker.service network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        "Restart=always",
        "RestartSec=5",
        f"TimeoutStopSec={pool.stop_seconds + 15}",
        f"EnvironmentFile={RELEASE_ENV_PATH}",
        f"ExecStartPre={SBIN_DIR}/rac-render-env-app",
        f"ExecStartPre=-{DOCKER} rm -f {pool.container}",
        _exec_start(_app_run_args(pool.container, ("gunicorn", "--config", pool.gunicorn_config, "wsgi:app"),
                                  publish=f"127.0.0.1:{pool.port}:8080")),
        f"ExecStop={DOCKER} stop --time={pool.stop_seconds} {pool.container}",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
    ])


def caddy_unit() -> str:
    """入口的 systemd 單元。容器內是 root，但丟掉所有 capability 只留綁定 80／443 所需的一個，並禁止提權與唯讀根檔案系統：
    非 root 加上禁止提權時，檔案 capability 不會生效，綁不了 80／443。"""
    chunks = [
        f"{DOCKER} run --rm --name=rac-caddy --network=host",
        f"--read-only --tmpfs=/config:{_TMPFS_OPTIONS}",
        "--cap-drop=ALL --cap-add=NET_BIND_SERVICE --security-opt=no-new-privileges",
        " ".join(_LOG_OPTIONS),
        f"--mount=type=bind,source={CADDYFILE_PATH},target=/etc/caddy/Caddyfile,readonly",
        f"--mount=type=bind,source={CADDY_DATA_DIR},target=/data",
        CADDY_IMAGE,
    ]
    return _text_file([
        "[Unit]",
        "Description=rac 展示環境：入口（Caddy，HTTPS 與分流）",
        "Requires=docker.service",
        "After=docker.service network-online.target rac-cs.service rac-rpg.service",
        "Wants=network-online.target rac-cs.service rac-rpg.service",
        "",
        "[Service]",
        "Type=simple",
        "Restart=always",
        "RestartSec=5",
        "TimeoutStopSec=30",
        f"ExecStartPre=-{DOCKER} rm -f rac-caddy",
        _exec_start(chunks),
        f"ExecStop={DOCKER} stop --time=15 rac-caddy",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
    ])


def release_env_file(tag: str) -> str:
    """`release.env` 的內容：目前使用的映像標籤。部署與回復只改這個檔案後重啟單元（task 1.6）。"""
    if not IMAGE_TAG_PATTERN.fullmatch(tag or ""):
        raise PlanError("映像標籤只能是小寫英數、底線、句點與連字號（最長 128 字）")
    return f"{IMAGE_TAG_VAR}={tag}\n"


def plain_env_file(target: Target, plain: dict[str, str]) -> str:
    """一般設定的環境檔（非機密）。docker 的環境檔每行一個 `名稱=值`，所以值不得含換行。"""
    settings = plain_settings(target, plain)
    for name, value in settings.items():
        if any(char in value for char in "\r\n\x00"):
            raise PlanError(f"一般設定 {name} 的值不得含換行或空字元")
    return "".join(f"{name}={value}\n" for name, value in settings.items())


_RENDER_ENV_TEMPLATE = r"""#!/bin/bash
# 由 scripts/aws_demo_plan.py 產生（單元：@UNIT@），不要手動修改；這個檔案本身不含任何機密值。
# 從 SSM 參數取出本單元需要的機密，寫到記憶體檔案系統上、只有擁有者可讀的環境檔。
# 環境檔不寫入持久磁碟、映像、日誌或命令列；這個腳本不會印出任何值。
set -euo pipefail
umask 077

REGION="@REGION@"
PREFIX="@PREFIX@"
RUN_DIR="@RUN_DIR@"
OUT="@OUT@"
NAMES=(
@NAMES@
)

install -d -m 0700 -o root -g root "$RUN_DIR"
if [ "$(stat -f -c %T "$RUN_DIR")" != "tmpfs" ]; then
  echo "rac-render-env：$RUN_DIR 不是記憶體檔案系統，中止（機密不得寫到持久磁碟）" >&2
  exit 1
fi

tmp="$(mktemp "$RUN_DIR/.@UNIT@.env.XXXXXX")"
trap 'rm -f "$tmp"' EXIT

for name in "${NAMES[@]}"; do
  if ! value="$(aws ssm get-parameter --region "$REGION" --name "$PREFIX/$name" --with-decryption --query Parameter.Value --output text)"; then
    echo "rac-render-env：取不到參數 $name" >&2
    exit 1
  fi
  if [ -z "$value" ] || [ "$value" = "None" ]; then
    echo "rac-render-env：參數 $name 是空的" >&2
    exit 1
  fi
  case "$value" in
    *$'\n'*|*$'\r'*)
      echo "rac-render-env：參數 $name 的值含換行，無法寫入環境檔" >&2
      exit 1
      ;;
  esac
  printf '%s=%s\n' "$name" "$value" >> "$tmp"
done

chmod 0600 "$tmp"
mv -f "$tmp" "$OUT"
trap - EXIT
"""


def render_env_script_path(unit: str) -> str:
    if unit not in UNITS:
        raise PlanError(f"不認得的單元：{unit}（只有 {'、'.join(UNITS)}）")
    return f"{SBIN_DIR}/rac-render-env-{unit}"


def render_env_script(target: Target, unit: str) -> str:
    """機密取出腳本：只取該單元需要的參數（應用程式單元不含 RDS 主要使用者密碼，design D8）。"""
    parameter_names_for_unit = unit_parameter_names(target, unit)         # 單元不認得時丟 PlanError
    replacements = {
        "@UNIT@": unit, "@REGION@": target.region, "@PREFIX@": SSM_PREFIX, "@RUN_DIR@": RUN_DIR,
        "@OUT@": f"{RUN_DIR}/{unit}.env", "@NAMES@": "\n".join(f"  {name}" for name in parameter_names_for_unit),
    }
    script = _RENDER_ENV_TEMPLATE
    for token, value in replacements.items():
        script = script.replace(token, value)
    return script


# ---- 排程計時器（task 1.5）----------------------------------------------------------------------------

_CRON_EVERY_N_MINUTES = re.compile(r"\*/(\d{1,2}) \* \* \* \*")
_CRON_HOURLY = "0 * * * *"
_CRON_DAILY = re.compile(r"(\d{1,2}) (\d{1,2}) \* \* \*")


def cron_to_on_calendar(cron: str) -> str:
    """把 cron 運算式轉成 systemd 的 OnCalendar（含時區）。只支援 `SCHEDULED_COMMANDS` 實際用到的三種形式：
    `*/N * * * *`（每 N 分鐘）、`0 * * * *`（每小時整點）、`M H * * *`（每日 H:M）；其他形式一律丟錯，
    使日後新增不支援的運算式時測試先失敗，而不是被悄悄轉成錯的時間。"""
    if cron == _CRON_HOURLY:
        return f"*-*-* *:00:00 {SCHEDULE_TIMEZONE}"
    every = _CRON_EVERY_N_MINUTES.fullmatch(cron)
    if every:
        minutes = int(every.group(1))
        if not 1 <= minutes <= 59:
            raise PlanError(f"不支援的 cron 運算式（分鐘間隔需介於 1 至 59）：{cron}")
        return f"*-*-* *:0/{minutes}:00 {SCHEDULE_TIMEZONE}"
    daily = _CRON_DAILY.fullmatch(cron)
    if daily:
        minute, hour = int(daily.group(1)), int(daily.group(2))
        if minute > 59 or hour > 23:
            raise PlanError(f"不支援的 cron 運算式（時間超出範圍）：{cron}")
        return f"*-*-* {hour:02d}:{minute:02d}:00 {SCHEDULE_TIMEZONE}"
    raise PlanError(f"不支援的 cron 運算式（只支援 */N * * * *、0 * * * *、M H * * *）：{cron}")


def job_unit_name(command: str) -> str:
    return assert_name(f"rac-job-{command}", what="排程工作的名稱")


def job_service(command: str) -> str:
    """一個排程指令的單次服務：同映像、同環境檔，`docker run --rm`，`flask --app wsgi <指令>`。"""
    name = job_unit_name(command)
    return _text_file([
        "[Unit]",
        f"Description=rac 排程工作：{command}",
        "Requires=docker.service",
        "After=docker.service network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=oneshot",
        f"TimeoutStartSec={JOB_TIMEOUT_SECONDS}",
        f"EnvironmentFile={RELEASE_ENV_PATH}",
        f"ExecStartPre={SBIN_DIR}/rac-render-env-app",
        f"ExecStartPre=-{DOCKER} rm -f {name}",
        _exec_start(_app_run_args(name, ("flask", "--app", "wsgi", command))),
    ])


def job_timer(command: str, cron: str) -> str:
    """計時器：時區 Asia/Taipei；`Persistent=true` 讓關機期間錯過的工作在開機後補跑一次。"""
    name = job_unit_name(command)
    return _text_file([
        "[Unit]",
        f"Description=rac 排程：{command}（cron {cron}，{SCHEDULE_TIMEZONE}）",
        "",
        "[Timer]",
        f"OnCalendar={cron_to_on_calendar(cron)}",
        "Persistent=true",
        f"Unit={name}.service",
        "",
        "[Install]",
        "WantedBy=timers.target",
    ])


def scheduled_units(scheduled: dict[str, str] | None = None) -> dict[str, str]:
    """由 `SCHEDULED_COMMANDS` 產生的全部排程單元（檔名 → 內容）。沒有給清單時取應用程式的清單，
    所以清單新增指令時產生的排程隨之包含它；人工類指令（`MANUAL_COMMANDS`）出現在清單裡就丟錯。"""
    commands = SCHEDULED_COMMANDS if scheduled is None else scheduled
    units: dict[str, str] = {}
    for command, cron in commands.items():
        if command in MANUAL_COMMANDS:
            raise PlanError(f"人工類指令不得排程：{command}")
        units[f"{job_unit_name(command)}.service"] = job_service(command)
        units[f"{job_unit_name(command)}.timer"] = job_timer(command, cron)
    return units


def host_files(target: Target, plain: dict[str, str]) -> tuple[HostFile, ...]:
    """要放到主機的全部檔案（路徑、權限、內容），順序固定。不含 `release.env`（部署時才寫，見 `release_env_file`）。
    環境檔的目錄（`HOST_DIRECTORIES`）與 RDS 的 CA 套件由主機初始化負責。"""
    files = [
        HostFile(CADDYFILE_PATH, "0644", caddyfile(target)),
        HostFile(PLAIN_ENV_PATH, "0644", plain_env_file(target, plain)),
    ]
    files += [HostFile(render_env_script_path(unit), "0755", render_env_script(target, unit)) for unit in UNITS]
    files += [HostFile(f"{UNIT_DIR}/{pool.container}.service", "0644", pool_unit(pool)) for pool in POOLS]
    files.append(HostFile(f"{UNIT_DIR}/rac-caddy.service", "0644", caddy_unit()))
    files += [HostFile(f"{UNIT_DIR}/{name}", "0644", content) for name, content in scheduled_units().items()]
    return tuple(files)
