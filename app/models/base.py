"""資料模型的共用慣例（見 AGENTS.md 第 4 節）。

- 約束命名規則固定，讓 Alembic 在 SQLite 與 SQL Server 上產生一致、可預測的名稱。
- 對外識別使用 UUID 字串（`public_id`），內部整數主鍵不對外暴露。
- 時間一律以 UTC 儲存；由 Python 端維護，不使用伺服器端函式，避免兩種資料庫的語法差異。
- 文字欄位使用 Unicode（SQL Server 對應 NVARCHAR），結構化資料使用 JSON。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, Index, MetaData, Unicode, column
from sqlalchemy.dialects import mssql
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator, TypeEngine

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    """目前的 UTC 時間（帶時區資訊）。"""
    return datetime.now(timezone.utc)


def new_public_id() -> str:
    """產生對外使用的 UUID 字串。"""
    return str(uuid.uuid4())


class UTCDateTime(TypeDecorator[datetime]):
    """UTC 時間：寫入時轉為 UTC 並去除時區，讀出時補回 UTC，兩種資料庫的行為一致。

    - 傳入帶時區的時間 → 先轉為 UTC。
    - 傳入不帶時區的時間 → 視為已是 UTC。
    - SQL Server 使用 DATETIME2(6)，保留微秒精度（舊的 DATETIME 只有約 3 毫秒）。
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "mssql":
            return dialect.type_descriptor(mssql.DATETIME2(precision=6))
        return dialect.type_descriptor(DateTime(timezone=False))

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    """所有業務模型的宣告式基底；後續各 change 的資料表都繼承它。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def unique_when_not_null(index_name: str, column_name: str) -> Index:
    """可為空的欄位的「唯一」約束：多筆 NULL 允許，非 NULL 的值不得重複。

    為什麼不能直接用 `unique=True`：SQL Server 的 UNIQUE 約束只允許「一個」NULL，
    SQLite 卻允許多個，兩者行為不同。改用「只涵蓋非 NULL 列」的部分唯一索引，兩邊行為一致。
    使用方式：在模型的 `__table_args__` 加入 `unique_when_not_null("uq_<表>_<欄>", "<欄>")`，
    且該欄位本身不要再設 `unique=True`。
    """
    only_present = column(column_name).is_not(None)
    return Index(index_name, column_name, unique=True, sqlite_where=only_present, mssql_where=only_present)


class PublicIdMixin:
    """對外識別碼：UUID 字串，唯一。網址與 API 只使用它，且授權不得依賴它的不可猜測性。"""

    public_id: Mapped[str] = mapped_column(Unicode(36), unique=True, default=new_public_id)


class TimestampMixin:
    """建立與更新時間（UTC）。更新時間在每次 UPDATE 時由 ORM 自動改為當下。"""

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
