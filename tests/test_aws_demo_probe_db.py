"""aws-demo-environment 規格「連線必須驗證伺服器憑證」：資料庫連線探測腳本（scripts/aws_demo_probe_db.py）。

探測以應用程式的連線設定（DATABASE_URL）執行 SELECT 1：成功才允許 migration；憑證無法驗證時失敗並回報類別，
**拒絕任何放寬或不合格的設定（TrustServerCertificate、關閉加密、ServerCertificate）**，輸出不含連線字串、密碼、登入名稱與主機。
單元測試用假的引擎；標記 sqlserver 的整合測試對本機 SQL Server 取得真實的驅動程式錯誤，確認分類（含在地化的訊息）。
"""
import ast
import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.config import PROJECT_ROOT
from tests.conftest import require_sqlserver_test_url
from tests.support.scripts import load_script

probe = load_script("aws_demo_probe_db")
boot = load_script("aws_demo_bootstrap_db")
plan = load_script("aws_demo_plan")

HOST = "demo-db.c0ffee123.ap-southeast-2.rds.amazonaws.com"
USER = "rac_app_sentinel"
PASSWORD = "App-Sentinel-77c1-Pw"
DRIVER = "driver=ODBC+Driver+18+for+SQL+Server"
CA = plan.RDS_CA_BUNDLE_PATH                      # 舊方案（已否決）的 ServerCertificate 值；只用來造出「應被拒絕」的連線字串
GOOD_URL = f"mssql+pyodbc://{USER}:{PASSWORD}@{HOST}:1433/rac_demo?{DRIVER}&Encrypt=yes"
SENTINELS = (HOST, USER, PASSWORD, "rac_demo", "1433")


class FakeEngine:
    def __init__(self):
        self.disposed = 0

    def dispose(self):
        self.disposed += 1


def run_probe(url, *, query=lambda engine: 1, factory=None):
    messages: list[str] = []
    engines: list[FakeEngine] = []
    received: list[str] = []

    def engine_factory(database_url):
        received.append(database_url)
        engine = FakeEngine()
        engines.append(engine)
        return engine

    code = probe.probe(
        {} if url is None else {"DATABASE_URL": url}, messages.append,
        engine_factory=factory or engine_factory, query=query,
    )
    return code, messages, engines, received


def assert_no_connection_details(messages):
    text = "\n".join(messages)
    for sentinel in SENTINELS:
        assert sentinel not in text, sentinel
    assert "mssql" not in text.lower() and "://" not in text


# ---- 成功 --------------------------------------------------------------------------------------


def test_a_good_connection_string_succeeds_and_the_report_describes_only_the_option_shape():
    code, messages, engines, received = run_probe(GOOD_URL)
    assert code == 0
    assert messages[0].startswith("成功：") and "SELECT 1" in messages[0]
    assert messages[1] == "連線選項：Encrypt=yes；沒有 ServerCertificate（以映像的系統憑證庫驗證）；沒有 TrustServerCertificate"
    assert_no_connection_details(messages)
    assert received == [GOOD_URL], "探測要用應用程式的同一個 DATABASE_URL"
    assert [engine.disposed for engine in engines] == [1]


def test_encrypt_may_be_left_out_because_the_driver_defaults_to_yes_and_the_system_store_verifies_the_certificate():
    url = f"mssql+pyodbc://{USER}:{PASSWORD}@{HOST}/rac_demo?{DRIVER}"
    code, messages, *_ = run_probe(url)
    assert code == 0
    assert "Encrypt=（未指定，驅動程式預設為 yes）" in messages[1] and "沒有 ServerCertificate" in messages[1]
    assert_no_connection_details(messages)


# ---- 拒絕放寬或不合格的設定（不連線）----------------------------------------------------------


