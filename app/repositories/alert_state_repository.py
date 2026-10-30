"""告警狀態的資料存取（見 operational-alerts 規格與 ops-alerting 的 design D2、D3）。

固定的五項告警各一列。狀態轉換靠**條件式 UPDATE 搶佔**：比對的是呼叫端讀到的舊值（狀態、嚴重度、最近通知時間），
`rowcount == 1` 的行程才負責寄信，其餘行程什麼都不做；寄信全部失敗時以 `restore` 還原（只在 `last_notified_at` 仍是自己
搶到的值時成立，不會蓋掉別人之後的狀態）。這個類別不提交也不回復交易，由 Service 決定提交點；只用 ORM 表達式。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.alert_state import STATE_OK, AlertState


class AlertStateRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_all(self) -> dict[str, AlertState]:
        return {row.alert_key: row for row in self._session.scalars(select(AlertState))}

    def ensure(self, alert_key: str, now: datetime) -> AlertState:
        """取得這項告警的狀態列，沒有就建立（初始為正常）。並行建立時唯一約束讓其中一個失敗，改為讀取對方建立的那一列。"""
        existing = self._session.scalar(select(AlertState).where(AlertState.alert_key == alert_key))
        if existing is not None:
            return existing
        try:
            with self._session.begin_nested():
                row = AlertState(alert_key=alert_key, state=STATE_OK, severity=None, changed_at=now, updated_at=now)
                self._session.add(row)
                self._session.flush()
            return row
        except IntegrityError:
            found = self._session.scalar(select(AlertState).where(AlertState.alert_key == alert_key))
            assert found is not None
            return found

    def _run(self, statement) -> int:
        self._session.flush()
        result = self._session.execute(statement.execution_options(synchronize_session=False))
        self._session.expire_all()
        return result.rowcount

    def claim(
        self,
        alert_key: str,
        *,
        expected_state: str,
        expected_severity: str | None,
        expected_notified_at: datetime | None,
        state: str,
        severity: str | None,
        changed_at: datetime | None,
        notified_at: datetime | None,
        value: float,
        now: datetime,
    ) -> bool:
        """條件式 UPDATE：目前的狀態、嚴重度與最近通知時間仍是呼叫端讀到的值，才寫入新狀態。回傳是否搶到。

        `changed_at` 或 `notified_at` 為 None 代表該欄位不改動（提醒與升級不改「狀態開始時間」；靜默降級不改「最近通知時間」）。
        """
        values: dict[str, object] = {"state": state, "severity": severity, "last_value": value, "updated_at": now}
        if changed_at is not None:
            values["changed_at"] = changed_at
        if notified_at is not None:
            values["last_notified_at"] = notified_at
        statement = (
            update(AlertState)
            .where(AlertState.alert_key == alert_key, AlertState.state == expected_state)
            .where(AlertState.severity.is_(None) if expected_severity is None else AlertState.severity == expected_severity)
            .where(
                AlertState.last_notified_at.is_(None)
                if expected_notified_at is None
                else AlertState.last_notified_at == expected_notified_at
            )
            .values(**values)
        )
        return self._run(statement) == 1

    def restore(
        self,
        alert_key: str,
        *,
        claimed_at: datetime,
        state: str,
        severity: str | None,
        changed_at: datetime | None,
        notified_at: datetime | None,
        now: datetime,
    ) -> bool:
        """寄信全部失敗時還原成搶佔之前的狀態。只在 `last_notified_at` 仍是自己搶到的值時成立。"""
        statement = (
            update(AlertState)
            .where(AlertState.alert_key == alert_key, AlertState.last_notified_at == claimed_at)
            .values(state=state, severity=severity, changed_at=changed_at or now, last_notified_at=notified_at, updated_at=now)
        )
        return self._run(statement) == 1

    def touch(self, alert_key: str, *, value: float, now: datetime) -> None:
        """沒有狀態變化時，只更新最近一次評估的數值（不動搶佔比對的欄位）。"""
        self._run(update(AlertState).where(AlertState.alert_key == alert_key).values(last_value=value, updated_at=now))
