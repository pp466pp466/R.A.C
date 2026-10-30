"""備份還原演練（deletion-replay 規格「還原演練可重複執行並驗證」與 launch-readiness 的 design D6）。

子命令：
    python scripts/restore_drill.py drill --backup <伺服器上看得到的 .bak 路徑> --source-db <原資料庫名稱> [--ids-file 刪除.txt] [--keep]
    python scripts/restore_drill.py logs-to-ids --input <日誌.json> --output <刪除.txt>

drill：把備份還原成名稱帶 `_restore_drill` 的**暫存資料庫**（原名稱後面加 `_restore_drill`）、比對結構版本、依序以預覽與實際模式執行
`flask replay-deletions`（可附日誌取出的檔案）、再執行一次確認為 0、檢查所有已刪除的帳號都不存在，最後輸出筆數與通過／失敗，
並刪除暫存資料庫（`--keep` 保留）。
- **目標名稱不符 `*_restore_drill` 一律拒絕**，也不得與連線字串中的資料庫同名：不可能對正式資料庫還原。
- 連線字串取自環境變數 `RESTORE_DRILL_URL`（具備建立與還原資料庫權限的 SQL Server 連線；不接受命令列參數，避免留在 shell 歷史）。
- 輸出只有筆數與通過／失敗，**不輸出任何使用者資料**。
- 這是維運腳本，允許使用原生 T-SQL（RESTORE 沒有 ORM 對應；先例是 C-4c／C-4d 驗收用的備份腳本）；它不在 `app/` 之內。

logs-to-ids：把日誌（Cloud Logging 匯出的 JSON 陣列，或應用程式 stdout 的逐行 JSON）中 `account.deleted_by_request` 與
`account.inactive_purged` 事件的「使用者編號,刪除時間」取出，**丟棄其餘全部欄位**；輸出 UTF-8（無 BOM），供 `replay-deletions --ids-file` 使用。
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.observability.lifecycle_events import EVENT_DELETED_BY_REQUEST  # noqa: E402
from app.observability.security_events import ACCOUNT_INACTIVE_PURGED  # noqa: E402

SCRATCH_SUFFIX = "_restore_drill"
SCRATCH_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,100}" + SCRATCH_SUFFIX + r"$")
DELETION_EVENTS = (EVENT_DELETED_BY_REQUEST, ACCOUNT_INACTIVE_PURGED)
URL_ENV = "RESTORE_DRILL_URL"


class DrillError(Exception):
    """演練無法進行或沒有通過。訊息只含名稱與筆數，不含使用者資料。"""


# ---- 暫存資料庫名稱的守門 ------------------------------------------------------------------


def scratch_name(source_db: str) -> str:
    return f"{source_db}{SCRATCH_SUFFIX}"


def assert_scratch(target: str, *, connected_database: str | None = None) -> None:
    """只允許對名稱帶 `_restore_drill` 的暫存資料庫還原；也不得與連線字串中的資料庫同名。"""
    if not SCRATCH_PATTERN.match(target or ""):
        raise DrillError(f"拒絕：還原目標必須是名稱以 {SCRATCH_SUFFIX} 結尾的暫存資料庫（只含英文字母、數字與底線）。")
    if connected_database and target.lower() == connected_database.lower():
        raise DrillError("拒絕：還原目標不得與連線字串中的資料庫同名。")


# ---- 日誌轉檔（純函式）--------------------------------------------------------------------


def _entries(text: str) -> list[dict]:
    stripped = text.strip()
    if not stripped:
        return []
    if stripped.startswith("["):
        loaded = json.loads(stripped)
        return [entry for entry in loaded if isinstance(entry, dict)]
    entries = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue   # 非 JSON 的行（例如啟動訊息）直接略過
        if isinstance(parsed, dict):
            entries.append(parsed)
    return entries


def _when(entry: dict, payload: dict) -> datetime | None:
    raw = entry.get("timestamp") or payload.get("time") or entry.get("time")
    if not isinstance(raw, str):
        return None
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def logs_to_ids(text: str) -> list[tuple[int, str]]:
    """（使用者編號, ISO 8601 時間）。**只取這兩個欄位**，事件以外的紀錄與其餘欄位全部丟棄；結果去重並排序。"""
    found: set[tuple[int, str]] = set()
    for entry in _entries(text):
        payload = entry.get("jsonPayload") if isinstance(entry.get("jsonPayload"), dict) else entry
        if payload.get("event") not in DELETION_EVENTS:
            continue
        user_id, when = payload.get("user_id"), _when(entry, payload)
        if isinstance(user_id, int) and not isinstance(user_id, bool) and user_id > 0 and when is not None:
            found.add((user_id, when.isoformat()))
    return sorted(found)


def run_logs_to_ids(input_path: str, output_path: str, out: Callable[[str], None]) -> int:
    try:
        text = Path(input_path).read_text(encoding="utf-8-sig")
        pairs = logs_to_ids(text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        out("失敗：無法讀取或解析日誌檔案。")
        return 1
    Path(output_path).write_bytes("".join(f"{user_id},{when}\n" for user_id, when in pairs).encode("utf-8"))   # 固定 LF、UTF-8 無 BOM
    out(f"已取出 {len(pairs)} 筆「使用者編號,刪除時間」（其餘欄位全部丟棄）。")
    return 0


# ---- 演練本體（需要 SQL Server）-----------------------------------------------------------


def _run(cursor, statement: str, params: tuple = ()) -> None:
    """執行並讀完所有結果集：RESTORE 會一路回傳進度訊息，驅動程式沒讀完就關閉游標，伺服器會中止作業而不報錯。"""
    cursor.execute(statement, params)
    while cursor.nextset():
        pass


def restore_scratch(admin_url: str, backup: str, target: str) -> None:
    """把備份還原成暫存資料庫 `target`（先確認名稱，已存在的暫存資料庫先刪除）。"""
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    url = make_url(admin_url)
    assert_scratch(target, connected_database=url.database)
    engine = create_engine(url.set(database="master"))
    raw = engine.raw_connection()
    try:
        raw.driver_connection.autocommit = True
        cursor = raw.cursor()
        cursor.execute("SELECT CAST(SERVERPROPERTY('InstanceDefaultDataPath') AS NVARCHAR(400))")
        data_path = cursor.fetchone()[0]
        escaped = backup.replace("'", "''")
        cursor.execute(f"RESTORE FILELISTONLY FROM DISK = N'{escaped}'")
        files = [(row[0], row[2]) for row in cursor.fetchall()]   # (LogicalName, Type: D 資料／L 記錄)
        drop_scratch(cursor, target)
        moves = ", ".join(
            f"MOVE N'{logical}' TO N'{data_path}{target}_{index}{'.ldf' if kind == 'L' else '.mdf'}'" for index, (logical, kind) in enumerate(files)
        )
        _run(cursor, f"RESTORE DATABASE [{target}] FROM DISK = N'{escaped}' WITH {moves}, REPLACE, STATS = 100")
    finally:
        raw.close()
        engine.dispose()


def drop_scratch(cursor, target: str) -> None:
    assert_scratch(target)
    cursor.execute("SELECT DB_ID(?)", (target,))
    if cursor.fetchone()[0] is not None:
        _run(cursor, f"ALTER DATABASE [{target}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
        _run(cursor, f"DROP DATABASE [{target}]")


def drop_scratch_database(admin_url: str, target: str) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    url = make_url(admin_url)
    assert_scratch(target, connected_database=url.database)
    engine = create_engine(url.set(database="master"))
    raw = engine.raw_connection()
    try:
        raw.driver_connection.autocommit = True
        drop_scratch(raw.cursor(), target)
    finally:
        raw.close()
        engine.dispose()


def _counts(output: str) -> dict[str, int]:
    labels = {"erased": r"(?:已抹除|將抹除)：(\d+) 個帳號", "reissued": r"重新使用）：(\d+) 個", "admin": r"管理員）：(\d+) 個", "absent": r"已經不存在：(\d+) 個"}
    return {key: int(match.group(1)) for key, pattern in labels.items() if (match := re.search(pattern, output))}


def run_drill(backup: str, source_db: str, ids_file: str | None, keep: bool, out: Callable[[str], None]) -> int:
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.orm import Session

    from app import create_app
    from app.config import load_config
    from app.repositories.deletion_tombstone_repository import DeletionTombstoneRepository
    from app.repositories.user_repository import REPLAY_ELIGIBLE, UserRepository
    from app.services.deletion_replay_service import parse_replay_file

    admin_url = os.environ.get(URL_ENV, "")
    if not admin_url:
        out(f"失敗：沒有設定環境變數 {URL_ENV}（連線字串不接受命令列參數）。")
        return 1
    target = scratch_name(source_db)
    try:
        assert_scratch(target, connected_database=make_url(admin_url).database)
    except DrillError as error:
        out(str(error))
        return 2

    results: list[tuple[str, bool]] = []

    def check(name: str, ok: bool, detail: str = "") -> bool:
        results.append((name, ok))
        out(f"{'通過' if ok else '失敗'}  {name}" + (f"（{detail}）" if detail else ""))
        return ok

    try:
        restore_scratch(admin_url, backup, target)
        check("備份已還原為暫存資料庫", True)
        target_url = make_url(admin_url).set(database=target).render_as_string(hide_password=False)
        engine = create_engine(target_url)
        try:
            with engine.connect() as connection:
                version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
            from alembic.config import Config
            from alembic.script import ScriptDirectory

            script = ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini")))
            known = {revision.revision for revision in script.walk_revisions()}
            check("結構版本是程式認得的版本", version in known, f"備份為 {version}，程式最新為 {script.get_current_head()}")

            with contextlib.redirect_stdout(io.StringIO()):   # 應用程式的 JSON 日誌（含內部編號）不混進演練輸出：演練只輸出筆數與通過／失敗
                app = create_app(load_config(environ={"APP_ENV": "testing", "DATABASE_URL": target_url}))
            runner = app.test_cli_runner()
            extra_args = ["--ids-file", ids_file] if ids_file else []
            preview = runner.invoke(args=["replay-deletions", "--dry-run", *extra_args])
            if not check("預覽可執行", preview.exit_code == 0):
                raise DrillError("預覽失敗")
            planned = _counts(preview.output)
            actual = runner.invoke(args=["replay-deletions", *extra_args])
            if not check("實際重新抹除可執行", actual.exit_code == 0):
                raise DrillError("重新抹除失敗")
            done = _counts(actual.output)
            check("實際抹除的筆數與預覽相同", done.get("erased") == planned.get("erased"), f"{done.get('erased')} 個")
            again = _counts(runner.invoke(args=["replay-deletions", *extra_args]).output)
            check("第二次執行抹除 0 個（可重複執行）", again.get("erased") == 0)

            with Session(engine) as session:
                records = {user_id: when for user_id, when in DeletionTombstoneRepository(session).records()}
                for record in parse_replay_file(Path(ids_file).read_text(encoding="utf-8-sig")) if ids_file else []:
                    records[record.user_id] = max(records.get(record.user_id, record.deleted_at), record.deleted_at)
                users = UserRepository(session)
                remaining = sum(1 for user_id, when in records.items() if users.replay_status(user_id, when) == REPLAY_ELIGIBLE)
            check("所有已刪除的帳號都不存在", remaining == 0, f"紀錄 {len(records)} 筆，仍存在 {remaining} 個")
        finally:
            engine.dispose()
    except DrillError:
        pass
    except Exception as error:  # noqa: BLE001 - 只輸出例外類型，不輸出訊息（可能含連線資訊）
        check("演練過程沒有未預期的錯誤", False, type(error).__name__)
    finally:
        if not keep:
            try:
                drop_scratch_database(admin_url, target)
                out(f"已刪除暫存資料庫 {target}。")
            except Exception as error:  # noqa: BLE001
                out(f"警告：暫存資料庫 {target} 沒有刪除（{type(error).__name__}），請手動清除。")
        else:
            out(f"已保留暫存資料庫 {target}。")
    passed = bool(results) and all(ok for _, ok in results)
    out("演練通過" if passed else "演練失敗")
    return 0 if passed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="備份還原演練與日誌轉檔。")
    sub = parser.add_subparsers(dest="command", required=True)
    drill = sub.add_parser("drill", help="把備份還原到暫存資料庫並演練重新抹除")
    drill.add_argument("--backup", required=True, help="SQL Server 伺服器上看得到的 .bak 路徑")
    drill.add_argument("--source-db", required=True, help="原資料庫名稱；暫存資料庫為「原名稱_restore_drill」")
    drill.add_argument("--ids-file", help="logs-to-ids 產生的檔案（涵蓋備份之後才發生的刪除）")
    drill.add_argument("--keep", action="store_true", help="演練後保留暫存資料庫")
    convert = sub.add_parser("logs-to-ids", help="把日誌轉成 replay-deletions 的 --ids-file")
    convert.add_argument("--input", required=True)
    convert.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "logs-to-ids":
        return run_logs_to_ids(args.input, args.output, out)
    return run_drill(args.backup, args.source_db, args.ids_file, args.keep, out)


if __name__ == "__main__":
    sys.exit(main())
