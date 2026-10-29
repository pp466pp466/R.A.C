"""access-control 規格「對話資料的存取一律帶擁有者條件」：對話相關的 Repository 沒有只憑識別碼的方法。

這個測試以 AST 檢查原始碼，不依賴執行：`ChatRepository` 的每個公開方法，第一個參數（`self` 之後）都必須是 `user_id`；
而且它不繼承任何基底類別——`BaseRepository` 的 `get`、`get_by_public_id`、`list`、`delete` 都不帶擁有者，
繼承了就等於留一條能繞過隔離的路。
"""
import ast

from app.config import PROJECT_ROOT

SOURCE = PROJECT_ROOT / "app" / "repositories" / "chat_repository.py"


def violations(source: str, class_name: str = "ChatRepository") -> list[str]:
    tree = ast.parse(source)
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name), None)
    assert cls is not None, f"找不到 {class_name}"
    problems = []
    if cls.bases:
        problems.append(f"{class_name} 不得繼承任何基底類別（基底的方法不帶擁有者）：{[ast.unparse(b) for b in cls.bases]}")
    for node in cls.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name.startswith("_"):
            continue
        params = [a.arg for a in node.args.args]
        if params[:2] != ["self", "user_id"]:
            problems.append(f"{node.name}：第一個參數必須是 user_id，實際是 {params[1:2] or '（沒有參數）'}")
    return problems


def test_every_public_method_of_the_chat_repository_takes_the_owner_first():
    assert violations(SOURCE.read_text(encoding="utf-8-sig")) == []


def test_the_check_is_not_vacuous_it_sees_the_real_methods():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8-sig"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ChatRepository")
    public = [n.name for n in cls.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")]
    assert len(public) >= 15 and {"get_conversation", "append_message", "delete_conversation"} <= set(public)


def test_the_check_really_fails_for_a_method_without_the_owner_or_with_a_base_class():
    """反向驗證：用暫時的原始碼證明這個檢查抓得到問題。"""
    missing_owner = "class ChatRepository:\n    def get_by_public_id(self, public_id): ...\n"
    assert "get_by_public_id" in violations(missing_owner)[0]
    wrong_order = "class ChatRepository:\n    def get(self, conversation_id, user_id): ...\n"
    assert violations(wrong_order)
    with_base = "class ChatRepository(BaseRepository):\n    def get(self, user_id): ...\n"
    assert "基底" in violations(with_base)[0]
    private_is_fine = "class ChatRepository:\n    def _helper(self, statement): ...\n    def get(self, user_id): ...\n"
    assert violations(private_is_fine) == []

