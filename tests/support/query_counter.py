"""數資料庫查詢次數的測試輔助：證明「一頁的查詢次數固定、不隨筆數增加」（rpg-entry-api 規格），不依賴實作細節。

用法：
    with count_statements(session) as statements:
        ...（要量測的呼叫）
    assert len(statements) == 2

掛在 Session 綁定的引擎的 `before_cursor_execute`（每一個送到資料庫的陳述式一次，含 SQLite 與 SQL Server）。
量測前先 `session.expire_all()`，這樣已載入的物件不會讓之後的存取少算一次查詢。
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import event
from sqlalchemy.orm import Session


@contextmanager
def count_statements(session: Session) -> Iterator[list[str]]:
    engine = session.get_bind()
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)
