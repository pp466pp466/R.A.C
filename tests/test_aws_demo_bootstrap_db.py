"""aws-demo-environment 規格「應用程式使用專屬的最小權限登入」「連線必須驗證伺服器憑證」：
資料庫初始化腳本（scripts/aws_demo_bootstrap_db.py）的名稱與密碼驗證、語句產生、可重複執行、輸出不含機密。

單元測試用假的伺服器（不連資料庫）；最後一組標記 sqlserver 的整合測試連 TEST_SQLSERVER_URL 的本機 SQL Server 做真實的冪等驗證
（沒有設定或連不到時標為跳過，不是通過）。
"""
import ast
import contextlib
import os
import re
import subprocess
import sys

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.config import PROJECT_ROOT
from tests.conftest import require_sqlserver_test_url
from tests.support.scripts import load_script

boot = load_script("aws_demo_bootstrap_db")
plan = load_script("aws_demo_plan")
demo = load_script("aws_demo")

HOST = "demo-db.c0ffee123.ap-southeast-2.rds.amazonaws.com"
MASTER_PW = "Master-Sentinel-9f3a-Pw"
APP_PW = "App-Sentinel-77c1-Pw"
SERVER_ENV = {boot.MASTER_PASSWORD_ENV: MASTER_PW, boot.APP_PASSWORD_ENV: APP_PW}
SECRETS = (MASTER_PW, APP_PW)


# ---- 假的伺服器 --------------------------------------------------------------------------------


class FakeServer:
    """只模擬這支腳本用到的語句：存在性查詢與四種 DDL。記錄每一條語句，並可指定在某條語句失敗。"""

    def __init__(self):
        self.databases: set[str] = set()
        self.logins: dict[str, str] = {}               # 登入 → 建立時的密碼
        self.users: dict[str, dict[str, str]] = {}     # 資料庫 → {使用者: 對應的登入}
        self.owners: dict[str, set[str]] = {}          # 資料庫 → db_owner 的成員
        self.log: list[tuple[str, str]] = []           # (資料庫, 語句)
        self.fail_on: tuple[str, Exception] | None = None
        self.connect_error: tuple[str, Exception] | None = None

    @contextlib.contextmanager
    def connect(self, database):
        if self.connect_error and self.connect_error[0] == database:
            raise self.connect_error[1]
        yield FakeCursor(self, database)

    def ddl(self) -> list[str]:
        return [sql for _, sql in self.log if not sql.startswith("SELECT")]


class FakeCursor:
    def __init__(self, server: FakeServer, database: str):
        self.server, self.database, self._row = server, database, None

    def execute(self, sql, params=()):
        server = self.server
        server.log.append((self.database, sql))
        if server.fail_on and server.fail_on[0] in sql:
            raise server.fail_on[1]
        users = server.users.setdefault(self.database, {})
        if sql == boot.DATABASE_EXISTS:
            self._row = (1 if params[0] in server.databases else None,)
        elif sql == boot.LOGIN_EXISTS:
            self._row = (1 if params[0] in server.logins else 0,)
        elif sql == boot.USER_NAME_EXISTS:
            self._row = (1 if params[0] in users else 0,)
        elif sql == boot.USER_MATCHES_LOGIN:
            self._row = (1 if users.get(params[0]) == params[1] else 0,)
        elif sql == boot.IS_OWNER_MEMBER:
            self._row = (1 if params[0] in server.owners.get(self.database, set()) else 0,)
        elif match := re.fullmatch(r"CREATE DATABASE \[(\w+)\]", sql):
            server.databases.add(match.group(1))
        elif match := re.fullmatch(r"CREATE LOGIN \[(\w+)\] WITH PASSWORD = N'([^']*)', .*", sql):
            server.logins[match.group(1)] = match.group(2)
        elif match := re.fullmatch(r"CREATE USER \[(\w+)\] FOR LOGIN \[(\w+)\]", sql):
            users[match.group(1)] = match.group(2)
        elif match := re.fullmatch(r"ALTER ROLE \[db_owner\] ADD MEMBER \[(\w+)\]", sql):
            server.owners.setdefault(self.database, set()).add(match.group(1))
        else:
            raise AssertionError(f"腳本執行了預期之外的語句：{sql}")

    def fetchone(self):
        return self._row

    def nextset(self):
        return False


