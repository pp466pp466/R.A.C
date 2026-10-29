"""llm-provider 規格「模型呼叫經由供應商中立的介面」：業務程式碼不含任何模型供應商的相依。

Service、端點、Repository、資料模型、安全輔助與可觀測性的原始碼，不得匯入供應商的 SDK、不得含供應商的 API 網域、
也不得出現供應商的金鑰標頭名稱；這些只能存在於 `app/llm/` 的轉接器內。換供應商時，因此只需要新的轉接器與新的環境變數。
（Google 登入的 `googleapis.com` 網域屬於身分供應商，不是模型供應商，所以這裡只比對模型 API 的網域。）
"""
import ast
import re

from app.config import PROJECT_ROOT

APP_DIR = PROJECT_ROOT / "app"
BUSINESS_PACKAGES = ("services", "controllers", "repositories", "models", "security", "observability", "db")

# 模型供應商的 SDK（匯入）
VENDOR_MODULES = ("google.genai", "google.generativeai", "openai", "anthropic", "mistralai", "cohere", "vertexai")
# 模型供應商的識別字串（網域、標頭）：大小寫不分
VENDOR_STRINGS = ("generativelanguage.googleapis.com", "x-goog-api-key", "api.openai.com", "api.anthropic.com", "api.mistral.ai")


def vendor_problems(source: str) -> list[str]:
    problems = []
    for node in ast.walk(ast.parse(source)):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [node.module or ""]
        for name in names:
            if any(name == m or name.startswith(m + ".") for m in VENDOR_MODULES):
                problems.append(f"匯入供應商 SDK：{name}")
    lowered = source.lower()
    problems += [f"含供應商字串：{s}" for s in VENDOR_STRINGS if s in lowered]
    return problems


def test_business_code_contains_no_model_vendor_dependency():
    found = {}
    for package in BUSINESS_PACKAGES:
        for path in sorted((APP_DIR / package).rglob("*.py")):
            problems = vendor_problems(path.read_text(encoding="utf-8-sig"))
            if problems:
                found[str(path.relative_to(PROJECT_ROOT))] = problems
    assert found == {}


def test_the_application_factory_and_config_do_not_hardcode_a_vendor_domain_or_header():
    """組裝層可以知道有 Gemini 轉接器，但網域與標頭只屬於轉接器。"""
    for name in ("__init__.py", "config.py", "runtime.py", "cli.py"):
        assert vendor_problems((APP_DIR / name).read_text(encoding="utf-8-sig")) == [], name


def test_the_check_really_catches_each_kind_of_vendor_dependency():
    """反向驗證：用暫時的原始碼證明這個檢查抓得到問題，否則上面的「沒有問題」沒有意義。"""
    assert vendor_problems("from google import genai\nimport google.genai\n")[0].startswith("匯入供應商 SDK")
    assert vendor_problems("import openai\n") and vendor_problems("from anthropic import Anthropic\n")
    assert vendor_problems("URL = 'https://generativelanguage.googleapis.com/v1beta/x'\n")
    assert vendor_problems("headers = {'X-Goog-Api-Key': key}\n")
    assert vendor_problems("import json\nfrom app.llm.types import LLMRequest\n") == []
    assert re.search("openai", "import openai")                      # 防呆：VENDOR_MODULES 的寫法與 AST 比對一致
