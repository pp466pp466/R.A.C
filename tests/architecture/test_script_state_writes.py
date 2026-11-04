"""script-store 規格「非草稿的內容不可變」與「每個劇本至多一個上架版本與一個進行中的版本」的結構守門。

版本的狀態與兩個輔助欄位（`live_script_id`、`open_script_id`）只由 `ScriptRepository` 寫入，因為資料庫的部分唯一索引
靠它們保證「至多一個上架版本、至多一個進行中的版本」，輔助欄位與狀態一旦被別處各寫各的就會失去這個保證。
這裡以 AST 檢查原始碼：`app/` 裡除了模型定義與 `script_repository.py` 之外，不得有人
- 提到輔助欄位的名稱；
- 建立 `ScriptVersion(...)` 物件或對它執行 `update(...)`／`delete(...)`；
- 對 `ScriptVersion` 的 `status` 屬性賦值。
並附反向驗證，證明檢查真的抓得到違規的寫法。
"""
import ast

from app.config import PROJECT_ROOT

APP = PROJECT_ROOT / "app"
ALLOWED = {"app/models/script.py", "app/repositories/script_repository.py"}
HELPER_COLUMNS = {"live_script_id", "open_script_id"}


def violations_in(source: str) -> list[str]:
    tree = ast.parse(source)
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in HELPER_COLUMNS:
            problems.append(f"第 {node.lineno} 行：提到輔助欄位 {node.attr}")
        elif isinstance(node, ast.keyword) and node.arg in HELPER_COLUMNS:
            problems.append(f"第 {node.lineno} 行：以關鍵字寫入輔助欄位 {node.arg}")
        elif isinstance(node, ast.Name) and node.id in HELPER_COLUMNS:
            problems.append(f"第 {node.lineno} 行：提到輔助欄位 {node.id}")
        elif isinstance(node, ast.Call):
            callee = ast.unparse(node.func)
            first = ast.unparse(node.args[0]) if node.args else ""
            if callee == "ScriptVersion":
                problems.append(f"第 {node.lineno} 行：直接建立 ScriptVersion")
            elif callee in ("update", "delete", "sa.update", "sa.delete") and first == "ScriptVersion":
                problems.append(f"第 {node.lineno} 行：直接 {callee}(ScriptVersion)")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == "status" and "version" in ast.unparse(target.value).lower():
                    problems.append(f"第 {node.lineno} 行：對版本的 status 賦值")
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


def test_only_the_script_repository_writes_version_status_and_helper_columns():
    assert offending_files() == {}


def test_the_scan_sees_the_repository_and_its_writes():
    repository = (APP / "repositories" / "script_repository.py").read_text(encoding="utf-8-sig")
    problems = violations_in(repository)
    assert any("輔助欄位" in p for p in problems) and any("直接建立 ScriptVersion" in p or "ScriptVersion)" in p for p in problems)
    assert len(list(APP.rglob("*.py"))) > 50, "沒有掃到預期數量的檔案，掃描可能是空洞的"


def test_the_scan_really_flags_each_kind_of_violation():
    assert violations_in("def f(v):\n    return v.live_script_id\n")
    assert violations_in("from sqlalchemy import update\nupdate(ScriptVersion).values(open_script_id=1)\n")
    assert violations_in("def f():\n    return ScriptVersion(script_id=1)\n")
    assert violations_in("def f(version):\n    version.status = 'published'\n")
    assert violations_in("from sqlalchemy import delete\ndelete(ScriptVersion).where(1)\n")
    assert violations_in("def f(v):\n    return ScriptVersion.live_script_id == v\n")


def test_the_scan_leaves_innocent_code_alone():
    assert violations_in("def f(session):\n    return session.scalar(select(ScriptVersion).where(ScriptVersion.status == 'published'))\n") == []
    assert violations_in("def f(user):\n    user.status = 'active'\n") == []
    assert violations_in("def f(save):\n    save.status = 'ended'\n") == []