def run(server: FakeServer, password: str = APP_PW, db: str = "rac_demo", login: str = "rac_app"):
    return boot.bootstrap(server.connect, db, login, password, master_user="racmaster")


# ---- 名稱與密碼的白名單 ------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["rac_demo", "Rac_Demo2", "a", "x" * 63])
def test_ordinary_database_names_are_accepted(name):
    assert boot.validate_database_name(name) == name


@pytest.mark.parametrize("name", [
    "", "1abc", "_abc", "a b", "a;b", "a]b", "[a]", "a'b", 'a"b', "a-b", "a.b", "x" * 64, "專案", "rac_demo\n", "a\x00b",
    "master", "MODEL", "msdb", "tempdb", "rdsadmin", "db_owner", "sysadmin", "systemdb", None, 5,
])
def test_names_outside_the_whitelist_or_reserved_are_refused_as_database_names(name):
    with pytest.raises(boot.BootstrapError):
        boot.validate_database_name(name)


@pytest.mark.parametrize("name", ["", "a b", "x;y", "a]b", "sa", "SA", "dbo", "public", "guest", "master", "rdsadmin", "db_owner", "sysadmin", "專案"])
def test_names_outside_the_whitelist_or_reserved_are_refused_as_login_names(name):
    with pytest.raises(boot.BootstrapError):
        boot.validate_login_name(name)


def test_the_application_login_may_never_be_the_master_user_in_any_letter_case():
    with pytest.raises(boot.BootstrapError):
        boot.validate_login_name("racmaster", master_user="racmaster")
    with pytest.raises(boot.BootstrapError):
        boot.validate_login_name("RacMaster", master_user="racmaster")
    assert boot.validate_login_name("rac_app", master_user="racmaster") == "rac_app"


@pytest.mark.parametrize("name", ["sa", "SA", "rdsadmin", "a b", "x;y"])
def test_the_master_user_may_not_be_sa_or_unsafe(name):
    with pytest.raises(boot.BootstrapError):
        boot.validate_master_user(name)


def test_every_password_the_runner_generates_passes_the_password_check():
    for _ in range(200):
        assert boot.validate_password(demo.generate_password(), "x")


@pytest.mark.parametrize("value", [
    "short-1", "a" * 11, "a" * 129, "has space in-it-123", "has'quote-123456", 'has"quote-123456', "semi;colon-123456", "brace}close-1234", "brace{open-12345",
    "new\nline-1234567", "tab\there-12345", "非ASCII密碼-123456789", "slash/back\\-12345", "", None, 12345678901234,
])
def test_passwords_with_unsafe_or_wrong_shaped_characters_are_refused_and_the_message_never_contains_the_value(value):
    with pytest.raises(boot.BootstrapError) as raised:
        boot.validate_password(value, "環境變數 DB_APP_PASSWORD")
    if isinstance(value, str) and len(value) > 3:
        assert value not in str(raised.value)
    assert "DB_APP_PASSWORD" in str(raised.value)


@pytest.mark.parametrize("host", ["demo-db.c0ffee123.ap-southeast-2.rds.amazonaws.com", "localhost", "10.0.0.5"])
def test_host_names_are_accepted(host):
    assert boot.validate_host(host) == host


@pytest.mark.parametrize("host", ["", "host name", "host;DROP", "host,1433", "host:1433", "-leading.example", "trailing.example-", "a" * 254, "主機", None])
def test_anything_that_is_not_a_plain_host_name_is_refused(host):
    with pytest.raises(boot.BootstrapError):
        boot.validate_host(host)


@pytest.mark.parametrize("port", [0, -1, 65536, "1433", None, True, 1.5])
def test_ports_must_be_integers_in_range(port):
    with pytest.raises(boot.BootstrapError):
        boot.validate_port(port)


