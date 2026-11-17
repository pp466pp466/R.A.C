"""AWS 展示環境的資料庫連線探測（aws-demo-environment 規格「連線必須驗證伺服器憑證」、design D7）。

在**主映像內以一次性容器執行**（與 `aws_demo_bootstrap_db.py` 相同，腳本由主機掛入、工作目錄是 `/app`）：用**應用程式的連線設定**
（環境變數 `DATABASE_URL`，並透過應用程式的引擎工廠 `app.db.dialect.create_engine_for`）對資料庫執行最簡單的查詢（`SELECT 1`），
確認連線經加密、伺服器憑證通過驗證後成功。**成功才允許進行 migration 與部署**；憑證無法驗證時停止並回報，不退而放寬。

    python aws_demo_probe_db.py

- **輸出只有成功或失敗與錯誤類別**：不輸出連線字串、密碼、登入名稱、主機位址與例外訊息；失敗時只有類別（`tls_certificate`、`login_failed`、
  `timeout`、`network`、`driver_missing`、`unknown`）、例外類型與 SQLSTATE。成功時另印出連線選項的型態（`Encrypt` 的值、沒有
  `ServerCertificate`、沒有 `TrustServerCertificate`），供記錄採用的連線字串型態（task 5.3）。
- **拒絕不合格的設定（不連線，結束代碼 2）**：`TrustServerCertificate` 為真、`Encrypt` 明確關閉或為 optional、不是 SQL Server 的連線字串、
  含 `ServerCertificate`（那是整份憑證的精確比對、需要 strict 加密，與 RDS 不相容；憑證信任改由映像的系統憑證庫提供，design D7）。
  這些情況下不嘗試任何連線，也不產生任何放寬的設定。
- 結束代碼：0 成功；1 連線或查詢失敗；2 設定不合格（沒有連線）。

腳本不匯入其他維運腳本（可單獨掛入）；錯誤分類與 `aws_demo_bootstrap_db.py` 的 `classify_error` 相同，`tests/test_aws_demo_probe_db.py` 比對兩者。
"""
from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable, Iterator, Mapping

DATABASE_URL_ENV = "DATABASE_URL"
TRUTHY = frozenset({"yes", "true", "1", "on"})
ENCRYPT_OFF = frozenset({"no", "false", "0", "off", "optional"})
SQLSTATE = re.compile(r"[0-9A-Z]{5}")
# 驅動程式的訊息會依作業系統語系在地化（例如繁體中文 Windows 的 SSL 錯誤是中文），所以除了英文，也比對不分語系的標記與錯誤碼：
# SSL／TLS、Windows 憑證錯誤碼（-2146893019 不受信任的根、-2146762487 與 -2146762496）、逾時碼 (258)。
TLS_WORDS = re.compile(r"certificate|\bssl\b|\btls\b|憑證|-2146893019|-2146762487|-2146762496")
TIMEOUT_WORDS = re.compile(r"timeout|timed out|逾時|\(258\)")


class OrphanedUser(Exception):
    """與 aws_demo_bootstrap_db.py 的分類保持相同的型別名稱（探測本身不會產生它）。"""


class ProbeRefused(Exception):
    """連線設定不合格。訊息只含原因，**絕不含連線字串的任何部分**。"""


# ---- 錯誤分類（與 aws_demo_bootstrap_db.py 相同；測試比對）---------------------------------------


def sqlstate_of(error: BaseException) -> str | None:
    for candidate in (error, getattr(error, "orig", None)):
        args = getattr(candidate, "args", None)
        if args and isinstance(args[0], str) and SQLSTATE.fullmatch(args[0]):
            return args[0]
    return None


def _chain(error: BaseException) -> Iterator[BaseException]:
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


def describe_failure(error: BaseException) -> str:
    """類別、例外類型與 SQLSTATE；**沒有例外訊息**（驅動程式的訊息可能含伺服器位址或登入名稱）。"""
    parts = [f"類別 {classify_error(error)}", f"例外 {type(error).__name__}"]
    state = sqlstate_of(error)
    if state:
        parts.append(f"SQLSTATE {state}")
    return "失敗：連線探測沒有通過（" + "、".join(parts) + "）"


