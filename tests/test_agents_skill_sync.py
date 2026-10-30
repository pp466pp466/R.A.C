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
    # 2026-10-28：備援模型、NPC 角色卡、內容分級、年齡確認與草案個資
    (5, "備援模型：主要模型發生 429、5xx 或單次呼叫逾時 20 秒時，一律切換到備援模型：客服為 gemini-3.5-flash-lite（GEMINI_FALLBACK_MODEL），RPG 為 OPENROUTER_FALLBACK_MODEL（RPG 不切換到 Gemini）", "references/llm-streaming.md"),
    (5, "暫時性錯誤 429、5xx、逾時則改為切換到備援模型，這次切換視為該次重試，不受「已耗時小於 15 秒」的限制", "references/llm-streaming.md"),
    (5, "後端單次請求總預算（含重試與切換備援）30 秒", "references/llm-streaming.md"),
    (6, "RPG 的「當前世界狀態」包含在場 NPC 的角色卡切片", "references/llm-streaming.md"),
    (7, "NPC 角色卡：NPC 的特質（表層或隱藏）、弧線階段", "references/business-encoding.md"),
    (7, "內容分級：存檔帶 content_tier 與 sexual_content_level（0 至 3）旗標", "references/business-encoding.md"),
    # 2026-10-28 後續修訂：年齡確認改為生日加年齡區間，未滿 13 歲不得註冊，13 至 17 歲只能使用安全模式
    (7, "未滿 18 歲者一律為安全模式，sexual_content_level 固定為 0", "references/business-encoding.md"),
    (7, "成年者由玩家主動在 sexual_content_level 1（浪漫、曖昧）與 2（感官但不露骨）之間切換，預設從 0 開始", "references/business-encoding.md"),
    (7, "系統只提醒玩家可切換分級，不得自動調高", "references/business-encoding.md"),
    (10, "年齡確認：註冊（含 Google 首次註冊）須填寫生日，並從下拉式選項選擇年齡區間", "references/auth-security.md"),
    (10, "未滿 13 歲者不得註冊（不建立帳號，也不保存其填寫的生日）", "references/auth-security.md"),
    (10, "13 至 17 歲者可以使用本平台（含 AI 功能），但只能使用安全模式", "references/auth-security.md"),
    (10, "管理員後台只提供年齡區間的彙總統計，不顯示個別使用者的生日", "references/auth-security.md"),
    (11, "連續 12 個月未登入即直接刪除（不經 7 天寬限期", "references/privacy-observability.md"),
    (11, "清除連續 12 個月未登入的帳號", "references/privacy-observability.md"),
    # 2026-10-29：閒置帳號清除不納入管理員（age-verification-birthdate 的假設，經使用者確認後寫入規範）
    (11, "角色為 admin 的帳號不納入，否則唯一的管理員閒置一年就會消失", "references/privacy-observability.md"),
    (11, "閒置帳號清除不納入管理員", "references/privacy-observability.md"),
    (11, "以去識別的統計方式了解用戶群", "references/privacy-observability.md"),
    (10, "玩家撰寫的個性、角色與世界文字同樣視為使用者文字", "references/auth-security.md"),
    (10, "待審的性格原型草案（僅含去識別後的草案內容，不含對話全文）", "references/auth-security.md"),
    (11, "性格原型草案：草案含玩家衍生文字，視為個資", "references/privacy-observability.md"),
    # 2026-10-30：RPG 以記事簿取代摘要（free 2000、plus 5000、vip 10000 字），AI 溢出可用預備空間，玩家編輯不得超過容量
    (6, "RPG 的長期記憶是記事簿（見下方），**RPG 不使用摘要**：「記事簿 + 最近 10 輪原文 + 當前世界狀態」", "references/llm-streaming.md"),
    (6, "容量依會員等級為 free 2000、plus 5000、vip 10000 字（Unicode 字元，NFC 正規化後計），另有容量 25% 的預備空間（500 / 1250 / 2500 字），預備空間只供 AI 寫入", "references/llm-streaming.md"),
    (6, "每完成 10 輪（第 10、20、30… 輪，中斷的回合不計）", "references/llm-streaming.md"),
    (6, "新條目超過剩餘空間時仍完整寫入，但總長不得超過容量加預備空間", "references/llm-streaming.md"),
    (6, "記事簿長度已超過容量時為「已滿」：不呼叫模型、不補抓被跳過的輪次", "references/llm-streaming.md"),
    (6, "儲存後的長度不得超過容量，沒有預備空間", "references/llm-streaming.md"),
    (5, "只有客服使用 GEMINI_MODEL", "references/llm-streaming.md"),
    (9, "變數名稱 GEMINI_API_KEY（只有客服使用）；RPG 用途使用 OpenAI 相容端點，變數名稱 OPENROUTER_API_KEY", "references/deployment.md"),
    (9, "所有機密（Gemini 金鑰、OpenRouter 金鑰、SECRET_KEY", "references/deployment.md"),
    (5, "模型為環境變數 OPENROUTER_MODEL（備援為 OPENROUTER_FALLBACK_MODEL），不使用 Gemini", "references/llm-streaming.md"),
    (6, "OPENROUTER_MODEL，備援 OPENROUTER_FALLBACK_MODEL，不使用 Gemini", "references/llm-streaming.md"),
    (7, "RPG 記事簿容量 2000 / 5000 / 10000 字（見第 6 節）", "references/business-encoding.md"),
    (10, "記事簿內容，含由玩家文字抓取的重點，亦同", "references/auth-security.md"),
    (11, "RPG 記事簿：內容由玩家文字衍生，視為個資", "references/privacy-observability.md"),
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