@pytest.mark.parametrize("path", ["rds-ca.pem", "/etc/../etc/passwd", "/etc/rac/rds ca.pem", "/etc/rac/rds;ca.pem", "/etc/rac/{x}.pem", "/", "", None])
def test_the_ca_bundle_path_must_be_a_plain_absolute_path(path):
    with pytest.raises(boot.BootstrapError):
        boot.validate_ca_bundle(path)


def test_the_defaults_are_the_constants_of_the_plan_module():
    """腳本在映像內單獨執行、不匯入計畫模組，所以預設值各寫一份；這裡確認兩邊沒有分岔。"""
    assert boot.DEFAULT_DB_NAME == plan.DB_NAME
    assert boot.DEFAULT_APP_LOGIN == plan.DB_APP_LOGIN
    assert boot.DEFAULT_MASTER_USER == plan.RDS_MASTER_USER
    assert boot.DEFAULT_PORT == plan.RDS_PORT
    assert boot.DEFAULT_CA_BUNDLE == plan.RDS_CA_BUNDLE_PATH
    assert {boot.MASTER_PASSWORD_ENV, boot.APP_PASSWORD_ENV} == set(plan.DB_ONLY_SECRETS)
    target = plan.Target(account_id="123456789012", notify_emails=("ops@example.com",))
    assert set(plan.unit_parameter_names(target, "bootstrap")) == {boot.MASTER_PASSWORD_ENV, boot.APP_PASSWORD_ENV}
    boot.validate_database_name(plan.DB_NAME)
    boot.validate_login_name(plan.DB_APP_LOGIN, master_user=plan.RDS_MASTER_USER)
    boot.validate_master_user(plan.RDS_MASTER_USER)


# ---- 語句 --------------------------------------------------------------------------------------


def test_the_four_statements_are_exactly_these_and_quote_every_identifier():
    assert boot.create_database_statement("rac_demo") == "CREATE DATABASE [rac_demo]"
    assert boot.create_login_statement("rac_app", "rac_demo", APP_PW) == (
        f"CREATE LOGIN [rac_app] WITH PASSWORD = N'{APP_PW}', DEFAULT_DATABASE = [rac_demo], CHECK_POLICY = ON, CHECK_EXPIRATION = OFF"
    )
    assert boot.create_user_statement("rac_app") == "CREATE USER [rac_app] FOR LOGIN [rac_app]"
    assert boot.grant_owner_statement("rac_app") == "ALTER ROLE [db_owner] ADD MEMBER [rac_app]"


@pytest.mark.parametrize("name", ["a]; DROP DATABASE x --", "a b", "x'y", "", "[a]", "專案", None])
def test_no_statement_can_be_produced_from_an_unsafe_identifier_even_when_the_caller_forgot_to_validate(name):
    for build in (boot.create_database_statement, boot.create_user_statement, boot.grant_owner_statement, boot.quote_identifier):
        with pytest.raises(boot.BootstrapError):
            build(name)
    with pytest.raises(boot.BootstrapError):
        boot.create_login_statement(name, "rac_demo", APP_PW)
    with pytest.raises(boot.BootstrapError):
        boot.create_login_statement("rac_app", name, APP_PW)


@pytest.mark.parametrize("password", ["x'; DROP LOGIN sa; --1234", "has space in-it-123", "a" * 11])
def test_the_login_statement_refuses_a_password_that_could_break_out_of_the_literal(password):
    with pytest.raises(boot.BootstrapError):
        boot.create_login_statement("rac_app", "rac_demo", password)


def test_the_login_statement_has_no_exemption_from_expiry_policy_beyond_the_expiry_flag():
    statement = boot.create_login_statement("rac_app", "rac_demo", APP_PW)
    assert "CHECK_POLICY = ON" in statement and "CHECK_EXPIRATION = OFF" in statement
    assert "sysadmin" not in statement.lower() and "MUST_CHANGE" not in statement


