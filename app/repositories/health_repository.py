"""就緒檢查用的 Repository：以 ORM 表達式確認資料庫可用，不使用原生 SQL 字串。"""
from sqlalchemy import literal, select
from sqlalchemy.orm import Session


class HealthRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def ping(self) -> bool:
        return self._session.scalar(select(literal(1))) == 1
