"""劇本檢舉的資料存取（見 script-reports 規格與 rpg-script-reports 的 design D1、D2）。

- **寫入結案欄位（`closed_at`、`closed_result`）的唯一檔案**：`tests/architecture/test_script_report_writes.py` 以 AST 守門。
  版本離開上架（下架、被新版本取代）時，`ScriptRepository` 的轉移在同一個交易內呼叫 `close_open`；管理員結「不成立」也呼叫它。
- 屬於「某一位使用者」的方法（建立、計數、匯出）第一個參數都是 `reporter_id`；以版本為單位的方法（結案、清除）只由
  `ScriptRepository` 與 `AdminService` 呼叫。管理員讀取檢舉內容走 `AdminScriptRepository`（唯讀、拿不到檢舉人欄位），不在這裡。
- 玩家文字（自寫文字）視為個資：這個檔案不記錄、不拋出含內容的訊息。
- 只用 ORM，不提交也不回復交易（交易由請求級 Session 統一處理）；唯一約束衝突只回復這一筆（SAVEPOINT）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterator, Sequence

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.models.script import ScriptVersion
from app.models.script_report import CLOSED_RESULTS, ScriptReport
from app.repositories.base import BaseRepository
from app.repositories.errors import AlreadyReportedError

_DELETE_CHUNK = 500      # SQL Server 單一語句最多約 2100 個參數；以集合為單位的刪除分批執行


class ScriptReportRepository(BaseRepository[ScriptReport]):
    model = ScriptReport

    def add(
        self, reporter_id: int, version_id: int, reasons: Sequence[str], red_line: bool, note: str | None, *, now: datetime | None = None
    ) -> ScriptReport:
        """寫一筆檢舉。理由與紅線旗標由 Service 驗證與計算（這裡不重算）。

        (檢舉人, 版本) 的唯一約束擋下重複時拋出 `AlreadyReportedError`——只回復這一筆（SAVEPOINT），不影響呼叫者交易中的其他變更；
        包含並行的兩個請求同時寫入的情況（資料庫保證只有一個成功）。**不是**唯一約束造成的完整性錯誤（例如版本不存在）原樣拋出。
        """
        report = ScriptReport(
            reporter_id=reporter_id, version_id=version_id, reasons_json=list(reasons), red_line=bool(red_line), note=note,
        )
        if now is not None:
            report.created_at = now
            report.updated_at = now
        try:
            with self._session.begin_nested():
                self._session.add(report)
                self._session.flush()
        except IntegrityError:
            if self.exists(reporter_id, version_id):
                raise AlreadyReportedError from None
            raise
        return report

    def exists(self, reporter_id: int, version_id: int) -> bool:
        """這位使用者是否已檢舉過這個版本（不論是否已結案）。"""
        return self._session.scalar(
            select(ScriptReport.id).where(ScriptReport.reporter_id == reporter_id, ScriptReport.version_id == version_id)
        ) is not None

    def count_between(self, reporter_id: int, start: datetime, end: datetime) -> int:
        """期間內（起含訖不含）這位使用者送出的檢舉筆數，每日上限用；不論是否已結案。"""
        return self._session.execute(
            select(func.count(ScriptReport.id)).where(
                ScriptReport.reporter_id == reporter_id, ScriptReport.created_at >= start, ScriptReport.created_at < end,
            )
        ).scalar_one()

    def close_open(self, version_id: int, result: str, now: datetime) -> int:
        """把這個版本**未結案**的檢舉全部結案，回傳結案筆數。

        條件式 UPDATE（`closed_at IS NULL`）：已結案的不被改寫（結案是歷史事實，結果與時間不會被後來的結案覆蓋），
        並行的兩個結案只有一個命中每一列。結果必須是固定列舉（程式錯誤時拋出 `ValueError`）。
        """
        if result not in CLOSED_RESULTS:
            raise ValueError("檢舉的結案結果必須是固定列舉。")
        outcome = self._session.execute(
            update(ScriptReport)
            .where(ScriptReport.version_id == version_id, ScriptReport.closed_at.is_(None))
            .values(closed_at=now, closed_result=result, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        return outcome.rowcount

    def close_open_by_public_id(self, version_public_id: str, result: str, now: datetime) -> int:
        """同 `close_open`，目標以版本的**對外識別碼**給（管理員端只有對外識別碼，沒有內部編號）；回傳結案筆數。

        只把識別碼換成內部編號再呼叫 `close_open`（結案欄位的寫入點仍只有 `close_open` 一個，見
        `tests/architecture/test_script_report_writes.py`）。識別碼不存在時一筆都不結（回傳 0），與「沒有未結案的檢舉」同一種結果；
        結果不合列舉時不論版本是否存在都拋出 `ValueError`（程式錯誤，不靜默）。"""
        if result not in CLOSED_RESULTS:
            raise ValueError("檢舉的結案結果必須是固定列舉。")
        version_id = self._session.scalar(select(ScriptVersion.id).where(ScriptVersion.public_id == version_public_id))
        return 0 if version_id is None else self.close_open(version_id, result, now)

    def delete_for_versions(self, version_ids: Sequence[int]) -> int:
        """刪除這些版本的全部檢舉（不論是否結案），回傳刪除筆數。

        抹除流程在刪除版本之前呼叫它：`script_reports.version_id` 是無動作的外鍵（避免 SQL Server 的多重串聯路徑），
        版本被刪時還有檢舉會讓刪除失敗。空集合回傳 0、不執行任何語句。
        """
        ids = list(dict.fromkeys(version_ids))
        deleted = 0
        for start in range(0, len(ids), _DELETE_CHUNK):
            chunk = ids[start:start + _DELETE_CHUNK]
            deleted += self._session.execute(
                delete(ScriptReport).where(ScriptReport.version_id.in_(chunk)).execution_options(synchronize_session=False)
            ).rowcount
        return deleted

    def export_for_reporter(self, user_id: int, *, page_size: int = 200) -> Iterator[dict]:
        """個人資料匯出：這位檢舉人自己送出的檢舉，由舊到新，分頁讀取。

        只選被檢舉版本的對外識別碼、理由、自寫文字與建立時間：**不選結案欄位**（檢舉人不會被通知結果）、
        不選版本標題或作者（不洩漏被檢舉作者的任何資訊）、不含別人的檢舉。
        """
        last_id = 0
        while True:
            rows = self._session.execute(
                select(ScriptReport.id, ScriptVersion.public_id, ScriptReport.reasons_json, ScriptReport.note, ScriptReport.created_at)
                .join(ScriptVersion, ScriptVersion.id == ScriptReport.version_id)
                .where(ScriptReport.reporter_id == user_id, ScriptReport.id > last_id)
                .order_by(ScriptReport.id)
                .limit(page_size)
            ).all()
            if not rows:
                return
            for row in rows:
                yield {
                    "version_public_id": row.public_id, "reasons": list(row.reasons_json or []),
                    "text": row.note, "created_at": row.created_at,
                }
            last_id = rows[-1].id