# ---- 流程：建立、略過、中途失敗 ----------------------------------------------------------------


def test_the_first_run_creates_the_database_then_the_login_then_the_user_then_the_owner_role_in_that_order():
    server = FakeServer()
    outcome = run(server)
    assert outcome == boot.Outcome(boot.CREATED, boot.CREATED, boot.CREATED, boot.GRANTED)
    assert server.ddl() == [
        "CREATE DATABASE [rac_demo]",
        boot.create_login_statement("rac_app", "rac_demo", APP_PW),
        "CREATE USER [rac_app] FOR LOGIN [rac_app]",
        "ALTER ROLE [db_owner] ADD MEMBER [rac_app]",
    ]
    where = {sql: database for database, sql in server.log if not sql.startswith("SELECT")}
    assert where["CREATE DATABASE [rac_demo]"] == "master"
    assert where[boot.create_login_statement("rac_app", "rac_demo", APP_PW)] == "master"
    assert where["CREATE USER [rac_app] FOR LOGIN [rac_app]"] == "rac_demo" and where["ALTER ROLE [db_owner] ADD MEMBER [rac_app]"] == "rac_demo"


def test_the_second_run_executes_no_ddl_at_all_and_never_resets_the_existing_logins_password():
    server = FakeServer()
    run(server)
    before = list(server.log)
    outcome = run(server, password="Another-Sentinel-55d2-Pw")
    assert outcome == boot.Outcome(boot.EXISTS, boot.EXISTS, boot.EXISTS, boot.EXISTS)
    assert server.ddl() == [sql for _, sql in before if not sql.startswith("SELECT")], "第二次不應再執行任何 DDL"
    assert server.logins["rac_app"] == APP_PW, "既有登入的密碼不得被重設"
    assert not any("ALTER LOGIN" in sql for _, sql in server.log)


def test_it_fills_in_only_what_is_missing():
    server = FakeServer()
    server.databases.add("rac_demo")
    outcome = run(server)
    assert outcome == boot.Outcome(boot.EXISTS, boot.CREATED, boot.CREATED, boot.GRANTED)
    assert not any(sql.startswith("CREATE DATABASE") for sql in server.ddl())

    server = FakeServer()
    run(server)
    server.owners["rac_demo"].clear()      # 角色被移除：只補角色
    server.log.clear()
    assert run(server) == boot.Outcome(boot.EXISTS, boot.EXISTS, boot.EXISTS, boot.GRANTED)
    assert server.ddl() == ["ALTER ROLE [db_owner] ADD MEMBER [rac_app]"]


def test_a_same_named_database_user_mapped_to_another_login_stops_the_run_and_changes_nothing():
    server = FakeServer()
    server.databases.add("rac_demo")
    server.logins["rac_app"] = "old"
    server.users["rac_demo"] = {"rac_app": "some_other_login"}
    with pytest.raises(boot.StepFailed) as raised:
        run(server)
    assert raised.value.step == "檢查資料庫使用者"
    assert boot.classify_error(raised.value.__cause__) == "orphaned_user"
    assert server.ddl() == [] and server.users["rac_demo"] == {"rac_app": "some_other_login"}


def test_a_failing_step_stops_everything_after_it_and_names_the_step():
    for fragment, step, executed in (
        ("CREATE DATABASE", "建立資料庫", 0),
        ("CREATE LOGIN", "建立登入", 1),
        ("CREATE USER", "建立資料庫使用者", 2),
        ("ALTER ROLE", "授予擁有者角色", 3),
    ):
        server = FakeServer()
        server.fail_on = (fragment, RuntimeError(f"boom {APP_PW}"))
        with pytest.raises(boot.StepFailed) as raised:
            run(server)
        assert raised.value.step == step, fragment
        assert len(server.ddl()) == executed + 1, fragment      # 失敗的那一條有被送出，之後的都沒有


