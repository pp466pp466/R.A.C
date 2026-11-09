"""rpg-turn-engine 的結構守門（任務 3.6）：

1. **純邏輯模組**（提示詞、上下文、世界狀態、輸出審查）不匯入資料模型、Repository、資料庫、Flask，也不碰網路、時間與隨機：
   它們必須能在沒有資料庫的情況下單元測試，相同輸入永遠得到相同輸出（rpg-turn-context「組裝函式是不碰資料庫與網路的純函式」）。
   只檢查**直接**的匯入；它們依賴的 `script_rules`、`script_spec` 等同樣是純運算的服務模組（只引用資料模型裡的常數）。
2. **世界狀態欄位**（`entities_json`、`goals_json`、`milestone_progress_json`）只有 Service 可以寫入：Controller 與其他層不得賦值、不得以關鍵字傳入、
   不得呼叫 Repository 的世界狀態寫入方法（`update_world_state`、`finish_turn`）。寫入由 Service 驗證模型的提議（`rpg_world.apply_proposal`）後經 Repository 進行。
都以 AST 檢查原始碼，並附反向驗證，證明檢查真的抓得到違規的寫法。
"""
import ast

from app.config import PROJECT_ROOT

APP = PROJECT_ROOT / "app"
PURE_MODULES = ("rpg_context.py", "rpg_world.py", "rpg_output_guard.py", "rpg_output_rules.py", "rpg_prompt.py")
FORBIDDEN_IMPORTS = (
    "app.models", "app.repositories", "app.db", "app.controllers", "app.security", "app.observability", "app.llm.openai_compatible",
    "app.llm.transport", "app.llm.gemini", "app.services.llm_service", "app.services.rpg_stream",
    "flask", "sqlalchemy", "werkzeug", "requests", "httpx", "urllib", "http", "socket", "ssl", "threading", "time", "datetime", "random", "secrets", "os",
)
WORLD_FIELDS = {"entities_json", "goals_json", "milestone_progress_json"}
WORLD_WRITE_METHODS = {"update_world_state", "finish_turn"}
# 同名欄位但屬於別的資料表（客服訊息的實體解析結果），與存檔的世界狀態無關
UNRELATED = {"app/models/chat.py", "app/repositories/chat_repository.py"}
REPOSITORY = "app/repositories/session_repository.py"
MODEL = "app/models/rpg_session.py"


def imports_of(source: str) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            names += [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
        elif isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
    return names


def forbidden_imports(source: str) -> list[str]:
    return [
        name for name in imports_of(source)
        if any(name == prefix or name.startswith(prefix + ".") for prefix in FORBIDDEN_IMPORTS)
    ]


# ---- 規則一：純邏輯模組 ------------------------------------------------------------------------------


def test_the_pure_rpg_modules_exist_and_import_no_database_framework_network_time_or_randomness():
    for filename in PURE_MODULES:
        path = APP / "services" / filename
        assert path.exists(), filename
        assert forbidden_imports(path.read_text(encoding="utf-8-sig")) == [], filename


def test_the_import_check_sees_real_imports_and_flags_each_forbidden_kind():
    assert "app.services.script_rules" in imports_of((APP / "services" / "rpg_world.py").read_text(encoding="utf-8-sig"))
    for statement in (
        "from app.models.rpg_session import RpgSession", "from app.models import rpg_session", "import app.models.user",
        "from app.repositories.session_repository import SessionRepository", "from app.repositories import session_repository", "from app.db.session import get_session",
        "from flask import g", "import flask", "import sqlalchemy", "from sqlalchemy.orm import Session", "import time", "from datetime import datetime",
        "import random", "import urllib.request", "import socket", "from app.llm.transport import UrllibTransport", "from app.services.llm_service import LlmService",
        "from app.controllers import game_api_controller", "import os",
    ):
        assert forbidden_imports(statement + "\n"), statement
    for statement in ("import re", "import copy", "from dataclasses import dataclass", "from app.llm.types import LLMContent", "from app.services.script_rules import check_text_rules", "import hashlib"):
        assert forbidden_imports(statement + "\n") == [], statement


# ---- 規則二：世界狀態欄位只有 Service 可以寫 -----------------------------------------------------------


def violations_in(source: str, relative: str) -> list[str]:
    """`relative` 是相對於專案根目錄的路徑（正斜線）；Service 層與 Repository／模型自己的寫入不算違規，由呼叫端決定要不要掃它們。"""
    problems: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr in WORLD_FIELDS:
                    problems.append(f"第 {node.lineno} 行：對 {target.attr} 賦值")
                if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant) and target.slice.value in WORLD_FIELDS:
                    problems.append(f"第 {node.lineno} 行：以索引寫入 {target.slice.value}")
        elif isinstance(node, ast.keyword) and node.arg in WORLD_FIELDS:
            problems.append(f"第 {node.value.lineno if hasattr(node.value, 'lineno') else 0} 行：以關鍵字傳入 {node.arg}")
        elif isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id if isinstance(node.func, ast.Name) else ""
            if name in WORLD_WRITE_METHODS and not relative.startswith("app/services/"):
                problems.append(f"第 {node.lineno} 行：在 Service 之外呼叫 {name}")
    return problems


def offending_files() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(APP.rglob("*.py")):
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        if relative.startswith("app/services/") or relative in UNRELATED | {REPOSITORY, MODEL}:
            continue
        problems = violations_in(path.read_text(encoding="utf-8-sig"), relative)
        if problems:
            found[relative] = problems
    return found


def test_only_services_write_the_world_state_columns_and_call_the_repository_write_methods():
    assert offending_files() == {}


def test_controllers_never_mention_the_world_state_columns_at_all():
    for path in sorted((APP / "controllers").glob("*.py")):
        names = {node.attr for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))) if isinstance(node, ast.Attribute)}
        assert not names & WORLD_FIELDS, path.name


def test_the_scan_sees_the_repository_writes_and_covers_enough_files():
    assert len(list(APP.rglob("*.py"))) > 50, "沒有掃到預期數量的檔案，掃描可能是空洞的"
    repository = (PROJECT_ROOT / REPOSITORY).read_text(encoding="utf-8-sig")
    assert any("以關鍵字傳入" in problem for problem in violations_in(repository, REPOSITORY)), "掃描看不到 Repository 的寫入，檢查可能是空洞的"
    service = (APP / "services" / "rpg_world.py").read_text(encoding="utf-8-sig")
    assert violations_in(service, "app/services/rpg_world.py") == []                        # 字典的字串鍵不是寫入存檔


def test_the_scan_really_flags_each_kind_of_violation():
    controller = "app/controllers/game_api_controller.py"
    assert violations_in("def f(s):\n    s.entities_json = {}\n", controller)
    assert violations_in("def f(s):\n    s.goals_json['active'] = []\n    s.milestone_progress_json = {}\n", controller)
    assert violations_in("def f(s):\n    s.milestone_progress_json |= {}\n", controller)
    assert violations_in("def f(r):\n    r.update_world_state(1, 'x', expected_version=1, elapsed_months=3)\n", controller)
    assert violations_in("def f(r):\n    r.finish_turn(1, 'x', 'c')\n", controller)
    assert violations_in("def f(r):\n    r.finish_turn(1, 'x', 'c')\n", "app/services/rpg_turn_runner.py") == []
    assert violations_in("def f(s):\n    s.execute(update(RpgSession).values(entities_json={}))\n", "app/observability/x.py")
    assert violations_in("def f(w):\n    w['entities_json'] = {}\n", controller)
    assert violations_in("def f(s):\n    return s.entities_json\n", controller) == []                # 讀取不受限制
