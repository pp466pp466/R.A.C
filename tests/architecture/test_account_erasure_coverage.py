"""account-deletion 與 account-retention 規格：新增的資料表不能漏掉「抹除一個人」。

`test_user_foreign_keys.py` 保證每個指向 `users.id` 的外鍵都有 CASCADE 或 SET NULL。這裡更進一步：
把「會被抹除流程處理的資料表」登記成明確的清單，新增資料表時這個測試會失敗，逼開發者決定它屬於哪一類，
而不是默默留下不屬於任何人的個資。三類：
- 串聯刪除（CASCADE）：資料跟著使用者或對話一起消失。
- 匿名化保留（SET NULL）：紀錄保留，使用者欄位變空值（用量帳本、稽核）。
- 沒有外鍵但以整數存編號：必須由抹除流程明確處理（目前只有帳本的 `conversation_id`，由 `clear_ledger_conversation_refs` 在刪除前清空；
  刪除紀錄的 `user_id` 本來就是刪除後才寫入，不指向任何存在的列）。
"""
from sqlalchemy import Column, ForeignKey, Integer, MetaData, Table

from app.models import Base

CASCADED_FROM_USERS = {
    "user_identities.user_id", "auth_tokens.user_id", "chat_conversations.user_id",
    "lock_strikes.user_id", "lock_appeals.user_id",     # google-first-login：鎖定計次與申訴（申訴是個資，隨帳號刪除）
    "scripts.creator_id", "sessions.user_id",           # rpg-data-and-script：劇本與存檔（玩家文字，隨帳號刪除；失敗時偏向刪除）
}
CASCADED_FROM_CONVERSATIONS = {"chat_messages.conversation_id"}
# rpg-data-and-script：往下的串聯層。每一層都要登記，新增的子表才不會變成沒人負責的個資
CASCADED_FROM_SESSIONS = {"memory_logs.session_id"}
CASCADED_FROM_SCRIPTS = {"script_versions.script_id"}
CASCADED_FROM_VERSIONS = {"script_version_tags.version_id"}
# 無動作的外鍵（不是 SET NULL 也不是串聯）：SQL Server 不允許刪除使用者時有多重串聯路徑（錯誤 1785），
# 所以刪除者必須先把參照清為空值；漏掉時刪除會失敗，而不是留下懸空的編號。
NO_ACTION_REFERENCES = {"sessions.source_version_id"}
ANONYMISED_WITH_SET_NULL = {"llm_call_logs.user_id", "admin_audit_logs.admin_user_id"}
UNLINKED_REFERENCES = {"llm_call_logs.conversation_id", "deletion_tombstones.user_id"}
CASCADE_PARENTS = {"chat_conversations", "sessions", "scripts", "script_versions"}      # 除了 users 以外，有子表串聯的父表


def classify(metadata: MetaData) -> dict[str, set[str]]:
    cascaded_users, anonymised, unlinked, no_action = set(), set(), set(), set()
    cascaded_by_parent: dict[str, set[str]] = {}
    for table in metadata.sorted_tables:
        linked = {fk.parent.name for fk in table.foreign_keys}
        for foreign_key in table.foreign_keys:
            where = f"{table.name}.{foreign_key.parent.name}"
            action = (foreign_key.ondelete or "").upper()
            target = foreign_key.column.table.name
            if target == "users" and action == "CASCADE":
                cascaded_users.add(where)
            elif target == "users" and action == "SET NULL":
                anonymised.add(where)
            elif target != "users" and action == "CASCADE":
                cascaded_by_parent.setdefault(target, set()).add(where)
            elif target != "users" and not action:
                no_action.add(where)
        for column in table.columns:
            if column.name in linked or column.primary_key:
                continue
            if column.name in ("user_id", "conversation_id") or column.name.endswith("_user_id"):
                unlinked.add(f"{table.name}.{column.name}")
    return {
        "users": cascaded_users, "conversations": cascaded_by_parent.get("chat_conversations", set()), "anonymised": anonymised,
        "unlinked": unlinked, "sessions": cascaded_by_parent.get("sessions", set()), "scripts": cascaded_by_parent.get("scripts", set()),
        "versions": cascaded_by_parent.get("script_versions", set()), "no_action": no_action, "parents": set(cascaded_by_parent),
    }