def test_connection_failures_are_reported_as_their_own_steps():
    server = FakeServer()
    server.connect_error = ("master", ConnectionError("no route"))
    with pytest.raises(boot.StepFailed) as raised:
        run(server)
    assert raised.value.step == "連線到伺服器" and server.log == []

    server = FakeServer()
    server.connect_error = ("rac_demo", ConnectionError("no route"))
    with pytest.raises(boot.StepFailed) as raised:
        run(server)
    assert raised.value.step == "連線到資料庫"
    assert server.ddl()[:2] == ["CREATE DATABASE [rac_demo]", boot.create_login_statement("rac_app", "rac_demo", APP_PW)], "伺服器層已完成的部分保留，重跑即可補完"


def test_validation_runs_before_any_connection():
    server = FakeServer()
    for args in (("master", "rac_app", APP_PW), ("rac_demo", "sa", APP_PW), ("rac_demo", "rac_app", "short")):
        with pytest.raises(boot.BootstrapError):
            boot.bootstrap(server.connect, *args)
    with pytest.raises(boot.BootstrapError):
        boot.bootstrap(server.connect, "rac_demo", "racmaster", APP_PW, master_user="racmaster")
    assert server.log == []


# ---- 主程式：輸入、連線字串與輸出 --------------------------------------------------------------


def invoke(argv, environ=None, *, server=None, ca_exists=True, factory=None):
    messages: list[str] = []
    seen: dict = {}
    server = server or FakeServer()

    def connect_factory(url):
        seen["url"] = url
        return server.connect

    code = boot.main(
        argv, environ=SERVER_ENV if environ is None else environ, out=messages.append,
        connect_factory=factory or connect_factory, file_exists=lambda path: ca_exists,
    )
    return code, messages, seen, server


def test_a_successful_run_reports_names_and_states_only():
    code, messages, seen, server = invoke(["--host", HOST])
    assert code == 0
    assert messages == [
        "資料庫 rac_demo：已建立", "登入 rac_app：已建立", "資料庫使用者 rac_app：已建立", "擁有者角色 db_owner：已授予", "完成：資料庫初始化",
    ]
    code, messages, *_ = invoke(["--host", HOST], server=server)
    assert code == 0 and "登入 rac_app：已存在，略過（不變更既有密碼）" in messages and "擁有者角色 db_owner：已具備，略過" in messages


def test_the_connection_verifies_the_server_certificate_and_has_no_way_to_trust_everything():
    _, _, seen, _ = invoke(["--host", HOST, "--port", "1433"])
    url = seen["url"]
    rendered = url.render_as_string(hide_password=False)
    assert url.query == {"driver": "ODBC Driver 18 for SQL Server", "Encrypt": "yes", "ServerCertificate": plan.RDS_CA_BUNDLE_PATH}
    assert "TrustServerCertificate".lower() not in rendered.lower()
    assert url.username == "racmaster" and url.password == MASTER_PW and url.host == HOST and url.port == 1433
    assert url.username != "sa" and url.password != APP_PW, "用主要使用者連線，而不是應用程式登入"


def test_there_is_no_command_line_option_that_relaxes_verification_or_takes_a_password(capsys):
    options = {action.dest for action in boot.build_parser()._actions}
    assert options == {"help", "host", "port", "master_user", "db_name", "app_login", "ca_bundle"}
    for argv in (["--host", HOST, "--trust-server-certificate"], ["--host", HOST, "--password=" + APP_PW], ["--host", HOST, "--password", APP_PW], ["--host", HOST, "--pass", APP_PW]):
        with pytest.raises(SystemExit) as raised:
            boot.main(argv, environ=SERVER_ENV, out=lambda text: None)
        assert raised.value.code == 2
    captured = capsys.readouterr()
    assert APP_PW not in captured.out + captured.err and "trust-server" not in captured.out + captured.err, "錯誤訊息不回顯命令列內容"


