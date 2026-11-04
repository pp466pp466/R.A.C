"""劇本送審紀錄的資料存取（見 script-submission 規格與 rpg-script-authoring 的 design D2、D8）。

屬於「某一位使用者」的紀錄，所以讀寫方法的第一個參數都是 `user_id`；跨使用者的期限清除（12 個月）在 `RetentionRepository`。
紀錄只有使用者、結果、類別列舉與時間：**沒有**版本識別碼、標題、內容或任何劇本文字。只用 ORM，不提交也不回復交易。
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterator, Sequence

from sqlalchemy import func, select

from app.models.script_submission import (
    COUNTED_OUTCOMES, SUBMISSION_OUTCOMES, ScriptSubmission,
)
from app.repositories.base import BaseRepository


class ScriptSubmissionRepository(BaseRepository[ScriptSubmission]):
    model = ScriptSubmission

    def record(self, user_id: int, outcome: str, categories: Sequence[str], now: datetime) -> ScriptSubmission:
        """寫一筆紀錄。結果必須是固定列舉（程式錯誤時拋出 `ValueError`）；類別由呼叫端保證是審查的固定列舉。"""
        if outcome not in SUBMISSION_OUTCOMES:
            raise ValueError("送審紀錄的結果必須是固定列舉。")
        return self.add(ScriptSubmission(user_id=user_id, outcome=outcome, categories_json=list(categories) or None, created_at=now))

    def count_counted_between(self, user_id: int, *, start: datetime, end: datetime) -> int:
        """期間內（起含訖不含）計入每日送審次數的筆數：成功送審與被自動拒絕；解除暫停不算。"""
        return self._session.execute(
            select(func.count(ScriptSubmission.id)).where(
                ScriptSubmission.user_id == user_id,
                ScriptSubmission.outcome.in_(COUNTED_OUTCOMES),
                ScriptSubmission.created_at >= start,
                ScriptSubmission.created_at < end,
            )
        ).scalar_one()

    def recent_outcomes(self, user_id: int, limit: int) -> list[str]:
        """最近 `limit` 筆紀錄的結果（由新到舊；同一時間以編號大者在前）。連續被自動拒絕的判斷用它。"""
        return list(
            self._session.scalars(
                select(ScriptSubmission.outcome)
                .where(ScriptSubmission.user_id == user_id)
                .order_by(ScriptSubmission.created_at.desc(), ScriptSubmission.id.desc())
                .limit(limit)
            )
        )

    def export_for_user(self, user_id: int, *, page_size: int = 200) -> Iterator[dict]:
        """個人資料匯出：這位使用者的全部紀錄（結果、類別、時間），由舊到新，分頁讀取。"""
        last_id = 0
        while True:
            rows = list(
                self._session.scalars(
                    select(ScriptSubmission)
                    .where(ScriptSubmission.user_id == user_id, ScriptSubmission.id > last_id)
                    .order_by(ScriptSubmission.id)
                    .limit(page_size)
                )
            )
            if not rows:
                return
            for row in rows:
                yield {"outcome": row.outcome, "categories": list(row.categories_json or []), "created_at": row.created_at}
            last_id = rows[-1].id