def test_every_table_that_points_at_a_person_is_registered_in_the_erasure_lists():
    found = classify(Base.metadata)
    assert found["users"] == CASCADED_FROM_USERS, (
        "指向 users 的 CASCADE 資料表與登記的清單不同；新增資料表時請確認它被抹除流程涵蓋，再更新 CASCADED_FROM_USERS。"
    )
    assert found["conversations"] == CASCADED_FROM_CONVERSATIONS
    assert found["parents"] == CASCADE_PARENTS, "出現了新的串聯父表：請登記它的子表，確認個資不會在刪除時被遺漏。"
    assert found["sessions"] == CASCADED_FROM_SESSIONS and found["scripts"] == CASCADED_FROM_SCRIPTS
    assert found["versions"] == CASCADED_FROM_VERSIONS
    assert found["no_action"] == NO_ACTION_REFERENCES, "有無動作的外鍵：刪除者必須先清空參照，請登記並由抹除流程處理。"
    assert found["anonymised"] == ANONYMISED_WITH_SET_NULL
    assert found["unlinked"] == UNLINKED_REFERENCES, (
        "有資料表以整數存使用者或對話編號、卻沒有外鍵；抹除流程不會自動處理它。請在設計中說明如何處理，"
        "並由 AccountErasureService 明確清除，再更新 UNLINKED_REFERENCES。"
    )


def test_the_erasure_service_clears_every_unlinked_conversation_reference():
    """沒有外鍵的對話編號（目前只有帳本）必須有對應的清除方法，且在刪除使用者之前被呼叫。"""
    import inspect

    from app.services.account_erasure_service import AccountErasureService

    source = inspect.getsource(AccountErasureService.erase)
    assert source.index("clear_ledger_conversation_refs") < source.index("delete(user_id)")
    assert {"llm_call_logs.conversation_id"} == {item for item in UNLINKED_REFERENCES if item.endswith(".conversation_id")}


def test_the_erasure_service_clears_every_no_action_reference_before_deleting_the_user():
    """無動作的外鍵（`sessions.source_version_id`）不會被資料庫串聯處理，必須由抹除流程在刪除使用者之前明確清空，
    否則別人的存檔會擋住刪除（rpg-data-and-script）。"""
    import inspect

    from app.services.account_erasure_service import AccountErasureService

    source = inspect.getsource(AccountErasureService.erase)
    assert source.index("clear_script_version_refs") < source.index("delete(user_id)")
    assert {"sessions.source_version_id"} == set(NO_ACTION_REFERENCES), "新增無動作的外鍵時，抹除流程要有對應的清空方法並更新這個測試"


# ---- 自我驗證：用暫時的 metadata 證明分類真的偵測得到 ----------------------------------------

def _metadata(*child_columns) -> MetaData:
    metadata = MetaData()
    Table("users", metadata, Column("id", Integer, primary_key=True))
    Table("chat_conversations", metadata, Column("id", Integer, primary_key=True))
    Table("child", metadata, Column("id", Integer, primary_key=True), *child_columns)
    return metadata


def test_a_new_cascading_table_changes_the_classification():
    found = classify(_metadata(Column("user_id", ForeignKey("users.id", ondelete="CASCADE"), nullable=False)))
    assert found["users"] == {"child.user_id"}


def test_a_new_set_null_table_is_seen_as_anonymised():
    found = classify(_metadata(Column("user_id", ForeignKey("users.id", ondelete="SET NULL"), nullable=True)))
    assert found["anonymised"] == {"child.user_id"}


def test_a_column_that_stores_an_id_without_a_foreign_key_is_reported():
    assert classify(_metadata(Column("conversation_id", Integer)))["unlinked"] == {"child.conversation_id"}
    assert classify(_metadata(Column("author_user_id", Integer)))["unlinked"] == {"child.author_user_id"}
    assert classify(_metadata(Column("note", Integer)))["unlinked"] == set()


def test_a_new_cascade_parent_and_a_no_action_reference_are_detected():
    """rpg-data-and-script：除了 users 以外的父表串聯，以及無動作的外鍵，都會被分類出來而不是被忽略。"""
    metadata = MetaData()
    Table("users", metadata, Column("id", Integer, primary_key=True))
    Table("parent", metadata, Column("id", Integer, primary_key=True))
    Table("child", metadata, Column("id", Integer, primary_key=True), Column("parent_id", ForeignKey("parent.id", ondelete="CASCADE")))
    Table("loose", metadata, Column("id", Integer, primary_key=True), Column("parent_id", ForeignKey("parent.id")))
    found = classify(metadata)
    assert found["parents"] == {"parent"} and found["no_action"] == {"loose.parent_id"}
