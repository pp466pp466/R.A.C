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
}
CASCADED_FROM_CONVERSATIONS = {"chat_messages.conversation_id"}
ANONYMISED_WITH_SET_NULL = {"llm_call_logs.user_id", "admin_audit_logs.admin_user_id"}
UNLINKED_REFERENCES = {"llm_call_logs.conversation_id", "deletion_tombstones.user_id"}


def classify(metadata: MetaData) -> dict[str, set[str]]:
    cascaded_users, cascaded_conversations, anonymised, unlinked = set(), set(), set(), set()
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
            elif target == "chat_conversations" and action == "CASCADE":
                cascaded_conversations.add(where)
        for column in table.columns:
            if column.name in linked or column.primary_key:
                continue
            if column.name in ("user_id", "conversation_id") or column.name.endswith("_user_id"):
                unlinked.add(f"{table.name}.{column.name}")
    return {"users": cascaded_users, "conversations": cascaded_conversations, "anonymised": anonymised, "unlinked": unlinked}


def test_every_table_that_points_at_a_person_is_registered_in_the_erasure_lists():
    found = classify(Base.metadata)
    assert found["users"] == CASCADED_FROM_USERS, (
        "指向 users 的 CASCADE 資料表與登記的清單不同；新增資料表時請確認它被抹除流程涵蓋，再更新 CASCADED_FROM_USERS。"
    )
    assert found["conversations"] == CASCADED_FROM_CONVERSATIONS
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
