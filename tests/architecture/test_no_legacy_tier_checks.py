"""safe-mode-toggle 2.5 的全碼庫守門：內容模式只有 `safe` 與 `adult` 兩種，舊的三值判定與依年齡的上限已經移除。

1. `app/` 的 Python 不得再有三值的過渡相容層：`CONTENT_TIERS`、`CONTENT_TIER_STANDARD`、`effective_tier`、`content_access`、
   `author_content_cap`、`legacy_*` 判定、`max_tier`、`ContentTierRestricted`、`adult_mode_enabled` 都不得出現；
2. 舊值 `standard` 的字面值只允許出現在 `app/services/content_tier.py` 的讀取正規化：其他檔案不得拿它與內容模式比較
   （`== "standard"`、`in ("safe", "standard", ...)`、`!= "standard"`）。錢包的模型鍵 `standard` 是另一回事，不在這個檢查內；
3. 生效模式只有 `AccessPolicy.effective_mode`／`save_mode`／`mode_state` 一個出處：其他程式不得自己讀 `.content_tier_preference`
   來組裝模式（只有 `AccessPolicy` 與 `CurrentUser` 的欄位轉換可以讀它）。
都以 AST 檢查原始碼並附反向驗證，證明檢查抓得到問題。
"""
import ast

import pytest

from app.config import PROJECT_ROOT

APP = PROJECT_ROOT / "app"
CONTENT_TIER_MODULE = APP / "services" / "content_tier.py"

REMOVED_NAMES = frozenset({
    "CONTENT_TIERS", "CONTENT_TIER_STANDARD", "effective_tier", "content_access", "ContentAccess", "author_content_cap", "max_tier",
    "legacy_parse_tier", "legacy_tier_rank", "legacy_most_conservative", "ContentTierRestricted", "adult_mode_enabled",
    "ContentModeRestricted", "AdultModeUnavailable", "ADULT_RULES_VERSION",
})
# 可以讀取 `.content_tier_preference` 的地方：政策物件（生效模式的唯一出處）與 CurrentUser 的欄位轉換（把資料列複製成請求內的快照）
PREFERENCE_READERS = frozenset({"services/access_policy.py", "services/current_user.py"})


def _python_files():
    return sorted(path for path in APP.rglob("*.py") if "__pycache__" not in path.parts)


def removed_name_violations(source: str) -> list[str]:
    tree = ast.parse(source)
    found = []
    for node in ast.walk(tree):
        name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
        if name in REMOVED_NAMES:
            found.append(f"{name}（第 {node.lineno} 行）")
        elif isinstance(node, ast.alias) and node.name in REMOVED_NAMES:
            found.append(f"匯入 {node.name}")
        elif isinstance(node, ast.arg) and node.arg in REMOVED_NAMES:
            found.append(f"參數 {node.arg}（第 {node.lineno} 行）")
        elif isinstance(node, ast.keyword) and node.arg in REMOVED_NAMES:
            found.append(f"關鍵字 {node.arg}（第 {node.lineno} 行）")
    return found


def _is_standard(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == "standard"


def standard_comparison_violations(source: str) -> list[str]:
    """與內容模式有關的 `standard` 比較：比較式的任一側是 `"standard"`，或 `in` 的容器含 `"standard"`，且另一側的原始碼含 `tier`。"""
    tree = ast.parse(source)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        sides = [node.left, *node.comparators]
        if not any(_is_standard(side) or (isinstance(side, (ast.Tuple, ast.List, ast.Set)) and any(_is_standard(e) for e in side.elts)) for side in sides):
            continue
        if any("tier" in ast.unparse(side).lower() for side in sides):
            found.append(f"第 {node.lineno} 行：{ast.unparse(node)}")
    return found


def preference_read_violations(source: str) -> list[str]:
    tree = ast.parse(source)
    return [f"第 {n.lineno} 行" for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "content_tier_preference"]


# ---- 全碼庫 ----------------------------------------------------------------------------------

@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.relative_to(APP).as_posix())
def test_no_removed_three_value_or_age_cap_name_is_left_in_app(path):
    found = removed_name_violations(path.read_text(encoding="utf-8-sig"))
    assert not found, f"{path.relative_to(APP).as_posix()} 仍使用已移除的名稱：{found}"


@pytest.mark.parametrize("path", [p for p in _python_files() if p != CONTENT_TIER_MODULE], ids=lambda p: p.relative_to(APP).as_posix())
def test_the_legacy_standard_value_is_only_normalized_in_content_tier_py(path):
    found = standard_comparison_violations(path.read_text(encoding="utf-8-sig"))
    assert not found, f"{path.relative_to(APP).as_posix()} 拿舊值 standard 與內容模式比較（只允許在 content_tier.py 讀取正規化）：{found}"


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.relative_to(APP).as_posix())
def test_only_the_policy_and_the_preference_services_read_the_stored_preference(path):
    relative = path.relative_to(APP).as_posix()
    if relative in PREFERENCE_READERS:
        pytest.skip("允許讀取")
    found = preference_read_violations(path.read_text(encoding="utf-8-sig"))
    assert not found, f"{relative} 自己讀取玩家的內容模式偏好 {found}：生效模式請問 AccessPolicy"


def test_the_content_tier_module_defines_the_legacy_value_exactly_once():
    source = CONTENT_TIER_MODULE.read_text(encoding="utf-8-sig")
    assert source.count('LEGACY_TIER_STANDARD = "standard"') == 1


# ---- 反向驗證：檢查抓得到問題 ------------------------------------------------------------------

@pytest.mark.parametrize("source", [
    "from app.models.user import CONTENT_TIERS",
    "x = policy.effective_tier(user)",
    "y = policy.content_access(user).is_adult",
    "def f(max_tier): pass",
    "z = validate_script(raw, max_tier='adult')",
    "raise ContentTierRestricted()",
    "AccessPolicy(adult_mode_enabled=True)",
])
def test_the_name_check_notices_a_removed_name(source):
    assert removed_name_violations(source)


@pytest.mark.parametrize("source", [
    'if save.content_tier == "standard": pass',
    'if "standard" == tier: pass',
    'ok = tier in ("safe", "standard")',
    'ok = version.content_tier != "standard"',
])
def test_the_comparison_check_notices_a_standard_comparison(source):
    assert standard_comparison_violations(source)


@pytest.mark.parametrize("source", [
    'DEFAULT_MODEL_KEY = "standard"',
    'if model_key == "standard": pass',
    'ok = tier in ("safe", "adult")',
])
def test_the_comparison_check_leaves_unrelated_code_alone(source):
    assert not standard_comparison_violations(source)


def test_the_preference_check_notices_a_direct_read():
    assert preference_read_violations("mode = user.content_tier_preference")
    assert not preference_read_violations("mode = policy.effective_mode(user)")
