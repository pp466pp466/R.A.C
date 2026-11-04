"""設計文件（docs/ 底下的 Mermaid 報告）與程式的一致性檢查。

這些測試把「最容易漂移的事實」變成會失敗的檢查：新增資料表、路由、migration、環境變數、分層、前端檔案或 change 而沒有更新
對應的設計文件，就會在這裡失敗。它們只能確認「有沒有提到」，不能確認「描述得對不對」，內容的正確性仍靠更新時對照程式檢視。
維護規則見 docs/README.md；提醒用的技能在 .claude/skills/design-docs-sync/SKILL.md。
"""
import re
from pathlib import Path

import pytest

from app import create_app
from app.config import PROJECT_ROOT, VALID_ENVS, load_config

DOCS = PROJECT_ROOT / "docs"
APP = PROJECT_ROOT / "app"

# 新增設計文件時：加進這裡，並列入 docs/README.md 的索引
DESIGN_DOCS = (
    "系統架構-architecture.md", "資料模型-data-model.md", "帳號與安全-auth-security.md", "客服對話-chat-service.md", "模型呼叫層-llm-layer.md", "前端結構-frontend.md",
    "營運與部署-operations.md", "路線圖-roadmap.md", "project資料結構文件.md",
)
# 不是設計文件的既有檔案（探索與決策紀錄、舊版報告）
OTHER_DOCS = {
    "README.md", "report.md", "RPG.md", "spec_report_v2.1.1.md", "spec_report_v2.2.0.md", "ai_v2_0_0.md",
    "content-policy-research.md",
}
DIAGRAM_TYPES = ("flowchart", "sequenceDiagram", "erDiagram", "stateDiagram-v2", "graph")


