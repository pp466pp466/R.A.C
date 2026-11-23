"""Vue 掛載之前的遮蔽規則：頁面載入較慢時，使用者不應該看到原始的 `{{ }}` 範本文字。

2026-11-23 展示環境（雪梨，從台灣載入要等十幾個腳本）的遊戲主畫面：根元素有 `v-cloak`，但 `game.css` 的 `[v-cloak] { display: none; }`
被同檔後面的 `.gm-shell { display: flex; }` 蓋掉（特異性相同、寫在後面），掛載之前整份範本（`{{ title }}`、所有 Modal 與提示）直接露出來。
修正是每個樣式檔的規則都加 `!important`；這裡守住它，之後新增樣式檔或改寫規則時不會再退回去。
"""
import re

from app.config import PROJECT_ROOT

CSS_DIR = PROJECT_ROOT / "app" / "static" / "css"
TEMPLATES = PROJECT_ROOT / "app" / "templates"
RULE = re.compile(r"\[v-cloak\]\s*\{([^}]*)\}")


def declared():
    found = {}
    for path in sorted(CSS_DIR.glob("*.css")):
        rules = RULE.findall(path.read_text(encoding="utf-8-sig"))
        if rules:
            found[path.name] = rules
    return found


def test_every_stylesheet_that_declares_the_cloak_rule_forces_it_with_important():
    found = declared()
    assert {"auth.css", "game.css", "cs.css", "admin.css", "scripts.css", "wallet.css"} <= set(found), sorted(found)
    for name, rules in found.items():
        for body in rules:
            assert re.search(r"display:\s*none\s*!important", body), f"{name} 的 [v-cloak] 規則沒有 !important：{body.strip()}"


def test_the_game_session_root_needs_the_important_because_its_own_class_sets_display():
    """反向驗證這條規則為什麼必要：`.gm-shell` 自己設定了 display（所以沒有 !important 的 `[v-cloak]` 會被蓋掉）；根元素確實有 `v-cloak`。"""
    css = (CSS_DIR / "game.css").read_text(encoding="utf-8-sig")
    shell = re.search(r"^\.gm-shell\s*\{([^}]*)\}", css, re.M)
    assert shell and re.search(r"display:\s*flex", shell.group(1))
    html = (TEMPLATES / "game" / "session.html").read_text(encoding="utf-8-sig")
    assert '<div id="game-app" class="gm-shell" v-cloak>' in html
    assert "css/game.css" in html                                                        # 頁面確實載入含有遮蔽規則的樣式檔


def loaded_stylesheets(path):
    """範本載入的樣式檔：自己寫的加上它 `extends` 的父範本（遞迴，含 base.html）。"""
    text = path.read_text(encoding="utf-8-sig")
    names = set(re.findall(r"css/([\w-]+\.css)", text))
    parent = re.search(r"\{%\s*extends\s+[\"']([^\"']+)[\"']\s*%\}", text)
    return names | (loaded_stylesheets(TEMPLATES / parent.group(1)) if parent else set())


def test_every_template_with_a_cloaked_root_loads_a_stylesheet_that_has_the_rule():
    """每個有 `v-cloak` 的範本，載入的樣式檔（含它繼承的父範本，如 base.html、auth_base.html）至少有一個宣告遮蔽規則。"""
    with_rule = set(declared())
    checked = 0
    for path in sorted(TEMPLATES.rglob("*.html")):
        if "v-cloak" not in path.read_text(encoding="utf-8-sig"):
            continue
        checked += 1
        loaded = loaded_stylesheets(path)
        assert loaded & with_rule, f"{path.relative_to(TEMPLATES)} 有 v-cloak，但沒有載入任何宣告 [v-cloak] 規則的樣式檔（載入：{sorted(loaded)}）"
    assert checked >= 8, "沒有掃到預期數量的範本，比對可能是空洞的"