@pytest.mark.parametrize("argv, environ, ca_exists", [
    (["--host", HOST], {}, True),                                                                              # 沒有任何密碼
    (["--host", HOST], {boot.MASTER_PASSWORD_ENV: MASTER_PW}, True),                                           # 少應用程式密碼
    (["--host", HOST], {boot.APP_PASSWORD_ENV: APP_PW}, True),                                                 # 少主要使用者密碼
    (["--host", HOST], {boot.MASTER_PASSWORD_ENV: MASTER_PW, boot.APP_PASSWORD_ENV: MASTER_PW}, True),         # 兩個密碼相同
    (["--host", HOST], {boot.MASTER_PASSWORD_ENV: "short", boot.APP_PASSWORD_ENV: APP_PW}, True),
    (["--host", HOST], {boot.MASTER_PASSWORD_ENV: MASTER_PW, boot.APP_PASSWORD_ENV: "has;semi-colon-12345"}, True),
    (["--host", "bad host"], None, True),
    (["--host", HOST, "--db-name", "master"], None, True),
    (["--host", HOST, "--db-name", "x];DROP"], None, True),
    (["--host", HOST, "--app-login", "sa"], None, True),
    (["--host", HOST, "--app-login", "racmaster"], None, True),
    (["--host", HOST, "--master-user", "sa"], None, True),
    (["--host", HOST, "--port", "70000"], None, True),
    (["--host", HOST, "--ca-bundle", "relative.pem"], None, True),
    (["--host", HOST], None, False),                                                                           # 憑證套件檔案不存在
])
def test_bad_input_is_refused_with_exit_code_2_before_any_connection_and_without_echoing_secrets(argv, environ, ca_exists):
    def must_not_connect(url):
        raise AssertionError("輸入不合法時不得連線")

    code, messages, *_ = invoke(argv, environ, ca_exists=ca_exists, factory=must_not_connect)
    assert code == 2 and messages and all(message.startswith("拒絕：") for message in messages)
    text = "\n".join(messages)
    assert not any(secret in text for secret in SECRETS)
    assert "has;semi" not in text and "short" not in text.replace("shortened", "")


def test_the_missing_ca_bundle_never_turns_into_trusting_everything():
    code, messages, seen, _ = invoke(["--host", HOST], ca_exists=False)
    assert code == 2 and seen == {} and any("憑證套件" in message for message in messages)


@pytest.mark.parametrize("error", [
    RuntimeError(f"password={MASTER_PW} app={APP_PW} server={HOST} user=racmaster"),
    OperationalError(f"CREATE LOGIN [rac_app] WITH PASSWORD = N'{APP_PW}'", {}, RuntimeError(f"{HOST} {MASTER_PW}")),
    ConnectionError(f"mssql+pyodbc://racmaster:{MASTER_PW}@{HOST}:1433/master"),
])
def test_a_database_failure_prints_step_category_and_exception_type_but_never_the_message(error):
    for fragment, expected_step in ((None, "連線到伺服器"), ("CREATE LOGIN", "建立登入")):
        server = FakeServer()
        if fragment is None:
            server.connect_error = ("master", error)
        else:
            server.fail_on = (fragment, error)
        code, messages, *_ = invoke(["--host", HOST], server=server)
        text = "\n".join(messages)
        assert code == 1 and expected_step in text and type(error).__name__ in text and "類別" in text
        for forbidden in (*SECRETS, HOST, "racmaster", "CREATE LOGIN", "WITH PASSWORD"):
            assert forbidden not in text, forbidden


def test_an_unexpected_exception_outside_the_steps_is_also_reported_without_its_message():
    def exploding_factory(url):
        raise RuntimeError(f"{MASTER_PW} {HOST}")

    code, messages, *_ = invoke(["--host", HOST], factory=exploding_factory)
    text = "\n".join(messages)
    assert code == 1 and "RuntimeError" in text and MASTER_PW not in text and HOST not in text


def test_the_script_entry_point_refuses_bad_input_without_connecting_and_prints_utf8_chinese():
    environment = {key: value for key, value in os.environ.items() if key not in SERVER_ENV}
    result = subprocess.run(
        [sys.executable, "-I", "-X", "utf8", str(PROJECT_ROOT / "scripts" / "aws_demo_bootstrap_db.py"), "--host", "bad host"],
        capture_output=True, env=environment, timeout=60, check=False,
    )
    assert result.returncode == 2
    assert "拒絕：" in result.stdout.decode("utf-8")


