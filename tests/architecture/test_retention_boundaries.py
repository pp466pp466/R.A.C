"""data-retention 規格：期限清除的邊界。

- `RetentionRepository`（唯一能依期限刪除稽核紀錄、帳本與對話的類別）只被 `DataRetentionService` 與組裝處匯入；
- 稽核的一般途徑 `AdminAuditRepository` 仍然只有新增與讀取，沒有任何修改或刪除的公開方法（期限屆滿的清除是唯一的例外，
  而它在 `RetentionRepository`，不在稽核 Repository）。
"""
import ast

from app.config import PROJECT_ROOT

APP_DIR = PROJECT_ROOT / "app"
ALLOWED_IMPORTERS = {"app/services/data_retention_service.py", "app/services/registry.py"}
MODULE = "app.repositories.retention_repository"
MUTATING_PREFIXES = ("update", "delete", "remove", "purge", "clear", "edit", "modify", "set")


def importers(files: dict[str, str]) -> set[str]:
    found = set()
    for path, source in files.items():
        for node in ast.walk(ast.parse(source)):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = [module] + [f"{module}.{a.name}" for a in node.names]
            if any(n == MODULE or n.startswith(MODULE + ".") for n in names):
                found.add(path)
    return found


def project_files() -> dict[str, str]:
    return {
        path.relative_to(PROJECT_ROOT).as_posix(): path.read_text(encoding="utf-8-sig")
        for path in APP_DIR.rglob("*.py")
    }


def test_only_the_retention_service_and_the_assembly_import_the_retention_repository():
    found = importers(project_files())
    assert found <= ALLOWED_IMPORTERS, f"其他檔案匯入了期限清除專用的 Repository：{sorted(found - ALLOWED_IMPORTERS)}"
    assert found == ALLOWED_IMPORTERS            # 兩個都用到它：這個檢查不是空的


def test_the_check_really_fails_for_another_importer():
    files = {"app/services/chat_service.py": f"from {MODULE} import RetentionRepository\n", "app/x.py": "import os\n"}
    assert importers(files) == {"app/services/chat_service.py"}
    plain = {"app/services/y.py": f"import {MODULE}\n"}
    assert importers(plain) == {"app/services/y.py"}


def test_the_audit_repository_has_no_way_to_change_or_delete_a_record():
    source = (APP_DIR / "repositories" / "admin_audit_repository.py").read_text(encoding="utf-8-sig")
    cls = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == "AdminAuditRepository")
    public = [n.name for n in cls.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")]
    assert public and not [name for name in public if name.startswith(MUTATING_PREFIXES)], public
