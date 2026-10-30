"""application-layering 規格「後台專用的資料存取只供後台 Service 使用」與「後台 Service 的每個公開方法先檢查管理員」。

與 `test_chat_repository_scoping.py` 對稱：一般路徑強制每個方法帶擁有者，後台路徑強制每個方法先過管理員檢查、
且後台專用的 Repository 只有一個入口。兩者都以 AST 檢查原始碼，不依賴執行；並各附反向驗證，證明檢查抓得到問題。
"""
import ast

from app.config import PROJECT_ROOT

APP = PROJECT_ROOT / "app"
SERVICE = APP / "services" / "admin_service.py"
ADMIN_REPOSITORIES = ("app.repositories.admin_repository", "app.repositories.admin_audit_repository")
# 唯一允許匯入後台專用 Repository 的檔案：後台 Service 本身，與負責組裝的 registry
ALLOWED_IMPORTERS = {"app/services/admin_service.py", "app/services/registry.py"}


# ---- 規則一：每個公開方法先檢查管理員 --------------------------------------------------------

def _first_statement(node: ast.FunctionDef) -> ast.stmt | None:
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]                                   # 略過文件字串
    return body[0] if body else None


def method_violations(source: str, class_name: str = "AdminService") -> list[str]:
    tree = ast.parse(source)
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name), None)
    assert cls is not None, f"找不到 {class_name}"
    problems = []
    for node in cls.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name.startswith("_"):
            continue
        params = [a.arg for a in node.args.args]
        first = _first_statement(node)
        ok = (
            len(params) >= 2 and isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
            and ast.unparse(first.value.func) == "self._policy.require_admin"
            and [ast.unparse(a) for a in first.value.args] == [params[1]]        # 檢查的就是第一個參數（呼叫的管理員）
        )
        if not ok:
            problems.append(f"{node.name}：第一個陳述式必須是 self._policy.require_admin({params[1] if len(params) > 1 else '管理員'})")
    return problems


def test_every_public_method_of_the_admin_service_checks_the_admin_first():
    assert method_violations(SERVICE.read_text(encoding="utf-8-sig")) == []


def test_the_method_check_is_not_vacuous_it_sees_the_real_methods():
    tree = ast.parse(SERVICE.read_text(encoding="utf-8-sig"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "AdminService")
    public = {n.name for n in cls.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")}
    assert len(public) >= 12 and {"overview", "lookup_user", "suspend_user", "read_conversation", "delete_conversation", "failures", "audit_log"} <= public


def test_the_method_check_really_fails_for_a_method_that_forgets_the_check():
    forgot = "class AdminService:\n    def overview(self, admin):\n        return 1\n"
    assert "overview" in method_violations(forgot)[0]
    late = "class AdminService:\n    def overview(self, admin):\n        x = 1\n        self._policy.require_admin(admin)\n"
    assert method_violations(late)
    wrong_argument = "class AdminService:\n    def overview(self, admin):\n        self._policy.require_admin(None)\n"
    assert method_violations(wrong_argument)
    wrong_policy = "class AdminService:\n    def overview(self, admin):\n        self._policy.is_admin(admin)\n"
    assert method_violations(wrong_policy)
    no_admin_param = "class AdminService:\n    def overview(self):\n        self._policy.require_admin(None)\n"
    assert method_violations(no_admin_param)


def test_a_docstring_before_the_check_is_fine_and_private_helpers_are_exempt():
    fine = (
        "class AdminService:\n    def overview(self, admin):\n        '''說明'''\n        self._policy.require_admin(admin)\n"
        "        return 1\n    def _helper(self, x):\n        return x\n"
    )
    assert method_violations(fine) == []


# ---- 規則二：後台專用的 Repository 只有一個入口 -----------------------------------------------

def import_violations(files: dict[str, str]) -> list[str]:
    problems = []
    for relative, source in sorted(files.items()):
        if relative in ALLOWED_IMPORTERS:
            continue
        for node in ast.walk(ast.parse(source)):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            hit = [n for n in names if any(n == repo or n.startswith(repo + ".") for repo in ADMIN_REPOSITORIES)]
            if hit:
                problems.append(f"{relative} 不得匯入後台專用的 Repository：{hit[0]}")
    return problems


def project_sources() -> dict[str, str]:
    return {path.relative_to(PROJECT_ROOT).as_posix(): path.read_text(encoding="utf-8-sig") for path in APP.rglob("*.py")}


def test_only_the_admin_service_and_the_registry_import_the_admin_repositories():
    assert import_violations(project_sources()) == []


def test_the_import_check_sees_the_real_importers():
    sources = project_sources()
    importers = {
        relative for relative, source in sources.items()
        if any(repo in source for repo in ADMIN_REPOSITORIES)
    }
    assert {"app/services/admin_service.py", "app/services/registry.py"} <= importers
    assert not [r for r in importers if r not in ALLOWED_IMPORTERS and not r.startswith("app/repositories/admin_")]


def test_the_import_check_really_fails_for_a_controller_or_another_service():
    controller = {"app/controllers/cs_api_controller.py": "from app.repositories.admin_repository import AdminRepository\n"}
    assert "cs_api_controller" in import_violations(controller)[0]
    other_service = {"app/services/chat_service.py": "import app.repositories.admin_audit_repository\n"}
    assert import_violations(other_service)
    module_form = {"app/services/usage_service.py": "from app.repositories import admin_repository\n"}
    assert import_violations(module_form)
    allowed = {"app/services/admin_service.py": "from app.repositories.admin_repository import AdminRepository\n"}
    assert import_violations(allowed) == []
    unrelated = {"app/services/chat_service.py": "from app.repositories.chat_repository import ChatRepository\n"}
    assert import_violations(unrelated) == []


def test_the_admin_repositories_do_not_inherit_the_generic_base_whose_methods_ignore_ownership():
    for name in ("admin_repository.py", "admin_audit_repository.py"):
        tree = ast.parse((APP / "repositories" / name).read_text(encoding="utf-8-sig"))
        for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
            assert not cls.bases, f"{cls.name} 不得繼承基底類別：{[ast.unparse(b) for b in cls.bases]}"