@pytest.mark.parametrize("extra", [
    "&TrustServerCertificate=yes", "&TrustServerCertificate=YES", "&TrustServerCertificate=true", "&TrustServerCertificate=1", "&trustservercertificate=Yes",
])
def test_trusting_every_certificate_is_refused_without_connecting(extra):
    code, messages, engines, received = run_probe(f"mssql+pyodbc://{USER}:{PASSWORD}@{HOST}/rac_demo?{DRIVER}&Encrypt=yes{extra}")
    assert code == 2 and engines == [] and received == []
    assert "TrustServerCertificate" in messages[0] and messages[0].startswith("拒絕：")
    assert_no_connection_details(messages)


@pytest.mark.parametrize("encrypt", ["no", "false", "0", "optional", "NO", "Optional"])
def test_turning_encryption_off_is_refused_without_connecting(encrypt):
    code, messages, engines, _ = run_probe(f"mssql+pyodbc://{USER}:{PASSWORD}@{HOST}/rac_demo?{DRIVER}&Encrypt={encrypt}")
    assert code == 2 and engines == [] and "加密" in messages[0]
    assert_no_connection_details(messages)


def test_trust_server_certificate_no_is_not_a_relaxation_and_passes_the_guard():
    """兩個選項名稱相近：TrustServerCertificate=no 不是放寬，也不是 ServerCertificate，應通過守門。"""
    code, messages, *_ = run_probe(f"mssql+pyodbc://{USER}:{PASSWORD}@{HOST}/rac_demo?{DRIVER}&Encrypt=yes&TrustServerCertificate=no")
    assert code == 0 and "沒有 ServerCertificate" in messages[1]


@pytest.mark.parametrize("extra", [
    f"&ServerCertificate={CA}", "&servercertificate=/x.pem", "&SERVERCERTIFICATE=/etc/rac/rds-ca-bundle.pem",
    f"&ServerCertificate={CA}&TrustServerCertificate=no",
])
def test_a_connection_string_with_server_certificate_is_refused_without_connecting(extra):
    """design D7：ServerCertificate 是整份憑證的精確比對（需要 strict 加密），與 RDS 不相容——已否決，連線字串不得含它。"""
    code, messages, engines, received = run_probe(f"mssql+pyodbc://{USER}:{PASSWORD}@{HOST}/rac_demo?{DRIVER}&Encrypt=yes{extra}")
    assert code == 2 and engines == [] and received == []
    assert messages[0].startswith("拒絕：") and "ServerCertificate" in messages[0] and "系統憑證庫" in messages[0]
    assert_no_connection_details(messages)
    assert "/x.pem" not in "\n".join(messages) and CA not in "\n".join(messages)


@pytest.mark.parametrize("url", ["sqlite+pysqlite:///:memory:", "postgresql://u:p@h/db", "mysql+pymysql://u:p@h/db"])
def test_only_sql_server_connection_strings_are_accepted(url):
    code, messages, engines, _ = run_probe(url)
    assert code == 2 and engines == [] and "SQL Server" in messages[0]


@pytest.mark.parametrize("url", [None, "", "not a url", f"mssql+pyodbc://{USER}:{PASSWORD}@{HOST}:notaport/rac_demo"])
def test_a_missing_or_malformed_connection_string_is_refused_and_never_echoed(url):
    code, messages, engines, _ = run_probe(url)
    assert code == 2 and engines == [] and messages[0].startswith("拒絕：")
    assert_no_connection_details(messages)
    if url:
        assert url not in "\n".join(messages)


# ---- 失敗：只輸出類別、例外類型與 SQLSTATE -----------------------------------------------------


class _Driver(Exception):
    """假的 pyodbc 例外：第一個引數是 SQLSTATE。"""


def _failing(error):
    def query(engine):
        raise error

    return query


def test_a_failed_query_prints_category_type_and_sqlstate_but_never_the_driver_message_or_connection_details():
    message = f"[08001] [Microsoft][ODBC Driver 18 for SQL Server]SSL Provider: certificate verify failed ({HOST}) user {USER} password {PASSWORD}"
    error = OperationalError(f"SELECT 1 -- mssql+pyodbc://{USER}:{PASSWORD}@{HOST}", {}, _Driver("08001", message))
    code, messages, engines, _ = run_probe(GOOD_URL, query=_failing(error))
    assert code == 1 and len(messages) == 1
    assert messages[0] == "失敗：連線探測沒有通過（類別 tls_certificate、例外 OperationalError、SQLSTATE 08001）"
    assert_no_connection_details(messages)
    assert [engine.disposed for engine in engines] == [1], "失敗時也要釋放連線池"


