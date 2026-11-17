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
- 部署、回復與拆除（task 1.6）各是一份獨立的計畫（`deploy_plan`、`rollback_plan`、`teardown_plan`），與 `build_plan` 的步驟 id 不重複：
  部署依序快照 → 建置 → 遷移 → 重啟 → 驗證（第一次部署在建置與遷移之間多一組 `db-init`：資料庫初始化與憑證探測，用 `--group db-init` 明確執行，
  例行部署的整份計畫不含它）；回復只把 `release.env` 換回前一版標籤並重啟兩個池，沒有任何降級遷移；
  拆除每一步都先以**帶標籤的查詢**找出資源，沒有符合才略過，所以不會碰到帳號中的其他資源。
  在主機上執行的命令一律走 SSM Run Command（`send-command` 之後接 `wait-…` 步驟輪詢結果），內容只用 ASCII
  （`file://` 的 JSON 在 Windows 以系統地區編碼讀取，繁體中文會變成亂碼）。
- 主機初始化（task 4.4 現場撰寫、4.5 回寫）是 `build_plan` 的 `host-init` 組：交換檔與 docker、RDS 的 CA 套件、17 個主機檔案、單元啟用（不啟動）、
  機密取出檢查、停用主機內的 sshd；每一段一個 SSM 步驟加一個等待步驟，且都以標準輸出的一行固定文字驗證結果。放置檔案的步驟需要一般設定
  （`build_plan(plain=…)`），沒有給就被擋下（與沒有 `accept_cost` 的計費步驟同一個機制）。