def read(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8-sig")


def all_design_text() -> str:
    return "\n".join(read(name) for name in DESIGN_DOCS)


def mermaid_blocks(text: str) -> list[str]:
    return re.findall(r"```mermaid\n(.*?)\n```", text, flags=re.S)


def pitfalls(block: str) -> list[str]:
    """Mermaid 語法中常見、而且在 GitHub 或 VS Code 預覽才會發現的問題。"""
    found = []
    lines = [line for line in block.splitlines() if line.strip()]
    first = lines[0].strip() if lines else ""
    if not first.startswith(DIAGRAM_TYPES):
        found.append(f"不支援的圖類型 {first!r}")
    if "&gt;" in block or "&lt;" in block:
        found.append("標籤使用了 HTML 實體")
    for line in lines:
        if line.count('"') % 2:
            found.append(f"引號沒有成對：{line.strip()}")
        if first.startswith("sequenceDiagram") and ("->>" in line or "-->>" in line) and ";" in line:
            found.append(f"序列圖的訊息含分號（會結束訊息）：{line.strip()}")
    return found


def pitfalls(block: str) -> list[str]:
    """Mermaid 語法中常見、而且要到 GitHub 或 VS Code 預覽才會發現的問題。"""
    found = []
    lines = [line for line in block.splitlines() if line.strip()]
    first = lines[0].strip() if lines else ""
    if not first.startswith(DIAGRAM_TYPES):
        found.append(f"不支援的圖類型 {first!r}")
    if "&gt;" in block or "&lt;" in block:
        found.append("標籤使用了 HTML 實體")
    for line in lines:
        if line.count('"') % 2:
            found.append(f"引號沒有成對：{line.strip()}")
        if first.startswith("sequenceDiagram") and ("->>" in line or "-->>" in line) and ";" in line:
            found.append(f"序列圖的訊息含分號（會結束訊息）：{line.strip()}")
    return found


@pytest.fixture(scope="module")
def app():
    return create_app(load_config(environ={"APP_ENV": "testing"}))


# ---- 結構 -------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", DESIGN_DOCS)
def test_each_design_doc_has_the_header_a_diagram_and_a_maintenance_note(name):
    text = read(name)
    header = "\n".join(text.splitlines()[:5])
    assert re.search(r"^> 狀態：.+ ｜ 最後更新：\d{4}-\d{2}-\d{2} ｜ 對應 change：", header, flags=re.M), f"{name} 缺少標頭（狀態、最後更新、對應 change）"
    assert mermaid_blocks(text), f"{name} 沒有任何 Mermaid 圖"
    assert "**何時更新本文**" in text, f"{name} 缺少「何時更新本文」"


def test_the_index_lists_every_design_doc_and_nothing_unregistered_hides_in_docs():
    index = read("README.md")
    for name in DESIGN_DOCS:
        assert f"]({name})" in index, f"docs/README.md 的索引沒有列出 {name}"
    present = {path.name for path in DOCS.glob("*.md")}
    unregistered = present - set(DESIGN_DOCS) - OTHER_DOCS
    assert unregistered == set(), f"docs/ 有未登記的 Markdown：{sorted(unregistered)}（加進 tests/test_design_docs.py 的 DESIGN_DOCS 並列入索引）"


@pytest.mark.parametrize("name", (*DESIGN_DOCS, "README.md"))
def test_mermaid_blocks_use_a_supported_diagram_type_and_avoid_known_syntax_pitfalls(name):
    text = read(name)
    assert text.count("```") % 2 == 0, f"{name} 的程式碼區塊沒有成對"
    for block in mermaid_blocks(text):
        assert pitfalls(block) == [], f"{name}：{pitfalls(block)}"


@pytest.mark.parametrize("name", (*DESIGN_DOCS, "README.md"))
def test_relative_markdown_links_point_to_files_that_exist(name):
    for target in re.findall(r"\]\(([^)#\s]+\.md)(?:#[^)]*)?\)", read(name)):
        assert (DOCS / target).is_file(), f"{name} 的連結 {target} 找不到"


# ---- 資料模型 ----------------------------------------------------------------------------------


def migration_revisions() -> tuple[list[str], str]:
    revisions, downs = [], set()
    for path in sorted((PROJECT_ROOT / "migrations" / "versions").glob("*.py")):
        source = path.read_text(encoding="utf-8-sig")
        revisions.append(re.search(r'^revision\s*(?::\s*str)?\s*=\s*"([^"]+)"', source, flags=re.M).group(1))
        down = re.search(r'^down_revision\s*(?::[^=]+)?=\s*(?:"([^"]+)"|None)', source, flags=re.M)
        if down and down.group(1):
            downs.add(down.group(1))
    head = [rev for rev in revisions if rev not in downs]
    assert len(head) == 1, f"migration 有分岔或沒有頭：{head}"
    return revisions, head[0]


def test_every_table_and_the_latest_migration_are_described_in_the_data_model_doc():
    from app.models import Base

    text = read("資料模型-data-model.md")
    missing = [table for table in Base.metadata.tables if table not in text]
    assert missing == [], f"資料模型-data-model.md 沒有提到資料表：{missing}"
    revisions, head = migration_revisions()
    assert f"`{head}`" in text, f"資料模型-data-model.md 的「資料庫版本」不是最新的 migration：{head}"
    for revision in revisions:
        assert revision.split("_", 1)[0] in text, f"資料模型-data-model.md 的 migration 歷程缺少 {revision}"


def test_the_entity_relationship_diagram_declares_every_table():
    from app.models import Base

    blocks = [block for block in mermaid_blocks(read("資料模型-data-model.md")) if block.lstrip().startswith("erDiagram")]
    assert blocks, "資料模型-data-model.md 沒有 erDiagram"
    for table in Base.metadata.tables:
        assert re.search(rf"^\s*{table}\s*\{{", blocks[0], flags=re.M), f"erDiagram 沒有宣告實體 {table}"


# ---- 端點 --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prefix, doc",
    [("/api/auth/", "帳號與安全-auth-security.md"), ("/api/cs/", "客服對話-chat-service.md"), ("/api/admin/", "帳號與安全-auth-security.md")],
)
def test_every_api_route_appears_in_its_design_doc(app, prefix, doc):
    text = read(doc)
    rules = [rule for rule in app.url_map.iter_rules() if rule.rule.startswith(prefix)]
    assert sum(len(rule.methods - {"HEAD", "OPTIONS"}) for rule in rules) >= 6, "沒有掃到預期數量的路由，檢查可能是空洞的"
    assert [rule.rule for rule in rules if rule.rule not in text] == [], f"{doc} 沒有提到路由"


def test_the_pages_are_mentioned_in_the_docs(app):
    text = all_design_text()
    for rule in app.url_map.iter_rules():
        if rule.endpoint.startswith(("auth_pages.", "cs_pages.", "admin_pages.")):
            assert rule.rule in text, f"設計文件沒有提到頁面 {rule.rule}"


