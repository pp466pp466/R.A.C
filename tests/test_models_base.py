"""database-access 規格：欄位慣例、Unicode 與 JSON 往返、唯一字串正規化、時間戳。

使用 db_engine fixture：同一組測試在 SQLite 上一定執行，在 SQL Server 上（標記 sqlserver）選擇性執行。
測試用的模型使用獨立的 Base，不會混入專案的 metadata。
"""
import re
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import JSON, Unicode, UnicodeText, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.models.base import PublicIdMixin, TimestampMixin, UTCDateTime, unique_when_not_null, utcnow
from app.services.normalization import normalize_unique_text


class _TestBase(DeclarativeBase):
    pass


class Item(_TestBase, PublicIdMixin, TimestampMixin):
    __tablename__ = "t_item"
    __table_args__ = (unique_when_not_null("uq_t_item_email", "email"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Unicode(100))
    body: Mapped[str | None] = mapped_column(UnicodeText, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    email: Mapped[str | None] = mapped_column(Unicode(255), nullable=True)
    happened_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


@pytest.fixture()
def session(db_engine):
    _TestBase.metadata.drop_all(db_engine)
    _TestBase.metadata.create_all(db_engine)
    with Session(db_engine) as s:
        yield s
    _TestBase.metadata.drop_all(db_engine)


def _fresh(db_engine, item_id: int) -> Item:
    with Session(db_engine) as other:
        return other.get(Item, item_id)


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def test_traditional_chinese_and_emoji_round_trip(session, db_engine):
    text = "繁體中文：測試「引號」與表情符號 😀🎉，以及罕用字 𠮷"
    session.add(Item(id=1, name=text, body=text * 20))
    session.commit()
    stored = _fresh(db_engine, 1)
    assert stored.name == text
    assert stored.body == text * 20


def test_json_round_trip_with_chinese_keys(session, db_engine):
    payload = {"角色": {"名稱": "夏侯傑", "好感度": 20}, "清單": [1, "二", None, True], "空": {}}
    session.add(Item(id=1, name="x", payload=payload))
    session.commit()
    assert _fresh(db_engine, 1).payload == payload


def test_public_id_is_generated_unique_uuid(session):
    a, b = Item(id=1, name="a"), Item(id=2, name="b")
    session.add_all([a, b])
    session.commit()
    assert UUID_RE.match(a.public_id)
    assert a.public_id != b.public_id


def test_timestamps_are_utc_and_updated_at_changes_on_update(session, db_engine):
    item = Item(id=1, name="before")
    session.add(item)
    session.commit()
    created = _fresh(db_engine, 1).created_at
    assert created.tzinfo == timezone.utc
    assert abs(utcnow() - created) < timedelta(minutes=1)

    time.sleep(0.02)
    item.name = "after"
    session.commit()
    again = _fresh(db_engine, 1)
    assert again.created_at == created                      # 建立時間不變
    assert again.updated_at > created                       # 更新時間改為更新當下
    assert again.updated_at.tzinfo == timezone.utc


def test_datetime_keeps_microseconds_and_normalises_offsets_to_utc(session, db_engine):
    taipei = timezone(timedelta(hours=8))
    moment = datetime(2026, 10, 27, 14, 30, 15, 123456, tzinfo=taipei)
    session.add(Item(id=1, name="tz", happened_at=moment))
    session.commit()
    stored = _fresh(db_engine, 1).happened_at
    assert stored == moment                                  # 同一時間點
    assert stored.tzinfo == timezone.utc
    assert stored.hour == 6                                  # 台北 14 點 = UTC 6 點
    assert stored.microsecond == 123456                      # 微秒不遺失


def test_naive_datetime_is_treated_as_utc(session, db_engine):
    session.add(Item(id=1, name="naive", happened_at=datetime(2026, 1, 2, 3, 4, 5)))
    session.commit()
    assert _fresh(db_engine, 1).happened_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def test_normalization_rule():
    assert normalize_unique_text("  Alice@Example.COM \n") == "alice@example.com"
    composed, decomposed = "café", "café"          # é 的兩種 Unicode 表示
    assert composed != decomposed
    assert normalize_unique_text(composed) == normalize_unique_text(decomposed)


def test_values_differing_only_by_case_conflict_after_normalisation(session):
    session.add(Item(id=1, name="a", email=normalize_unique_text("Alice@Example.com")))
    session.commit()
    session.add(Item(id=2, name="b", email=normalize_unique_text("alice@example.com")))
    with pytest.raises(IntegrityError):
        session.commit()


def test_nullable_unique_column_allows_many_nulls_but_not_duplicate_values(session):
    """SQL Server 的 UNIQUE 只允許一個 NULL，SQLite 允許多個；部分唯一索引讓兩邊一致。"""
    session.add_all([Item(id=1, name="a"), Item(id=2, name="b"), Item(id=3, name="c")])  # 三筆 email 皆為 NULL
    session.commit()
    session.add_all([Item(id=4, name="d", email="x@example.com")])
    session.commit()
    session.add(Item(id=5, name="e", email="x@example.com"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_identical_lookup_after_normalisation_finds_the_row(session):
    session.add(Item(id=1, name="a", email=normalize_unique_text("Alice@Example.com")))
    session.commit()
    found = session.scalar(select(Item).where(Item.email == normalize_unique_text("ALICE@example.COM")))
    assert found is not None and found.id == 1