佔位符（`{{…}}`）：
- `{{名稱}}`：先前某個步驟用 `capture` 存下的標準輸出（例如 `{{host-sg-id}}`）；執行器在執行時代入。
- `{{input_file}}`：`input_json` 寫成的暫存檔路徑。
- `{{secret:名稱}}`：只出現在 `input_json` 內，由執行器代入機密值。
"""
from __future__ import annotations

import base64
import hashlib
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

SSM_CORE_POLICY_ARN = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"   # 主機角色唯一附加的受管政策（建立與拆除共用）

RDS_PORT = 1433
RDS_MASTER_USER = "racmaster"          # 不是 sa；密碼隨機產生、只存在 SSM（design D8）
DB_NAME = "rac_demo"                    # 專屬資料庫與登入的名稱（建立由 aws_demo_bootstrap_db.py 負責）
DB_APP_LOGIN = "rac_app"
RDS_BACKUP_RETENTION_DAYS = 1           # Free plan 不接受 7 天（FreeTierRestrictionError）；規格只要求大於 0
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

GROUPS = ("preflight", "budget", "network", "iam", "storage", "secrets", "rds", "ec2", "rds-ready", "alarms", "host-init")

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
    blocked: bool = False                # 因缺少必要的輸入（沒有 accept_cost、沒有一般設定）而被清空指令（只留說明）
    interactive: bool = False            # 需要人工動作（argv 可為空）；執行器等待使用者確認
    input_json: str | None = None        # 經暫存檔傳給指令的 JSON 範本（含 {{secret:名稱}}）；argv 以 file://{{input_file}} 參照
    exists_argv: tuple[str, ...] | None = None   # 先執行這個；成功（且 exists_nonempty 時輸出非空）代表資源已存在，略過本步
    exists_nonempty: bool = False
    capture: str | None = None           # 把標準輸出（去除前後空白）存成這個名稱；已存在時改存 exists_argv 的輸出
    poll: Check | None = None            # 輪詢：重複執行直到輸出等於 expect；必須同時有 timeout_seconds 與 interval_seconds
    timeout_seconds: int | None = None
    interval_seconds: int | None = None
    verify: tuple[Check, ...] = ()       # 執行後的唯讀驗證
    run_if_found: bool = False           # 拆除步驟：先執行 exists_argv（查詢只會回傳帶本專案標籤的資源），輸出為空代表沒有符合的資源，略過本步；
                                         # 非空才執行 argv，且 capture 存的是 exists_argv 的輸出（argv 可用 {{capture}}）；與「已存在就略過」相反
    poll_fail: tuple[str, ...] = ()      # 輪詢時輸出等於其中任一值就立即失敗（例如 SSM 命令的 Failed），不必等到逾時


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
    ssm_core = SSM_CORE_POLICY_ARN
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


def _refresh_parameter_step(target: Target, var: str, group: str) -> Step:
    """覆寫既有的 SecureString 參數（只限由腳本組成的機密）。連線字串的型態改版後（design D7：去掉 `ServerCertificate`），
    SSM 內既有的 `DATABASE_URL` 仍是舊型態，而 `put-parameter-…` 對已存在的參數會略過；這一步由執行器重新組成值並經暫存檔傳入
    （值不在計畫內、不在命令列、不顯示）。覆寫不能同時指定標籤（AWS 會拒絕），原有的標籤保留。
    驗證只看名稱與型別，不讀出明文（所以計畫內沒有任何 `--with-decryption`）。重複執行的結果相同。"""
    if var not in COMPOSED_SECRETS:
        raise PlanError(f"{var} 不是由腳本組成的機密：不覆寫（隨機產生的機密重新產生會讓既有的資源與參數不一致）")
    name = parameter_name(var)
    body = {"Name": name, "Type": "SecureString", "Value": "{{secret:" + var + "}}", "Overwrite": True}
    return Step(
        f"refresh-parameter-{var}", group,
        f"覆寫機密參數 {name}（重新組成連線字串：只有 Encrypt=yes，沒有 ServerCertificate；值經暫存檔傳入、不顯示；既有的值會被取代，原有標籤保留）",
        _aws(target, "ssm", "put-parameter", "--cli-input-json", "file://{{input_file}}"),
        input_json=_json(body),
        verify=(Check(_aws(target, "ssm", "describe-parameters", "--parameter-filters", f"Key=Name,Option=Equals,Values={name}",
                           *_text("to_string(Parameters[0].[Name,Type])")), _json([name, "SecureString"])),))


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
        _refresh_parameter_step(target, "DATABASE_URL", "rds-ready"),
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


def build_plan(target: Target, *, accept_cost: bool = False, groups: tuple[str, ...] = GROUPS,
               plain: dict[str, str] | None = None) -> list[Step]:
    """依固定順序產生步驟。`groups` 只取其中幾組（順序不變）。

    順序（design D1）：帳號確認 → 預算與 SNS → 網路 → 角色 → 傳檔儲存貯體 → 機密 → RDS（只建立，不等待）→
    EC2 與 Elastic IP（RDS 等待期間做）→ 等 RDS 可用並寫入 DATABASE_URL → 主機告警 → 主機初始化（機密取出檢查需要 `DATABASE_URL`）。
    沒有 `accept_cost` 時，計費步驟（RDS、EC2、Elastic IP）只留說明，不輸出指令。
    `plain` 是一般設定（非機密；主機上的 `plain.env` 由它產生）：沒有給時，放置主機檔案的那一步只留說明、不輸出指令。
    """
    unknown = [g for g in groups if g not in GROUPS]
    if unknown:
        raise PlanError("不認得的步驟組：" + "、".join(unknown))
    builders = {
        "preflight": _preflight_steps, "budget": _budget_steps, "network": _network_steps, "iam": _iam_steps,
        "storage": _storage_steps, "secrets": _secrets_steps, "rds": _rds_steps, "ec2": _ec2_steps,
        "rds-ready": _rds_ready_steps, "alarms": _alarm_steps,
        "host-init": lambda chosen: _host_init_steps(chosen, plain),
    }
    steps: list[Step] = []
    for group in GROUPS:
        if group in groups:
            steps.extend(builders[group](target))
    if not accept_cost:
        steps = [_without_command(step) for step in steps]
    return steps


def _blocked_step(step: Step, reason: str) -> Step:
    """清空指令（也不輸出 JSON 範本與驗證），只留說明；保留 capture，使後面的佔位符仍有來源。執行到這一步會停止。"""
    return replace(step, argv=(), input_json=None, exists_argv=None, verify=(), blocked=True, title=f"{step.title}——{reason}")


def _without_command(step: Step) -> Step:
    """沒有 accept_cost 時，計費步驟不輸出指令。"""
    if not step.billing:
        return step
    return _blocked_step(step, "需要 --accept-cost 才會輸出建立指令（執行器會先顯示預估費用）")


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
# design D7：RDS 的 CA 套件由主機初始化 B 段放在主機，之後只是「建置輸入」：建置映像時 COPY 進衍生層、加進映像的系統憑證庫；
# 容器不掛載它，DATABASE_URL 也不指向它（ServerCertificate 與 RDS 不相容，已否決）
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
    不得提權、限制日誌；機密環境檔來自記憶體檔案系統。`--pull=never`：標籤不存在時直接失敗，不去公開倉庫找同名映像。
    **沒有任何憑證掛載**：RDS 的 CA 已在建置時加進映像的系統憑證庫（design D7）。"""
    chunks = [f"{DOCKER} run --rm --name={name} --pull=never"]
    if publish is not None:
        chunks.append(f"--publish={publish}")
    chunks += [
        f"--env-file={PLAIN_ENV_PATH} --env-file={RUN_DIR}/app.env",
        f"--read-only --tmpfs=/dev/shm:{_TMPFS_OPTIONS} --tmpfs=/tmp:{_TMPFS_OPTIONS}",
        "--cap-drop=ALL --security-opt=no-new-privileges",
        " ".join(_LOG_OPTIONS),
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


# ---- 部署、回復與拆除（task 1.6）-------------------------------------------------------------------------------
# 三份獨立的計畫：`deploy_plan`、`rollback_plan`、`teardown_plan`。步驟 id 與 `build_plan` 的不重複（執行器以 id 選單步）。
# 在主機上執行的命令走 SSM Run Command：`send-command` 的 JSON 經暫存檔傳入、輸出存成命令編號，接著一個 `wait-…` 步驟輪詢結果。
# 傳給 AWS 的 JSON 只用 ASCII（Windows 的 AWS CLI 以系統地區編碼讀 `file://`），所以主機上的腳本訊息都是英文。

DEPLOY_GROUPS = ("prepare", "snapshot", "build", "db-init", "migrate", "restart", "verify")    # 快照 → 建置 → （首次）資料庫初始化與探測 → 遷移 → 重啟 → 驗證
FIRST_DEPLOY_GROUPS = ("db-init",)                  # 只在第一次部署以 `--group db-init` 執行：用 RDS 主要使用者建立專屬資料庫與登入（design D8），例行部署不重做
ROUTINE_DEPLOY_GROUPS = tuple(group for group in DEPLOY_GROUPS if group not in FIRST_DEPLOY_GROUPS)   # `deploy_plan` 沒指定 groups 時的整份計畫
ROLLBACK_GROUPS = ("prepare", "restart", "verify")
TEARDOWN_GROUPS = ("compute", "database", "snapshots", "network", "iam", "storage", "notify", "verify")
DEPLOY_RESOURCES = ("db-snapshot",)                 # 部署時才建立的資源類型（不屬於 build_plan 的 ALLOWED_RESOURCES）
COMMIT_TAG_PATTERN = re.compile(r"[0-9a-f]{7,40}")  # 映像標籤就是提交短碼（可對應提交；也符合 RDS 快照名稱的限制）
PREVIOUS_RELEASE_ENV_PATH = f"{HOST_CONFIG_DIR}/release.previous.env"   # 部署前一版的標籤（回復用）
LOCAL_ARCHIVE_DIR = "build/aws-demo"                # 本機放原始碼壓縮檔的目錄（`build/` 已被 .gitignore；執行器要先建立它）
SOURCE_KEY_PREFIX = "source"                        # 傳檔儲存貯體內的物件前綴
IMAGE_USER_ID = "10001"                             # Dockerfile 的 `useradd --uid 10001`（映像以非 root 執行）
APP_DIR = "/app"                                    # Dockerfile 的 WORKDIR（映像內不得有 .env 與 .git）
APP_BASE_IMAGE_REPO = "rac-app-base"                # design D7：共用的 Dockerfile 建成基底映像，再疊一層 RDS 的 CA 成為 rac-app
CA_TRUST_DIR = "/usr/local/share/ca-certificates"   # update-ca-certificates 只收這個目錄內副檔名為 .crt 的檔案
CA_TRUST_FILE = "rds-ca-bundle.crt"                 # 衍生層內的檔名（同時是衍生映像建置內容裡的檔名）
CA_STORE_FILE = "/etc/ssl/certs/ca-certificates.crt"   # 系統憑證庫（所有憑證串在一起）；check-image 數它的憑證張數
CA_DERIVED_NAME = "Dockerfile"                      # 衍生映像建置內容裡的檔名
BOOTSTRAP_SCRIPT = "aws_demo_bootstrap_db.py"       # 資料庫初始化：在主映像內以 RDS 主要使用者一次性執行（design D8）
PROBE_SCRIPT = "aws_demo_probe_db.py"               # 憑證探測：以應用程式登入的 DATABASE_URL 對資料庫執行 SELECT 1（design D7）
OPS_SCRIPTS = (BOOTSTRAP_SCRIPT, PROBE_SCRIPT)      # 這兩支維運腳本不在映像內：經傳檔儲存貯體放到主機暫存目錄，再唯讀掛進一次性容器
OPS_KEY_PREFIX = "ops"                              # 傳檔儲存貯體內放維運腳本的前綴（與 `source/` 分開）
OPS_WORK_PREFIX = "/var/tmp/rac-ops"                # 主機上的暫存目錄（持久磁碟；AL2023 的 /tmp 是記憶體檔案系統；用後由 trap 刪除）
OPS_CONTAINER_DIR = "/ops"                          # 維運腳本在一次性容器內的掛載目錄
DB_ENDPOINT_CAPTURE = "db-endpoint"                 # 資料庫初始化的 `--host`（RDS 端點，執行時以唯讀查詢取得）
POOL_READY_PATH = "/readyz"                         # 含資料庫檢查；主機內對回送位址探測
SSM_FAILED_STATUSES = ("Failed", "Cancelled", "TimedOut", "Cancelling")
BUILD_TIMEOUT_SECONDS = 1800                        # t3.small 上建置映像（pip 與 ODBC 驅動）的上限
SNAPSHOT_TIMEOUT_SECONDS = 1800
TEARDOWN_RDS_TIMEOUT_SECONDS = 2400
VALID_FINAL_SNAPSHOT = (True, False, None)          # None＝尚未選擇：RDS 刪除步驟不輸出指令


def _commit_tag(tag: str) -> str:
    if not isinstance(tag, str) or not COMMIT_TAG_PATTERN.fullmatch(tag):
        raise PlanError("映像標籤必須是提交短碼（7 至 40 位小寫十六進位）：標籤要能對應到提交")
    return tag


def _host_filters(target: Target) -> tuple[str, ...]:
    n = names(target)
    return ("--filters", f"Name=tag:Name,Values={n.host}", f"Name=tag:Project,Values={PROJECT_TAG}",
            f"Name=tag:Environment,Values={ENV}", "Name=instance-state-name,Values=running")


def _find_host_step(target: Target, step_id: str, group: str, *, capture: str = "instance-id") -> Step:
    """主機的實例編號不從建立時帶過來（部署可能在很久之後、另一次執行）：用名稱與標籤找；必須剛好一台。"""
    n = names(target)
    return Step(step_id, group, f"找出主機 {n.host}（running、帶本專案標籤；找不到或不只一台即停止）",
                _aws(target, "ec2", "describe-instances", *_host_filters(target), *_text("Reservations[].Instances[].InstanceId")),
                capture=capture,
                verify=(Check(_aws(target, "ec2", "describe-instances", *_host_filters(target), *_text("length(Reservations[].Instances[])")), "1"),))


def _ssm_steps(target: Target, step_id: str, group: str, title: str, lines: list[str], *, timeout: int,
               expect: str | None = None, instance: str = "instance-id") -> list[Step]:
    """在主機上執行一段腳本：`send-command`（輸出命令編號）加 `wait-…`（輪詢到 Success；Failed、Cancelled、TimedOut 立即停止）。
    `expect` 不是 None 時，等待步驟驗證命令的標準輸出（去除前後空白）等於它。腳本內容只用 ASCII。
    `instance` 是主機實例編號的擷取名稱（預設 `instance-id`；主機初始化用自己的查詢步驟，不依賴建立主機的步驟）。"""
    command = f"{step_id}-command-id"
    placeholder = "{{" + instance + "}}"
    body = {"InstanceIds": [placeholder], "DocumentName": "AWS-RunShellScript", "Comment": step_id, "TimeoutSeconds": 60,
            "Parameters": {"commands": lines, "executionTimeout": [str(timeout)]}}
    invocation = ("--command-id", "{{" + command + "}}", "--instance-id", placeholder)
    verify = ()
    if expect is not None:
        verify = (Check(_aws(target, "ssm", "get-command-invocation", *invocation, *_text("StandardOutputContent")), expect),)
    return [
        Step(step_id, group, title,
             _aws(target, "ssm", "send-command", "--cli-input-json", "file://{{input_file}}", *_text("Command.CommandId")),
             input_json=_json(body), capture=command),
        Step(f"wait-{step_id}", group, f"等待主機上的命令完成：{step_id}（失敗、取消或逾時即停止；細節看該命令的輸出）",
             poll=Check(_aws(target, "ssm", "get-command-invocation", *invocation, *_text("Status")), "Success"),
             timeout_seconds=timeout + 120, interval_seconds=10, poll_fail=SSM_FAILED_STATUSES, verify=verify),
    ]


# 主機上的腳本片段：全部 ASCII、POSIX sh 相容（SSM 的 AWS-RunShellScript 以 sh 執行），沒有任何機密值

def _one_off_lines(tag: str, name: str, command: tuple[str, ...], *, pipe: str = "") -> list[str]:
    """用指定標籤的映像、與兩個池相同的強化與環境檔，執行一次性容器（遷移、種子、檢查）。
    環境檔的機密先由 `rac-render-env-app` 取到記憶體檔案系統；容器內的標籤變數由這份腳本自己設定（`APP_IMAGE_REF` 展開 `${RAC_IMAGE_TAG}`）。"""
    run = " ".join(_app_run_args(name, command)) + pipe
    return [f"{SBIN_DIR}/rac-render-env-app", f"{IMAGE_TAG_VAR}={tag}", f"{DOCKER} rm -f {name} >/dev/null 2>&1 || true", run]


def derived_dockerfile(tag: str) -> str:
    """衍生映像的 Dockerfile（design D7）：在基底映像 `rac-app-base:<tag>` 上，把主機的 RDS CA 套件加進系統憑證庫。
    只有五個指令：`FROM`、`USER root`、`COPY`、`RUN update-ca-certificates`、`USER app`；沒有 `ARG`／`ENV`、沒有網路取用、沒有任何機密。
    變更只是**新增**這份區域 CA 套件的根憑證，不移除、不關閉任何驗證（check-image 以憑證張數核對）。共用的 `Dockerfile` 不動。"""
    return _text_file([
        f"FROM {APP_BASE_IMAGE_REPO}:{_commit_tag(tag)}",
        "USER root",
        f"COPY {CA_TRUST_FILE} {CA_TRUST_DIR}/{CA_TRUST_FILE}",
        "RUN update-ca-certificates",
        "USER app",
    ])


def _build_lines(target: Target, tag: str) -> list[str]:
    """兩段建置：①共用的 `Dockerfile` 建成基底映像 `rac-app-base:<tag>`；②衍生 Dockerfile（見 `derived_dockerfile`）加一層主機上的
    RDS CA 套件，建成 `rac-app:<tag>`。套件檔以 `.crt` 為副檔名複製進衍生映像的建置內容（`update-ca-certificates` 只收 `.crt`），
    衍生映像的建置內容只有這個檔案與 Dockerfile。"""
    n = names(target)
    key = f"{SOURCE_KEY_PREFIX}/{tag}.tar.gz"
    delimiter = "RAC_DERIVED_DOCKERFILE"
    return [
        "set -eu", "export HOME=/root",
        'work="$(mktemp -d /var/tmp/rac-build.XXXXXX)"',           # 持久磁碟（AL2023 的 /tmp 是記憶體檔案系統）
        "trap 'rm -rf \"$work\"' EXIT",
        f'aws s3 cp "s3://{n.bucket}/{key}" "$work/source.tar.gz" --region {target.region} --only-show-errors',
        'mkdir "$work/src"',
        'tar -xzf "$work/source.tar.gz" -C "$work/src"',
        f'{DOCKER} build --tag {APP_BASE_IMAGE_REPO}:{tag} "$work/src"',
        'mkdir "$work/ca"',
        f'install -m 0644 {RDS_CA_BUNDLE_PATH} "$work/ca/{CA_TRUST_FILE}"',
        f'cat > "$work/ca/{CA_DERIVED_NAME}" <<\'{delimiter}\'',
        *derived_dockerfile(tag).splitlines(),
        delimiter,
        f'{DOCKER} build --tag {APP_IMAGE_REPO}:{tag} "$work/ca"',
    ]


def _image_check_lines(tag: str) -> list[str]:
    """映像以非 root 執行、且工作目錄內沒有 .env 與 .git（輸出一行：`<uid> clean`）；另外核對系統憑證庫：
    部署用映像的憑證張數必須等於「基底映像的張數加主機上 RDS 套件的張數」（只新增、沒有減少；不符就以錯誤結束，標準輸出不變）。"""
    image = f"{APP_IMAGE_REPO}:{tag}"
    base = f"{APP_BASE_IMAGE_REPO}:{tag}"
    probe = f"if [ -e {APP_DIR}/.env ] || [ -e {APP_DIR}/.git ]; then echo dirty; else echo clean; fi"
    return [
        "set -eu", "export HOME=/root",
        f'user="$({DOCKER} run --rm --pull=never --entrypoint id {image} -u)"',
        f"state=\"$({DOCKER} run --rm --pull=never --entrypoint sh {image} -c '{probe}')\"",
        f"base_certs=\"$({DOCKER} run --rm --pull=never --entrypoint grep {base} -c 'BEGIN CERTIFICATE' {CA_STORE_FILE})\"",
        f"image_certs=\"$({DOCKER} run --rm --pull=never --entrypoint grep {image} -c 'BEGIN CERTIFICATE' {CA_STORE_FILE})\"",
        f"bundle_certs=\"$(grep -c 'BEGIN CERTIFICATE' {RDS_CA_BUNDLE_PATH})\"",
        'if [ "$image_certs" -ne "$((base_certs + bundle_certs))" ]; then',
        '  echo "ca store mismatch: base=$base_certs bundle=$bundle_certs image=$image_certs" >&2',
        "  exit 1",
        "fi",
        'echo "$user $state"',
    ]


def ops_script_sha256(name: str) -> str:
    """維運腳本（`OPS_SCRIPTS`）目前內容的 SHA-256：上傳之後主機下載時比對，不符就在執行之前失敗（路徑只限這兩個固定名稱）。"""
    if name not in OPS_SCRIPTS:
        raise PlanError(f"不是維運腳本：{name}（只有 {'、'.join(OPS_SCRIPTS)}）")
    return hashlib.sha256((SCRIPTS_DIR / name).read_bytes()).hexdigest()


def _ops_container_lines(target: Target, tag: str, script: str, *, container: str, unit: str, env_files: str, command: str) -> list[str]:
    """在主機上以一次性容器（映像 `rac-app:<tag>`）執行一支維運腳本：下載到暫存目錄、比對 SHA-256、由機密取出腳本寫環境檔
    （記憶體檔案系統；結束時一律刪除）、腳本唯讀掛入。強化選項與兩個池相同（唯讀根、丟掉所有 capability、不得提權），**沒有任何憑證掛載**
    （RDS 的 CA 在映像的系統憑證庫，design D7）。容器的輸出（含繁體中文的進度與失敗類別）送到標準錯誤，標準輸出只有一行
    `docker-exit=<代碼>`，所以等待步驟能以固定文字驗證；容器非零結束時整段腳本也以同一個代碼結束（SSM 狀態為 Failed）。"""
    n = names(target)
    digest = ops_script_sha256(script)
    env_out = f"{RUN_DIR}/{unit}.env"
    mount = f'--mount=type=bind,source="$work/{script}",target={OPS_CONTAINER_DIR}/{script},readonly'
    return [
        "set -eu", "export HOME=/root",
        f'work="$(mktemp -d {OPS_WORK_PREFIX}.XXXXXX)"',
        f"trap 'rm -rf \"$work\" {env_out}' EXIT",
        'chmod 0755 "$work"',
        f'aws s3 cp "s3://{n.bucket}/{OPS_KEY_PREFIX}/{script}" "$work/{script}" --region {target.region} --only-show-errors',
        f'echo "{digest}  $work/{script}" | sha256sum -c --status -',
        f'chmod 0644 "$work/{script}"',
        render_env_script_path(unit),
        f"{DOCKER} rm -f {container} >/dev/null 2>&1 || true",
        "set +e",
        f"{DOCKER} run --rm --name={container} --pull=never {env_files} --env PYTHONIOENCODING=utf-8 "
        f"--read-only --tmpfs=/dev/shm:{_TMPFS_OPTIONS} --tmpfs=/tmp:{_TMPFS_OPTIONS} --cap-drop=ALL --security-opt=no-new-privileges "
        f"{mount} {APP_IMAGE_REPO}:{tag} {command} >&2",
        "rc=$?",
        'echo "docker-exit=$rc"',
        "exit $rc",
    ]


def _bootstrap_db_lines(target: Target, tag: str) -> list[str]:
    """資料庫初始化：環境檔只含 RDS 主要使用者的連線機密（`bootstrap` 單元），腳本以它建立專屬資料庫與登入；可重複執行。"""
    return _ops_container_lines(
        target, tag, BOOTSTRAP_SCRIPT, container="rac-bootstrap-db", unit="bootstrap",
        env_files=f"--env-file={RUN_DIR}/bootstrap.env",
        command=f"python {OPS_CONTAINER_DIR}/{BOOTSTRAP_SCRIPT} --host {{{{{DB_ENDPOINT_CAPTURE}}}}}")


def _probe_db_lines(target: Target, tag: str) -> list[str]:
    """憑證探測：與應用程式相同的環境檔（`plain.env` 加 `app.env`，應用程式登入的 `DATABASE_URL`），只做 `SELECT 1`。"""
    return _ops_container_lines(
        target, tag, PROBE_SCRIPT, container="rac-probe-db", unit="app",
        env_files=f"--env-file={PLAIN_ENV_PATH} --env-file={RUN_DIR}/app.env", command=f"python {OPS_CONTAINER_DIR}/{PROBE_SCRIPT}")


def _upload_ops_step(target: Target, script: str) -> Step:
    """本機：把維運腳本以伺服器端加密上傳到傳檔儲存貯體的 `ops/` 前綴（每次都重傳；物件內容以 SHA-256 在主機端核對）。"""
    n = names(target)
    key = f"{OPS_KEY_PREFIX}/{script}"
    return Step(f"upload-ops-{script.removeprefix('aws_demo_').removesuffix('.py').replace('_', '-')}", "db-init",
                f"上傳維運腳本 scripts/{script} 到 s3://{n.bucket}/{key}（伺服器端加密；主機下載後比對 SHA-256）",
                _aws(target, "s3", "cp", f"scripts/{script}", f"s3://{n.bucket}/{key}", "--sse", "AES256", "--only-show-errors"),
                verify=(Check(_aws(target, "s3api", "head-object", "--bucket", n.bucket, "--key", key, *_text("ServerSideEncryption")), "AES256"),))


def _migrate_lines(tag: str) -> list[str]:
    return ["set -eu", "export HOME=/root", *_one_off_lines(tag, "rac-migrate", ("alembic", "upgrade", "head"))]


def _migration_head_lines(tag: str) -> list[str]:
    """`alembic current` 在資料庫已是最新版時會在版本後面標 `(head)`；輸出 1 代表是最新版、0 代表不是（含執行失敗）。"""
    *prepare, run = _one_off_lines(tag, "rac-migrate-check", ("alembic", "current"), pipe=" 2>/dev/null | grep -c '(head)' || true")
    return ["set -eu", "export HOME=/root", *prepare, f'heads="$({run})"', 'echo "$heads"']


def _seed_lines(tag: str) -> list[str]:
    return ["set -eu", "export HOME=/root", *_one_off_lines(tag, "rac-seed", ("flask", "--app", "wsgi", "seed-scripts"))]


def _switch_release_lines(tag: str) -> list[str]:
    """把 `release.env` 換成指定標籤並重啟兩個池。映像不存在時在改任何檔案之前就失敗；換掉之前先把目前的標籤存成 `release.previous.env`
    （同一個標籤重複部署時不覆蓋，避免前一版被自己蓋掉）。檔案以暫存檔加改名寫入（原子）。"""
    line = release_env_file(tag).strip()          # 同時驗證標籤格式
    return [
        "set -eu",
        f"{DOCKER} image inspect {APP_IMAGE_REPO}:{tag} >/dev/null",
        f"cd {HOST_CONFIG_DIR}",
        f"if [ -f release.env ] && ! grep -qx '{line}' release.env; then cp -p release.env release.previous.env; fi",
        f"printf '%s\\n' '{line}' > release.env.new",
        "chmod 0644 release.env.new",
        "mv -f release.env.new release.env",
        "systemctl restart " + " ".join(f"{pool.container}.service" for pool in POOLS),
    ]


def _pool_check_lines() -> list[str]:
    """兩個池各自在回送位址上回 200（`/readyz` 含資料庫檢查；最多等 150 秒），最後輸出兩個容器實際使用的映像。"""
    ports = " ".join(str(pool.port) for pool in POOLS)
    images = " ".join(f"$({DOCKER} inspect --format '{{{{.Config.Image}}}}' {pool.container})" for pool in POOLS)
    return [
        "set -eu",
        f"for port in {ports}; do",
        "  ok=0",
        "  for attempt in $(seq 1 30); do",
        f'    if curl -fsS -o /dev/null --max-time 5 "http://127.0.0.1:$port{POOL_READY_PATH}"; then ok=1; break; fi',
        "    sleep 5",
        "  done",
        '  if [ "$ok" != 1 ]; then echo "pool on port $port is not ready" >&2; exit 1; fi',
        "done",
        f'echo "{images}"',
    ]


def _pool_check_expect(tag: str) -> str:
    return " ".join(f"{APP_IMAGE_REPO}:{tag}" for _ in POOLS)


# ---- 主機初始化（task 4.4 在主機上現場撰寫並逐項驗證，4.5 回寫成 `build_plan` 的 `host-init` 組）---------------------------
# 六段，每段一個 SSM 步驟加一個等待步驟，順序固定：A 系統 → B 憑證 → C 檔案 → D 單元 → E 機密取出檢查 → F 停用 sshd。
# 每一段都能重複執行（做過的不重做、結果相同）；沒有任何一段啟動 rac-* 單元（啟動屬於部署與排程階段）。
# 每段腳本只在標準輸出印一行固定文字，等待步驟以它驗證（`_ssm_steps` 的 expect）；腳本內容只用 ASCII、沒有任何機密值。

SWAP_FILE = "/swapfile"
SWAP_MB = 2048                                   # design D1：2 GB 交換檔（2 GB 記憶體的主機要跑兩個池與 Caddy）
HOST_PACKAGE = "docker"                          # Amazon Linux 2023 的套件庫提供；aws CLI 由系統內建
RDS_CA_BUNDLE_URL = "https://truststore.pki.rds.amazonaws.com/{region}/{region}-bundle.pem"   # AWS 公開的區域 CA 套件
HOST_INIT_TIMEOUT_SECONDS = 600                  # 安裝 docker 與下載套件的上限
HOST_INIT_SHORT_TIMEOUT_SECONDS = 180            # 其餘各段
HOST_INIT_GROUP = "host-init"
HOST_INSTANCE_CAPTURE = "host-instance-id"          # 主機初始化自己的「找出主機」步驟擷取的實例編號
_TOKEN = re.compile(r"@[A-Z][A-Z_]*@")
_SAFE_HOST_PATH = re.compile(r"/[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*")
_SAFE_HOST_MODE = re.compile(r"0[0-7]{3}")

_HOST_SYSTEM_TEMPLATE = r"""set -eu
export HOME=/root
@DIRECTORIES@
if ! swapon --show=NAME --noheadings | grep -qx @SWAP_FILE@; then
  if [ ! -f @SWAP_FILE@ ]; then
    rm -f @SWAP_FILE@.new
    dd if=/dev/zero of=@SWAP_FILE@.new bs=1M count=@SWAP_MB@ status=none
    chmod 600 @SWAP_FILE@.new
    mkswap @SWAP_FILE@.new >/dev/null
    mv @SWAP_FILE@.new @SWAP_FILE@
  fi
  swapon @SWAP_FILE@
fi
grep -q '^@SWAP_FILE@ ' /etc/fstab || echo '@SWAP_FILE@ none swap defaults 0 0' >> /etc/fstab
dnf install -y -q @PACKAGE@ >/dev/null
systemctl enable --now docker >/dev/null
echo "docker=$(systemctl is-active docker) swap=$(swapon --show=NAME --noheadings | grep -cx @SWAP_FILE@ || true) dirs=$(stat -c %a @STAT_PATHS@ | paste -sd, -)"
"""

_HOST_CA_TEMPLATE = r"""set -eu
export HOME=/root
url=@URL@
tmp="$(mktemp /var/tmp/rds-ca.XXXXXX)"
trap 'rm -f "$tmp"' EXIT
curl -fsS --proto '=https' --tlsv1.2 -o "$tmp" "$url"
count="$(grep -c 'BEGIN CERTIFICATE' "$tmp" || true)"
[ "$count" -ge 1 ]
openssl x509 -in "$tmp" -noout
install -m 0644 -o root -g root "$tmp" @CA_PATH@
echo "ca-bundle=$(stat -c %a @CA_PATH@)"
"""

# `put 權限 路徑 sha256 base64`：先解碼到暫存檔並比對雜湊（不符就在安裝之前失敗），安裝後再比對一次並檢查權限
_HOST_FILES_HEAD = r"""set -eu
export HOME=/root
tmp="$(mktemp /var/tmp/rac-file.XXXXXX)"
trap 'rm -f "$tmp"' EXIT
n=0
put() {
  printf '%s' "$4" | base64 -d > "$tmp"
  printf '%s  %s\n' "$3" "$tmp" | sha256sum -c - >/dev/null
  install -m "$1" -o root -g root "$tmp" "$2"
  printf '%s  %s\n' "$3" "$2" | sha256sum -c - >/dev/null
  [ "$(stat -c %a "$2")" = "${1#0}" ]
  n=$((n + 1))
}"""

_HOST_UNITS_TEMPLATE = r"""set -eu
export HOME=/root
systemctl daemon-reload
systemctl enable @UNITS@ >/dev/null
echo "enabled=$(systemctl is-enabled @UNITS@ | grep -cx enabled || true)"
"""

_HOST_SECRETS_TEMPLATE = r"""set -eu
export HOME=/root
trap 'rm -f @OUT@' EXIT
@RENDER@
fs="$(stat -f -c %T @RUN_DIR@)"
mode="$(stat -c %a @RUN_DIR@)/$(stat -c %a @OUT@)"
count="$(grep -c = @OUT@ || true)"
echo "secrets=$count fs=$fs mode=$mode"
"""

_HOST_SSHD_TEMPLATE = r"""set -eu
export HOME=/root
if systemctl list-unit-files sshd.service --no-legend | grep -q '^sshd.service'; then
  systemctl disable --now sshd.service >/dev/null
fi
echo "sshd=$(systemctl is-active sshd.service || true) port22=$(ss -tlnH | awk '{print $4}' | grep -c ':22$' || true) ssm=$(systemctl is-active amazon-ssm-agent || true)"
"""


def _script(template: str, **tokens: str) -> list[str]:
    """把 `@名稱@` 代入模板並拆成行（SSM 的 commands 一行一個元素）。沒有代完或含非 ASCII 字元時丟錯。"""
    text = template
    for name, value in tokens.items():
        text = text.replace(f"@{name}@", value)
    if _TOKEN.search(text):
        raise PlanError("主機腳本模板有沒有代入的標記")
    if not text.isascii():
        raise PlanError("主機腳本只能用 ASCII（傳給 AWS 的 JSON 在 Windows 以系統地區編碼讀取）")
    return text.splitlines()


def rds_ca_bundle_url(target: Target) -> str:
    return RDS_CA_BUNDLE_URL.format(region=target.region)


def host_enabled_units() -> tuple[str, ...]:
    """主機初始化要啟用（enable）但**不啟動**的單元：兩個池、Caddy 與每個排程的計時器（排程的 .service 由計時器觸發，不單獨啟用）。"""
    return (*(f"{pool.container}.service" for pool in POOLS), "rac-caddy.service",
            *(name for name in scheduled_units() if name.endswith(".timer")))


def host_file_count() -> int:
    """`host_files()` 放置的檔案數（不依賴一般設定的內容；測試比對它與 `host_files()` 的長度一致）。"""
    return 2 + len(UNITS) + len(POOLS) + 1 + len(scheduled_units())


def _host_system_lines() -> list[str]:
    return _script(
        _HOST_SYSTEM_TEMPLATE,
        DIRECTORIES="\n".join(f"install -d -m {mode} -o root -g root {path}" for path, mode in HOST_DIRECTORIES),
        SWAP_FILE=SWAP_FILE, SWAP_MB=str(SWAP_MB), PACKAGE=HOST_PACKAGE, STAT_PATHS=" ".join(path for path, _ in HOST_DIRECTORIES))


def _host_system_expect() -> str:
    modes = ",".join(format(int(mode, 8), "o") for _, mode in HOST_DIRECTORIES)
    return f"docker=active swap=1 dirs={modes}"


def _host_ca_lines(target: Target) -> list[str]:
    return _script(_HOST_CA_TEMPLATE, URL=rds_ca_bundle_url(target), CA_PATH=RDS_CA_BUNDLE_PATH)


def _host_files_lines(files: tuple[HostFile, ...]) -> list[str]:
    """每個檔案一行 `put`：內容以 base64 傳（含繁體中文，不直接放進 JSON）、附 sha256，路徑與權限先檢查格式。"""
    lines = _HOST_FILES_HEAD.splitlines()
    for item in files:
        if not _SAFE_HOST_PATH.fullmatch(item.path) or not _SAFE_HOST_MODE.fullmatch(item.mode):
            raise PlanError("主機檔案的路徑或權限格式不合法")
        raw = item.content.encode("utf-8")
        lines.append(f"put {item.mode} {item.path} {hashlib.sha256(raw).hexdigest()} {base64.b64encode(raw).decode('ascii')}")
    lines.append('echo "files=$n"')
    return lines


def _host_units_lines() -> list[str]:
    return _script(_HOST_UNITS_TEMPLATE, UNITS=" ".join(host_enabled_units()))


def _host_secrets_lines() -> list[str]:
    return _script(_HOST_SECRETS_TEMPLATE, RENDER=render_env_script_path("app"), RUN_DIR=RUN_DIR, OUT=f"{RUN_DIR}/app.env")


def _host_sshd_lines() -> list[str]:
    return _script(_HOST_SSHD_TEMPLATE)


def _host_init_steps(target: Target, plain: dict[str, str] | None) -> list[Step]:
    """先找出主機（唯讀查詢，不依賴建立主機的步驟，所以單步或另一次執行時不需要 `--accept-cost`），然後依序：
    A 系統（目錄、交換檔、docker）→ B 憑證（RDS 的 CA 套件）→ C 檔案（`host_files()`）→ D 單元（啟用、不啟動）→
    E 機密取出檢查（用主機角色取全部機密、確認在記憶體檔案系統、立即刪除）→ F 停用主機內的 sshd。
    沒有一般設定（`plain`）時，C 只留說明、不輸出指令（內容含 `plain.env`，沒有它就不能產生）。"""
    g = HOST_INIT_GROUP
    count = host_file_count()
    secret_count = len(unit_parameter_names(target, "app"))

    def ssm(step_id: str, title: str, lines: list[str], *, expect: str, timeout: int = HOST_INIT_SHORT_TIMEOUT_SECONDS) -> list[Step]:
        return _ssm_steps(target, step_id, g, title, lines, timeout=timeout, expect=expect, instance=HOST_INSTANCE_CAPTURE)

    files = ssm("host-init-files",
                f"主機初始化 C（檔案）：放置 {count} 個主機檔案（Caddyfile、plain.env、兩支取出腳本、單元與計時器；內容以 base64 傳送，"
                "先比對雜湊再安裝，安裝後再比對一次並檢查權限）",
                _host_files_lines(host_files(target, plain)) if plain is not None else [], expect=f"files={count}")
    if plain is None:
        files[0] = _blocked_step(files[0], "需要 --vars-file（一般設定檔，例如 aws-demo.vars）才會輸出放置檔案的指令")
    return [
        _find_host_step(target, "host-init-find-host", g, capture=HOST_INSTANCE_CAPTURE),
        *ssm("host-init-system",
             f"主機初始化 A（系統）：建立 {' 與 '.join(path for path, _ in HOST_DIRECTORIES)}、{SWAP_MB // 1024} GB 交換檔、安裝並啟動 docker（可重複執行）",
             _host_system_lines(), expect=_host_system_expect(), timeout=HOST_INIT_TIMEOUT_SECONDS),
        *ssm("host-init-ca", f"主機初始化 B（憑證）：從 AWS 公開位址取得 RDS 的 CA 套件，至少一張憑證且能解析才放到 {RDS_CA_BUNDLE_PATH}",
             _host_ca_lines(target), expect="ca-bundle=644"),
        *files,
        *ssm("host-init-units", "主機初始化 D（單元）：daemon-reload，並啟用（不啟動）兩個池、Caddy 與每個排程計時器；啟動留給部署與排程階段",
             _host_units_lines(), expect=f"enabled={len(host_enabled_units())}"),
        *ssm("host-init-secrets",
             f"主機初始化 E（機密取出檢查）：以主機角色執行 rac-render-env-app，確認取得全部 {secret_count} 個機密、{RUN_DIR} 是記憶體檔案系統且權限正確；"
             "不印任何值，檢查後立即刪除環境檔",
             _host_secrets_lines(), expect=f"secrets={secret_count} fs=tmpfs mode=700/600"),
        *ssm("host-init-sshd", "主機初始化 F（SSH）：停用主機內的 sshd（對外本來就沒有 22；之後的管理只走 SSM）",
             _host_sshd_lines(), expect="sshd=inactive port22=0 ssm=active"),
    ]


def snapshot_name(target: Target, tag: str) -> str:
    """遷移前的 RDS 快照名稱：每個版本標籤一個（重複部署同一個標籤時沿用，因為它仍是遷移之前的還原點）。"""
    return assert_name(f"{names(target).db_instance}-pre-{_commit_tag(tag)}", what="快照名稱")


def deploy_plan(target: Target, tag: str, *, groups: tuple[str, ...] | None = None) -> list[Step]:
    """新版部署：資料庫快照 → 建置映像 → 遷移 → 重啟兩個池 → 驗證（每個階段一組，順序固定；`groups` 只取其中幾組，
    沒指定時是例行部署的整份計畫 `ROUTINE_DEPLOY_GROUPS`，**不含**只在第一次部署執行的 `db-init`）。

    - `tag` 是要部署的提交短碼：原始碼以 `git archive <tag>` 取出（只含已提交內容，不含 .env），經傳檔儲存貯體到主機建置，
      先建基底映像 `rac-app-base:<tag>`，再加一層主機上的 RDS CA 套件，標記為 `rac-app:<tag>`（design D7），前一版的映像保留供回復。
    - 遷移之前一定有快照；遷移（含種子）失敗就停止，不重啟兩個池；遷移是 expand-only，所以舊版程式仍能在新結構上運作。
    - 沒有任何步驟降級資料庫結構。
    """
    groups = ROUTINE_DEPLOY_GROUPS if groups is None else groups
    unknown = [g for g in groups if g not in DEPLOY_GROUPS]
    if unknown:
        raise PlanError("不認得的步驟組：" + "、".join(unknown))
    tag = _commit_tag(tag)
    n = names(target)
    snapshot = snapshot_name(target, tag)
    snapshot_arn = f"arn:aws:rds:{target.region}:{target.account_id}:snapshot:{snapshot}"
    archive = f"{LOCAL_ARCHIVE_DIR}/source-{tag}.tar.gz"
    object_key = f"{SOURCE_KEY_PREFIX}/{tag}.tar.gz"
    steps: dict[str, list[Step]] = {group: [] for group in DEPLOY_GROUPS}

    steps["prepare"].append(_find_host_step(target, "deploy-find-host", "prepare"))

    steps["snapshot"] += [
        Step("snapshot-db", "snapshot", f"建立 RDS 快照 {snapshot}（遷移之前的還原點；已存在就略過）",
             _aws(target, "rds", "create-db-snapshot", "--db-instance-identifier", n.db_instance, "--db-snapshot-identifier", snapshot,
                  "--tags", _json(tag_list(snapshot))),
             resource="db-snapshot",
             exists_argv=_aws(target, "rds", "describe-db-snapshots", "--db-snapshot-identifier", snapshot, *_text("DBSnapshots[].DBSnapshotIdentifier")),
             exists_nonempty=True,
             verify=(Check(_aws(target, "rds", "list-tags-for-resource", "--resource-name", snapshot_arn, *_text("TagList[?Key=='Project'].Value|[0]")), PROJECT_TAG),)),
        Step("wait-snapshot-db", "snapshot", f"等待快照 {snapshot} 進入 available（失敗即停止；不等快照完成就不能遷移）",
             poll=Check(_aws(target, "rds", "describe-db-snapshots", "--db-snapshot-identifier", snapshot, *_text("DBSnapshots[0].Status")), "available"),
             timeout_seconds=SNAPSHOT_TIMEOUT_SECONDS, interval_seconds=30, poll_fail=("failed",)),
    ]

    steps["build"] += [
        Step("archive-source", "build", f"本機：git archive {tag} 取出已提交的原始碼壓縮檔（不含 .env 與未提交的變更）到 {archive}",
             ("git", "archive", "--format=tar.gz", f"--output={archive}", tag)),
        Step("upload-source", "build", f"上傳壓縮檔到傳檔儲存貯體 s3://{n.bucket}/{object_key}（伺服器端加密）",
             _aws(target, "s3", "cp", archive, f"s3://{n.bucket}/{object_key}", "--sse", "AES256", "--only-show-errors"),
             verify=(Check(_aws(target, "s3api", "head-object", "--bucket", n.bucket, "--key", object_key, *_text("ServerSideEncryption")), "AES256"),)),
        *_ssm_steps(target, "build-image", "build",
                    f"在主機上建置映像：先建基底映像 {APP_BASE_IMAGE_REPO}:{tag}，再加一層主機上的 RDS CA 套件成為 {APP_IMAGE_REPO}:{tag}（design D7；約數分鐘；前一版的映像保留）",
                    _build_lines(target, tag), timeout=BUILD_TIMEOUT_SECONDS),
        *_ssm_steps(target, "check-image", "build",
                    f"確認映像以非 root（uid {IMAGE_USER_ID}）執行、{APP_DIR} 內沒有 .env 與 .git，且系統憑證庫的憑證數＝基底映像的憑證數＋RDS 套件的憑證數",
                    _image_check_lines(tag), timeout=300, expect=f"{IMAGE_USER_ID} clean"),
    ]

    steps["db-init"] += [
        Step("deploy-find-db-endpoint", "db-init", "取得 RDS 的端點位址（唯讀；資料庫初始化的 --host）",
             _aws(target, "rds", "describe-db-instances", "--db-instance-identifier", n.db_instance, *_text("DBInstances[0].Endpoint.Address")),
             capture=DB_ENDPOINT_CAPTURE),
        *(_upload_ops_step(target, script) for script in OPS_SCRIPTS),
        *_ssm_steps(target, "bootstrap-db", "db-init",
                    f"資料庫初始化（design D8）：用 RDS 主要使用者在 {APP_IMAGE_REPO}:{tag} 的一次性容器內建立專屬資料庫與登入（已存在就略過，可重複執行；"
                    "只在第一次部署執行；容器的輸出在該命令的標準錯誤）",
                    _bootstrap_db_lines(target, tag), timeout=600, expect="docker-exit=0"),
        *_ssm_steps(target, "probe-db", "db-init",
                    f"憑證探測（design D7）：以應用程式登入的連線字串對資料庫執行 SELECT 1，連線必須加密並由映像的系統憑證庫驗證憑證；"
                    "失敗就停止、不放寬任何驗證",
                    _probe_db_lines(target, tag), timeout=300, expect="docker-exit=0"),
    ]

    steps["migrate"] += [
        *_ssm_steps(target, "migrate-db", "migrate", "執行 alembic upgrade head（一次性容器；失敗即停止，不重啟兩個池）",
                    _migrate_lines(tag), timeout=900),
        *_ssm_steps(target, "check-migration-head", "migrate", "確認資料庫版本已是最新（alembic current 標有 head）",
                    _migration_head_lines(tag), timeout=300, expect="1"),
        *_ssm_steps(target, "seed-scripts", "migrate", "執行 flask seed-scripts（建立或更新內建劇本；冪等，遷移之後執行）",
                    _seed_lines(tag), timeout=600),
    ]

    steps["restart"] += _ssm_steps(
        target, "switch-release", "restart", f"把 {RELEASE_ENV_PATH} 換成 {tag}（前一版存成 release.previous.env）並重啟客服池與 RPG 池",
        _switch_release_lines(tag), timeout=300)

    steps["verify"] += _ssm_steps(
        target, "check-pools", "verify", f"驗證兩個池在回送位址上 {POOL_READY_PATH} 回 200，且都在跑 {APP_IMAGE_REPO}:{tag}",
        _pool_check_lines(), timeout=300, expect=_pool_check_expect(tag))

    selected = set(groups)
    if selected & {"build", "db-init", "migrate", "restart", "verify"}:
        selected.add("prepare")             # 在主機上執行的命令都要實例編號；找主機是唯讀的，所以一律自動帶上
    plan: list[Step] = []
    for group in DEPLOY_GROUPS:
        if group in selected:
            plan.extend(steps[group])
    return plan


def rollback_plan(target: Target, tag: str) -> list[Step]:
    """回復：只把兩個池換回前一版的映像標籤並重啟；**不執行任何遷移或降級、不還原資料庫**（遷移是 expand-only，
    舊版程式可在新結構上運作）。`tag` 是要回到的標籤（部署時存在主機的 `release.previous.env`）；該映像不在主機上就在改任何檔案前失敗。"""
    tag = _commit_tag(tag)
    steps = [_find_host_step(target, "rollback-find-host", "prepare")]
    steps += _ssm_steps(target, "rollback-switch-release", "restart", f"把 {RELEASE_ENV_PATH} 換回 {tag} 並重啟客服池與 RPG 池（不動資料庫）",
                        _switch_release_lines(tag), timeout=300)
    steps += _ssm_steps(target, "rollback-check-pools", "verify", f"驗證兩個池在回送位址上 {POOL_READY_PATH} 回 200，且都在跑 {APP_IMAGE_REPO}:{tag}",
                        _pool_check_lines(), timeout=300, expect=_pool_check_expect(tag))
    return steps


# ---- 拆除 -------------------------------------------------------------------------------------------------


def _owned(tags: str) -> str:
    """JMESPath：只有 Environment 與 Project 兩個標籤都符合時才回傳非空（`&&` 在前者為空清單時回傳前者）；
    `tags` 是回應中標籤清單的路徑（例如 `TagList`、`Role.Tags`）。"""
    return f"{tags}[?Key=='Environment' && Value=='{ENV}'] && {tags}[?Key=='Project' && Value=='{PROJECT_TAG}'].Value"


def _owned_ec2_filters(*extra: str) -> tuple[str, ...]:
    return ("--filters", f"Name=tag:Project,Values={PROJECT_TAG}", f"Name=tag:Environment,Values={ENV}", *extra)


def _delete_step(step_id: str, group: str, title: str, argv: tuple[str, ...], find: tuple[str, ...], *, capture: str | None = None) -> Step:
    """拆除的一步：先 `find`（帶標籤的查詢），沒有符合的資源就略過；有才執行 `argv`。"""
    return Step(step_id, group, title, argv, exists_argv=find, exists_nonempty=True, run_if_found=True, capture=capture)


def predeploy_snapshot_pattern(target: Target) -> re.Pattern:
    """部署前快照的名稱：`<資料庫>-pre-<提交短碼>`（與 `snapshot_name()` 同一個格式）。最終快照（`-final-`）與其他名稱都不符。"""
    return re.compile(re.escape(names(target).db_instance) + "-pre-" + COMMIT_TAG_PATTERN.pattern)


def is_predeploy_snapshot(target: Target, name: object) -> bool:
    return isinstance(name, str) and predeploy_snapshot_pattern(target).fullmatch(name) is not None


def snapshot_listing_step(target: Target) -> Step:
    """唯讀：列出該資料庫仍存在的手動快照（部署前快照與最終快照；`--output text` 是以空白分隔的名稱）。
    執行器在拆除之前先單獨執行它、讓使用者看到清單並明確回覆要保留的例外（見 `snapshots_to_delete`）；
    拆除計畫的最後也會再列一次，顯示還留著什麼。"""
    n = names(target)
    return Step("td-list-remaining-snapshots", "verify",
                "列出該資料庫仍存在的手動快照（部署前快照與最終快照）；部署前快照在你明確回覆要保留的例外之後才會被刪，最終快照永遠不由本計畫刪除",
                _aws(target, "rds", "describe-db-snapshots", "--snapshot-type", "manual",
                     *_text(f"DBSnapshots[?starts_with(DBSnapshotIdentifier, '{n.db_instance}-')].DBSnapshotIdentifier")),
                capture="remaining-snapshots")


def snapshots_to_delete(target: Target, listed: tuple[str, ...] | list[str], keep: tuple[str, ...] | list[str] = ()) -> tuple[str, ...]:
    """要刪的部署前快照＝列出的快照中，名稱符合部署前快照格式、且不在使用者指定的例外（`keep`）內者；保持列出的順序、去除重複。
    最終快照與其他名稱一律不在結果內。`keep` 的名稱必須是列出的快照之一（打錯名稱就丟錯，不會悄悄變成「全部刪除」）。
    「沒有例外」是明確傳入空的 `keep`（呼叫端必須向使用者確認過，不能把沉默當成沒有例外）。"""
    listed_unique = tuple(dict.fromkeys(listed))
    unknown = [name for name in keep if name not in listed_unique]
    if unknown:
        raise PlanError("要保留的快照不在列出的清單內（名稱打錯了？）：" + "、".join(repr(name) for name in unknown))
    return tuple(name for name in listed_unique if is_predeploy_snapshot(target, name) and name not in keep)


def _validated_delete_snapshots(target: Target, delete_snapshots: object) -> tuple[str, ...] | None:
    if delete_snapshots is None:
        return None
    if not isinstance(delete_snapshots, (tuple, list)):
        raise PlanError("delete_snapshots 必須是 None（尚未決定）或快照名稱的 tuple")
    chosen = tuple(delete_snapshots)
    bad = [name for name in chosen if not is_predeploy_snapshot(target, name)]
    if bad:
        raise PlanError("只能刪除本資料庫的部署前快照（<資料庫>-pre-<提交短碼>），最終快照與其他名稱不在範圍內："
                        + "、".join(repr(name) for name in bad))
    if len(set(chosen)) != len(chosen):
        raise PlanError("delete_snapshots 有重複的名稱")
    return chosen


def teardown_plan(target: Target, *, final_snapshot: bool | None, today: date | None = None,
                  delete_snapshots: tuple[str, ...] | None = None) -> list[Step]:
    """拆除展示環境：只刪帶有本專案標籤（Project 與 Environment）的資源，逐項先查詢、沒有符合就略過；順序與建立相反。

    `final_snapshot` **沒有預設值**：True＝刪除 RDS 前保留最終快照（需要 `today` 讓快照名稱唯一）、False＝不保留、
    None＝尚未選擇，RDS 刪除步驟不輸出指令（`blocked`）。

    `delete_snapshots`（design D14）：要刪的部署前快照名單。None＝尚未決定，`td-delete-snapshots` 是沒有指令的 `blocked` 佔位；
    元組＝只對名單裡的快照產生 `td-delete-snapshot-<名稱>` 步驟（空元組＝明確沒有要刪的）。名稱必須是本資料庫的部署前快照
    （`<資料庫>-pre-<提交短碼>`），**最終快照永遠不由本計畫刪除**，不論 `final_snapshot` 的選擇；名單由執行器在使用者看過
    `snapshot_listing_step` 並明確回覆例外之後，以 `snapshots_to_delete` 算出。
    """
    if final_snapshot not in VALID_FINAL_SNAPSHOT or isinstance(final_snapshot, int) and not isinstance(final_snapshot, bool):
        raise PlanError("final_snapshot 必須明確是 True（保留最終快照）、False（不保留）或 None（尚未選擇）")
    chosen_snapshots = _validated_delete_snapshots(target, delete_snapshots)
    n = names(target)
    region, account = target.region, target.account_id
    final_id = None
    if final_snapshot:
        if not isinstance(today, date):
            raise PlanError("保留最終快照時必須提供日期（快照名稱要唯一）")
        final_id = assert_name(f"{n.db_instance}-final-{today:%Y%m%d}", what="最終快照名稱")

    delete = _delete_step
    steps: list[Step] = []
    # compute：告警 → 主機 → Elastic IP
    steps += [
        delete("td-delete-ec2-alarm", "compute", f"刪除告警 {n.alarm}（帶標籤才刪）",
               _aws(target, "cloudwatch", "delete-alarms", "--alarm-names", n.alarm),
               _aws(target, "cloudwatch", "list-tags-for-resource", "--resource-arn", f"arn:aws:cloudwatch:{region}:{account}:alarm:{n.alarm}", *_text(_owned("Tags")))),
        delete("td-terminate-ec2", "compute", "終止帶本專案標籤的主機（磁碟隨主機刪除）",
               _aws(target, "ec2", "terminate-instances", "--instance-ids", "{{td-instance-id}}"),
               _aws(target, "ec2", "describe-instances", *_owned_ec2_filters("Name=instance-state-name,Values=pending,running,stopping,stopped"),
                    *_text("Reservations[].Instances[].InstanceId")), capture="td-instance-id"),
        Step("td-wait-ec2-terminated", "compute", "等待帶本專案標籤的主機全部終止（Elastic IP 與安全群組要等它終止才能刪）",
             poll=Check(_aws(target, "ec2", "describe-instances",
                             *_owned_ec2_filters("Name=instance-state-name,Values=pending,running,stopping,stopped,shutting-down"),
                             *_text("length(Reservations[].Instances[])")), "0"),
             timeout_seconds=600, interval_seconds=10),
        delete("td-release-eip", "compute", "釋放帶本專案標籤的 Elastic IP（停止公有 IPv4 計費）",
               _aws(target, "ec2", "release-address", "--allocation-id", "{{td-eip-id}}"),
               _aws(target, "ec2", "describe-addresses", *_owned_ec2_filters(), *_text("Addresses[].AllocationId")), capture="td-eip-id"),
    ]

    # database：RDS（需要明確選擇是否保留最終快照）→ 子網路群組
    rds_find = _aws(target, "rds", "list-tags-for-resource", "--resource-name", f"arn:aws:rds:{region}:{account}:db:{n.db_instance}", *_text(_owned("TagList")))
    if final_snapshot is None:
        steps.append(Step("td-delete-rds", "database",
                          f"刪除 RDS {n.db_instance}——需要明確選擇是否保留最終快照（沒有預設值）：保留或不保留，擇一後才會輸出刪除指令",
                          blocked=True))
    else:
        snapshot_args = ("--final-db-snapshot-identifier", final_id) if final_snapshot else ("--skip-final-snapshot",)
        kept = f"保留最終快照 {final_id}" if final_snapshot else "不保留最終快照（資料庫內容永久消失）"
        steps.append(delete("td-delete-rds", "database", f"刪除 RDS {n.db_instance}（{kept}；自動備份一併刪除）",
                            _aws(target, "rds", "delete-db-instance", "--db-instance-identifier", n.db_instance, *snapshot_args, "--delete-automated-backups"),
                            rds_find))
    steps += [
        Step("td-wait-rds-deleted", "database", f"等待 RDS {n.db_instance} 消失（刪除約數分鐘，保留最終快照時更久）",
             poll=Check(_aws(target, "rds", "describe-db-instances", *_text(f"length(DBInstances[?DBInstanceIdentifier=='{n.db_instance}'])")), "0"),
             timeout_seconds=TEARDOWN_RDS_TIMEOUT_SECONDS, interval_seconds=30),
        delete("td-delete-db-subnet-group", "database", f"刪除資料庫子網路群組 {n.subnet_group}（帶標籤才刪）",
               _aws(target, "rds", "delete-db-subnet-group", "--db-subnet-group-name", n.subnet_group),
               _aws(target, "rds", "list-tags-for-resource", "--resource-name", f"arn:aws:rds:{region}:{account}:subgrp:{n.subnet_group}", *_text(_owned("TagList")))),
    ]

    # snapshots：部署前快照（RDS 消失之後；名單由使用者看過清單、指定例外之後決定，見 D14）；最終快照不在這裡
    if chosen_snapshots is None:
        steps.append(Step("td-delete-snapshots", "snapshots",
                          f"刪除 {n.db_instance} 的部署前快照——需要先列出快照並由你明確回覆要保留的例外（「沒有例外」也要明說）後才會輸出刪除指令；最終快照不會被刪",
                          blocked=True))
    for name in chosen_snapshots or ():
        steps.append(delete(f"td-delete-snapshot-{name}", "snapshots", f"刪除部署前快照 {name}（你沒有指定保留；帶標籤才刪）",
                            _aws(target, "rds", "delete-db-snapshot", "--db-snapshot-identifier", name),
                            _aws(target, "rds", "list-tags-for-resource", "--resource-name", f"arn:aws:rds:{region}:{account}:snapshot:{name}",
                                 *_text(_owned("TagList")))))

    # network：資料庫群組（它的規則參照主機群組，所以先刪）→ 主機群組
    for step_id, name, capture in (("td-delete-db-sg", n.db_sg, "td-db-sg-id"), ("td-delete-host-sg", n.host_sg, "td-host-sg-id")):
        steps.append(delete(step_id, "network", f"刪除安全群組 {name}（名稱與標籤都符合才刪）",
                            _aws(target, "ec2", "delete-security-group", "--group-id", "{{" + capture + "}}"),
                            _aws(target, "ec2", "describe-security-groups", *_owned_ec2_filters(f"Name=group-name,Values={name}"), *_text("SecurityGroups[].GroupId")),
                            capture=capture))

    # iam：角色從設定檔移出 → 設定檔 → 內嵌政策 → 受管政策 → 角色（每一步都以角色或設定檔的標籤把關）
    role_owned = _aws(target, "iam", "get-role", "--role-name", n.role, *_text(_owned("Role.Tags")))
    profile_owned = _aws(target, "iam", "get-instance-profile", "--instance-profile-name", n.instance_profile, *_text(_owned("InstanceProfile.Tags")))
    in_profile = f"InstanceProfile.Roles[?RoleName=='{n.role}'] && {_owned('InstanceProfile.Tags')}"
    steps += [
        delete("td-remove-role-from-profile", "iam", f"把角色 {n.role} 從設定檔 {n.instance_profile} 移出",
               _aws(target, "iam", "remove-role-from-instance-profile", "--instance-profile-name", n.instance_profile, "--role-name", n.role),
               _aws(target, "iam", "get-instance-profile", "--instance-profile-name", n.instance_profile, *_text(in_profile))),
        delete("td-delete-instance-profile", "iam", f"刪除執行個體設定檔 {n.instance_profile}（帶標籤才刪）",
               _aws(target, "iam", "delete-instance-profile", "--instance-profile-name", n.instance_profile), profile_owned),
        delete("td-delete-role-policy", "iam", f"刪除角色 {n.role} 的內嵌政策 {n.role_policy}（角色帶標籤才刪）",
               _aws(target, "iam", "delete-role-policy", "--role-name", n.role, "--policy-name", n.role_policy), role_owned),
        delete("td-detach-ssm-policy", "iam", f"從角色 {n.role} 移除受管政策 AmazonSSMManagedInstanceCore（角色帶標籤才動）",
               _aws(target, "iam", "detach-role-policy", "--role-name", n.role, "--policy-arn", SSM_CORE_POLICY_ARN), role_owned),
        delete("td-delete-role", "iam", f"刪除角色 {n.role}（帶標籤才刪）",
               _aws(target, "iam", "delete-role", "--role-name", n.role), role_owned),
    ]

    # storage：傳檔儲存貯體（含物件）→ 機密參數
    steps.append(delete("td-delete-bucket", "storage", f"刪除傳檔儲存貯體 {n.bucket} 與其中的原始碼壓縮檔（帶標籤才刪；沒有開版本控制）",
                        _aws(target, "s3", "rb", f"s3://{n.bucket}", "--force"),
                        _aws(target, "s3api", "get-bucket-tagging", "--bucket", n.bucket, *_text(_owned("TagSet")))))
    for var in parameter_names(target):
        name = parameter_name(var)
        steps.append(delete(f"td-delete-parameter-{var}", "storage", f"刪除機密參數 {name}（帶標籤才刪）",
                            _aws(target, "ssm", "delete-parameter", "--name", name),
                            _aws(target, "ssm", "list-tags-for-resource", "--resource-type", "Parameter", "--resource-id", name, *_text(_owned("TagList")))))

    # notify：SNS 主題（訂閱隨之消失）→ 預算（最後刪，所以拆除期間費用告警仍有效）
    topic_arn = sns_topic_arn(target)
    steps += [
        delete("td-delete-sns-topic", "notify", f"刪除 SNS 主題 {n.topic}（帶標籤才刪；電子郵件訂閱隨之消失）",
               _aws(target, "sns", "delete-topic", "--topic-arn", topic_arn),
               _aws(target, "sns", "list-tags-for-resource", "--resource-arn", topic_arn, *_text(_owned("Tags")))),
        delete("td-delete-budget", "notify", f"刪除預算 {n.budget}（帶標籤才刪）",
               _aws(target, "budgets", "delete-budget", "--account-id", account, "--budget-name", n.budget),
               _aws(target, "budgets", "list-tags-for-resource", "--resource-arn", f"arn:aws:budgets::{account}:budget/{n.budget}", *_text(_owned("ResourceTags")))),
    ]

    # verify：列出仍保留的手動快照（唯讀）→ 帶標籤的資源已清空，且各服務沒有以專案名稱開頭的資源
    prefix = NAME_PREFIX
    steps += [
        snapshot_listing_step(target),
        Step("td-wait-tagged-resources-gone", "verify", "等待帶本專案標籤的資源清空（標籤查詢有延遲；手動快照除外），並逐一確認各服務沒有殘留",
             poll=Check(_aws(target, "resourcegroupstaggingapi", "get-resources", "--tag-filters", f"Key=Project,Values={PROJECT_TAG}",
                             f"Key=Environment,Values={ENV}", *_text("length(ResourceTagMappingList[?!contains(ResourceARN, ':snapshot:')])")), "0"),
             timeout_seconds=300, interval_seconds=15,
             verify=(
                 Check(_aws(target, "iam", "list-roles", *_text(f"length(Roles[?starts_with(RoleName, '{prefix}')])")), "0"),
                 Check(_aws(target, "iam", "list-instance-profiles", *_text(f"length(InstanceProfiles[?starts_with(InstanceProfileName, '{prefix}')])")), "0"),
                 Check(_aws(target, "s3api", "list-buckets", *_text(f"length(Buckets[?Name=='{n.bucket}'])")), "0"),
                 Check(_aws(target, "sns", "list-topics", *_text(f"length(Topics[?ends_with(TopicArn, ':{n.topic}')])")), "0"),
                 Check(_aws(target, "budgets", "describe-budgets", "--account-id", account, *_text(f"length(Budgets[?BudgetName=='{n.budget}'])")), "0"),
                 Check(_aws(target, "ssm", "describe-parameters", "--parameter-filters", f"Key=Path,Option=Recursive,Values={SSM_PREFIX}", *_text("length(Parameters)")), "0"),
             )),
    ]
    return steps
