"""各類資料的保存期限與清除（見 data-retention 規格與 privacy-lifecycle 的 design D10）。

| 類別 | 條件 | 處理 |
|---|---|---|
| 對話與訊息 | 最後活動早於 365 天（12 個月） | 刪除；帳本指向它的對話編號先清為空值 |
| 用量帳本 | 建立早於 365 天 | 刪除 |
| 失敗的模型原文 | 建立早於 14 天 | 原文清為空值，帳本那一筆保留 |
| 驗證權杖 | 到期早於 7 天（不論是否已使用） | 刪除 |
| 管理稽核紀錄 | 建立早於 730 天（24 個月） | 刪除 |
| 帳號鎖定計次 | 建立早於 7 天（計次視窗只有 15 分鐘） | 刪除 |
| 中斷或被擋回合的部分內容 | 最後更新早於 14 天 | `llm_raw_response` 清為空字串，回合紀錄（序號、編號、狀態、時間）保留 |
| RPG 存檔 | 最後活動早於 365 天（12 個月；沒有回合的存檔以建立時間算） | 刪除；回合與記事簿一併消失，不論進行中或已完結 |
| 劇本送審紀錄 | 建立早於 365 天（12 個月） | 刪除；只刪紀錄，帳號的暫停發佈旗標不受影響 |

帳號刪除紀錄（`deletion_tombstones`）**不納入**：它不含個資，而且備份還原後要靠它重新執行刪除。**申訴也不納入**：它的保存期限與所屬帳號
相同（帳號刪除時一併刪除），不由這裡清除。月以天計（與閒置帳號清除相同）。
這些數值要與 `/privacy` 的保存期限表一致，`tests/test_legal_pages.py` 比對。

每一類獨立、分批（500）、每批各自提交；某一類發生資料庫錯誤時回復該批、記錄類別（不含任何內容）、繼續其餘類別，
最後由呼叫端以非零結束代碼回報。預覽模式只計數，不改變任何資料、不記事件。可重複執行：第二次各類都是 0。
清除只看近期以外的紀錄，不影響今日額度、全站今日用量與主要模型暫時跳過的判斷（它們只讀最近幾天的紀錄）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from app.observability import lifecycle_events
from app.repositories.retention_repository import RetentionRepository

CONVERSATION_RETENTION = timedelta(days=365)
LEDGER_RETENTION = timedelta(days=365)
ERROR_DETAIL_RETENTION = timedelta(days=14)
AUTH_TOKEN_RETENTION = timedelta(days=7)
ADMIN_AUDIT_RETENTION = timedelta(days=730)
LOCK_STRIKE_RETENTION = timedelta(days=7)
SESSION_RETENTION = timedelta(days=365)
SCRIPT_SUBMISSION_RETENTION = timedelta(days=365)
TURN_CONTENT_RETENTION = timedelta(days=14)
BATCH_SIZE = 500


@dataclass(frozen=True)
class CategoryResult:
    category: str
    matched: int            # 預覽：符合條件的筆數；實際執行：這次實際處理的筆數
    failed: bool = False


@dataclass(frozen=True)
class RetentionReport:
    results: tuple[CategoryResult, ...]
    dry_run: bool

    @property
    def failed(self) -> list[str]:
        return [r.category for r in self.results if r.failed]


class DataRetentionService:
    def __init__(
        self,
        *,
        repository: RetentionRepository,
        clock: Callable[[], datetime],
        commit: Callable[[], None],
        rollback: Callable[[], None],
        batch_size: int = BATCH_SIZE,
    ) -> None:
        r = repository
        self._clock = clock
        self._commit = commit
        self._rollback = rollback
        self._batch_size = batch_size
        # (類別, 期限, 找出一批, 計數, 處理一批)；順序即執行順序。失敗原文排在帳本之前：同時符合兩者的舊紀錄先清原文、再刪整筆，
        # 這樣預覽（各類獨立計數）與實際執行的筆數一致
        self._plan = (
            (lifecycle_events.CATEGORY_CONVERSATIONS, CONVERSATION_RETENTION, r.find_conversations, r.count_conversations, r.purge_conversations),
            (lifecycle_events.CATEGORY_ERROR_DETAIL, ERROR_DETAIL_RETENTION, r.find_error_detail, r.count_error_detail, r.purge_error_detail),
            (lifecycle_events.CATEGORY_LEDGER, LEDGER_RETENTION, r.find_ledger, r.count_ledger, r.purge_ledger),
            (lifecycle_events.CATEGORY_AUTH_TOKENS, AUTH_TOKEN_RETENTION, r.find_auth_tokens, r.count_auth_tokens, r.purge_auth_tokens),
            (lifecycle_events.CATEGORY_ADMIN_AUDIT, ADMIN_AUDIT_RETENTION, r.find_admin_audit, r.count_admin_audit, r.purge_admin_audit),
            (lifecycle_events.CATEGORY_LOCK_STRIKES, LOCK_STRIKE_RETENTION, r.find_lock_strikes, r.count_lock_strikes, r.purge_lock_strikes),
            # 回合的部分內容排在存檔之前：同時符合兩者的舊回合先清內容、再隨存檔刪除，預覽（各類獨立計數）與實際執行的筆數一致
            (lifecycle_events.CATEGORY_TURN_CONTENT, TURN_CONTENT_RETENTION, r.find_turn_content, r.count_turn_content, r.purge_turn_content),
            (lifecycle_events.CATEGORY_SESSIONS, SESSION_RETENTION, r.find_sessions, r.count_sessions, r.purge_sessions),
            (
                lifecycle_events.CATEGORY_SCRIPT_SUBMISSIONS, SCRIPT_SUBMISSION_RETENTION, r.find_script_submissions,
                r.count_script_submissions, r.purge_script_submissions,
            ),
        )

    def run(self, *, dry_run: bool = False) -> RetentionReport:
        now = self._clock()
        results: list[CategoryResult] = []
        for category, retention, find, count, purge in self._plan:
            cutoff = now - retention
            if dry_run:
                results.append(CategoryResult(category, count(cutoff)))
                continue
            try:
                purged = self._purge(find, purge, cutoff)
            except Exception:  # noqa: BLE001 - 一類失敗不阻止其他類別；只記錄類別，不記錄例外內容
                self._rollback()
                lifecycle_events.log_retention_failed(category=category)
                results.append(CategoryResult(category, 0, failed=True))
                continue
            lifecycle_events.log_retention_purged(category=category, count=purged)
            results.append(CategoryResult(category, purged))
        return RetentionReport(tuple(results), dry_run)

    def _purge(self, find: Callable, purge: Callable, cutoff: datetime) -> int:
        total = 0
        while True:
            ids = find(cutoff, self._batch_size)
            if not ids:
                return total
            done = purge(ids)
            self._commit()
            total += done
            if done == 0:                      # 這一批一筆都沒處理到（例如被並行的流程搶先）：不再重試，避免無窮迴圈
                return total