# ---- 分層與注入 --------------------------------------------------------------------------------


def test_the_architecture_doc_names_every_layer_and_every_injected_dependency():
    import inspect

    from tests.architecture.test_layering import FORBIDDEN_IMPORTS

    text = read("系統架構-architecture.md")
    for layer in FORBIDDEN_IMPORTS:
        # 不在意欄位前後的空白：編輯器的 Markdown 格式化會把表格對齊補上空白，那只是排版
        assert re.search(rf"\|\s*{re.escape(layer)}\s*\|", text), f"系統架構-architecture.md 的禁止匯入表沒有 {layer} 層"
    injected = [name for name, p in inspect.signature(create_app).parameters.items() if p.kind is inspect.Parameter.KEYWORD_ONLY]
    assert [name for name in injected if name not in text] == [], "系統架構-architecture.md 沒有提到 create_app 的注入參數"


# ---- 模型呼叫層 --------------------------------------------------------------------------------


def test_the_llm_doc_covers_every_ledger_status_and_every_llm_setting():
    from app.models.llm_call_log import CALL_STATUSES

    text = read("模型呼叫層-llm-layer.md")
    assert [status for status in CALL_STATUSES if f"`{status}`" not in text] == []
    env_names = {
        line.partition("=")[0].strip()
        for line in (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    llm_settings = sorted(name for name in env_names if name.startswith(("GEMINI_", "LLM_")))
    assert len(llm_settings) >= 8
    assert [name for name in llm_settings if f"`{name}`" not in text and name not in text] == [], "模型呼叫層-llm-layer.md 沒有提到設定"


# ---- 營運 --------------------------------------------------------------------------------------


def test_the_operations_doc_covers_every_environment_and_every_maintenance_command(app):
    text = read("營運與部署-operations.md")
    assert [env for env in VALID_ENVS if env not in text] == []
    commands = sorted(app.cli.commands)
    assert {"create-admin", "purge-inactive-accounts", "age-stats"} <= set(commands)
    assert [command for command in commands if command not in text] == [], "營運與部署-operations.md 沒有提到維運指令"


# ---- 前端 --------------------------------------------------------------------------------------


def test_the_frontend_doc_names_every_client_module_of_the_chat_page_the_admin_console_and_the_shared_components():
    text = read("前端結構-frontend.md")
    files = sorted(path.name for folder in ("cs", "ui", "admin") for path in (APP / "static" / "js" / folder).glob("*.js"))
    assert len(files) >= 9
    assert {"admin.js", "admin-format.js"} <= set(files)
    assert [name for name in files if name not in text] == [], "前端結構-frontend.md 沒有提到前端檔案"


# ---- 路線圖 ------------------------------------------------------------------------------------


def test_the_roadmap_lists_every_archived_and_active_change():
    changes = PROJECT_ROOT / "openspec" / "changes"
    archived = [re.sub(r"^\d{4}-\d{2}-\d{2}-", "", path.name) for path in (changes / "archive").iterdir() if path.is_dir()]
    active = [path.name for path in changes.iterdir() if path.is_dir() and path.name != "archive"]
    assert archived, "沒有掃到封存的 change，檢查可能是空洞的"
    text = read("路線圖-roadmap.md")
    assert [name for name in (*archived, *active) if name not in text] == [], "路線圖-roadmap.md 沒有列出 change"


# ---- 反向驗證：檢查本身抓得到問題 -----------------------------------------------------------------


def test_the_checks_really_notice_a_missing_table_route_or_pitfall(tmp_path):
    """用暫時的內容證明上面的比對不是空洞的。"""
    assert "ghost_table" not in read("資料模型-data-model.md")
    assert "/api/cs/ghost" not in read("客服對話-chat-service.md")
    block = mermaid_blocks("```mermaid\nsequenceDiagram\n    A->>B: 第一句; 第二句\n```\n")[0]
    assert pitfalls(block) and "分號" in pitfalls(block)[0]
    assert pitfalls('flowchart TD\n    A["沒有結尾引號] --> B') and pitfalls("pie\n    A : 1")
    assert pitfalls('flowchart TD\n    A["a &gt; b"] --> B')
    assert pitfalls('flowchart TD\n    A["正常"] --> B["也正常"]') == []
    assert mermaid_blocks("沒有圖") == []
