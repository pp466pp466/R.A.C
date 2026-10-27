"""架構檢查：業務邏輯、Repository 與 migration 不得出現原生 SQL（見 AGENTS.md 第 4 節）。

以 AST 靜態掃描 `app/` 與 `migrations/`。唯一白名單是 SQLite 連線初始化所在的 `app/db/dialect.py`。
掃描規則本身也有單元測試，確保它真的抓得到違規寫法。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from app.config import PROJECT_ROOT

SCAN_DIRS = ("app", "migrations")
ALLOWLIST = {Path("app/db/dialect.py")}

# 以 SQL 關鍵字開頭、且後面還有內容的字串（例如 "SELECT 1"、"PRAGMA foreign_keys=ON"）
SQL_START = re.compile(
    r"^\s*(select|insert|update|delete|create|alter|drop|pragma|truncate|merge)\s+\S", re.IGNORECASE
)
FORBIDDEN_CALLS = {"exec_driver_sql", "executescript"}


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    ids.add(id(body[0].value))
    return ids


def _is_stringish(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mod, ast.Add)):
        return _is_stringish(node.left) or _is_stringish(node.right)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        return True
    return False


def find_raw_sql(source: str) -> list[str]:
    """回傳違規描述清單；空清單代表沒有發現原生 SQL。"""
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    findings: list[str] = []

    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root == "sqlalchemy" and any(alias.name == "text" for alias in node.names):
                findings.append(f"第 {line} 行：匯入 sqlalchemy.text")
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name == "text" and (
                isinstance(func, ast.Name)
                or (isinstance(func, ast.Attribute) and getattr(func.value, "id", "") in {"sa", "sqlalchemy"})
            ):
                findings.append(f"第 {line} 行：呼叫 text()")
            if name in FORBIDDEN_CALLS:
                findings.append(f"第 {line} 行：呼叫 {name}()")
            if name == "execute" and node.args and _is_stringish(node.args[0]):
                findings.append(f"第 {line} 行：以字串呼叫 execute()")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings and SQL_START.match(node.value):
                findings.append(f"第 {line} 行：疑似原生 SQL 字串")
    return findings


def scan_project() -> dict[str, list[str]]:
    violations: dict[str, list[str]] = {}
    for folder in SCAN_DIRS:
        for path in sorted((PROJECT_ROOT / folder).rglob("*.py")):
            relative = path.relative_to(PROJECT_ROOT)
            if relative in ALLOWLIST:
                continue
            found = find_raw_sql(path.read_text(encoding="utf-8-sig"))
            if found:
                violations[str(relative)] = found
    return violations


def test_project_contains_no_raw_sql_outside_the_allowlist():
    assert scan_project() == {}


def test_allowlist_is_only_the_dialect_module_and_it_exists():
    assert ALLOWLIST == {Path("app/db/dialect.py")}
    assert (PROJECT_ROOT / "app" / "db" / "dialect.py").is_file()


# ---- 掃描規則本身的單元測試：確認真的抓得到各種寫法 --------------------------------

def test_scanner_detects_text_import_and_call():
    assert find_raw_sql("from sqlalchemy import text\nx = text('SELECT 1')\n")
    assert find_raw_sql("import sqlalchemy as sa\nx = sa.text('x')\n")


def test_scanner_detects_driver_level_execution():
    assert find_raw_sql("conn.exec_driver_sql('x')\n")
    assert find_raw_sql("conn.executescript('x')\n")


def test_scanner_detects_execute_with_string_variants():
    assert find_raw_sql("op.execute('DROP TABLE t')\n")
    assert find_raw_sql("conn.execute(f'SELECT {x}')\n")
    assert find_raw_sql("conn.execute('a' + b)\n")
    assert find_raw_sql("conn.execute('x {}'.format(1))\n")


def test_scanner_detects_sql_looking_string_assigned_to_a_variable():
    assert find_raw_sql("sql = 'SELECT * FROM users'\n")
    assert find_raw_sql("q = 'PRAGMA foreign_keys=ON'\n")


def test_scanner_allows_orm_code_and_docstrings():
    clean = '''
"""SELECT 這種字眼出現在模組說明中是允許的。"""
from sqlalchemy import select, literal

def ping(session):
    """UPDATE 也只是說明文字。"""
    return session.scalar(select(literal(1)))
'''
    assert find_raw_sql(clean) == []
    assert find_raw_sql("op.execute(sa.update(table).values(a=1))\n") == []
