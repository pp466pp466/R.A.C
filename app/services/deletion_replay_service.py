"""備份還原後重新抹除已刪除的帳號（見 deletion-replay 規格與 launch-readiness 的 design D5）。

使用者刪除帳號之後，資料庫從備份還原會讓他復活。規範要求還原時依 `deletion_tombstones` 重新執行刪除；備份**之後**才發生的刪除
不在還原後的資料庫裡，所以也接受由日誌取出的「使用者編號,刪除時間」檔案（`account.deleted_by_request` 與 `account.inactive_purged`
事件帶內部編號）。

- 兩種來源合併後，**每個編號只看最晚的刪除時間**：同一個編號可能先後屬於不同的人（SQL Server 的 IDENTITY 在還原後從備份時的種子繼續，
  備份之後註冊再被刪除的編號會被還原後的新使用者重新使用）。
- **只抹除「建立時間不晚於刪除時間」且不是管理員的帳號**；新註冊而沿用舊編號的使用者不會被誤刪（計入「略過」）。
  條件由資料庫在刪除當下判斷（條件式 DELETE），沒有先讀再刪的競態。
- 走既有的單一抹除出口 `AccountErasureService`（清帳本的對話編號 → 條件式刪除 → 提交），條件不成立整個回復。
- 抹除成功時，若該編號沒有不早於這次的刪除紀錄就補寫一筆（刪除時間沿用原本的時間，原因為 `restore_replay`），使之後再還原仍涵蓋。
- 可重複執行：已經不存在的帳號計入「不存在」。預覽只計數，不改變任何資料、不記事件。
- 輸出與事件只有筆數與內部編號，沒有任何個資、檔案路徑或檔案內容。**必須在還原後、對外開放流量之前執行**。
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from app.models.deletion_tombstone import REASON_RESTORE_REPLAY
from app.observability import lifecycle_events
from app.repositories.deletion_tombstone_repository import DeletionTombstoneRepository
from app.repositories.user_repository import REPLAY_ADMIN, REPLAY_ELIGIBLE, REPLAY_MISSING, REPLAY_REISSUED, UserRepository
from app.services.account_erasure_service import AccountErasureService


class ReplayFileError(ValueError):
    """檔案格式錯誤。只帶行號，**不帶行的內容**（內容可能是任何東西，不回顯）。"""

    def __init__(self, line_number: int) -> None:
        super().__init__(f"檔案第 {line_number} 行格式錯誤：每行應為「使用者編號,刪除時間（ISO 8601）」")
        self.line_number = line_number


@dataclass(frozen=True)
class ReplayRecord:
    user_id: int
    deleted_at: datetime


@dataclass(frozen=True)
class ReplayReport:
    dry_run: bool
    erased: int            # 預覽時是「將抹除」的筆數
    reissued: int          # 略過：編號已被還原後的新使用者重新使用
    admin: int             # 略過：管理員
    absent: int            # 已經不存在（重複執行時的正常結果）


def parse_replay_file(text: str) -> list[ReplayRecord]:
    """解析「編號,時間」檔案。**整份先驗證**，任何一行錯誤就拋出 `ReplayFileError`（呼叫端據此在抹除任何帳號之前失敗）。"""
    records: list[ReplayRecord] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 2 or not parts[0].isascii() or not parts[0].isdigit() or int(parts[0]) <= 0:
            raise ReplayFileError(number)
        try:
            when = datetime.fromisoformat(parts[1])
        except ValueError:
            raise ReplayFileError(number) from None
        records.append(ReplayRecord(int(parts[0]), when if when.tzinfo else when.replace(tzinfo=timezone.utc)))
    return records


class DeletionReplayService:
    def __init__(
        self,
        *,
        users: UserRepository,
        tombstones: DeletionTombstoneRepository,
        erasure: AccountErasureService,
        on_replayed: Callable[[int], None] = lambda user_id: lifecycle_events.log_deletion_replayed(user_id=user_id),
    ) -> None:
        self._users = users
        self._tombstones = tombstones
        self._erasure = erasure
        self._on_replayed = on_replayed

    def _merged(self, extra: Iterable[ReplayRecord]) -> dict[int, datetime]:
        latest: dict[int, datetime] = {}
        for user_id, deleted_at in [*self._tombstones.records(), *((r.user_id, r.deleted_at) for r in extra)]:
            if user_id not in latest or deleted_at > latest[user_id]:
                latest[user_id] = deleted_at
        return latest

    def run(self, extra: Iterable[ReplayRecord] = (), *, dry_run: bool = False) -> ReplayReport:
        erased = reissued = admin = absent = 0
        for user_id, deleted_at in sorted(self._merged(extra).items()):
            status = self._users.replay_status(user_id, deleted_at)
            if status == REPLAY_MISSING:
                absent += 1
            elif status == REPLAY_ADMIN:
                admin += 1
            elif status == REPLAY_REISSUED:
                reissued += 1
            elif status == REPLAY_ELIGIBLE:
                if dry_run:
                    erased += 1
                elif self._erase(user_id, deleted_at):
                    erased += 1
                else:
                    absent += 1   # 檢查之後、刪除之前消失（或剛好符合條件的狀態改變）：條件式刪除沒有刪到，整個交易已回復
        return ReplayReport(dry_run, erased, reissued, admin, absent)

    def _erase(self, user_id: int, deleted_at: datetime) -> bool:
        return self._erasure.erase(
            user_id,
            delete=lambda uid: self._users.delete_if_created_not_after(uid, deleted_at),
            reason=REASON_RESTORE_REPLAY,
            now=deleted_at,
            write_tombstone=not self._tombstones.has_record_not_before(user_id, deleted_at),
            on_erased=self._on_replayed,
        )
