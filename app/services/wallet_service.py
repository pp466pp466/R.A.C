"""遊戲幣錢包的業務邏輯（見 game-wallet、wallet-api 規格與 design D2 至 D5）。

- **所有改變餘額的操作走同一個內部迴圈 `_transact`**：讀錢包 → 算新餘額與帳本列 → 由 Repository 以版本號做條件式更新並寫帳本。
  版本不符就重讀重試（至多 `MAX_ATTEMPTS` 次，之後 `WalletBusy`）；冪等鍵已存在就直接回傳既有的結果。沒有任何行程內的鎖。
- **預扣、結算、退回**供 RC-2 的回合引擎呼叫（沒有 HTTP 端點）：冪等鍵由呼叫端決定，同一個鍵永遠是同一筆預扣；結算與退回
  互斥，由預扣列的結算結果欄位（只能從空值設定一次）裁判。
- **`_prepare`**（查詢、預扣、簽到與記錄之前）：建立錢包、補發首次註冊贈幣、退回逾時的預扣；三者都冪等且有界。結算與退回
  本身不做這些，所以一個走得比較久的回合不會在自己結算之前被當成逾時。
- 設定值（贈幣數、簽到數、上限、逾時、幣價）每次操作開始時現讀一次，在該次重試之間固定使用。
- 帳本只放固定列舉與整數；日誌事件沒有接收文字的參數（`wallet_events`）。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from app.models.user import ROLE_ADMIN
from app.models.wallet import (
    BUCKET_FREE, BUCKET_PAID, BUCKETS, ENTRY_ADMIN_ADJUST, ENTRY_DAILY_CHECKIN, ENTRY_SIGNUP_GRANT, ENTRY_TURN_HOLD, ENTRY_TURN_REFUND,
    RESOLUTION_REFUNDED, RESOLUTION_SETTLED,
)
from app.observability import wallet_events
from app.repositories.wallet_repository import APPLIED, DUPLICATE, ApplyResult, EntryDraft, EntryRow, WalletRepository, WalletState
from app.services import wallet_messages
from app.services.age_policy import TAIPEI
from app.services.current_user import CurrentUser, iso_utc
from app.services.errors import (
    AlreadyCheckedIn, CheckinOverflow, HoldAlreadyRefunded, HoldAlreadySettled, InsufficientCoins, NotFound, ValidationError, WalletBusy,
)
from app.services.quota_policy import taipei_day_bounds
from app.services.runtime_limits import WALLET_MODEL_KEYS, LimitError, RuntimeLimits

MAX_ATTEMPTS = 5                    # 版本比對失敗後最多重讀重試的次數，之後 `WalletBusy`
STALE_HOLD_BATCH = 20               # 每次存取最多處理幾筆逾時預扣（有界）
HOLD_KEY_MAX = 180                  # 冪等鍵上限：退回列的鍵是 `refund:` 加上預扣的鍵，必須放得進 200 字
ENTRIES_DEFAULT_LIMIT = 20
ENTRIES_MAX_LIMIT = 100

DEFAULT_MODEL_KEY = "standard"


def checkin_amounts(free_balance: int, grant: int, cap: int) -> tuple[int, int]:
    """簽到的（實際入帳量, 作廢量）：入帳量是簽到數與「上限減免費餘額」中較小者（不小於 0），其餘作廢。
    預覽與實際入帳共用這一個函式，所以兩者不會分歧。"""
    gain = min(grant, max(0, cap - free_balance))
    return gain, grant - gain


@dataclass(frozen=True)
class Hold:
    """一筆預扣：幣價、各從免費與付費扣了多少，以及它是否早就存在（同一個冪等鍵的重送）。"""

    key: str
    cost: int
    free_taken: int
    paid_taken: int
    existed: bool
    resolution: str | None = None      # 重送時既有預扣的結算結果：None（尚未）、`settled` 或 `refunded`（例如逾時被退回）


@dataclass(frozen=True)
class CheckinPreview:
    claimable: bool
    grant: int
    would_gain: int
    would_lose: int
    next_reset_at: datetime


@dataclass(frozen=True)
class WalletView:
    free_balance: int
    paid_balance: int
    free_cap: int
    turn_cost: dict[str, int]
    checkin: CheckinPreview

    @property
    def total(self) -> int:
        return self.free_balance + self.paid_balance

    def to_dict(self) -> dict[str, Any]:
        """對外的 JSON：沒有任何內部編號。"""
        return {
            "free_balance": self.free_balance,
            "paid_balance": self.paid_balance,
            "total": self.total,
            "free_cap": self.free_cap,
            "turn_cost": dict(self.turn_cost),
            "checkin": {
                "claimable": self.checkin.claimable,
                "grant": self.checkin.grant,
                "would_gain": self.checkin.would_gain,
                "would_lose": self.checkin.would_lose,
                "next_reset_at": iso_utc(self.checkin.next_reset_at),
            },
        }


@dataclass(frozen=True)
class EntryPage:
    items: list[dict[str, Any]]
    next_before: int | None

    def to_dict(self) -> dict[str, Any]:
        return {"entries": self.items, "next_before": self.next_before}


@dataclass(frozen=True)
class _Plan:
    new_free: int
    new_paid: int
    draft: EntryDraft


class WalletService:
    def __init__(self, *, wallets: WalletRepository, limits: RuntimeLimits, clock: Callable[[], datetime]) -> None:
        self._wallets = wallets
        self._limits = limits
        self._clock = clock

    # =========================================================================
    # 查詢與簽到
    # =========================================================================

    def view(self, current: CurrentUser) -> WalletView:
        """目前的餘額、上限、幣價與簽到預覽。會先建立錢包、補發既有玩家的贈幣、退回逾時的預扣。"""
        state = self._prepare(current)
        return self._view(current, state)

    def checkin(self, current: CurrentUser, *, confirm_overflow: bool = False) -> WalletView:
        """今日簽到。入帳量小於簽到數（會有幣作廢）而沒有確認時，在寫入任何東西之前拋出 `CheckinOverflow`，也不消耗當日簽到。"""
        self._prepare(current)
        now = self._clock()
        key = self._checkin_key(current.id, now)
        if self._wallets.entry(current.id, key) is not None:
            raise AlreadyCheckedIn()
        grant, cap = self._limits.coin_checkin_grant(), self._limits.coin_free_cap()

        def plan(state: WalletState) -> _Plan:
            gain, lost = checkin_amounts(state.free_balance, grant, cap)
            if lost > 0 and not confirm_overflow:
                wallet_events.log_checkin_overflow(user_id=current.id, coins=gain, lost_coins=lost)
                raise CheckinOverflow(gain, lost)
            return _Plan(state.free_balance + gain, state.paid_balance, EntryDraft(ENTRY_DAILY_CHECKIN, key, gain, 0))

        result = self._transact(current.id, key, plan, now)
        if result.status == DUPLICATE:                                  # 並行的另一個相同請求先成功
            raise AlreadyCheckedIn()
        entry = result.entry
        wallet_events.log_checkin(user_id=current.id, coins=entry.free_delta, lost_coins=grant - entry.free_delta)
        return self._view(current, self._wallets.state(current.id))

    def entries(self, current: CurrentUser, *, before_seq: int | None = None, limit: int = ENTRIES_DEFAULT_LIMIT) -> EntryPage:
        """記錄頁：由新到舊的帳本，游標是該使用者自己的序號。已退回的預扣與退回列不出現。"""
        self._prepare(current)
        size = max(1, min(int(limit), ENTRIES_MAX_LIMIT))
        rows = self._wallets.list_entries(current.id, before_seq=before_seq, limit=size + 1)
        page = rows[:size]
        items = [
            {
                "kind": row.kind,
                "label": wallet_messages.label(row.kind, row.resolution),
                "pending": row.kind == ENTRY_TURN_HOLD and row.resolution is None,
                "free_delta": row.free_delta,
                "paid_delta": row.paid_delta,
                "created_at": iso_utc(row.created_at),
                "seq": row.seq,
            }
            for row in page
        ]
        return EntryPage(items, page[-1].seq if len(rows) > size and page else None)

    # =========================================================================
    # 贈幣
    # =========================================================================

    def ensure_signup_grant(self, current: CurrentUser) -> int:
        """首次註冊贈幣：玩家、已完成年齡確認、還沒有 `signup:<使用者>` 這筆帳本時發放；回傳入帳量（沒有發放為 0）。
        冪等，重複呼叫不會再發。管理員不發放。入帳量受免費幣上限限制。"""
        if current.role == ROLE_ADMIN or not current.age_confirmed:
            return 0
        key = f"signup:{current.id}"
        if self._wallets.entry(current.id, key) is not None:
            return 0
        now = self._clock()
        self._wallets.create_if_missing(current.id, now)
        grant, cap = self._limits.coin_signup_grant(), self._limits.coin_free_cap()

        def plan(state: WalletState) -> _Plan:
            gain = min(grant, max(0, cap - state.free_balance))
            return _Plan(state.free_balance + gain, state.paid_balance, EntryDraft(ENTRY_SIGNUP_GRANT, key, gain, 0))

        result = self._transact(current.id, key, plan, now)
        if result.status != APPLIED:
            return 0
        wallet_events.log_signup_granted(user_id=current.id, coins=result.entry.free_delta)
        return result.entry.free_delta

    # =========================================================================
    # 預扣、結算、退回（供回合引擎呼叫）
    # =========================================================================

    def hold(self, current: CurrentUser, *, key: str, session_public_id: str | None = None, model_key: str = DEFAULT_MODEL_KEY) -> Hold:
        """以冪等鍵預扣一輪的幣價（先扣免費、不足的部分扣付費）。同一個鍵重送回傳既有的預扣（`existed=True`）。
        合計餘額不足拋出 `InsufficientCoins`，不改變任何東西。"""
        if not isinstance(key, str) or not 1 <= len(key) <= HOLD_KEY_MAX:
            raise ValidationError(fields={"key": f"冪等鍵必須是 1 至 {HOLD_KEY_MAX} 字的文字。"})
        if session_public_id is not None and (not isinstance(session_public_id, str) or len(session_public_id) > 36):
            raise ValidationError(fields={"session": "存檔識別碼不正確。"})
        try:
            cost = self._limits.coin_turn_cost(model_key)
        except LimitError:
            raise ValidationError(fields={"model_key": "不認得的模型。"}) from None
        self._prepare(current)
        now = self._clock()

        existing = self._wallets.entry(current.id, key)
        if existing is not None:
            return self._existing_hold(existing, key)

        def plan(state: WalletState) -> _Plan:
            total = state.free_balance + state.paid_balance
            if total < cost:
                wallet_events.log_insufficient(user_id=current.id, balance=total, coins=cost)
                raise InsufficientCoins(total, cost)
            from_free = min(state.free_balance, cost)
            from_paid = cost - from_free
            return _Plan(
                state.free_balance - from_free, state.paid_balance - from_paid,
                EntryDraft(ENTRY_TURN_HOLD, key, -from_free, -from_paid, session_public_id=session_public_id),
            )

        result = self._transact(current.id, key, plan, now)
        if result.status == DUPLICATE:
            return self._existing_hold(result.entry, key)
        wallet_events.log_hold(user_id=current.id, coins=cost, model_key=model_key)
        return Hold(key, cost, -result.entry.free_delta, -result.entry.paid_delta, existed=False)

    def settle(self, current: CurrentUser, key: str) -> None:
        """確認扣款：餘額不再變動，預扣標記為已結算。重送成功；已退回的預扣拋出 `HoldAlreadyRefunded`；
        不存在或不屬於這位使用者的預扣一律當作不存在。"""
        hold = self._hold_or_not_found(current.id, key)
        if self._wallets.resolve_hold(current.id, key, RESOLUTION_SETTLED):
            wallet_events.log_settle(user_id=current.id, coins=-(hold.free_delta + hold.paid_delta))
            return
        latest = self._hold_or_not_found(current.id, key)
        if latest.resolution == RESOLUTION_REFUNDED:
            raise HoldAlreadyRefunded()

    def refund(self, current: CurrentUser, key: str) -> None:
        """退回預扣：依帳本記錄的拆分原路退回免費與付費餘額（不受免費幣上限限制）。重送成功；已結算的預扣拋出
        `HoldAlreadySettled`；不存在或不屬於這位使用者的預扣一律當作不存在。"""
        hold = self._hold_or_not_found(current.id, key)
        if hold.resolution == RESOLUTION_SETTLED:
            raise HoldAlreadySettled()
        if hold.resolution == RESOLUTION_REFUNDED:
            return
        if not self._refund_hold(current.id, hold):
            latest = self._hold_or_not_found(current.id, key)
            if latest.resolution == RESOLUTION_SETTLED:
                raise HoldAlreadySettled()

    # =========================================================================
    # 管理員調整
    # =========================================================================

    def adjust(self, target_user_id: int, *, bucket: str, delta: int) -> tuple[int, int]:
        """調整指定使用者的某個餘額（管理員；呼叫端已驗證權限與密碼並負責稽核）。不受免費幣上限限制；調整後該餘額小於 0
        拋出 `InsufficientCoins`。回傳調整後的（免費餘額, 付費餘額）。每次調整都是新的一筆（冪等鍵是隨機的）。"""
        if bucket not in BUCKETS:
            raise ValidationError(fields={"bucket": "餘額種類必須是 free 或 paid。"})
        if isinstance(delta, bool) or not isinstance(delta, int) or delta == 0:
            raise ValidationError(fields={"delta": "增減量必須是不為 0 的整數。"})
        now = self._clock()
        self._wallets.create_if_missing(target_user_id, now)
        key = f"admin:{uuid.uuid4()}"

        def plan(state: WalletState) -> _Plan:
            free, paid = state.free_balance, state.paid_balance
            new_free, new_paid = (free + delta, paid) if bucket == BUCKET_FREE else (free, paid + delta)
            if new_free < 0 or new_paid < 0:
                wallet_events.log_insufficient(user_id=target_user_id, balance=free + paid, coins=abs(delta))
                raise InsufficientCoins(free + paid, abs(delta), "這個餘額不足以扣除這個數量。")
            return _Plan(
                new_free, new_paid,
                EntryDraft(ENTRY_ADMIN_ADJUST, key, delta if bucket == BUCKET_FREE else 0, delta if bucket == BUCKET_PAID else 0),
            )

        result = self._transact(target_user_id, key, plan, now)
        entry = result.entry
        wallet_events.log_adjust(
            user_id=target_user_id, bucket=bucket, coins=delta, balance=entry.free_after if bucket == BUCKET_FREE else entry.paid_after,
        )
        return entry.free_after, entry.paid_after

    # =========================================================================
    # 內部
    # =========================================================================

    def _prepare(self, current: CurrentUser) -> WalletState:
        """建立錢包、補發贈幣（既有玩家與先前失敗的）、退回逾時的預扣，回傳最新的錢包狀態。"""
        self._wallets.create_if_missing(current.id, self._clock())
        self.ensure_signup_grant(current)
        self._release_stale(current.id)
        state = self._wallets.state(current.id)
        assert state is not None
        return state

    def _release_stale(self, user_id: int) -> None:
        cutoff = self._clock() - timedelta(seconds=self._limits.coin_hold_timeout())
        for hold in self._wallets.stale_open_holds(user_id, cutoff=cutoff, limit=STALE_HOLD_BATCH):
            if self._refund_hold(user_id, hold):
                wallet_events.log_stale_released(user_id=user_id, coins=-(hold.free_delta + hold.paid_delta))

    def _refund_hold(self, user_id: int, hold: EntryRow) -> bool:
        """搶到「已退回」的標記就入帳退款；兩者在同一個 SAVEPOINT 內，所以一起成立或一起不成立。回傳是否由這次呼叫退回。"""
        refund_free, refund_paid = -hold.free_delta, -hold.paid_delta
        refund_key = f"refund:{hold.idempotency_key}"
        with self._wallets.savepoint():
            if not self._wallets.resolve_hold(user_id, hold.idempotency_key, RESOLUTION_REFUNDED):
                return False

            def plan(state: WalletState) -> _Plan:
                return _Plan(
                    state.free_balance + refund_free, state.paid_balance + refund_paid,
                    EntryDraft(
                        ENTRY_TURN_REFUND, refund_key, refund_free, refund_paid, session_public_id=hold.session_public_id,
                        ref_key=hold.idempotency_key,
                    ),
                )

            self._transact(user_id, refund_key, plan, self._clock())
        wallet_events.log_refund(user_id=user_id, coins=refund_free + refund_paid)
        return True

    def _hold_or_not_found(self, user_id: int, key: str) -> EntryRow:
        entry = self._wallets.entry(user_id, key) if isinstance(key, str) else None
        if entry is None or entry.kind != ENTRY_TURN_HOLD:
            raise NotFound()
        return entry

    @staticmethod
    def _existing_hold(entry: EntryRow, key: str) -> Hold:
        if entry.kind != ENTRY_TURN_HOLD:
            raise ValidationError(fields={"key": "這個冪等鍵已用於其他用途。"})
        return Hold(key, -(entry.free_delta + entry.paid_delta), -entry.free_delta, -entry.paid_delta, existed=True, resolution=entry.resolution)

    def _transact(self, user_id: int, key: str, plan: Callable[[WalletState], _Plan], now: datetime):
        """所有改變餘額的操作的唯一迴圈（見模組說明）。`plan` 依目前的錢包狀態算出新餘額與帳本列，不得有副作用；
        它拋出的錯誤（餘額不足、需要確認）直接往外傳，沒有寫入任何東西。"""
        for _ in range(MAX_ATTEMPTS):
            existing = self._wallets.entry(user_id, key)
            if existing is not None:
                return ApplyResult(DUPLICATE, existing)
            state = self._wallets.state(user_id)
            if state is None:
                state = self._wallets.create_if_missing(user_id, now)
            chosen = plan(state)
            result = self._wallets.apply(
                user_id, expected_version=state.version, new_free=chosen.new_free, new_paid=chosen.new_paid, draft=chosen.draft, now=now,
            )
            if result.status in (APPLIED, DUPLICATE):
                return result
        raise WalletBusy()

    def _checkin_key(self, user_id: int, now: datetime) -> str:
        start, _end = taipei_day_bounds(now)
        return f"checkin:{user_id}:{start.astimezone(TAIPEI).date().isoformat()}"

    def _view(self, current: CurrentUser, state: WalletState) -> WalletView:
        now = self._clock()
        _start, end = taipei_day_bounds(now)
        claimable = self._wallets.entry(current.id, self._checkin_key(current.id, now)) is None
        grant, cap = self._limits.coin_checkin_grant(), self._limits.coin_free_cap()
        gain, lost = checkin_amounts(state.free_balance, grant, cap) if claimable else (0, 0)
        return WalletView(
            free_balance=state.free_balance,
            paid_balance=state.paid_balance,
            free_cap=cap,
            turn_cost={model: self._limits.coin_turn_cost(model) for model in WALLET_MODEL_KEYS},
            checkin=CheckinPreview(claimable, grant, gain, lost, end),
        )
