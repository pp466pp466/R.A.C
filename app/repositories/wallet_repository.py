"""錢包與帳本的資料存取（見 game-wallet 規格與 design D2、D3）。

**所有方法都帶 `user_id`**，查詢一律帶 `user_id` 條件（不提供只憑編號取資料的方法）；不提交交易，只用 ORM。
這是唯一可以寫入 `wallets` 與 `wallet_entries` 的地方（`tests/architecture/test_wallet_writes.py` 以 AST 守住）。

- 讀取一律用欄位查詢（不載入實體）：實體載入會被 Session 的身分對應表快取，同一個 Session 內第二次讀就看不到剛提交的修改；
  欄位查詢每次都問資料庫，CAS 重試迴圈才讀得到最新的版本號。
- **餘額異動只有一個入口 `apply`**：在一個 SAVEPOINT 內做「以版本號比對的條件式 UPDATE」加「寫一筆帳本列」。版本不符回傳
  `STALE`（呼叫端重讀重試）；冪等鍵衝突（並行的相同請求）時整個 SAVEPOINT 回復（餘額更新一併復原）並回傳既有的帳本列。
  不使用 `SELECT ... FOR UPDATE` 或任何資料庫專屬語法：兩個資料庫的條件式 UPDATE 都會對該列加寫鎖，後到者在前者提交後重新評估
  `WHERE`，版本已變所以影響 0 列。
- 預扣列的 `resolution` 是帳本唯一會被修改的欄位：`resolve_hold` 的條件式 UPDATE 只在它還是空值時成立，結算與退回因此互斥。
- **SAVEPOINT 一律經過 `_savepoint()`**：pysqlite 在遇到第一個寫入之前不會開始交易，若 SAVEPOINT 是交易裡的第一個陳述式，
  SQLite 會把它當成最外層交易，`RELEASE` 等於 `COMMIT`，外層請求之後回復時它不會跟著回復（SQL Server 沒有這個問題）。
  所以進入 SAVEPOINT 之前先做一次不影響任何列的條件式 UPDATE，讓交易先開始；兩個資料庫的行為因此一致。
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.wallet import (
    ENTRY_DAILY_CHECKIN, ENTRY_SIGNUP_GRANT, ENTRY_TURN_HOLD, ENTRY_TURN_REFUND, RESOLUTION_REFUNDED, Wallet, WalletEntry,
)

APPLIED = "applied"            # 異動成功，帶新的帳本列
STALE = "stale"                # 版本號已被別人改變，沒有任何異動，呼叫端重讀後重試
DUPLICATE = "duplicate"        # 這個冪等鍵已經存在（先前或並行的相同請求），沒有任何異動，帶既有的帳本列


@dataclass(frozen=True)
class WalletState:
    """錢包某一刻的快照（欄位查詢的結果，不是被 Session 追蹤的實體）。"""

    user_id: int
    free_balance: int
    paid_balance: int
    version: int


@dataclass(frozen=True)
class EntryDraft:
    """要寫入的帳本列（`seq` 與異動後的餘額由 `apply` 依新的版本與餘額填入）。"""

    kind: str
    idempotency_key: str
    free_delta: int
    paid_delta: int
    session_public_id: str | None = None
    ref_key: str | None = None


@dataclass(frozen=True)
class EntryRow:
    """讀出的帳本列（資料快照）。"""

    seq: int
    kind: str
    idempotency_key: str
    free_delta: int
    paid_delta: int
    free_after: int
    paid_after: int
    session_public_id: str | None
    ref_key: str | None
    resolution: str | None
    created_at: datetime


@dataclass(frozen=True)
class ApplyResult:
    status: str
    entry: EntryRow | None = None


class _StaleVersion(Exception):
    """SAVEPOINT 內部使用：條件式更新沒有命中。"""


_ENTRY_COLUMNS = (
    WalletEntry.seq, WalletEntry.kind, WalletEntry.idempotency_key, WalletEntry.free_delta, WalletEntry.paid_delta,
    WalletEntry.free_after, WalletEntry.paid_after, WalletEntry.session_public_id, WalletEntry.ref_key, WalletEntry.resolution,
    WalletEntry.created_at,
)


def _entry_row(row: Any) -> EntryRow:
    return EntryRow(*row)


class WalletRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    # ---- 錢包 ---------------------------------------------------------------------------

    def state(self, user_id: int) -> WalletState | None:
        row = self._session.execute(
            select(Wallet.free_balance, Wallet.paid_balance, Wallet.version).where(Wallet.user_id == user_id)
        ).one_or_none()
        return None if row is None else WalletState(user_id, row[0], row[1], row[2])

    def create_if_missing(self, user_id: int, now: datetime) -> WalletState:
        """第一次需要時才建立錢包（兩個餘額為 0）。並行的另一個請求搶先建立時，只回復這一筆並讀取既有的。"""
        existing = self.state(user_id)
        if existing is not None:
            return existing
        try:
            with self._savepoint():
                self._session.add(Wallet(user_id=user_id, free_balance=0, paid_balance=0, version=0, created_at=now, updated_at=now))
                self._session.flush()
        except IntegrityError:
            pass
        created = self.state(user_id)
        if created is None:                                  # 不可能：唯一衝突代表別人已經建立
            raise RuntimeError("建立錢包失敗")
        return created

    def apply(
        self, user_id: int, *, expected_version: int, new_free: int, new_paid: int, draft: EntryDraft, now: datetime,
    ) -> ApplyResult:
        """原子地改變餘額並寫一筆帳本列（見模組說明）。`new_free`、`new_paid` 必須不小於 0（Service 保證，資料庫的 CHECK 是第二道防線）。"""
        seq = expected_version + 1
        try:
            with self._savepoint():
                result = self._session.execute(
                    update(Wallet)
                    .where(and_(Wallet.user_id == user_id, Wallet.version == expected_version))
                    .values(free_balance=new_free, paid_balance=new_paid, version=seq, updated_at=now)
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    raise _StaleVersion
                self._session.add(WalletEntry(
                    user_id=user_id, seq=seq, kind=draft.kind, idempotency_key=draft.idempotency_key, free_delta=draft.free_delta,
                    paid_delta=draft.paid_delta, free_after=new_free, paid_after=new_paid, session_public_id=draft.session_public_id,
                    ref_key=draft.ref_key, resolution=None, created_at=now,
                ))
                self._session.flush()
        except _StaleVersion:
            return ApplyResult(STALE)
        except IntegrityError:
            existing = self.entry(user_id, draft.idempotency_key)
            if existing is None:                             # 冪等鍵以外的約束被違反：不是並行重送，不吞掉
                raise
            return ApplyResult(DUPLICATE, existing)
        entry = self.entry(user_id, draft.idempotency_key)
        return ApplyResult(APPLIED, entry)

    def savepoint(self):
        """一個 SAVEPOINT 的情境管理員：區塊內拋出例外時，區塊內的所有寫入（含預扣的結算標記）一併復原。
        退回預扣時用它包住「標記已退回」與「寫退回帳本」，使兩者一起成立或一起不成立。"""
        return self._savepoint()

    @contextmanager
    def _savepoint(self):
        """見模組說明：先讓交易開始（一個不影響任何列的 UPDATE），再進入 SAVEPOINT。"""
        self._session.execute(
            update(Wallet).where(Wallet.id == -1).values(version=Wallet.version).execution_options(synchronize_session=False)
        )
        with self._session.begin_nested() as nested:
            yield nested

    # ---- 帳本 ---------------------------------------------------------------------------

    def entry(self, user_id: int, key: str) -> EntryRow | None:
        row = self._session.execute(
            select(*_ENTRY_COLUMNS).where(WalletEntry.user_id == user_id, WalletEntry.idempotency_key == key)
        ).one_or_none()
        return None if row is None else _entry_row(row)

    def list_entries(self, user_id: int, *, before_seq: int | None, limit: int) -> list[EntryRow]:
        """記錄頁：依序號由新到舊（序號小於 `before_seq`）。**排除已退回的預扣與全部退回列**（淨額為零，玩家沒有感知差異）。"""
        statement = select(*_ENTRY_COLUMNS).where(
            WalletEntry.user_id == user_id,
            WalletEntry.kind != ENTRY_TURN_REFUND,
            # 不用 NOT (...)：尚未結算的預扣 resolution 是 NULL，三值邏輯會讓它被排除，但它必須顯示為「處理中」
            or_(WalletEntry.kind != ENTRY_TURN_HOLD, WalletEntry.resolution.is_(None), WalletEntry.resolution != RESOLUTION_REFUNDED),
        )
        if before_seq is not None:
            statement = statement.where(WalletEntry.seq < before_seq)
        rows = self._session.execute(statement.order_by(WalletEntry.seq.desc()).limit(limit)).all()
        return [_entry_row(row) for row in rows]

    def stale_open_holds(self, user_id: int, *, cutoff: datetime, limit: int) -> list[EntryRow]:
        """逾時仍未結算或退回的預扣（建立時間早於 `cutoff`），依序號由舊到新，最多 `limit` 筆（有界）。"""
        rows = self._session.execute(
            select(*_ENTRY_COLUMNS)
            .where(
                WalletEntry.user_id == user_id, WalletEntry.kind == ENTRY_TURN_HOLD, WalletEntry.resolution.is_(None),
                WalletEntry.created_at < cutoff,
            )
            .order_by(WalletEntry.seq).limit(limit)
        ).all()
        return [_entry_row(row) for row in rows]

    def resolve_hold(self, user_id: int, key: str, to: str) -> bool:
        """預扣的結算結果從空值設定為 `to`（條件式 UPDATE，只有還是空值時成立）。回傳是否由這次呼叫設定成功。"""
        result = self._session.execute(
            update(WalletEntry)
            .where(
                WalletEntry.user_id == user_id, WalletEntry.idempotency_key == key, WalletEntry.kind == ENTRY_TURN_HOLD,
                WalletEntry.resolution.is_(None),
            )
            .values(resolution=to)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    def export_wallet(self, user_id: int) -> tuple[WalletState, list[EntryRow]] | None:
        """個人資料匯出：餘額與**全部**帳本列（含已退回的預扣與退回列）。沒有錢包回傳 `None`，**不建立錢包**。"""
        state = self.state(user_id)
        if state is None:
            return None
        rows = self._session.execute(select(*_ENTRY_COLUMNS).where(WalletEntry.user_id == user_id).order_by(WalletEntry.seq)).all()
        return state, [_entry_row(row) for row in rows]

    def iter_entries(self, user_id: int, *, page_size: int = 500) -> Iterator[EntryRow]:
        """全部帳本列（依序號），分頁讀取；給大量匯出用。"""
        after = 0
        while True:
            rows = self._session.execute(
                select(*_ENTRY_COLUMNS).where(WalletEntry.user_id == user_id, WalletEntry.seq > after).order_by(WalletEntry.seq).limit(page_size)
            ).all()
            if not rows:
                return
            for row in rows:
                yield _entry_row(row)
            after = rows[-1][0]

    # ---- 營運用的唯讀彙總（不屬於任何單一使用者）-----------------------------------------

    def grant_totals(self, *, start: datetime, end: datetime) -> dict[str, tuple[int, int, int]]:
        """每日發放量的唯讀彙總（供日後監控贈幣與簽到）：`{種類: (帳本筆數, 發放的免費幣合計, 不重複的帳號數)}`，
        種類只有 `signup_grant` 與 `daily_checkin`；`start` 含、`end` 不含。"""
        rows = self._session.execute(
            select(
                WalletEntry.kind, func.count(), func.coalesce(func.sum(WalletEntry.free_delta), 0), func.count(func.distinct(WalletEntry.user_id)),
            )
            .where(WalletEntry.kind.in_((ENTRY_SIGNUP_GRANT, ENTRY_DAILY_CHECKIN)), WalletEntry.created_at >= start, WalletEntry.created_at < end)
            .group_by(WalletEntry.kind)
        ).all()
        found = {kind: (int(count), int(total), int(accounts)) for kind, count, total, accounts in rows}
        return {kind: found.get(kind, (0, 0, 0)) for kind in (ENTRY_SIGNUP_GRANT, ENTRY_DAILY_CHECKIN)}
