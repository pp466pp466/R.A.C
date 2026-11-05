"""rpg-protagonist 規格「主角年齡是劇中年齡，開存檔後不可更改」與「主角存放於存檔，姓名與年齡同步」的結構守門。

存檔的主角姓名、角色年齡與主角資料（`player_name`、`character_age`、`protagonist_json`）**只在開存檔時寫入一次**，
而且只由 `SessionRepository.create` 從同一張角色卡寫入——這樣三者永遠一致，開存檔之後也沒有任何方法能修改年齡（錨定事實）。
這裡以 AST 檢查 `app/` 的原始碼：除了模型定義與 `session_repository.py` 的 `create` 之外，不得有人
- 對這三個屬性賦值；
- 在任何呼叫中以同名的關鍵字傳入（`RpgSession(player_name=...)`、`update(RpgSession).values(character_age=...)`）。
讀取不受限制（匯出、顯示要讀）。並附反向驗證，證明檢查真的抓得到違規的寫法。
"""
import ast

from app.config import PROJECT_ROOT

APP = PROJECT_ROOT / "app"
COLUMNS = {"player_name", "character_age", "protagonist_json"}
MODEL = "app/models/rpg_session.py"
REPOSITORY = "app/repositories/session_repository.py"
ONLY_FUNCTION = "create"


def write_sites(source: str) -> list[tuple[int, str, str | None]]:
    """(行號, 欄位名稱, 所在的函式名稱) 的清單：寫入這三個欄位的地方。"""
    tree = ast.parse(source)
    found: list[tuple[int, str, str | None]] = []

    def walk(node: ast.AST, function: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else function
            if isinstance(child, ast.keyword) and child.arg in COLUMNS:
                found.append((child.value.lineno, child.arg, function))
            elif isinstance(child, (ast.Assign, ast.AugAssign)):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                for target in targets:
                    if isinstance(target, ast.Attribute) and target.attr in COLUMNS:
                        found.append((child.lineno, target.attr, function))
            walk(child, name)

    walk(tree, None)
    return found


def offending_files() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(APP.rglob("*.py")):
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        if relative == MODEL:
            continue
        sites = write_sites(path.read_text(encoding="utf-8-sig"))
        if relative == REPOSITORY:
            sites = [site for site in sites if site[2] != ONLY_FUNCTION]
        if sites:
            found[relative] = [f"第 {line} 行：寫入 {column}（在 {function or '模組層'}）" for line, column, function in sites]
    return found


def test_only_session_repository_create_writes_the_protagonist_columns():
    assert offending_files() == {}


def test_the_scan_sees_the_repository_create_and_a_lot_of_files():
    sites = write_sites((APP / "repositories" / "session_repository.py").read_text(encoding="utf-8-sig"))
    assert {column for _, column, function in sites if function == ONLY_FUNCTION} == COLUMNS
    assert len(list(APP.rglob("*.py"))) > 50, "沒有掃到預期數量的檔案，掃描可能是空洞的"


def test_the_scan_really_flags_each_kind_of_write():
    assert write_sites("def f(save):\n    save.character_age = 30\n") == [(2, "character_age", "f")]
    assert write_sites("def f(save):\n    save.player_name += 'x'\n") == [(2, "player_name", "f")]
    assert write_sites("def f():\n    return update(RpgSession).values(character_age=30)\n")[0][1] == "character_age"
    assert write_sites("def f():\n    return RpgSession(protagonist_json={})\n")[0][1] == "protagonist_json"
    assert write_sites("x = Foo(player_name='a')\n") == [(1, "player_name", None)]


def test_reads_and_unrelated_names_are_not_flagged():
    assert write_sites("def f(save):\n    return save.character_age, save.player_name, {'player_name': 1}\n") == []
    assert write_sites("def f(player_name):\n    return player_name\n") == []
    assert write_sites("def f(save):\n    save.other = 1\n") == []
