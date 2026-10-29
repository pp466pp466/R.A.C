"""架構檢查：層級依賴方向單向（見 AGENTS.md 第 3 節與 application-layering 規格）。

方向：端點（controllers）與 HTTP 安全輔助（security）→ Service → Repository → 資料模型。
- 端點與 HTTP 安全輔助不得直接存取資料庫工作階段、Repository、資料模型或 SQLAlchemy（需要資料時呼叫 Service）；
- Service 不得依賴端點，也不得依賴 HTTP 請求物件（不得匯入 flask）；
- Repository 與資料模型不得依賴 Service、端點，也不得匯入 flask；
- 資料庫基礎（db）與可觀測性（observability）不得依賴 Service、Repository、端點。
`app/__init__.py`（應用程式工廠）與 `app/config.py` 是組裝層，不受限制。
"""
from __future__ import annotations

import ast
from pathlib import Path

from app.config import PROJECT_ROOT

APP_DIR = PROJECT_ROOT / "app"

# 各層「禁止匯入」的模組前綴
FORBIDDEN_IMPORTS: dict[str, tuple[str, ...]] = {
    "controllers": ("app.repositories", "app.db", "app.models", "sqlalchemy"),
    "security": ("app.repositories", "app.db", "app.models", "sqlalchemy"),
    "services": ("app.controllers", "flask"),
    "repositories": ("app.services", "app.controllers", "flask"),
    "models": ("app.services", "app.repositories", "app.controllers", "flask"),
    "db": ("app.services", "app.repositories", "app.controllers"),
    "observability": ("app.services", "app.repositories", "app.controllers"),
    # 模型呼叫層：供應商中立的型別與轉接器，不依賴任何業務層（Service 依賴它，不是反過來）
    "llm": ("app.services", "app.repositories", "app.controllers", "app.models", "flask"),
}


def _matches(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def _absolute_module(node: ast.ImportFrom, current_package: str) -> str:
    """把相對匯入換成絕對模組名稱。"""
    if node.level == 0:
        return node.module or ""
    parts = current_package.split(".")
    base = parts[: len(parts) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


def imported_modules(source: str, current_package: str) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = _absolute_module(node, current_package)
            found.append((node.lineno, module))
            # `from app import repositories` 這種寫法：把名稱也視為子模組
            found.extend((node.lineno, f"{module}.{alias.name}") for alias in node.names)
    return found


def violations_for(layer: str, source: str, current_package: str) -> list[str]:
    result = []
    for lineno, module in imported_modules(source, current_package):
        for prefix in FORBIDDEN_IMPORTS[layer]:
            if _matches(module, prefix):
                result.append(f"第 {lineno} 行：{layer} 不得依賴 {prefix}（匯入 {module}）")
    return sorted(set(result))


def scan_project() -> dict[str, list[str]]:
    problems: dict[str, list[str]] = {}
    for layer in FORBIDDEN_IMPORTS:
        for path in sorted((APP_DIR / layer).rglob("*.py")):
            relative = path.relative_to(PROJECT_ROOT)
            package = ".".join(relative.with_suffix("").parts[:-1])  # 例如 app.controllers
            found = violations_for(layer, path.read_text(encoding="utf-8-sig"), package)
            if found:
                problems[str(relative)] = found
    return problems


def test_layer_dependencies_follow_the_allowed_direction():
    assert scan_project() == {}


def test_every_layer_directory_exists():
    for layer in FORBIDDEN_IMPORTS:
        assert (APP_DIR / layer).is_dir(), f"app/{layer} 不存在"


# ---- 掃描規則本身的單元測試 ---------------------------------------------------

def test_controller_importing_a_repository_is_flagged():
    assert violations_for("controllers", "from app.repositories.base import BaseRepository\n", "app.controllers")


def test_controller_using_the_database_session_or_sqlalchemy_is_flagged():
    assert violations_for("controllers", "from app.db.session import get_session\n", "app.controllers")
    assert violations_for("controllers", "from sqlalchemy.orm import Session\n", "app.controllers")
    assert violations_for("controllers", "import sqlalchemy\n", "app.controllers")


def test_relative_imports_are_resolved_before_checking():
    assert violations_for("controllers", "from ..repositories import base\n", "app.controllers")
    assert violations_for("repositories", "from ..services import normalization\n", "app.repositories")


def test_service_depending_on_http_objects_or_controllers_is_flagged():
    assert violations_for("services", "from flask import request\n", "app.services")
    assert violations_for("services", "import flask\n", "app.services")
    assert violations_for("services", "from app.controllers import errors\n", "app.services")


def test_repository_depending_on_a_service_is_flagged():
    assert violations_for("repositories", "from app.services.normalization import normalize_unique_text\n", "app.repositories")


def test_imports_inside_functions_are_also_checked():
    source = "def handler():\n    from app.repositories.base import BaseRepository\n"
    assert violations_for("controllers", source, "app.controllers")


def test_http_security_helpers_may_not_read_the_database_directly():
    """application-layering 規格：HTTP 安全輔助直接讀資料庫 → 檢查失敗。"""
    assert violations_for("security", "from app.models.user import User\n", "app.security")
    assert violations_for("security", "from app.db.session import get_session\n", "app.security")
    assert violations_for("security", "from app.repositories.user_repository import UserRepository\n", "app.security")
    assert violations_for("security", "from sqlalchemy import select\n", "app.security")
    assert violations_for("security", "def f():\n    from app.models import base\n", "app.security")


def test_http_security_helpers_may_use_flask_services_and_config():
    allowed = (
        "from flask import session, request\n"
        "from app.services.errors import Unauthenticated\n"
        "from app.config import AppConfig\n"
        "from app.runtime import get_config\n"
    )
    assert violations_for("security", allowed, "app.security") == []


def test_the_security_package_is_scanned():
    assert "security" in FORBIDDEN_IMPORTS
    assert (APP_DIR / "security").is_dir()


def test_the_llm_package_is_scanned_and_stays_independent_of_the_business_layers():
    """llm-provider 規格：Service 依賴中立型別，轉接器不依賴任何業務層（避免循環，也讓更換供應商只動這一層）。"""
    assert "llm" in FORBIDDEN_IMPORTS and (APP_DIR / "llm").is_dir()
    for source in (
        "from app.services.errors import ServiceError\n", "from app.repositories.base import BaseRepository\n",
        "from app.controllers import errors\n", "from app.models.chat import ChatMessage\n", "from flask import current_app\n",
        "def f():\n    from app.services import registry\n",
    ):
        assert violations_for("llm", source, "app.llm"), source


def test_the_llm_package_may_use_the_standard_library_and_its_own_types():
    allowed = "import json\nimport urllib.request\nfrom app.llm.types import LLMRequest\nfrom app.config import LlmSettings\n"
    assert violations_for("llm", allowed, "app.llm") == []


def test_allowed_dependencies_pass():
    assert violations_for("controllers", "from app.services.normalization import normalize_unique_text\n", "app.controllers") == []
    assert violations_for("controllers", "from flask import Blueprint\n", "app.controllers") == []
    assert violations_for("services", "from app.repositories.base import BaseRepository\n", "app.services") == []
    assert violations_for("repositories", "from sqlalchemy import select\nfrom app.models.base import Base\n", "app.repositories") == []
