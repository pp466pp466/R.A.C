"""google-first-login 的資料模型：新欄位可為空、文字為 Unicode、時間為 UTC、外鍵串聯刪除、索引存在。"""
from sqlalchemy import Unicode, UnicodeText

from app.models import Base, LockAppeal, LockStrike, User
from app.models.base import UTCDateTime


def table(name: str):
    return Base.metadata.tables[name]


def test_the_new_user_columns_are_nullable_unicode_or_utc_and_indexed_where_scanned():
    users = table("users")
    for name in ("age_deadline_at", "locked_at", "lock_reason"):
        assert users.c[name].nullable, name
    assert isinstance(users.c.age_deadline_at.type, UTCDateTime) and isinstance(users.c.locked_at.type, UTCDateTime)
    assert isinstance(users.c.lock_reason.type, Unicode)
    indexed = {tuple(c.name for c in index.columns) for index in users.indexes}
    assert ("age_deadline_at",) in indexed and ("locked_at",) in indexed       # 每小時逾時清除與告警計數用


def test_lock_strikes_hold_only_the_account_a_kind_and_a_time():
    strikes = table("lock_strikes")
    assert {c.name for c in strikes.columns} == {"id", "user_id", "kind", "created_at"}
    assert isinstance(strikes.c.kind.type, Unicode) and isinstance(strikes.c.created_at.type, UTCDateTime)
    foreign_key = next(iter(strikes.c.user_id.foreign_keys))
    assert foreign_key.column.table.name == "users" and foreign_key.ondelete == "CASCADE"
    assert {tuple(c.name for c in i.columns) for i in strikes.indexes} == {("user_id", "created_at")}


def test_lock_appeals_are_personal_data_that_cascade_with_the_account():
    appeals = table("lock_appeals")
    assert {c.name for c in appeals.columns} == {"id", "public_id", "user_id", "content", "created_at", "handled_at"}
    assert isinstance(appeals.c.content.type, UnicodeText) and not appeals.c.content.nullable
    assert appeals.c.handled_at.nullable and appeals.c.public_id.unique
    foreign_key = next(iter(appeals.c.user_id.foreign_keys))
    assert foreign_key.column.table.name == "users" and foreign_key.ondelete == "CASCADE"
    assert LockAppeal.__tablename__ == "lock_appeals" and LockStrike.__tablename__ == "lock_strikes" and User.__tablename__ == "users"