def test_every_bullet_of_section_10_is_mirrored_verbatim_in_the_auth_security_reference():
    """第 10 節的 reference 是完整的逐字鏡像（其他章節的 reference 是摘要，不適用）。

    上面的片段比對只能證明「某一段原文還在」；條文改了其他字（例如密碼長度、年齡確認的做法）卻沒同步時，
    片段比對不會失敗。這裡逐條、整行比對，AGENTS.md 這一節任何一條被改動，鏡像沒跟著改就會失敗。
    """
    mirror = (SKILL_DIR / "references" / "auth-security.md").read_text(encoding="utf-8-sig")
    mirror_lines = {line.rstrip() for line in mirror.split("\n")}
    bullets = [line.rstrip() for line in agents_section(10).split("\n") if re.match(r"\s*- ", line)]
    assert len(bullets) >= 12, "沒有讀到預期數量的條文，比對可能是空洞的"
    stale = [line[:60] for line in bullets if line not in mirror_lines]
    assert not stale, f"references/auth-security.md 沒有與 AGENTS.md 第 10 節逐字一致：{stale}"


@pytest.mark.parametrize("key", ["- 保存期限：", "- 刪除帳號：", "- 當事人權利：", "- 排程工作"])
def test_the_section_11_retention_and_rights_bullets_are_mirrored_verbatim(key):
    """第 11 節的這四條在 reference 裡是逐字鏡像（「絕不記錄」在 reference 是摘要版，另以片段檢查）。"""
    agents_lines = [line.strip() for line in agents_section(11).split("\n") if line.strip().startswith(key)]
    assert len(agents_lines) == 1, key
    mirror = (SKILL_DIR / "references" / "privacy-observability.md").read_text(encoding="utf-8-sig")
    assert agents_lines[0] in {line.strip() for line in mirror.split("\n")}, f"privacy-observability.md 沒有逐字鏡像：{key}"


def test_the_section_7_content_rating_bullet_is_mirrored_verbatim():
    agents_line = next(line.strip() for line in agents_section(7).split("\n") if line.strip().startswith("- 內容分級："))
    mirror = (SKILL_DIR / "references" / "business-encoding.md").read_text(encoding="utf-8-sig")
    assert agents_line in {line.strip() for line in mirror.split("\n")}


def test_the_birthday_and_age_range_are_never_logged_and_the_old_checkbox_rule_is_gone_everywhere():
    mirror = (SKILL_DIR / "references" / "privacy-observability.md").read_text(encoding="utf-8-sig")
    assert "使用者 email、生日與年齡區間、使用者訊息全文" in agents_section(11)
    assert "使用者 email、生日與年齡區間、使用者訊息全文" in mirror
    for stale in ("以與同意條款分開的勾選確認年滿 18 歲", "不蒐集生日、年齡或證件", "既有帳號於帳號設定頁補確認"):
        assert stale not in AGENTS, f"AGENTS.md 仍有已被取代的年齡規則：{stale}"
        for path in SKILL_DIR.rglob("*.md"):
            assert stale not in path.read_text(encoding="utf-8-sig"), f"{path.name} 仍有已被取代的年齡規則：{stale}"


def test_skill_core_rules_mention_the_new_rules_and_checklist_items():
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8-sig")
    assert "unique_when_not_null" in skill
    assert "alembic.ini 與 script.py.mako 必須純 ASCII" in skill
    assert "UTF-8 with BOM" in skill
    assert skill.count("- [ ]") >= 14, "自查清單應包含新增的項目"
    assert "備援為 gemini-3.5-flash-lite" in skill and "age_confirmation_required" in skill


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


def test_rpg_notebook_rules_in_agents_and_rpg_design_doc_state_the_same_numbers():
    """記事簿的數字（容量、預備空間、每 10 輪）同時出現在 AGENTS.md 第 6 節、第 7 節與 docs/RPG.md，且 RPG 上下文不再含摘要。"""
    rpg = (PROJECT_ROOT / "docs" / "RPG.md").read_text(encoding="utf-8-sig")
    for text in (agents_section(6), rpg):
        assert "2000" in text and "5000" in text and "10000" in text
        assert "500 / 1250 / 2500" in text and "25%" in text
    assert "記事簿 + 最近 10 輪原文 + 當前世界狀態" in agents_section(6) and "記事簿 + 最近 10 輪原文 + 當前世界狀態" in rpg
    assert "RPG 不使用摘要" in agents_section(6) and "RPG 不使用摘要" in rpg
    assert "notebook_text" in rpg and "string memory_summary" not in rpg, "資料模型應改用記事簿欄位、不再有摘要欄位"


def test_agents_new_rules_use_the_existing_bullet_structure():
    """新增的條文必須沿用既有的項目符號格式（與同節其他條文一致），而不是另起一套。"""
    section4 = agents_section(4)
    assert re.search(r"^- 可為空的唯一欄位：", section4, re.M)
    assert re.search(r"^  - alembic\.ini 與 migration 範本", section4, re.M), "應為「資料表變更 (Migration)」底下的子項目"
    section8 = agents_section(8)
    assert re.search(r"^  - 以系統地區編碼讀取的檔案與腳本：", section8, re.M), "應為「檔案編碼與 I/O 規範」底下的子項目"
    assert len(re.findall(r"^    - ", section8.split("以系統地區編碼讀取的檔案與腳本：")[1], re.M)) >= 3
    # 2026-10-28 新增的條文同樣是所在章節的項目符號，而不是另起格式
    assert re.search(r"^- 備援模型：", agents_section(5), re.M)
    assert re.search(r"^- 年齡確認：", agents_section(10), re.M)
    assert re.search(r"^  - NPC 角色卡：", agents_section(7), re.M) and re.search(r"^  - 內容分級：", agents_section(7), re.M)