# ---- 錯誤分類 ----------------------------------------------------------------------------------


class _Driver(Exception):
    """假的 pyodbc 例外：第一個引數是 SQLSTATE。"""


def _wrapped(state: str, message: str) -> OperationalError:
    return OperationalError("SELECT 1", {}, _Driver(state, message))


CLASSIFICATION_SAMPLES = [
    (_wrapped("28000", "[28000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Login failed for user 'x'. (18456)"), "login_failed"),
    (_wrapped("08001", "[08001] [Microsoft][ODBC Driver 18 for SQL Server]SSL Provider: [error:0A000086:SSL routines::certificate verify failed:self-signed certificate in certificate chain]"), "tls_certificate"),
    (_wrapped("08001", "[08001] [Microsoft][ODBC Driver 18 for SQL Server]SSL 提供者: 憑證鏈結是由不受信任的授權單位發出的。 (-2146893019)"), "tls_certificate"),
    (_wrapped("08001", "[08001] [Microsoft][ODBC Driver 18 for SQL Server]TCP Provider: Timeout error [258]. (258)"), "timeout"),
    (_wrapped("HYT00", "[HYT00] [Microsoft][ODBC Driver 18 for SQL Server]Login timeout expired (0)"), "timeout"),
    (_wrapped("08001", "[08001] [Microsoft][ODBC Driver 18 for SQL Server]A network-related or instance-specific error has occurred while establishing a connection to SQL Server."), "network"),
    (_wrapped("IM002", "[IM002] [unixODBC][Driver Manager]Data source name not found and no default driver specified"), "driver_missing"),
    (_wrapped("42000", "[42000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]CREATE DATABASE permission denied in database 'master'. (262)"), "permission_denied"),
    (_wrapped("42000", "[42000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Password validation failed. The password does not meet Windows policy requirements. (15116)"), "password_policy"),
    (RuntimeError("something else"), "unknown"),
    (_wrapped("42000", "[42000] syntax error near 'x'"), "unknown"),
]


@pytest.mark.parametrize("error, category", CLASSIFICATION_SAMPLES)
def test_driver_errors_are_classified_into_fixed_categories(error, category):
    assert boot.classify_error(error) == category


def test_the_statement_a_wrapper_carries_never_influences_the_category():
    """SQLAlchemy 的包裝例外會附上執行的語句；就算語句裡剛好有關鍵字（密碼是隨機的），也只看驅動程式的訊息。"""
    error = OperationalError("CREATE LOGIN [x] WITH PASSWORD = N'certificate-timeout-login-failed'", {}, _Driver("42000", "[42000] syntax error"))
    assert boot.classify_error(error) == "unknown"


def test_the_sqlstate_is_taken_from_the_driver_and_only_when_it_has_five_characters():
    assert boot.sqlstate_of(_wrapped("08001", "x")) == "08001"
    assert boot.sqlstate_of(_Driver("28000", "x")) == "28000"
    assert boot.sqlstate_of(_Driver("not a state", "x")) is None
    assert boot.sqlstate_of(RuntimeError("x")) is None


# ---- 腳本本身的邊界 ----------------------------------------------------------------------------


