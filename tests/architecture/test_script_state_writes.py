"""script-store 規格「非草稿的內容不可變」與「每個劇本至多一個上架版本與一個進行中的版本」的結構守門。

版本的狀態與兩個輔助欄位（`live_script_id`、`open_script_id`）只由 `ScriptRepository` 寫入，因為資料庫的部分唯一索引
靠它們保證「至多一個上架版本、至多一個進行中的版本」，輔助欄位與狀態一旦被別處各寫各的就會失去這個保證。
這裡以 AST 檢查原始碼：`app/` 裡除了模型定義與 `script_repository.py` 之外，不得有人
- 提到輔助欄位的名稱；
- 建立 `ScriptVersion(...)` 物件或對它執行 `update(...)`／`delete(...)`；
- 對 `ScriptVersion` 的 `status` 屬性賦值。
並附反向驗證，證明檢查真的抓得到違規的寫法。

審查結果欄位（`review_hash`、`review_verdict`、`review_categories_json`、`reviewed_at`，rpg-script-authoring）是審查的中繼資料，
只能由 `ScriptRepository.record_review` 寫入（它條件式比對內容雜湊，內容被改動時舊結果不會蓋在新內容上）。讀取不受限制（審查服務要讀）；
這裡只抓「寫入」：對這些屬性賦值，或在任何呼叫中以同名的關鍵字傳入（`update(...).values(review_hash=...)`、`ScriptVersion(review_hash=...)`）。

決定欄位（`submitted_at`、`removed_by`、`decision_category`、`decision_note`、`decided_at`，rpg-script-publishing）是送審、退回與下架的中繼資料，
同樣只能由 `ScriptRepository` 的轉移方法寫入（與狀態在同一個條件式 UPDATE 內，狀態與決定不會各寫各的）；其中 `decision_note` 是只供
管理員看的審核備註。讀取不受限制；掃描規則與審查欄位相同，並另外釘住「哪些方法可以寫入」（見最後幾個測試）。
"""
import ast

from app.config import PROJECT_ROOT

APP = PROJECT_ROOT / "app"
ALLOWED = {"app/models/script.py", "app/repositories/script_repository.py"}
HELPER_COLUMNS = {"live_script_id", "open_script_id"}
REVIEW_COLUMNS = {"review_hash", "review_verdict", "review_categories_json", "reviewed_at"}
DECISION_COLUMNS = {"submitted_at", "removed_by", "decision_category", "decision_note", "decided_at"}


def violations_in(source: str) -> list[str]:
    tree = ast.parse(source)
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in HELPER_COLUMNS:
            problems.append(f"第 {node.lineno} 行：提到輔助欄位 {node.attr}")
        elif isinstance(node, ast.keyword) and node.arg in HELPER_COLUMNS:
            problems.append(f"第 {node.lineno} 行：以關鍵字寫入輔助欄位 {node.arg}")
        elif isinstance(node, ast.keyword) and node.arg in REVIEW_COLUMNS:
            problems.append(f"第 {node.lineno} 行：以關鍵字寫入審查欄位 {node.arg}")
        elif isinstance(node, ast.keyword) and node.arg in DECISION_COLUMNS:
            problems.append(f"第 {node.lineno} 行：以關鍵字寫入決定欄位 {node.arg}")
        elif isinstance(node, (ast.Assign, ast.AugAssign)) and any(
            isinstance(target, ast.Attribute) and target.attr in DECISION_COLUMNS
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        ):
            problems.append(f"第 {node.lineno} 行：對決定欄位賦值")
        elif isinstance(node, (ast.Assign, ast.AugAssign)) and any(
            isinstance(target, ast.Attribute) and target.attr in REVIEW_COLUMNS
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        ):
            problems.append(f"第 {node.lineno} 行：對審查欄位賦值")
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


def test_the_scan_flags_writes_to_the_review_columns_but_not_reads():
    assert violations_in("def f(v):\n    v.review_hash = 'x'\n")
    assert violations_in("from sqlalchemy import update\nupdate(Other).values(review_verdict='pass')\n")
    assert violations_in("def f():\n    return Other(reviewed_at=1)\n")
    assert violations_in("def f(v):\n    v.review_categories_json += [1]\n")
    assert violations_in("def f(v):\n    return v.review_hash == v.content_hash and v.review_verdict\n") == []
    assert violations_in("def f(v):\n    return list(v.review_categories_json or ()), v.reviewed_at\n") == []


def test_the_review_columns_are_written_only_by_record_review():
    repository = (APP / "repositories" / "script_repository.py").read_text(encoding="utf-8-sig")
    assert any("審查欄位" in p for p in violations_in(repository))
    assert repository.count("review_categories_json=") == 1 and "def record_review" in repository


def test_the_scan_leaves_innocent_code_alone():
    assert violations_in("def f(session):\n    return session.scalar(select(ScriptVersion).where(ScriptVersion.status == 'published'))\n") == []
    assert violations_in("def f(user):\n    user.status = 'active'\n") == []
    assert violations_in("def f(save):\n    save.status = 'ended'\n") == []


def test_the_scan_flags_writes_to_the_decision_columns_but_not_reads():
    assert violations_in("def f(v):\n    v.decision_note = 'x'\n")
    assert violations_in("def f(v):\n    v.removed_by = 'admin'\n")
    assert violations_in("from sqlalchemy import update\nupdate(Other).values(decision_category='other_policy')\n")
    assert violations_in("def f(now):\n    return Other(submitted_at=now)\n")
    assert violations_in("def f(now):\n    return Other(decided_at=now)\n")
    assert violations_in("def f(v):\n    v.decision_note += 'x'\n")
    assert any("決定欄位" in p for p in violations_in("def f(v):\n    v.decided_at = 1\n"))
    assert violations_in("def f(v):\n    return v.decision_note, v.removed_by, v.decision_category\n") == []
    assert violations_in("def f(v):\n    return v.submitted_at or v.created_at, v.decided_at\n") == []


def _functions_writing(source: str, columns: set[str]) -> dict[str, set[str]]:
    """每個決定欄位以關鍵字寫入的位置：欄位 → 包含該寫入的函式名稱。"""
    found: dict[str, set[str]] = {column: set() for column in columns}
    for function in ast.walk(ast.parse(source)):
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(function):
                if isinstance(node, ast.keyword) and node.arg in columns:
                    found[node.arg].add(function.name)
    return found


def test_the_decision_columns_are_written_only_by_the_named_transition_methods():
    """送審與撤回寫 `submitted_at`；退回、下架與恢復寫其餘四個：寫入點固定，新增的寫入點必須先修改這個測試（並檢討規格）。"""
    repository = (APP / "repositories" / "script_repository.py").read_text(encoding="utf-8-sig")
    assert any("決定欄位" in p for p in violations_in(repository))
    written = _functions_writing(repository, DECISION_COLUMNS)
    assert written["submitted_at"] == {"submit_for_review", "withdraw"}
    assert written["removed_by"] == {"_remove", "_restore"}
    assert written["decision_category"] == {"reject_version", "_remove", "_restore"}
    assert written["decision_note"] == {"reject_version", "_remove", "_restore"}
    assert written["decided_at"] == {"reject_version", "_remove", "_restore"}


def test_no_other_application_file_writes_the_decision_columns():
    found = {path: [p for p in problems if "決定欄位" in p] for path, problems in offending_files().items()}
    assert {path: problems for path, problems in found.items() if problems} == {}