def test_an_exception_raised_while_creating_the_engine_is_reported_the_same_way():
    def broken_factory(database_url):
        raise RuntimeError(f"cannot build {database_url}")

    code, messages, *_ = run_probe(GOOD_URL, factory=broken_factory)
    assert code == 1 and "例外 RuntimeError" in messages[0]
    assert_no_connection_details(messages)


def test_a_wrong_answer_to_select_one_is_a_failure():
    code, messages, *_ = run_probe(GOOD_URL, query=lambda engine: 2)
    assert code == 1 and messages == ["失敗：連線探測沒有通過（查詢結果不是 1）"]


def test_failing_to_dispose_the_engine_does_not_hide_the_result():
    class StubbornEngine(FakeEngine):
        def dispose(self):
            raise RuntimeError(PASSWORD)

    messages: list[str] = []
    code = probe.probe({"DATABASE_URL": GOOD_URL}, messages.append, engine_factory=lambda url: StubbornEngine(), query=lambda engine: 1)
    assert code == 0 and PASSWORD not in "\n".join(messages)


def test_the_script_refuses_command_line_arguments_so_the_connection_only_comes_from_the_environment():
    messages: list[str] = []
    assert probe.main(["--url", GOOD_URL], environ={"DATABASE_URL": GOOD_URL}, out=messages.append) == 2
    assert messages == ["拒絕：這支腳本不接受命令列參數（連線設定只來自環境變數 DATABASE_URL）"]


# ---- 錯誤分類與初始化腳本一致 ------------------------------------------------------------------

SAMPLES = [
    (OperationalError("x", {}, _Driver("28000", "[28000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Login failed for user 'x'. (18456)")), "login_failed"),
    (OperationalError("x", {}, _Driver("08001", "[08001] SSL Provider: [error:0A000086:SSL routines::certificate verify failed:self-signed certificate in certificate chain]")), "tls_certificate"),
    (OperationalError("x", {}, _Driver("08001", "[08001] SSL 提供者: 憑證鏈結是由不受信任的授權單位發出的。 (-2146893019)")), "tls_certificate"),
    (OperationalError("x", {}, _Driver("08001", "[08001] TCP Provider: Timeout error [258]. (258)")), "timeout"),
    (OperationalError("x", {}, _Driver("HYT00", "[HYT00] Login timeout expired (0)")), "timeout"),
    (OperationalError("x", {}, _Driver("08001", "[08001] A network-related or instance-specific error has occurred while establishing a connection to SQL Server.")), "network"),
    (OperationalError("x", {}, _Driver("IM002", "[IM002] [unixODBC][Driver Manager]Data source name not found and no default driver specified")), "driver_missing"),
    (OperationalError("x", {}, _Driver("42000", "[42000] CREATE DATABASE permission denied in database 'master'. (262)")), "permission_denied"),
    (OperationalError("x", {}, _Driver("42000", "[42000] Password validation failed. (15116)")), "password_policy"),
    (OperationalError("CREATE LOGIN x WITH PASSWORD = N'certificate-timeout'", {}, _Driver("42000", "[42000] syntax error")), "unknown"),
    (RuntimeError("something else"), "unknown"),
]


@pytest.mark.parametrize("error, category", SAMPLES)
def test_both_scripts_classify_the_same_errors_the_same_way(error, category):
    assert probe.classify_error(error) == category
    assert boot.classify_error(error) == category
    assert probe.sqlstate_of(error) == boot.sqlstate_of(error)


def test_the_two_copies_of_the_classification_rules_are_identical():
    assert probe.TLS_WORDS.pattern == boot.TLS_WORDS.pattern and probe.TIMEOUT_WORDS.pattern == boot.TIMEOUT_WORDS.pattern