# ---- 連線設定的守門（純函式）-------------------------------------------------------------------


def _query(url, name: str) -> str | None:
    for key, value in url.query.items():
        if key.lower() == name.lower():
            return (value[-1] if isinstance(value, tuple) else value).strip()
    return None


def check_options(url) -> str:
    """檢查連線字串沒有放寬驗證，回傳連線選項的型態描述（不含主機、登入名稱與密碼）。不合格就丟 `ProbeRefused`。"""
    if url.get_backend_name() != "mssql":
        raise ProbeRefused("DATABASE_URL 不是 SQL Server 的連線字串")
    trust = _query(url, "TrustServerCertificate")
    if trust is not None and trust.lower() in TRUTHY:
        raise ProbeRefused("DATABASE_URL 含 TrustServerCertificate=yes：不信任所有憑證，探測拒絕連線（也不會改用它）")
    encrypt = _query(url, "Encrypt")
    if encrypt is not None and encrypt.lower() in ENCRYPT_OFF:
        raise ProbeRefused("DATABASE_URL 的 Encrypt 被關閉：資料庫連線必須加密")
    if _query(url, "ServerCertificate") is not None:
        raise ProbeRefused("DATABASE_URL 含 ServerCertificate：它是整份憑證的精確比對（需要 strict 加密），與 RDS 不相容；憑證信任由映像的系統憑證庫提供")
    return f"Encrypt={encrypt or '（未指定，驅動程式預設為 yes）'}；沒有 ServerCertificate（以映像的系統憑證庫驗證）；沒有 TrustServerCertificate"


# ---- 探測本體 ----------------------------------------------------------------------------------


def default_engine_factory(database_url: str):
    """應用程式的引擎工廠（與應用程式使用同一套連線設定：登入逾時、pool_pre_ping 等）。容器的工作目錄是 /app，
    腳本由別處掛入時 `app` 不在匯入路徑上，所以退而把工作目錄加進去。"""
    try:
        from app.db.dialect import create_engine_for
    except ModuleNotFoundError:
        sys.path.insert(0, os.getcwd())
        from app.db.dialect import create_engine_for
    return create_engine_for(database_url)


def select_one(engine) -> int:
    """以 ORM 表達式執行最簡單的查詢（`SELECT 1`）。"""
    from sqlalchemy import literal, select

    with engine.connect() as connection:
        return connection.execute(select(literal(1))).scalar_one()


def probe(
    environ: Mapping[str, str],
    out: Callable[[str], None] = print,
    *,
    engine_factory: Callable[[str], object] = default_engine_factory,
    query: Callable[[object], int] = select_one,
) -> int:
    from sqlalchemy.engine import make_url

    raw = environ.get(DATABASE_URL_ENV, "")
    if not raw:
        out(f"拒絕：沒有設定環境變數 {DATABASE_URL_ENV}")
        return 2
    try:
        url = make_url(raw)
        options = check_options(url)
    except ProbeRefused as error:
        out(f"拒絕：{error}")
        return 2
    except Exception:  # noqa: BLE001 - 解析失敗的訊息可能回顯連線字串，只輸出固定的說明
        out(f"拒絕：{DATABASE_URL_ENV} 的格式不正確")
        return 2

    engine = None
    try:
        engine = engine_factory(raw)
        if query(engine) != 1:
            out("失敗：連線探測沒有通過（查詢結果不是 1）")
            return 1
    except Exception as error:  # noqa: BLE001 - 只輸出類別與例外類型
        out(describe_failure(error))
        return 1
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:  # noqa: BLE001
                pass
    out("成功：資料庫連線正常（SELECT 1），連線已加密且伺服器憑證通過驗證")
    out(f"連線選項：{options}")
    return 0


def main(argv: list[str] | None = None, *, environ: Mapping[str, str] | None = None, out: Callable[[str], None] = print, **hooks) -> int:
    if argv:
        out("拒絕：這支腳本不接受命令列參數（連線設定只來自環境變數 DATABASE_URL）")
        return 2
    return probe(os.environ if environ is None else environ, out, **hooks)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
