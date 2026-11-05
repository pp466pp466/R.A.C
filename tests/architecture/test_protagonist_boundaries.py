"""rpg-protagonist 與 protagonist-suggestion 的結構守門：
1. 主角相關 Service 的每個公開方法先過帳號與年齡閘門（`self._policy.require_chat_access(<呼叫者>)`），Controller 不自己判斷權限；
2. `/api/sessions/*` 的 Controller 不匯入資料庫、Repository、資料模型或審查層（層級依賴方向由 `test_layering.py` 另行守住）；
3. 純運算模組（驗證、提示詞）不依賴資料庫、網路或時間；
4. 開存檔需要主角：`SessionService.start_session` 的簽名沒有舊的 `player_name`、`character_age`，而 `SessionRepository.create` 的簽名
   第一個參數是擁有者。
都以 AST 檢查原始碼並附反向驗證，證明檢查抓得到問題。
"""
import ast

from app.config import PROJECT_ROOT

APP = PROJECT_ROOT / "app"
PROTAGONIST_SERVICE = APP / "services" / "protagonist_service.py"
SESSION_SERVICE = APP / "services" / "session_service.py"
SESSION_REPOSITORY = APP / "repositories" / "session_repository.py"
CONTROLLER = APP / "controllers" / "session_api_controller.py"
PURE_MODULES = ("protagonist.py", "protagonist_prompt.py")


def _first_statement(node: ast.FunctionDef) -> ast.stmt | None:
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    return body[0] if body else None


def gate_violations(source: str, class_name: str, exempt: tuple[str, ...] = ()) -> list[str]:
    tree = ast.parse(source)
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name), None)
    assert cls is not None, f"找不到 {class_name}"
    problems = []
    for node in cls.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name.startswith("_") or node.name in exempt:
            continue
        params = [a.arg for a in node.args.args]
        first = _first_statement(node)
        ok = (
            len(params) >= 2 and isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
            and ast.unparse(first.value.func) == "self._policy.require_chat_access"
            and [ast.unparse(a) for a in first.value.args] == [params[1]]
        )
        if not ok:
            problems.append(f"{node.name}：第一個陳述式必須是 self._policy.require_chat_access({params[1] if len(params) > 1 else '呼叫者'})")
    return problems


def imports_of(source: str) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            names += [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
        elif isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
    return names


# ---- 規則一：每個公開方法先過閘門 ----------------------------------------------------------------------------


def test_every_public_method_of_the_protagonist_service_passes_the_gate_first():
    assert gate_violations(PROTAGONIST_SERVICE.read_text(encoding="utf-8-sig"), "ProtagonistService") == []


def test_the_session_service_start_session_and_begin_turn_pass_the_gate_first():
    assert gate_violations(SESSION_SERVICE.read_text(encoding="utf-8-sig"), "SessionService") == []


def test_the_gate_check_is_not_vacuous_it_sees_the_real_methods():
    tree = ast.parse(PROTAGONIST_SERVICE.read_text(encoding="utf-8-sig"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ProtagonistService")
    assert {n.name for n in cls.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")} == {"setup", "suggest"}


def test_the_gate_check_really_fails_for_a_method_that_forgets_or_postpones_it():
    forgot = "class ProtagonistService:\n    def suggest(self, current):\n        return 1\n"
    assert "suggest" in gate_violations(forgot, "ProtagonistService")[0]
    late = "class ProtagonistService:\n    def suggest(self, current):\n        x = 1\n        self._policy.require_chat_access(current)\n"
    assert gate_violations(late, "ProtagonistService")
    wrong_argument = "class ProtagonistService:\n    def suggest(self, current):\n        self._policy.require_chat_access(None)\n"
    assert gate_violations(wrong_argument, "ProtagonistService")
    wrong_policy = "class ProtagonistService:\n    def suggest(self, current):\n        self._policy.can_use_chat(current)\n"
    assert gate_violations(wrong_policy, "ProtagonistService")
    fine = "class ProtagonistService:\n    def suggest(self, current):\n        '''說明'''\n        self._policy.require_chat_access(current)\n    def _helper(self, x):\n        return x\n"
    assert gate_violations(fine, "ProtagonistService") == []


# ---- 規則二：Controller 不碰資料層、不自己判斷權限 ------------------------------------------------------------


def test_the_sessions_controller_imports_no_database_repository_model_or_review_layer():
    forbidden = ("app.repositories", "app.db", "app.models", "sqlalchemy", "app.services.script_rules", "app.services.protagonist_review_service")
    found = [name for name in imports_of(CONTROLLER.read_text(encoding="utf-8-sig")) if name.startswith(forbidden)]
    assert found == []


def test_the_sessions_controller_has_no_permission_checks_of_its_own():
    tree = ast.parse(CONTROLLER.read_text(encoding="utf-8-sig"))
    names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)} | {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert not names & {"role", "is_admin", "require_admin", "require_owner", "is_owner", "member_tier", "content_tier", "effective_tier"}


def test_the_import_check_sees_real_imports_and_flags_a_repository_import():
    assert "app.security.session.require_login" in imports_of(CONTROLLER.read_text(encoding="utf-8-sig"))
    assert "app.repositories.session_repository" in imports_of("from app.repositories import session_repository\n") or \
        "app.repositories.session_repository" in imports_of("from app.repositories.session_repository import SessionRepository\n")


# ---- 規則三：純運算模組 ------------------------------------------------------------------------------------


def test_the_validation_and_prompt_modules_do_not_depend_on_database_network_or_time():
    forbidden = ("sqlalchemy", "flask", "requests", "httpx", "urllib", "socket", "datetime", "time", "app.repositories", "app.db", "app.controllers")
    for filename in PURE_MODULES:
        found = [name for name in imports_of((APP / "services" / filename).read_text(encoding="utf-8-sig")) if name.split(".")[0] in forbidden or name.startswith(forbidden[-3:])]
        assert found == [], (filename, found)


# ---- 規則四：開存檔的簽名 ----------------------------------------------------------------------------------


def _function(source: str, class_name: str, name: str) -> ast.FunctionDef:
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    return next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)


def test_start_session_takes_a_protagonist_not_a_bare_name_and_age():
    function = _function(SESSION_SERVICE.read_text(encoding="utf-8-sig"), "SessionService", "start_session")
    parameters = {a.arg for a in function.args.args + function.args.kwonlyargs}
    assert {"protagonist", "anchor", "skip_anchor"} <= parameters and not parameters & {"player_name", "character_age"}


def test_the_repository_create_takes_the_owner_first_and_a_protagonist():
    function = _function(SESSION_REPOSITORY.read_text(encoding="utf-8-sig"), "SessionRepository", "create")
    positional = [a.arg for a in function.args.args]
    assert positional[:2] == ["self", "user_id"]
    keyword_only = {a.arg for a in function.args.kwonlyargs}
    assert "protagonist" in keyword_only and not keyword_only & {"player_name", "character_age"}