# ---- 預設的引擎與查詢 --------------------------------------------------------------------------


def test_the_default_query_is_select_one_expressed_without_raw_sql():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        assert probe.select_one(engine) == 1
    finally:
        engine.dispose()
    tree = ast.parse((PROJECT_ROOT / "scripts" / "aws_demo_probe_db.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"text", "open", "exec", "eval"}, node.func.id


def test_the_default_engine_comes_from_the_application_factory():
    engine = probe.default_engine_factory("sqlite+pysqlite:///:memory:")
    try:
        assert engine.dialect.name == "sqlite" and probe.select_one(engine) == 1
    finally:
        engine.dispose()


def test_the_script_finds_the_application_package_through_the_working_directory_when_it_is_mounted_elsewhere():
    """容器內腳本由別處掛入、工作目錄是 /app：`app` 不在匯入路徑上，要退而用工作目錄。"""
    scripts_dir = str(PROJECT_ROOT / "scripts")
    code = (
        "import sys\n"
        f"sys.path.insert(0, {scripts_dir!r})\n"
        "import aws_demo_probe_db as probe\n"
        "engine = probe.default_engine_factory('sqlite+pysqlite:///:memory:')\n"
        "print(probe.select_one(engine))\n"
    )
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, cwd=PROJECT_ROOT, env=environment, timeout=120, check=False)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")[-300:]
    assert result.stdout.decode("utf-8").strip() == "1"


def test_the_script_entry_point_refuses_a_missing_connection_string_with_exit_code_2():
    environment = {key: value for key, value in os.environ.items() if key != "DATABASE_URL"}
    result = subprocess.run(
        [sys.executable, "-I", "-X", "utf8", str(PROJECT_ROOT / "scripts" / "aws_demo_probe_db.py")],
        capture_output=True, env=environment, timeout=120, check=False,
    )
    assert result.returncode == 2 and "拒絕：" in result.stdout.decode("utf-8")


def test_the_script_stays_standalone_and_never_logs():
    tree = ast.parse((PROJECT_ROOT / "scripts" / "aws_demo_probe_db.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "os", "re", "sys", "collections", "sqlalchemy", "app"}, imported


# ---- 整合：真實的 SQL Server（取得真實的驅動程式錯誤）-----------------------------------------


@pytest.mark.sqlserver
def test_against_a_real_server_an_untrusted_certificate_fails_with_the_tls_category_and_never_falls_back_to_trusting_it():
    """本機的 SQL Server 用自簽憑證：去掉 TrustServerCertificate 後，驗證必須失敗並歸為 tls_certificate（驅動程式的訊息依系統語系，這裡含中文）。
    若這台機器信任了該憑證（探測成功）也算符合期待：兩種結果都不會輸出連線細節。"""
    url = make_url(require_sqlserver_test_url())
    query = {key: value for key, value in url.query.items() if key.lower() != "trustservercertificate"}
    query["Encrypt"] = "yes"
    strict = url.set(query=query).render_as_string(hide_password=False)
    messages: list[str] = []
    code = probe.probe({"DATABASE_URL": strict}, messages.append)
    text = "\n".join(messages)
    assert code in (0, 1), text
    if code == 1:
        assert "類別 tls_certificate" in text and "SQLSTATE 08001" in text, text
    password = url.password or ""
    for secret in (password, url.host or "", url.username or ""):
        assert secret not in text


@pytest.mark.sqlserver
def test_against_a_real_server_a_wrong_password_is_classified_as_login_failed_with_sqlstate_28000():
    url = make_url(require_sqlserver_test_url()).set(password="Wrong-Sentinel-Password-123")
    engine = create_engine(url, connect_args={"timeout": 10})
    try:
        with pytest.raises(Exception) as raised:
            probe.select_one(engine)
    finally:
        engine.dispose()
    message = probe.describe_failure(raised.value)
    assert "類別 login_failed" in message and "SQLSTATE 28000" in message
    assert "Wrong-Sentinel-Password-123" not in message
    assert boot.classify_error(raised.value) == "login_failed"
