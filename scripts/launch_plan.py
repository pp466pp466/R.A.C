"""Google Cloud 部署計畫（見 deployment-plan 規格與 launch-readiness 的 design D2、D3）。

這個模組是**純函式**：只回傳步驟資料（`Step`），不呼叫任何程序、不讀環境變數、不含任何機密值。
`scripts/gcp_launch.py` 負責印出與（在 `--execute` 時）逐步執行；測試只比對這裡產生的資料，所以不需要 `gcloud`，
也不可能在測試中誤建資源。

- 環境變數分三類（`SECRET_VARS`、`PLAIN_VARS`、`NOT_DEPLOYED`），`.env.example` 的每個變數必須恰好屬於一類（測試守住）；
  機密由 Secret Manager 注入（`--set-secrets`，只有名稱與版本參照），一般設定以環境變數設定。
- 建立機密的值**不經命令列**：步驟只記 `stdin_from`（例如 `prompt:SECRET_KEY`），由執行器從標準輸入餵給 `gcloud ... --data-file=-`。
- 順序固定：準備 → 建置 → 備份 → migration → 部署 → 驗證 → 排程 → 告警；migration 失敗即停止（執行器不繼續）。
- 資源名稱一律帶環境名稱，staging 與 production 沒有任何相同的名稱，也沒有共用的資料庫或機密。
- 排程清單來自 `app.cli.SCHEDULED_COMMANDS`（單一來源）。
- **不建立 Cloud SQL 執行個體**：費用由使用者決定（N-9），由 C-5b 處理；這裡只對既有的執行個體備份。
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.cli import SCHEDULED_COMMANDS  # noqa: E402 - 需要先把專案根目錄放進 sys.path
from app.observability.alert_events import EVENT_CHECK_COMPLETED  # noqa: E402 - 平台告警引用的日誌事件名稱取自程式本身，改名會讓測試失敗
from app.observability.security_events import MAIL_SEND_FAILED  # noqa: E402

ENVIRONMENTS = ("staging", "production")
SCHEDULER_TIME_ZONE = "Asia/Taipei"

# ---- 環境變數的歸類（.env.example 的每個變數必須恰好出現在其中一類）----
# 機密：Secret Manager 注入，計畫與輸出只有名稱
SECRET_VARS = (
    "SECRET_KEY", "SECRET_KEY_FALLBACKS", "DATABASE_URL", "MAIL_SMTP_PASSWORD",
    "GEMINI_API_KEY", "OPENROUTER_API_KEY", "GOOGLE_OAUTH_CLIENT_SECRET",
)
# 啟動就必須有的機密（staging 與 production 缺少時應用程式啟動失敗）；其餘機密按需加上 `--with-secret`
REQUIRED_SECRETS = ("SECRET_KEY", "DATABASE_URL", "GEMINI_API_KEY")
# 一般設定：以環境變數設定。`APP_ENV` 由計畫依環境填入，其餘由呼叫者提供（`--vars-file`）
PLAIN_VARS = (
    "APP_ENV", "LOG_LEVEL", "BASE_URL",
    "MAIL_SMTP_HOST", "MAIL_SMTP_PORT", "MAIL_SMTP_SECURITY", "MAIL_SMTP_USER", "MAIL_FROM", "ALERT_EMAIL_TO",
    "GEMINI_MODEL", "GEMINI_FALLBACK_MODEL", "GEMINI_SUMMARY_MODEL", "LLM_CALL_TIMEOUT_SECONDS",
    "LLM_REQUEST_BUDGET_SECONDS", "LLM_BREAKER_SECONDS", "LLM_MAX_OUTPUT_TOKENS", "GEMINI_THINKING_BUDGET",
    "USAGE_DAILY_TOKEN_LIMIT", "USAGE_DAILY_CALL_LIMIT",
    "OPENROUTER_MODEL", "OPENROUTER_FALLBACK_MODEL", "OPENROUTER_BASE_URL", "OPENROUTER_ZDR", "OPENROUTER_JSON_SCHEMA", "OPENROUTER_REASONING",
    "ADULT_MODE_ENABLED", "ADULT_MODE_MODELS", "GOOGLE_OAUTH_CLIENT_ID",
    # gunicorn 的兩個旋鈕（正整數，預設 2 與 8）；GEMINI_BASE_URL 只給隔離的壓測服務指向假模型伺服器（production 設定了會啟動失敗）
    "WEB_CONCURRENCY", "GUNICORN_THREADS", "GEMINI_BASE_URL",
)
# 不部署：只給本機開發與測試用，絕不出現在 staging 與 production（TRUST_PROXY_HEADERS 在 staging 與 production 設定了會啟動失敗）
NOT_DEPLOYED = ("TEST_SQLSERVER_URL", "FLASK_DEBUG", "TRUST_PROXY_HEADERS")
# 已淘汰：應用程式讀到會忽略並警告，但部署計畫檔不得再含有它們
# （玩家只用 Google 登入，沒有信箱驗證；內容分級改為三種模式後，等級 3 的兩個變數被 ADULT_MODE_* 取代）
OBSOLETE_VARS = ("EMAIL_VERIFICATION_REQUIRED", "SEXUAL_CONTENT_LEVEL3_ENABLED", "SEXUAL_CONTENT_LEVEL3_MODELS")

# staging 與 production 需要由呼叫者提供的一般設定（缺少時應用程式啟動失敗，這裡先擋下）
REQUIRED_PLAIN = ("ALERT_EMAIL_TO", "MAIL_SMTP_HOST", "MAIL_SMTP_PORT", "MAIL_FROM")

SERVICE_API_LIST = (
    "run.googleapis.com", "sqladmin.googleapis.com", "secretmanager.googleapis.com", "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com", "cloudscheduler.googleapis.com", "monitoring.googleapis.com", "logging.googleapis.com",
)

GROUPS = ("secrets", "build", "migrate", "deploy", "schedule", "alerts")

# Cloud Run 單一請求逾時（秒）：gunicorn 的 worker 逾時是 60 秒、單次請求總預算 30 秒，留一倍的餘裕
SERVICE_REQUEST_TIMEOUT = 120
# 每個執行個體的並行請求數 = gunicorn 的 worker 數 × 執行緒數（gunicorn.conf.py 的初始值 2 × 8）；C-5b 依壓測調整
SERVICE_CONCURRENCY = 16
JOB_TASK_TIMEOUT_SECONDS = 600


class PlanError(ValueError):
    """計畫的輸入不合法（缺少參數、一般設定檔含機密名稱、未知的變數…）。訊息只含變數名稱，不含值。"""


@dataclass(frozen=True)
class Target:
    project: str
    region: str
    env: str
    sql_instance: str
    image_tag: str
    network: str | None = None          # Direct VPC egress：Cloud SQL for SQL Server 沒有 Cloud Run 的 unix socket，走私有 IP
    subnet: str | None = None
    min_instances: int = 0
    max_instances: int = 4
    with_secrets: tuple[str, ...] = ()  # 除必要機密之外要建立與注入的機密名稱
    notify_emails: tuple[str, ...] = ()  # 平台告警的收件人（只有 alerts 組使用，沒有預設值）
    rotate: tuple[str, ...] = ()         # 輪替：只為這些機密寫入新版本（不建立容器、服務帳號或權限）


@dataclass(frozen=True)
class Step:
    id: str
    group: str
    title: str
    argv: tuple[str, ...] = ()
    stdin_from: str | None = None       # 例如 "prompt:SECRET_KEY"；值由執行器從標準輸入餵給指令，不在 argv
    exists_argv: tuple[str, ...] | None = None   # 先執行這個 describe；成功代表資源已存在
    update_argv: tuple[str, ...] | None = None   # 已存在時改執行這個；沒有則略過這一步
    capture: str | None = None          # 把標準輸出存成這個名稱（例如服務網址）
    http_paths: tuple[str, ...] = ()    # 非空 = 對 `url_from` 擷取的網址做 GET 檢查（不是外部指令）
    url_from: str | None = None
    exists_nonempty: bool = False        # 「已存在」= 指令成功且標準輸出非空（用於 list 查詢）
    exists_capture: str | None = None    # 已存在時，把 exists_argv 的標準輸出存成這個名稱（給 update_argv 的 {{名稱}} 使用）


@dataclass(frozen=True)
class Names:
    service: str
    migrate_job: str
    repository: str
    image: str
    service_account: str
    sql_connection_hint: str


def names(target: Target) -> Names:
    env = target.env
    return Names(
        service=f"cs-web-{env}",
        migrate_job=f"cs-migrate-{env}",
        repository=f"cs-{env}",
        image=f"{target.region}-docker.pkg.dev/{target.project}/cs-{env}/app:{target.image_tag}",
        service_account=f"cs-run-{env}@{target.project}.iam.gserviceaccount.com",
        sql_connection_hint=f"{target.project}:{target.region}:{target.sql_instance}",
    )


def secret_id(target: Target, var: str) -> str:
    return f"cs-{target.env}-{var.lower().replace('_', '-')}"


def scheduled_job_name(target: Target, command: str) -> str:
    return f"cs-{command}-{target.env}"


def scheduler_name(target: Target, command: str) -> str:
    return f"cs-sched-{command}-{target.env}"


def secrets_in_use(target: Target) -> tuple[str, ...]:
    chosen = list(REQUIRED_SECRETS)
    for var in target.with_secrets:
        if var not in SECRET_VARS:
            raise PlanError(f"不是已知的機密變數：{var}")
        if var not in chosen:
            chosen.append(var)
    return tuple(chosen)


# ---- 驗證 ------------------------------------------------------------------------------


def validate(target: Target, plain: dict[str, str], *, need_plain: bool = True) -> None:
    for field_name, value in (("--project", target.project), ("--region", target.region), ("--sql-instance", target.sql_instance), ("--image-tag", target.image_tag)):
        if not value:
            raise PlanError(f"缺少 {field_name}")
    if target.env not in ENVIRONMENTS:
        raise PlanError(f"環境必須是 {' 或 '.join(ENVIRONMENTS)}")
    if target.env not in target.sql_instance:
        raise PlanError("--sql-instance 的名稱必須含環境名稱：staging 與 production 使用各自獨立的資料庫，不得共用")
    leaked = sorted(name for name in plain if name in SECRET_VARS)
    if leaked:
        raise PlanError("一般設定檔不得含機密變數（機密只走 Secret Manager）：" + "、".join(leaked))
    obsolete = sorted(name for name in plain if name in OBSOLETE_VARS)
    if obsolete:
        raise PlanError("這些變數已淘汰，請自設定檔移除：" + "、".join(obsolete))
    forbidden = sorted(name for name in plain if name in NOT_DEPLOYED)
    if forbidden:
        raise PlanError("這些變數不可部署：" + "、".join(forbidden))
    unknown = sorted(name for name in plain if name not in PLAIN_VARS)
    if unknown:
        raise PlanError("不認得的變數：" + "、".join(unknown))
    if "APP_ENV" in plain and plain["APP_ENV"] != target.env:
        raise PlanError("APP_ENV 必須與環境一致，且由計畫自動填入，不需要在設定檔中提供")
    if not need_plain:
        return
    missing = [name for name in REQUIRED_PLAIN if not plain.get(name)]
    if missing:
        raise PlanError("缺少必要的一般設定：" + "、".join(missing))
    if not plain.get("BASE_URL"):                     # Google 登入的 OAuth 回呼與管理員的重設連結都需要它
        raise PlanError("缺少必要的一般設定：BASE_URL")


def validate_notify_emails(target: Target) -> None:
    """平台告警只通知電子郵件，收件人由呼叫者提供，沒有預設值；訊息不回顯不合法的值。"""
    if not target.notify_emails:
        raise PlanError("缺少 --notify-email：平台告警的收件人沒有預設值")
    if len(set(target.notify_emails)) != len(target.notify_emails):
        raise PlanError("--notify-email 有重複的地址")
    for email in target.notify_emails:
        if not EMAIL_PATTERN.match(email):
            raise PlanError("--notify-email 必須是單純的電子郵件地址（不含顯示名稱、空白與換行）")


# ---- 旗標組裝 --------------------------------------------------------------------------


def _flag(name: str, value: str | int) -> str:
    return f"--{name}={value}"


def _env_vars_flag(target: Target, plain: dict[str, str]) -> str:
    values = {"APP_ENV": target.env, **plain}
    for delimiter in ("#", ";", "|", "~", "!", "@"):
        if not any(delimiter in key or delimiter in value for key, value in values.items()):
            break
    else:  # pragma: no cover - 實務上不會發生
        raise PlanError("一般設定的值含有所有可用的分隔字元，請簡化內容")
    body = delimiter.join(f"{key}={values[key]}" for key in sorted(values))
    return f"--set-env-vars=^{delimiter}^{body}"


def _secrets_flag(target: Target) -> str:
    return "--set-secrets=" + ",".join(f"{var}={secret_id(target, var)}:latest" for var in secrets_in_use(target))


def _network_flags(target: Target) -> tuple[str, ...]:
    if not (target.network and target.subnet):
        return ()
    return (_flag("network", target.network), _flag("subnet", target.subnet), "--vpc-egress=private-ranges-only")


def _common(target: Target) -> tuple[str, ...]:
    return (_flag("project", target.project), _flag("region", target.region))


# ---- 各組步驟 --------------------------------------------------------------------------


def _rotation_steps(target: Target) -> list[Step]:
    steps = []
    for var in target.rotate:
        if var not in SECRET_VARS:
            raise PlanError(f"不是已知的機密變數：{var}")
        sid = secret_id(target, var)
        steps.append(Step(f"add-secret-version-{sid}", "secrets", f"輪替：寫入 {sid} 的新版本（值從標準輸入讀取，不經命令列）；服務以 latest 參照，重新部署後生效",
                          ("gcloud", "secrets", "versions", "add", sid, "--data-file=-", _flag("project", target.project)), stdin_from=f"prompt:{var}"))
    return steps


def _secrets_steps(target: Target) -> list[Step]:
    if target.rotate:
        return _rotation_steps(target)
    n = names(target)
    steps = [
        Step("enable-apis", "secrets", "啟用需要的 Google Cloud API",
             ("gcloud", "services", "enable", *SERVICE_API_LIST, _flag("project", target.project))),
        Step("create-service-account", "secrets", f"建立執行用的服務帳號（{n.service_account}）",
             ("gcloud", "iam", "service-accounts", "create", f"cs-run-{target.env}", _flag("project", target.project),
              f"--display-name=cs-run-{target.env}"),
             exists_argv=("gcloud", "iam", "service-accounts", "describe", n.service_account, _flag("project", target.project))),
        Step("grant-cloudsql-client", "secrets", "授予服務帳號連線 Cloud SQL 的權限",
             ("gcloud", "projects", "add-iam-policy-binding", target.project, f"--member=serviceAccount:{n.service_account}",
              "--role=roles/cloudsql.client", "--condition=None")),
    ]
    for var in secrets_in_use(target):
        sid = secret_id(target, var)
        steps.append(Step(f"create-secret-{sid}", "secrets", f"建立機密容器 {sid}（對應環境變數 {var}）",
                          ("gcloud", "secrets", "create", sid, "--replication-policy=automatic", _flag("project", target.project)),
                          exists_argv=("gcloud", "secrets", "describe", sid, _flag("project", target.project))))
        steps.append(Step(f"add-secret-version-{sid}", "secrets", f"寫入 {sid} 的新版本（值從標準輸入讀取，不經命令列）",
                          ("gcloud", "secrets", "versions", "add", sid, "--data-file=-", _flag("project", target.project)),
                          stdin_from=f"prompt:{var}"))
        steps.append(Step(f"grant-secret-access-{sid}", "secrets", f"授予服務帳號讀取 {sid} 的權限",
                          ("gcloud", "secrets", "add-iam-policy-binding", sid, f"--member=serviceAccount:{n.service_account}",
                           "--role=roles/secretmanager.secretAccessor", _flag("project", target.project))))
    return steps


def _build_steps(target: Target) -> list[Step]:
    n = names(target)
    return [
        Step("create-repository", "build", f"建立映像庫 {n.repository}",
             ("gcloud", "artifacts", "repositories", "create", n.repository, "--repository-format=docker",
              _flag("location", target.region), _flag("project", target.project)),
             exists_argv=("gcloud", "artifacts", "repositories", "describe", n.repository, _flag("location", target.region),
                          _flag("project", target.project))),
        Step("build-image", "build", f"以 Cloud Build 建置並推送映像 {n.image}（內容依 Dockerfile 與 .dockerignore，不含 .env）",
             ("gcloud", "builds", "submit", ".", _flag("tag", n.image), _flag("project", target.project), _flag("region", target.region))),
    ]


def _job_deploy_argv(target: Target, plain: dict[str, str], job: str, command: str, args: tuple[str, ...]) -> tuple[str, ...]:
    n = names(target)
    return (
        "gcloud", "run", "jobs", "deploy", job, _flag("image", n.image), *_common(target),
        _flag("service-account", n.service_account), _env_vars_flag(target, plain), _secrets_flag(target),
        "--max-retries=0", "--parallelism=1", "--tasks=1", _flag("task-timeout", f"{JOB_TASK_TIMEOUT_SECONDS}s"),
        _flag("command", command), _flag("args", ",".join(args)), *_network_flags(target),
    )


def _migrate_steps(target: Target, plain: dict[str, str]) -> list[Step]:
    n = names(target)
    return [
        Step("backup-database", "migrate", f"migration 之前先備份資料庫（隨需備份 {target.sql_instance}）；備份完成才能繼續",
             ("gcloud", "sql", "backups", "create", _flag("instance", target.sql_instance), _flag("project", target.project))),
        Step("deploy-migrate-job", "migrate", f"建立或更新 migration 工作 {n.migrate_job}（alembic upgrade head，失敗不自動重試）",
             _job_deploy_argv(target, plain, n.migrate_job, "alembic", ("upgrade", "head"))),
        Step("run-migrate-job", "migrate", "執行 migration 並等待完成（失敗即停止，不部署新版程式）",
             ("gcloud", "run", "jobs", "execute", n.migrate_job, "--wait", *_common(target))),
    ]


def _deploy_steps(target: Target, plain: dict[str, str]) -> list[Step]:
    n = names(target)
    return [
        Step("deploy-service", "deploy", f"部署服務 {n.service}（gunicorn gthread；機密由 Secret Manager 注入）",
             ("gcloud", "run", "deploy", n.service, _flag("image", n.image), *_common(target),
              _flag("service-account", n.service_account), _env_vars_flag(target, plain), _secrets_flag(target),
              "--port=8080", _flag("timeout", SERVICE_REQUEST_TIMEOUT), _flag("concurrency", SERVICE_CONCURRENCY),
              "--cpu=1", "--memory=512Mi", _flag("min-instances", target.min_instances), _flag("max-instances", target.max_instances),
              "--ingress=all", "--allow-unauthenticated", *_network_flags(target))),
        Step("get-service-url", "deploy", "取得服務網址",
             ("gcloud", "run", "services", "describe", n.service, *_common(target), "--format=value(status.url)"),
             capture="service-url"),
        Step("verify-service", "deploy", "驗證：/healthz（存活）與 /readyz（含資料庫）都回 200",
             http_paths=("/healthz", "/readyz"), url_from="service-url"),
    ]


def _schedule_steps(target: Target, plain: dict[str, str]) -> list[Step]:
    n = names(target)
    steps: list[Step] = []
    for command, cron in SCHEDULED_COMMANDS.items():
        job = scheduled_job_name(target, command)
        sched = scheduler_name(target, command)
        steps.append(Step(f"deploy-job-{command}", "schedule", f"建立或更新排程工作 {job}（flask {command}）",
                          _job_deploy_argv(target, plain, job, "flask", ("--app", "wsgi", command))))
        steps.append(Step(f"grant-invoker-{command}", "schedule", f"授予服務帳號執行 {job} 的權限",
                          ("gcloud", "run", "jobs", "add-iam-policy-binding", job, f"--member=serviceAccount:{n.service_account}",
                           "--role=roles/run.invoker", *_common(target))))
        uri = f"https://run.googleapis.com/v2/projects/{target.project}/locations/{target.region}/jobs/{job}:run"
        create = ("gcloud", "scheduler", "jobs", "create", "http", sched, f"--schedule={cron}", _flag("time-zone", SCHEDULER_TIME_ZONE),
                  _flag("uri", uri), "--http-method=POST", f"--oauth-service-account-email={n.service_account}",
                  _flag("location", target.region), _flag("project", target.project))
        update = ("gcloud", "scheduler", "jobs", "update", "http", *create[5:])
        steps.append(Step(f"schedule-{command}", "schedule", f"排程 {sched}：{cron}（{SCHEDULER_TIME_ZONE}）",
                          create, exists_argv=("gcloud", "scheduler", "jobs", "describe", sched, _flag("location", target.region),
                                               _flag("project", target.project)), update_argv=update))
    return steps


def build_plan(target: Target, plain: dict[str, str], *, groups: tuple[str, ...] = GROUPS) -> list[Step]:
    """依固定順序產生步驟。`groups` 用來只取其中幾組（例如只看 migration），順序不變。"""
    unknown = [g for g in groups if g not in GROUPS]
    if unknown:
        raise PlanError("不認得的步驟組：" + "、".join(unknown))
    validate(target, plain, need_plain=any(g in groups for g in ("migrate", "deploy", "schedule")))
    if "alerts" in groups:
        validate_notify_emails(target)
    builders = {
        "secrets": lambda: _secrets_steps(target),
        "build": lambda: _build_steps(target),
        "migrate": lambda: _migrate_steps(target, plain),
        "deploy": lambda: _deploy_steps(target, plain),
        "schedule": lambda: _schedule_steps(target, plain),
        "alerts": lambda: alert_steps(target),
    }
    steps: list[Step] = []
    for group in GROUPS:
        if group in groups:
            steps.extend(builders[group]())
    return steps


# ---- 平台告警（platform-alerts 規格）----------------------------------------------------
# 門檻與窗口是常數，營運文件的「平台告警」表由測試與這裡比對（tests/test_platform_alerts.py）。
FIVE_XX_RATIO = 0.05                  # 5 分鐘內伺服器錯誤（5xx）占全部請求超過 5%
FIVE_XX_WINDOW_SECONDS = 300
FIVE_XX_MIN_REQUESTS = 20             # 窗口內請求數少於這個數時不通知（避免低流量時單一錯誤造成誤報）
UPTIME_PERIOD_MINUTES = 1             # 外部探測間隔
UPTIME_CONSECUTIVE_FAILURES = 3       # 連續失敗幾次才通知
UPTIME_PATHS = (("/healthz", "存活"), ("/readyz", "就緒（含資料庫）"))
MAIL_FAILED_WINDOW_SECONDS = 300
MAIL_FAILED_THRESHOLD = 3             # 窗口內 mail.send_failed 出現幾次以上時通知
CHECK_ALERTS_STALL_SECONDS = 900      # check-alerts 超過這麼久沒有成功執行（沒有心跳）時通知

EMAIL_PATTERN = re.compile(r"^[^\s@,;<>\"']+@[^\s@,;<>\"']+\.[^\s@,;<>\"']+$")


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _policy_json(policy: dict) -> str:
    return json.dumps(policy, ensure_ascii=False, separators=(",", ":"))


def _metric_name(target: Target, suffix: str) -> str:
    return f"cs-{target.env}-{suffix}"


def _scheduled_job_regex(target: Target) -> str:
    return "|".join(scheduled_job_name(target, command) for command in SCHEDULED_COMMANDS)   # 名稱只有小寫字母與連字號，不需要轉義


def alert_definitions(target: Target) -> dict[str, dict]:
    """平台告警的政策（Cloud Monitoring 的 JSON）。`{{…}}` 是執行時才知道的資源編號（外部探測），由執行器代入。"""
    n = names(target)
    service = f'resource.type="cloud_run_revision" AND resource.label."service_name"={_quote(n.service)}'
    requests = f'metric.type="run.googleapis.com/request_count" AND {service}'
    window = [{"alignmentPeriod": f"{FIVE_XX_WINDOW_SECONDS}s", "perSeriesAligner": "ALIGN_SUM", "crossSeriesReducer": "REDUCE_SUM"}]
    mail_metric = _metric_name(target, "mail-send-failed")
    heartbeat_metric = _metric_name(target, "alert-check-completed")
    prefix = f"{target.env}："
    percent = int(FIVE_XX_RATIO * 100)

    policies: dict[str, dict] = {
        "five-xx-ratio": {
            "displayName": f"{prefix}5xx 比率超過 {percent}%（{FIVE_XX_WINDOW_SECONDS // 60} 分鐘）",
            "combiner": "AND",
            "conditions": [
                {"displayName": f"5xx 占比大於 {percent}%", "conditionThreshold": {
                    "filter": f'{requests} AND metric.label."response_code_class"="5xx"', "denominatorFilter": requests,
                    "aggregations": window, "denominatorAggregations": window,
                    "comparison": "COMPARISON_GT", "thresholdValue": FIVE_XX_RATIO, "duration": "0s", "trigger": {"count": 1}}},
                {"displayName": f"窗口內請求數至少 {FIVE_XX_MIN_REQUESTS}", "conditionThreshold": {
                    "filter": requests, "aggregations": window,
                    "comparison": "COMPARISON_GE", "thresholdValue": FIVE_XX_MIN_REQUESTS, "duration": "0s", "trigger": {"count": 1}}},
            ],
        },
        "mail-send-failed": {
            "displayName": f"{prefix}寄信失敗（{MAIL_FAILED_WINDOW_SECONDS // 60} 分鐘內 {MAIL_FAILED_THRESHOLD} 次以上）",
            "combiner": "OR",
            "conditions": [{"displayName": f"{MAIL_SEND_FAILED} 達 {MAIL_FAILED_THRESHOLD} 次", "conditionThreshold": {
                "filter": f'metric.type="logging.googleapis.com/user/{mail_metric}"',
                "aggregations": [{"alignmentPeriod": f"{MAIL_FAILED_WINDOW_SECONDS}s", "perSeriesAligner": "ALIGN_SUM"}],
                "comparison": "COMPARISON_GE", "thresholdValue": MAIL_FAILED_THRESHOLD, "duration": "0s", "trigger": {"count": 1}}}],
        },
        "job-failed": {
            "displayName": f"{prefix}排程工作執行失敗",
            "combiner": "OR",
            "conditions": [{"displayName": "任一排程工作以非零代碼結束", "conditionThreshold": {
                "filter": ('metric.type="run.googleapis.com/job/completed_execution_count" AND resource.type="cloud_run_job" '
                           'AND metric.label."result"="failed" AND resource.label."job_name"=monitoring.regex.full_match('
                           + _quote(_scheduled_job_regex(target)) + ")"),
                "aggregations": [{"alignmentPeriod": "300s", "perSeriesAligner": "ALIGN_SUM"}],
                "comparison": "COMPARISON_GT", "thresholdValue": 0, "duration": "0s", "trigger": {"count": 1}}}],
        },
        "check-alerts-stalled": {
            "displayName": f"{prefix}告警檢查停擺（超過 {CHECK_ALERTS_STALL_SECONDS // 60} 分鐘沒有成功執行）",
            "combiner": "OR",
            "conditions": [{"displayName": f"{EVENT_CHECK_COMPLETED} 超過 {CHECK_ALERTS_STALL_SECONDS // 60} 分鐘沒有出現", "conditionAbsent": {
                "filter": f'metric.type="logging.googleapis.com/user/{heartbeat_metric}"',
                "aggregations": [{"alignmentPeriod": "60s", "perSeriesAligner": "ALIGN_SUM"}],
                "duration": f"{CHECK_ALERTS_STALL_SECONDS}s", "trigger": {"count": 1}}}],
        },
    }
    for path, label in UPTIME_PATHS:
        key = path.strip("/")
        policies[f"uptime-{key}"] = {
            "displayName": f"{prefix}{label}探測失敗（{path}，連續 {UPTIME_CONSECUTIVE_FAILURES} 次）",
            "combiner": "OR",
            "conditions": [{"displayName": f"{path} 連續 {UPTIME_CONSECUTIVE_FAILURES} 次探測失敗", "conditionThreshold": {
                "filter": ('metric.type="monitoring.googleapis.com/uptime_check/check_passed" AND resource.type="uptime_url" '
                           'AND metric.label."check_id"="{{uptime-' + key + '|last}}"'),
                "aggregations": [{"alignmentPeriod": f"{UPTIME_PERIOD_MINUTES * 60}s", "perSeriesAligner": "ALIGN_NEXT_OLDER",
                                  "crossSeriesReducer": "REDUCE_COUNT_FALSE", "groupByFields": ["resource.label.*"]}],
                "comparison": "COMPARISON_GT", "thresholdValue": 1, "duration": f"{UPTIME_PERIOD_MINUTES * 60 * UPTIME_CONSECUTIVE_FAILURES}s",
                "trigger": {"count": 1}}}],
        }
    return policies


def alert_steps(target: Target) -> list[Step]:
    n = names(target)
    project = _flag("project", target.project)
    steps: list[Step] = []
    channel_tokens: list[str] = []
    for index, email in enumerate(target.notify_emails):
        token = f"channel-{index}"
        channel_tokens.append("{{" + token + "}}")
        display = f"cs-{target.env}-notify-{index}"
        steps.append(Step(
            f"create-channel-{index}", "alerts", f"建立電子郵件通知管道 {display}（收件人由 --notify-email 提供，不寫入程式庫）",
            ("gcloud", "beta", "monitoring", "channels", "create", f"--display-name={display}", "--type=email",
             f"--channel-labels=email_address={email}", "--format=value(name)", project),
            exists_argv=("gcloud", "beta", "monitoring", "channels", "list", f"--filter=displayName={display}", "--format=value(name)", project),
            exists_nonempty=True, exists_capture=token, capture=token))
    metrics = (
        ("mail-send-failed", f"日誌型指標：{MAIL_SEND_FAILED} 的次數",
         f'resource.type="cloud_run_revision" AND resource.labels.service_name={_quote(n.service)} AND jsonPayload.event={_quote(MAIL_SEND_FAILED)}'),
        ("alert-check-completed", f"日誌型指標：{EVENT_CHECK_COMPLETED}（check-alerts 的心跳）的次數",
         f'resource.type="cloud_run_job" AND resource.labels.job_name={_quote(scheduled_job_name(target, "check-alerts"))} '
         f"AND jsonPayload.event={_quote(EVENT_CHECK_COMPLETED)}"),
    )
    for suffix, description, log_filter in metrics:
        metric = _metric_name(target, suffix)
        steps.append(Step(
            f"log-metric-{suffix}", "alerts", f"建立或更新{description}（{metric}）",
            ("gcloud", "logging", "metrics", "create", metric, f"--description={description}", f"--log-filter={log_filter}", project),
            exists_argv=("gcloud", "logging", "metrics", "describe", metric, project),
            update_argv=("gcloud", "logging", "metrics", "update", metric, f"--description={description}", f"--log-filter={log_filter}", project)))
    steps.append(Step("get-service-url-for-uptime", "alerts", "取得服務網址（建立外部探測用）",
                      ("gcloud", "run", "services", "describe", n.service, _flag("region", target.region), project, "--format=value(status.url)"),
                      capture="service-url"))
    for path, label in UPTIME_PATHS:
        key = path.strip("/")
        display = f"cs-{target.env}-uptime-{key}"
        steps.append(Step(
            f"uptime-{key}", "alerts", f"建立外部探測 {display}：每 {UPTIME_PERIOD_MINUTES} 分鐘請求 {path}（{label}）",
            ("gcloud", "monitoring", "uptime", "create", display, "--resource-type=uptime-url",
             "--resource-labels=host={{service-url|host}}," + f"project_id={target.project}", f"--path={path}", "--protocol=https",
             f"--period={UPTIME_PERIOD_MINUTES}", "--timeout=10", "--format=value(name)", project),
            exists_argv=("gcloud", "monitoring", "uptime", "list-configs", f"--filter=displayName={display}", "--format=value(name)", project),
            exists_nonempty=True, exists_capture=f"uptime-{key}", capture=f"uptime-{key}"))
    channels_flag = "--notification-channels=" + ",".join(channel_tokens)
    for key, policy in alert_definitions(target).items():
        display = policy["displayName"]
        body = _policy_json(policy)
        steps.append(Step(
            f"policy-{key}", "alerts", f"建立或更新告警政策：{display}",
            ("gcloud", "alpha", "monitoring", "policies", "create", f"--policy={body}", channels_flag, project),
            exists_argv=("gcloud", "alpha", "monitoring", "policies", "list", f"--filter=displayName={_quote(display)}", "--format=value(name)", project),
            exists_nonempty=True, exists_capture=f"policy-{key}",
            update_argv=("gcloud", "alpha", "monitoring", "policies", "update", "{{policy-" + key + "}}", f"--policy={body}", channels_flag, project)))
    return steps
