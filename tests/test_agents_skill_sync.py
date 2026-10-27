"""AGENTS.md 是唯一權威來源；skill（.claude/skills/python-flask-skill）必須與它逐字一致。

這裡守住「實作回饋後補進規範」的幾條規則：它們必須同時存在於 AGENTS.md 的正確章節與 skill 的對應 reference，
避免日後只改其中一邊而悄悄分歧。
"""
import re

import pytest

from app.config import PROJECT_ROOT

AGENTS = (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8-sig")
SKILL_DIR = PROJECT_ROOT / ".claude" / "skills" / "python-flask-skill"

# (AGENTS.md 章節編號, 規則中的一段原文, skill 內對應的檔案)
SYNCED_RULES = [
    (4, "可為空的唯一欄位：SQL Server 的 UNIQUE 約束只允許一個 NULL，SQLite 則允許多個", "references/database.md"),
    (4, "部分唯一索引（sqlite_where 與 mssql_where，以 ORM 表達式建立，不得使用原生 SQL 字串；專案提供 unique_when_not_null()）", "references/database.md"),
    (4, "alembic.ini 與 migration 範本 (script.py.mako) 必須是純 ASCII", "references/database.md"),
    (4, "alembic.ini 不得含連線字串，連線一律取自 DATABASE_URL", "references/database.md"),
    (8, "唯一例外是必須純 ASCII 的檔案（見下方檔案編碼規範）", "references/business-encoding.md"),
    (8, "以系統地區編碼讀取的檔案與腳本：", "references/business-encoding.md"),
    (8, "內含繁體中文或其他非 ASCII 字元的 .ps1 須存成 UTF-8 with BOM", "references/business-encoding.md"),
    (8, "一律以 Python 撰寫（UTF-8、參數以清單傳遞）；.ps1 只作為純 ASCII 的薄包裝", "references/business-encoding.md"),
]


def agents_section(number: int) -> str:
    match = re.search(rf"^## {number}\. .*?$(.*?)(?=^## \d+\. |\Z)", AGENTS, re.S | re.M)
    assert match, f"AGENTS.md 找不到第 {number} 節"
    return match.group(1)


@pytest.mark.parametrize(("section", "fragment", "skill_file"), SYNCED_RULES)
def test_rule_is_in_the_right_agents_section_and_mirrored_verbatim_in_the_skill(section, fragment, skill_file):
    assert fragment in agents_section(section), f"AGENTS.md 第 {section} 節缺少：{fragment}"
    skill_text = (SKILL_DIR / skill_file).read_text(encoding="utf-8-sig")
    assert fragment in skill_text, f"skill 的 {skill_file} 與 AGENTS.md 不一致，缺少：{fragment}"


def test_skill_core_rules_mention_the_new_rules_and_checklist_items():
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8-sig")
    assert "unique_when_not_null" in skill
    assert "alembic.ini 與 script.py.mako 必須純 ASCII" in skill
    assert "UTF-8 with BOM" in skill
    assert skill.count("- [ ]") >= 14, "自查清單應包含新增的項目"


def test_the_rules_describe_what_the_code_actually_does():
    from app.models import base

    assert hasattr(base, "unique_when_not_null")
    assert (PROJECT_ROOT / "alembic.ini").read_bytes().isascii()
    assert (PROJECT_ROOT / "migrations" / "script.py.mako").read_bytes().isascii()
    assert (PROJECT_ROOT / "scripts" / "verify-container.ps1").read_bytes().isascii()
    assert (PROJECT_ROOT / "scripts" / "verify_container.py").is_file()


def test_every_ps1_script_is_ascii_or_has_a_utf8_bom():
    for script in PROJECT_ROOT.rglob("*.ps1"):
        if {".venv", "node_modules", ".git"} & set(script.relative_to(PROJECT_ROOT).parts):
            continue
        raw = script.read_bytes()
        assert raw.isascii() or raw.startswith(b"\xef\xbb\xbf"), f"{script.name} 含非 ASCII 字元卻沒有 UTF-8 BOM"


def test_agents_new_rules_use_the_existing_bullet_structure():
    """新增的條文必須沿用既有的項目符號格式（與同節其他條文一致），而不是另起一套。"""
    section4 = agents_section(4)
    assert re.search(r"^- 可為空的唯一欄位：", section4, re.M)
    assert re.search(r"^  - alembic\.ini 與 migration 範本", section4, re.M), "應為「資料表變更 (Migration)」底下的子項目"
    section8 = agents_section(8)
    assert re.search(r"^  - 以系統地區編碼讀取的檔案與腳本：", section8, re.M), "應為「檔案編碼與 I/O 規範」底下的子項目"
    assert len(re.findall(r"^    - ", section8.split("以系統地區編碼讀取的檔案與腳本：")[1], re.M)) >= 3
