"""account-retention 規格：每一個指向 `users.id` 的外鍵，都必須在使用者被刪除時有明確的結果。

連續 12 個月未登入的帳號會被直接刪除（也包含日後的「刪除帳號」）。如果有資料表以外鍵指向使用者、卻沒有串聯刪除，
刪除會因為外鍵違反而整個失敗，或者（更糟）有人為了讓它過關而繞過它，留下不屬於任何人的個資。

規則：外鍵必須是 `ON DELETE CASCADE`（資料跟著使用者一起刪），或 `ON DELETE SET NULL` 且該欄位可為空（資料保留但匿名化，
例如 `llm_call_logs.user_id`）。C-3、C-4 新增對話、訊息等資料表時，這個測試會在它們沒做到時失敗，並指出是哪一個。
注意：這個測試只看得到「宣告了外鍵」的欄位；以整數存使用者編號但沒有外鍵的資料表（例如刪除紀錄本身，它刻意不設外鍵）
不在檢查範圍，新增這種資料表時須在設計中說明刪除時如何處理。
"""
from sqlalchemy import Column, ForeignKey, Integer, MetaData, Table

from app.models import Base

ACCEPTED = ("CASCADE", "SET NULL")


def offenders(metadata: MetaData) -> list[str]:
    found = []
    for table in metadata.sorted_tables:
        for foreign_key in table.foreign_keys:
            if foreign_key.column.table.name != "users":
                continue
            action = (foreign_key.ondelete or "").upper()
            where = f"{table.name}.{foreign_key.parent.name}"
            if action not in ACCEPTED:
                found.append(f"{where}：ON DELETE 是 {action or '（未設定）'}，須為 CASCADE 或 SET NULL")
            elif action == "SET NULL" and not foreign_key.parent.nullable:
                found.append(f"{where}：ON DELETE SET NULL 但欄位不可為空")
    return found


def test_every_foreign_key_to_users_cascades_or_nulls_the_reference():
    assert offenders(Base.metadata) == [], (
        "有資料表指向 users.id 卻沒有處理使用者被刪除的情況；請改為 ON DELETE CASCADE，或（需要保留匿名資料時）"
        "欄位可為空並設 ON DELETE SET NULL，並確認閒置帳號清除與帳號刪除的流程涵蓋它。"
    )


def test_the_check_is_not_vacuous_it_sees_the_member_tables():
    seen = {
        f"{table.name}.{fk.parent.name}"
        for table in Base.metadata.sorted_tables for fk in table.foreign_keys if fk.column.table.name == "users"
    }
    assert {"user_identities.user_id", "auth_tokens.user_id"} <= seen


def _metadata_with(child_columns) -> MetaData:
    metadata = MetaData()
    Table("users", metadata, Column("id", Integer, primary_key=True))
    Table("child", metadata, Column("id", Integer, primary_key=True), *child_columns)
    return metadata


def test_the_check_really_fails_for_a_foreign_key_without_cascade():
    """反向驗證：用一個暫時的、獨立的 metadata（不屬於應用程式的模型）證明這個檢查抓得到問題。"""
    bad = _metadata_with([Column("user_id", ForeignKey("users.id"), nullable=False)])
    assert offenders(bad) and "child.user_id" in offenders(bad)[0] and "未設定" in offenders(bad)[0]
    restrict = _metadata_with([Column("user_id", ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)])
    assert "RESTRICT" in offenders(restrict)[0]


def test_set_null_is_accepted_only_when_the_column_can_be_null():
    ok = _metadata_with([Column("user_id", ForeignKey("users.id", ondelete="SET NULL"), nullable=True)])
    broken = _metadata_with([Column("user_id", ForeignKey("users.id", ondelete="SET NULL"), nullable=False)])
    assert offenders(ok) == [] and "不可為空" in offenders(broken)[0]


def test_cascade_is_accepted():
    assert offenders(_metadata_with([Column("user_id", ForeignKey("users.id", ondelete="CASCADE"), nullable=False)])) == []
