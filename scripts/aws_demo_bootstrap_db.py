"""AWS 展示環境的資料庫初始化（aws-demo-environment 規格「應用程式使用專屬的最小權限登入」、design D8）。

在**主映像內以一次性容器執行**（`scripts/` 不在映像內，腳本由主機掛入）：用 RDS 主要使用者連線，建立專屬資料庫與專屬的
應用程式登入，並讓該登入成為資料庫的擁有者（`db_owner`；Alembic 遷移要建表與改表）。之後應用程式只用這個登入。

    python aws_demo_bootstrap_db.py --host <RDS 端點> [--port 1433] [--master-user racmaster] \\
        [--db-name rac_demo] [--app-login rac_app] [--ca-bundle /etc/rac/rds-ca-bundle.pem]

- **密碼只取環境變數**：`RDS_MASTER_PASSWORD`（主要使用者）與 `DB_APP_PASSWORD`（專屬登入），不接受命令列參數（避免留在行程清單與 shell
  歷史）；不寫入日誌、輸出與例外訊息。輸出只有名稱與「已建立／已存在」，失敗時只有步驟、錯誤類別、例外類型與 SQLSTATE，**不輸出例外訊息**
  （驅動程式的訊息可能含伺服器位址或登入名稱）。
- **可重複執行**：資料庫、登入、資料庫使用者與擁有者角色各自「不存在才建立」；**已存在的登入不重設密碼**（也不動權限以外的內容）。
- **連線一律驗證伺服器憑證**：`Encrypt=yes` 加 `ServerCertificate=<CA 套件路徑>`（design D7 候選 A），**沒有任何信任所有憑證的選項**。
- 名稱經白名單驗證（只含英數與底線、不得是系統資料庫或保留名稱），語句只有下面四個，識別字不接受外部字串拼接。
- 這是維運腳本，允許使用 T-SQL 的 `CREATE LOGIN／DATABASE`（沒有 ORM 對應）；它位於 `scripts/`，不在 `tests/architecture/test_no_raw_sql.py`
  掃描的 `app/` 與 `migrations/`（與 `scripts/restore_drill.py` 的 `RESTORE` 同類）。
- 結束代碼：0 成功；1 資料庫操作失敗；2 輸入不合法或缺少必要的環境變數（沒有連線）。

腳本只依賴映像內已有的 SQLAlchemy 與 pyodbc，不匯入 `app/`，也不匯入其他維運腳本（所以可以單獨掛入）；預設值與 `scripts/aws_demo_plan.py`
的常數相同，`tests/test_aws_demo_bootstrap_db.py` 比對。
"""
from __future__ import annotations

import argparse
import contextlib
import os
import re
import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass

DEFAULT_PORT = 1433
DEFAULT_MASTER_USER = "racmaster"
DEFAULT_DB_NAME = "rac_demo"
DEFAULT_APP_LOGIN = "rac_app"
DEFAULT_CA_BUNDLE = "/etc/rac/rds-ca-bundle.pem"
MASTER_PASSWORD_ENV = "RDS_MASTER_PASSWORD"
APP_PASSWORD_ENV = "DB_APP_PASSWORD"
ODBC_DRIVER = "ODBC Driver 18 for SQL Server"
LOGIN_TIMEOUT_SECONDS = 15

IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,62}")
HOST = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")   # RDS 端點的主機名稱
PASSWORD = re.compile(r"[A-Za-z0-9_-]{12,128}")                      # 與 scripts/aws_demo.py 的 generate_password 的字元集相同（測試比對）
CA_BUNDLE_PATH = re.compile(r"/[A-Za-z0-9._/-]{1,200}")

RESERVED_DATABASES = frozenset({"master", "model", "msdb", "tempdb", "distribution"})
RESERVED_PREFIXES = ("db_", "sys", "rds")                            # 資料庫角色、系統物件與 RDS 的內部帳號
RESERVED_LOGINS = frozenset({"sa", "public", "dbo", "guest", "information_schema"})

CREATED = "created"
EXISTS = "exists"
GRANTED = "granted"

