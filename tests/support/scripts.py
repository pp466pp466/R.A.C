"""載入 scripts/ 底下的維運腳本作為模組（scripts 不是套件，測試用路徑載入）。"""
from __future__ import annotations

import importlib.util
import sys
from types import ModuleType

from app.config import PROJECT_ROOT


def load_script(name: str) -> ModuleType:
    """載入 scripts/<name>.py；同一個名稱只載入一次（腳本之間可用 import 互相引用，所以也登記到 sys.modules）。"""
    if name in sys.modules:
        return sys.modules[name]
    scripts_dir = str(PROJECT_ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
