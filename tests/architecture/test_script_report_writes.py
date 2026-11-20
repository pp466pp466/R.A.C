"""script-reports 規格「結案只由 Repository 寫入」的結構守門（rpg-script-reports 的 design D2）。

檢舉的結案欄位（`closed_at`、`closed_result`）是歷史事實：結案之後不得被改寫、不得被別處各寫各的。它們只由
`ScriptReportRepository` 的 `close_open` 寫入（條件式 UPDATE：只結 `closed_at IS NULL` 的列，已結案的不被覆蓋）。
`ScriptRepository` 在版本離開上架時只「呼叫」這個方法，管理員結「不成立」同樣只呼叫它，所以
「版本狀態只在 `script_repository.py` 寫入」與「結案只在 `script_report_repository.py` 寫入」兩條守門互不衝突。

這裡以 AST 檢查原始碼：`app/` 裡除了模型定義與 `script_report_repository.py` 之外，不得有人
- 在任何呼叫中以同名關鍵字傳入（`update(...).values(closed_at=...)`、`ScriptReport(closed_result=...)`）；
- 對這兩個屬性賦值（含 `+=`）；
- 以字典寫法（`.values({"closed_at": ...})`、`**{...}`）或 `setattr(x, "closed_at", ...)` 繞過；
- 對 `ScriptReport` 直接執行 `update(...)`（任何欄位：整張表的更新都只能走 Repository 的方法）。
讀取不受限制（管理員清單與匯出都要讀）。並附反向驗證，證明檢查真的抓得到違規的寫法，也不會誤傷無辜的程式。
"""
import ast

from app.config import PROJECT_ROOT

APP = PROJECT_ROOT / "app"
ALLOWED = {"app/models/script_report.py", "app/repositories/script_report_repository.py"}
CLOSING_COLUMNS = {"closed_at", "closed_result"}


def violations_in(source: str) -> list[str]:
    tree = ast.parse(source)
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg in CLOSING_COLUMNS:
            problems.append(f"第 {node.lineno} 行：以關鍵字寫入結案欄位 {node.arg}")
        elif isinstance(node, (ast.Assign, ast.AugAssign)) and any(
            isinstance(target, ast.Attribute) and target.attr in CLOSING_COLUMNS
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        ):
            problems.append(f"第 {node.lineno} 行：對結案欄位賦值")
        elif isinstance(node, ast.Dict) and any(
            isinstance(key, ast.Constant) and key.value in CLOSING_COLUMNS for key in node.keys
        ):
            problems.append(f"第 {node.lineno} 行：字典寫法帶有結案欄位")
        elif isinstance(node, ast.Call):
            callee = ast.unparse(node.func)
            first = ast.unparse(node.args[0]) if node.args else ""
            if callee == "setattr" and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and node.args[1].value in CLOSING_COLUMNS:
                problems.append(f"第 {node.lineno} 行：以 setattr 寫入結案欄位")
            elif callee in ("update", "sa.update") and first == "ScriptReport":
                problems.append(f"第 {node.lineno} 行：直接 {callee}(ScriptReport)")
    return problems


def offending_files() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(APP.rglob("*.py")):
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        if relative in ALLOWED:
            continue
        problems = violations_in(path.read_text(encoding="utf-8-sig"))
        if problems:
            found[relative] = problems
    return found


def test_only_the_report_repository_writes_the_closing_columns():
    assert offending_files() == {}


def test_the_scan_sees_the_repository_and_its_writes():
    repository = (APP / "repositories" / "script_report_repository.py").read_text(encoding="utf-8-sig")
    problems = violations_in(repository)
    assert any("關鍵字寫入結案欄位 closed_at" in p for p in problems) and any("關鍵字寫入結案欄位 closed_result" in p for p in problems)
    assert len(list(APP.rglob("*.py"))) > 50, "沒有掃到預期數量的檔案，掃描可能是空洞的"


def test_the_scan_really_flags_each_kind_of_violation():
    assert violations_in("from sqlalchemy import update\nupdate(Other).values(closed_at=1)\n")
    assert violations_in("def f():\n    return Other(closed_result='x')\n")
    assert violations_in("def f(r):\n    r.closed_at = 1\n")
    assert violations_in("def f(r):\n    r.closed_result += 'x'\n")
    assert violations_in("def f(stmt):\n    return stmt.values({'closed_at': 1})\n")
    assert violations_in("def f(stmt, kw):\n    return stmt.values(**{'closed_result': 'x'})\n")
    assert violations_in("def f(r):\n    setattr(r, 'closed_at', 1)\n")
    assert violations_in("from sqlalchemy import update\nupdate(ScriptReport).values(note=None)\n")
    assert violations_in("import sqlalchemy as sa\nsa.update(ScriptReport)\n")
    assert any("結案欄位" in p for p in violations_in("def f(r):\n    r.closed_at = 1\n"))


def test_the_scan_leaves_reads_and_innocent_code_alone():
    assert violations_in("def f(r):\n    return r.closed_at is None and r.closed_result\n") == []
    assert violations_in("def f(session):\n    return session.scalar(select(ScriptReport).where(ScriptReport.closed_at.is_(None)))\n") == []
    assert violations_in("def f(r):\n    return {'closed': r.closed_at}\n") == []                       # 字典的鍵不是結案欄位名稱
    assert violations_in("from sqlalchemy import update\nupdate(Other).values(note=None)\n") == []
    assert violations_in("def f(order):\n    order.closed = True\n") == []
    assert violations_in("def f(r):\n    setattr(r, 'note', 1)\n") == []


def _functions_writing(source: str, columns: set[str]) -> dict[str, set[str]]:
    """每個結案欄位以關鍵字寫入的位置：欄位 → 包含該寫入的函式名稱。"""
    found: dict[str, set[str]] = {column: set() for column in columns}
    for function in ast.walk(ast.parse(source)):
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(function):
                if isinstance(node, ast.keyword) and node.arg in columns:
                    found[node.arg].add(function.name)
    return found


def test_the_closing_columns_are_written_only_by_close_open():
    """寫入點固定：新增的寫入點必須先修改這個測試（並檢討規格：結案是歷史事實，不得被改寫）。"""
    repository = (APP / "repositories" / "script_report_repository.py").read_text(encoding="utf-8-sig")
    written = _functions_writing(repository, CLOSING_COLUMNS)
    assert written == {"closed_at": {"close_open"}, "closed_result": {"close_open"}}


def test_the_only_update_statement_on_the_report_table_is_the_conditional_close():
    """Repository 內也只有 `close_open` 對 `ScriptReport` 執行 UPDATE，且帶 `closed_at IS NULL` 的條件（已結案的不被覆蓋）。"""
    repository = (APP / "repositories" / "script_report_repository.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(repository)
    updating = {
        function.name
        for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef)
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "update"
    }
    assert updating == {"close_open"}
    close_open = next(f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) and f.name == "close_open")
    assert "ScriptReport.closed_at.is_(None)" in ast.unparse(close_open)
