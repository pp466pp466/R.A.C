"""game-wallet 規格「帳本只增不改」「錢包不得為負」的結構守門。

餘額只能經過 `WalletRepository` 的條件式更新（以版本號比對）異動，帳本只由它新增；任何別處各寫各的，
都會繞過 CAS、冪等鍵與 `seq` 的連續性。這裡以 AST 檢查原始碼：`app/` 裡除了模型定義與 `wallet_repository.py` 之外，不得有人
- 建立 `Wallet(...)`／`WalletEntry(...)` 物件，或對它們執行 `update(...)`／`delete(...)`；
- 對餘額、版本與帳本欄位賦值，或以 `values(free_balance=...)` 寫入。
Controller 也不得直接匯入錢包 Repository（分層：Controller 只呼叫 Service）。並附反向驗證。
"""
import ast

from app.config import PROJECT_ROOT

APP = PROJECT_ROOT / "app"
ALLOWED = {"app/models/wallet.py", "app/repositories/wallet_repository.py"}
WALLET_COLUMNS = {
    "free_balance", "paid_balance", "free_delta", "paid_delta", "free_after", "paid_after", "idempotency_key", "resolution", "ref_key",
}
WALLET_MODELS = {"Wallet", "WalletEntry"}


def violations_in(source: str) -> list[str]:
    tree = ast.parse(source)
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and ast.unparse(node.func).rsplit(".", 1)[-1] == "values":
            # `update(...).values(free_balance=...)`：只有這種寫法才是寫入資料庫（資料類別的同名關鍵字引數不是）
            for keyword in node.keywords:
                if keyword.arg in WALLET_COLUMNS:
                    problems.append(f"第 {node.lineno} 行：以 values() 寫入錢包欄位 {keyword.arg}")
        elif isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Attribute) and t.attr in WALLET_COLUMNS for t in targets):
                problems.append(f"第 {node.lineno} 行：對錢包欄位賦值")
        elif isinstance(node, ast.Call):
            callee = ast.unparse(node.func)
            first = ast.unparse(node.args[0]) if node.args else ""
            if callee in WALLET_MODELS:
                problems.append(f"第 {node.lineno} 行：直接建立 {callee}")
            elif callee in ("update", "delete", "insert", "sa.update", "sa.delete", "sa.insert") and first in WALLET_MODELS:
                problems.append(f"第 {node.lineno} 行：直接 {callee}({first})")
    return problems


def offending_files() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(APP.rglob("*.py")):
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        if relative in ALLOWED:
            continue
        problems = violations_in(path.read_text(encoding="utf-8-sig"))
        if problems:
            found[relative] = problems
    return found


def test_only_the_wallet_repository_writes_balances_and_the_ledger():
    assert offending_files() == {}


def test_controllers_never_import_the_wallet_repository():
    for path in sorted((APP / "controllers").glob("*.py")):
        text = path.read_text(encoding="utf-8-sig")
        assert "wallet_repository" not in text and "WalletRepository" not in text, path.name


def test_the_scan_sees_the_repository_once_it_exists_and_covers_enough_files():
    assert len(list(APP.rglob("*.py"))) > 50, "沒有掃到預期數量的檔案，掃描可能是空洞的"
    repository = APP / "repositories" / "wallet_repository.py"
    if repository.exists():
        assert violations_in(repository.read_text(encoding="utf-8-sig")), "掃描看不到 Repository 的寫入，檢查可能是空洞的"


def test_the_scan_really_flags_each_kind_of_violation():
    assert violations_in("def f(w):\n    w.free_balance = 5\n")
    assert violations_in("def f(w):\n    w.paid_balance -= 30\n")
    assert violations_in("def f(s):\n    s.add(Wallet(user_id=1))\n")
    assert violations_in("def f(s):\n    s.add(WalletEntry(user_id=1))\n")
    assert violations_in("def f(s):\n    s.execute(update(Wallet).values(version=3))\n")
    assert violations_in("def f(s):\n    s.execute(update(x).values(free_balance=5))\n")
    assert violations_in("def f(s):\n    s.execute(delete(WalletEntry))\n")
    assert violations_in("def f(e):\n    e.resolution = 'settled'\n")
    assert violations_in("def f(w):\n    return w.free_balance\n") == []             # 讀取不受限制
    assert violations_in("def f(s):\n    return s.get(Wallet, 1)\n") == []