# 存在性查詢（名稱以參數傳入；識別字無法參數化，所以 DDL 另由經過白名單驗證的函式產生）
DATABASE_EXISTS = "SELECT DB_ID(?)"
LOGIN_EXISTS = "SELECT COUNT(*) FROM sys.server_principals WHERE name = ? AND type = 'S'"
USER_NAME_EXISTS = "SELECT COUNT(*) FROM sys.database_principals WHERE name = ?"
USER_MATCHES_LOGIN = (
    "SELECT COUNT(*) FROM sys.database_principals AS dp JOIN sys.server_principals AS sp ON dp.sid = sp.sid "
    "WHERE dp.name = ? AND sp.name = ?"
)
IS_OWNER_MEMBER = "SELECT IS_ROLEMEMBER('db_owner', ?)"


class BootstrapError(Exception):
    """輸入不合法（名稱、密碼、主機、路徑）。訊息只含欄位名稱與原因，**絕不含值**。"""


class StepFailed(Exception):
    """某個資料庫步驟失敗；原因在 `__cause__`（不會被輸出，只用來分類）。"""

    def __init__(self, step: str):
        super().__init__(step)
        self.step = step


class OrphanedUser(Exception):
    """資料庫內有同名的使用者，但它不是對應到這個登入（例如從別處還原的資料庫）；不自動修復。"""


# ---- 驗證（純函式）-----------------------------------------------------------------------------


def _check_identifier(name: object, field: str) -> str:
    if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
        raise BootstrapError(f"{field}：只能是英文字母開頭、只含英數與底線、最長 63 字")
    lowered = name.lower()
    if lowered.startswith(RESERVED_PREFIXES):
        raise BootstrapError(f"{field}：不得以 {'、'.join(RESERVED_PREFIXES)} 開頭（保留給系統使用）")
    return name


def validate_database_name(name: object) -> str:
    _check_identifier(name, "資料庫名稱")
    if name.lower() in RESERVED_DATABASES:
        raise BootstrapError("資料庫名稱：不得是系統資料庫")
    return name


def validate_login_name(name: object, *, master_user: str | None = None) -> str:
    _check_identifier(name, "應用程式登入名稱")
    lowered = name.lower()
    if lowered in RESERVED_LOGINS or lowered in RESERVED_DATABASES:
        raise BootstrapError("應用程式登入名稱：不得是系統保留的名稱")
    if master_user is not None and lowered == master_user.lower():
        raise BootstrapError("應用程式登入名稱：不得與 RDS 主要使用者相同（應用程式不得使用主要使用者）")
    return name


def validate_master_user(name: object) -> str:
    _check_identifier(name, "主要使用者名稱")
    if name.lower() in RESERVED_LOGINS:
        raise BootstrapError("主要使用者名稱：不得是 sa 或系統保留的名稱")
    return name


def validate_password(value: object, field: str) -> str:
    """只檢查形狀，訊息不含值。字元集與執行器產生的密碼相同：不含引號、分號、大括號、空白與非 ASCII。"""
    if not isinstance(value, str) or not PASSWORD.fullmatch(value):
        raise BootstrapError(f"{field}：長度 12 至 128 字，且只能含英數、底線與連字號")
    return value


def validate_host(value: object) -> str:
    if not isinstance(value, str) or not HOST.fullmatch(value):
        raise BootstrapError("主機：必須是主機名稱（只含英數、點與連字號）")
    return value