def test_the_script_stays_standalone_has_no_logging_and_never_opens_files():
    """腳本由主機單獨掛入映像：不得匯入 app 或其他維運腳本；也不得使用 logging 或讀寫檔案（密碼不落地、不進日誌）。"""
    tree = ast.parse((PROJECT_ROOT / "scripts" / "aws_demo_bootstrap_db.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"open", "exec", "eval", "__import__"}, node.func.id
    assert imported <= {"__future__", "argparse", "contextlib", "os", "re", "sys", "collections", "dataclasses", "sqlalchemy"}, imported


def test_the_script_lives_in_scripts_and_never_in_the_application_package():
    assert (PROJECT_ROOT / "scripts" / "aws_demo_bootstrap_db.py").is_file()
    assert not list((PROJECT_ROOT / "app").rglob("*bootstrap_db*"))
    for path in (PROJECT_ROOT / "app").rglob("*.py"):
        assert "aws_demo_bootstrap_db" not in path.read_text(encoding="utf-8"), path


# ---- 整合：真實的 SQL Server（冪等、權限、不重設密碼）------------------------------------------

IT_DB = "racbootit_db"
IT_LOGIN = "racbootit_login"


def _cleanup(cursor) -> None:
    assert IT_DB.startswith("racbootit_") and IT_LOGIN.startswith("racbootit_"), "只清除這個測試自己建立的名稱"
    cursor.execute("SELECT DB_ID(?)", (IT_DB,))
    if cursor.fetchone()[0] is not None:
        cursor.execute(f"ALTER DATABASE [{IT_DB}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
        cursor.execute(f"DROP DATABASE [{IT_DB}]")
    cursor.execute("SELECT COUNT(*) FROM sys.server_principals WHERE name = ?", (IT_LOGIN,))
    if cursor.fetchone()[0]:
        cursor.execute(f"DROP LOGIN [{IT_LOGIN}]")


@contextlib.contextmanager
def _master_cursor(url):
    engine = create_engine(url.set(database="master"), connect_args={"timeout": 10})
    try:
        try:
            raw = engine.raw_connection()
        except OperationalError:
            pytest.skip("連不到 TEST_SQLSERVER_URL 的 SQL Server，略過（不是通過）")
        try:
            raw.driver_connection.autocommit = True
            yield raw.cursor()
        finally:
            raw.close()
    finally:
        engine.dispose()


@pytest.mark.sqlserver
def test_on_a_real_sql_server_the_run_is_idempotent_gives_owner_rights_without_server_roles_and_never_resets_the_password():
    url = make_url(require_sqlserver_test_url())
    first_password, second_password = demo.generate_password(), demo.generate_password()
    assert first_password != second_password
    with _master_cursor(url) as cursor:
        _cleanup(cursor)
        try:
            connect = boot.make_connect(url)
            assert boot.bootstrap(connect, IT_DB, IT_LOGIN, first_password) == boot.Outcome(boot.CREATED, boot.CREATED, boot.CREATED, boot.GRANTED)

            app_url = url.set(username=IT_LOGIN, password=first_password, database=IT_DB)
            engine = create_engine(app_url, connect_args={"timeout": 10})
            try:
                with engine.connect() as connection:
                    assert connection.execute(text("SELECT DB_NAME()")).scalar_one() == IT_DB
                    assert connection.execute(text("SELECT IS_ROLEMEMBER('db_owner')")).scalar_one() == 1
                    assert connection.execute(text("SELECT IS_SRVROLEMEMBER('sysadmin')")).scalar_one() == 0, "應用程式登入不得有伺服器層級的管理角色"
                table = Table("bootstrap_probe", MetaData(), Column("id", Integer, primary_key=True))
                table.create(engine)       # 擁有者權限：遷移需要建表與改表
                table.drop(engine)
            finally:
                engine.dispose()

            assert boot.bootstrap(connect, IT_DB, IT_LOGIN, second_password) == boot.Outcome(boot.EXISTS, boot.EXISTS, boot.EXISTS, boot.EXISTS)

            still_works = create_engine(app_url, connect_args={"timeout": 10})
            try:
                with still_works.connect() as connection:
                    assert connection.execute(text("SELECT 1")).scalar_one() == 1
            finally:
                still_works.dispose()
            changed = create_engine(url.set(username=IT_LOGIN, password=second_password, database=IT_DB), connect_args={"timeout": 10})
            try:
                with pytest.raises(Exception) as raised:
                    changed.connect().close()
                assert boot.classify_error(raised.value) == "login_failed", "第二次執行不得把既有登入的密碼改成新的"
            finally:
                changed.dispose()
        finally:
            _cleanup(cursor)
