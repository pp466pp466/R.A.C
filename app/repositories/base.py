"""Repository 基底類別：只用 SQLAlchemy ORM，不提交交易、不寫原生 SQL。

交易由請求級 Session 統一提交或回復（app/db/session.py），Repository 只負責查詢與 flush。

注意：此處的 `get`、`list` 等方法不限定擁有者，只適用於「不屬於任何使用者」的資料。
屬於使用者的資料（例如對話）必須另外定義簽名帶 `user_id` 的方法，並禁止 API 路徑直接使用基底方法
（見 AGENTS.md 第 10 節）。
"""
from __future__ import annotations

from typing import Any, Generic, Sequence, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

ModelT = TypeVar("ModelT")


class BaseRepository(Generic[ModelT]):
    """各資料表 Repository 的共同基底；子類別設定 `model`。"""

    model: type[ModelT]

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, instance: ModelT) -> ModelT:
        self._session.add(instance)
        self._session.flush()  # 取得主鍵與預設值，但不提交
        return instance

    def get(self, primary_key: Any) -> ModelT | None:
        return self._session.get(self.model, primary_key)

    def get_by_public_id(self, public_id: str) -> ModelT | None:
        column = getattr(self.model, "public_id")
        return self._session.scalar(select(self.model).where(column == public_id))

    def list(self, *, limit: int = 100, offset: int = 0) -> Sequence[ModelT]:
        statement = select(self.model).limit(limit).offset(offset)
        return self._session.scalars(statement).all()

    def delete(self, instance: ModelT) -> None:
        self._session.delete(instance)
        self._session.flush()