def validate_port(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
        raise BootstrapError("連接埠：必須是 1 至 65535")
    return value


def validate_ca_bundle(value: object) -> str:
    if not isinstance(value, str) or not CA_BUNDLE_PATH.fullmatch(value) or ".." in value:
        raise BootstrapError("憑證套件路徑：必須是絕對路徑（只含英數、點、底線、連字號與斜線）")
    return value


# ---- 語句（純函式）-----------------------------------------------------------------------------


def quote_identifier(name: str) -> str:
    """方括號識別字。名稱必須先通過白名單（這裡再檢查一次：即使呼叫者漏驗證，也不可能產生含注入內容的語句）。"""
    if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
        raise BootstrapError("識別字不合法")
    return f"[{name}]"


def create_database_statement(db_name: str) -> str:
    return f"CREATE DATABASE {quote_identifier(db_name)}"


def create_login_statement(login: str, db_name: str, password: str) -> str:
    """建立 SQL 登入。DDL 不能用參數，所以密碼放進字串常值：先通過字元集驗證（不含引號），再把單引號加倍（兩道防線）。
    `CHECK_EXPIRATION = OFF`：展示環境的登入不會因密碼到期而中斷。"""
    validate_password(password, "應用程式登入密碼")
    literal = password.replace("'", "''")
    return (
        f"CREATE LOGIN {quote_identifier(login)} WITH PASSWORD = N'{literal}', "
        f"DEFAULT_DATABASE = {quote_identifier(db_name)}, CHECK_POLICY = ON, CHECK_EXPIRATION = OFF"
    )


def create_user_statement(login: str) -> str:
    return f"CREATE USER {quote_identifier(login)} FOR LOGIN {quote_identifier(login)}"


def grant_owner_statement(login: str) -> str:
    return f"ALTER ROLE [db_owner] ADD MEMBER {quote_identifier(login)}"


# ---- 錯誤分類（只輸出固定的類別，不輸出訊息）---------------------------------------------------

SQLSTATE = re.compile(r"[0-9A-Z]{5}")
# 驅動程式的訊息會依作業系統語系在地化（例如繁體中文 Windows 的 SSL 錯誤是中文），所以除了英文，也比對不分語系的標記與錯誤碼：
# SSL／TLS、Windows 憑證錯誤碼（-2146893019 不受信任的根、-2146762487 與 -2146762496）、逾時碼 (258)。
TLS_WORDS = re.compile(r"certificate|\bssl\b|\btls\b|憑證|-2146893019|-2146762487|-2146762496")
TIMEOUT_WORDS = re.compile(r"timeout|timed out|逾時|\(258\)")


def sqlstate_of(error: BaseException) -> str | None:
    """pyodbc 的例外第一個引數是 SQLSTATE；SQLAlchemy 把它包在 `orig`。只在符合五碼格式時回傳。"""
    for candidate in (error, getattr(error, "orig", None)):
        args = getattr(candidate, "args", None)
        if args and isinstance(args[0], str) and SQLSTATE.fullmatch(args[0]):
            return args[0]
    return None


def _chain(error: BaseException) -> Iterator[BaseException]:
    """例外與它的成因（`orig`、`__cause__`、`__context__`），去重。"""
    seen: set[int] = set()
    pending: list[BaseException] = [error]
    while pending:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        for linked in (getattr(current, "orig", None), current.__cause__, current.__context__):
            if isinstance(linked, BaseException):
                pending.append(linked)


def classify_error(error: BaseException) -> str:
    """把資料庫例外歸入固定的類別（只用來決定下一步，不輸出訊息）：login_failed、tls_certificate、timeout、driver_missing、
    network、password_policy、permission_denied、orphaned_user、unknown。
    比對的文字只取驅動程式的訊息：SQLAlchemy 的包裝例外會附上執行的語句（可能含密碼），所以有 `orig` 的包裝不參與比對。"""
    chain = list(_chain(error))
    if any(isinstance(item, OrphanedUser) for item in chain):
        return "orphaned_user"
    texts = " ".join(str(item).lower() for item in chain if not isinstance(getattr(item, "orig", None), BaseException))
    states = {state for state in (sqlstate_of(item) for item in chain) if state}
    if "28000" in states or "login failed" in texts:
        return "login_failed"
    if TLS_WORDS.search(texts):
        return "tls_certificate"
    if states & {"HYT00", "HYT01"} or TIMEOUT_WORDS.search(texts):
        return "timeout"
    if "IM002" in states or "data source name not found" in texts or "can't open lib" in texts:
        return "driver_missing"
    if any(word in texts for word in ("tcp provider", "named pipes", "network-related", "connection refused", "unreachable", "could not open a connection")):
        return "network"
    if "password validation failed" in texts or "password complexity" in texts:
        return "password_policy"
    if "permission" in texts and ("denied" in texts or "deny" in texts):
        return "permission_denied"
    return "unknown"


def describe_failure(error: BaseException, step: str | None = None) -> str:
    """失敗訊息：步驟、類別、例外類型與 SQLSTATE。**沒有例外訊息**，所以不可能帶出密碼、位址或登入名稱。"""
    cause = error.__cause__ if isinstance(error, StepFailed) and error.__cause__ is not None else error
    parts = [f"類別 {classify_error(cause)}", f"例外 {type(cause).__name__}"]
    state = sqlstate_of(cause)
    if state:
        parts.append(f"SQLSTATE {state}")
    where = f"步驟「{step}」，" if step else ""
    return f"失敗：{where}{'、'.join(parts)}"


# ---- 初始化本體 --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    database: str   # created / exists
    login: str      # created / exists
    user: str       # created / exists
    role: str       # granted / exists


Connect = Callable[[str], "contextlib.AbstractContextManager"]   # 資料庫名稱 → 內容管理器，產生自動提交模式的游標


def _scalar(cursor, statement: str, params: tuple = ()) -> object:
    cursor.execute(statement, params)
    row = cursor.fetchone()
    return None if row is None else row[0]


def _run(cursor, statement: str) -> None:
    """執行並讀完所有結果集（驅動程式沒讀完就關閉游標，伺服器會中止作業而不報錯）。"""
    cursor.execute(statement)
    while cursor.nextset():
        pass


def _step(name: str, action: Callable[[], object]) -> object:
    try:
        return action()
    except BootstrapError:
        raise
    except Exception as error:
        raise StepFailed(name) from error


def bootstrap(connect: Connect, db_name: str, login: str, password: str, *, master_user: str | None = None) -> Outcome:
    """依序：資料庫 → 登入 → 資料庫使用者 → 擁有者角色；每一步都先查再做，所以可重複執行。
    登入已存在時**不重設密碼**。資料庫內已有同名但對應到別的登入的使用者（孤立）時停止，不修改。"""
    validate_database_name(db_name)
    validate_login_name(login, master_user=master_user)
    validate_password(password, "應用程式登入密碼")

    with contextlib.ExitStack() as stack:
        server = _step("連線到伺服器", lambda: stack.enter_context(connect("master")))
        database = EXISTS if _step("檢查資料庫", lambda: _scalar(server, DATABASE_EXISTS, (db_name,))) is not None else CREATED
        if database == CREATED:
            _step("建立資料庫", lambda: _run(server, create_database_statement(db_name)))
        login_state = EXISTS if _step("檢查登入", lambda: _scalar(server, LOGIN_EXISTS, (login,))) else CREATED
        if login_state == CREATED:
            _step("建立登入", lambda: _run(server, create_login_statement(login, db_name, password)))

    with contextlib.ExitStack() as stack:
        inside = _step("連線到資料庫", lambda: stack.enter_context(connect(db_name)))
        named = _step("檢查資料庫使用者", lambda: _scalar(inside, USER_NAME_EXISTS, (login,)))
        matched = _step("檢查資料庫使用者", lambda: _scalar(inside, USER_MATCHES_LOGIN, (login, login)))
        if named and not matched:
            raise StepFailed("檢查資料庫使用者") from OrphanedUser()
        user = EXISTS if named else CREATED
        if user == CREATED:
            _step("建立資料庫使用者", lambda: _run(inside, create_user_statement(login)))
        member = _step("檢查擁有者角色", lambda: _scalar(inside, IS_OWNER_MEMBER, (login,)))
        role = EXISTS if member == 1 else GRANTED
        if role == GRANTED:
            _step("授予擁有者角色", lambda: _run(inside, grant_owner_statement(login)))
    return Outcome(database, login_state, user, role)


def make_connect(admin_url) -> Connect:
    """依連線字串（SQLAlchemy URL 物件）建立連線函式：每次呼叫開一條連到指定資料庫的自動提交連線（CREATE DATABASE 不能在交易內）。"""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    @contextlib.contextmanager
    def connect(database: str):
        engine = create_engine(admin_url.set(database=database), poolclass=NullPool, connect_args={"timeout": LOGIN_TIMEOUT_SECONDS})
        try:
            raw = engine.raw_connection()
            try:
                raw.driver_connection.autocommit = True
                yield raw.cursor()
            finally:
                raw.close()
        finally:
            engine.dispose()

    return connect


def build_admin_url(host: str, port: int, master_user: str, master_password: str, ca_bundle: str):
    """主要使用者的連線：加密並驗證伺服器憑證（`ServerCertificate`），**沒有 `TrustServerCertificate`**。"""
    from sqlalchemy.engine import URL

    return URL.create(
        "mssql+pyodbc", username=master_user, password=master_password, host=host, port=port, database="master",
        query={"driver": ODBC_DRIVER, "Encrypt": "yes", "ServerCertificate": ca_bundle},
    )


# ---- 命令列 ------------------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):   # noqa: D401 - 不回顯命令列內容（萬一有人把密碼放在參數上，也不會被印出）
        self.print_usage(sys.stderr)
        sys.stderr.write("參數錯誤（內容不顯示；密碼只能由環境變數提供）。\n")
        raise SystemExit(2)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(description="AWS 展示環境的資料庫初始化（建立專屬資料庫與應用程式登入，可重複執行）。", allow_abbrev=False)
    parser.add_argument("--host", required=True, help="RDS 端點的主機名稱")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--master-user", default=DEFAULT_MASTER_USER)
    parser.add_argument("--db-name", default=DEFAULT_DB_NAME)
    parser.add_argument("--app-login", default=DEFAULT_APP_LOGIN)
    parser.add_argument("--ca-bundle", default=DEFAULT_CA_BUNDLE, help="RDS 的 CA 套件路徑（唯讀掛入容器）")
    return parser


def _label(state: str) -> str:
    return {CREATED: "已建立", EXISTS: "已存在，略過", GRANTED: "已授予"}[state]


def main(
    argv: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    out: Callable[[str], None] = print,
    connect_factory: Callable[..., Connect] = make_connect,
    file_exists: Callable[[str], bool] = os.path.isfile,
) -> int:
    args = build_parser().parse_args(argv)
    env = os.environ if environ is None else environ
    try:
        host = validate_host(args.host)
        port = validate_port(args.port)
        master_user = validate_master_user(args.master_user)
        db_name = validate_database_name(args.db_name)
        login = validate_login_name(args.app_login, master_user=master_user)
        ca_bundle = validate_ca_bundle(args.ca_bundle)
        master_password = validate_password(env.get(MASTER_PASSWORD_ENV), f"環境變數 {MASTER_PASSWORD_ENV}")
        app_password = validate_password(env.get(APP_PASSWORD_ENV), f"環境變數 {APP_PASSWORD_ENV}")
        if master_password == app_password:
            raise BootstrapError("應用程式登入密碼不得與主要使用者密碼相同")
        if not file_exists(ca_bundle):
            raise BootstrapError("憑證套件檔案不存在（沒有它就無法驗證伺服器憑證；不會改用信任所有憑證）")
    except BootstrapError as error:
        out(f"拒絕：{error}")
        return 2

    try:
        admin_url = build_admin_url(host, port, master_user, master_password, ca_bundle)
        outcome = bootstrap(connect_factory(admin_url), db_name, login, app_password, master_user=master_user)
    except BootstrapError as error:
        out(f"拒絕：{error}")
        return 2
    except StepFailed as error:
        out(describe_failure(error, error.step))
        return 1
    except Exception as error:  # noqa: BLE001 - 只輸出類別與例外類型，不輸出訊息（可能含伺服器位址或登入名稱）
        out(describe_failure(error))
        return 1

    out(f"資料庫 {db_name}：{_label(outcome.database)}")
    out(f"登入 {login}：{_label(outcome.login)}" + ("（不變更既有密碼）" if outcome.login == EXISTS else ""))
    out(f"資料庫使用者 {login}：{_label(outcome.user)}")
    out(f"擁有者角色 db_owner：{'已授予' if outcome.role == GRANTED else '已具備，略過'}")
    out("完成：資料庫初始化")
    return 0


if __name__ == "__main__":
    sys.exit(main())
