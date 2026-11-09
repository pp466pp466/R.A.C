"""錢包測試共用的組裝：真的 Repository、真的 `RuntimeLimits`（資料庫的設定表）、假的只有時鐘。

`kit.service` 是被測的 `WalletService`；`kit.set_limit(key, value)` 直接寫設定表（不經稽核），模擬部署者調整過的值；
`kit.player(n)`／`kit.admin(n)` 建立已完成年齡確認的玩家與管理員並回傳 `CurrentUser`。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.repositories.runtime_limit_repository import RuntimeLimitRepository
from app.repositories.wallet_repository import WalletRepository
from app.services.current_user import CurrentUser
from app.services.runtime_limits import RuntimeLimits
from app.services.wallet_service import WalletService
from tests.support.auth import FakeClock, add_user


class WalletKit:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.clock = FakeClock()
        self.wallets = WalletRepository(session)
        self.limits = RuntimeLimits(limits=RuntimeLimitRepository(session), clock=self.clock)
        self.service = WalletService(wallets=self.wallets, limits=self.limits, clock=self.clock)

    # ---- 人 -----------------------------------------------------------------------------

    def player(self, n: int = 1, **overrides) -> CurrentUser:
        return CurrentUser.from_model(add_user(self.session, n, password=None, **overrides))

    def admin(self, n: int = 90) -> CurrentUser:
        return CurrentUser.from_model(add_user(self.session, n, role="admin", age=30))

    def unconfirmed(self, n: int = 50) -> CurrentUser:
        return CurrentUser.from_model(add_user(self.session, n, password=None, age=None))

    # ---- 設定 ---------------------------------------------------------------------------

    def set_limit(self, key: str, value: int) -> None:
        repository = RuntimeLimitRepository(self.session)
        repository.set_value(key, value, self.clock())
        self.session.flush()

    # ---- 觀察 ---------------------------------------------------------------------------

    def balances(self, user: CurrentUser) -> tuple[int, int]:
        state = self.wallets.state(user.id)
        return (0, 0) if state is None else (state.free_balance, state.paid_balance)

    def entry(self, user: CurrentUser, key: str):
        return self.wallets.entry(user.id, key)

    def all_entries(self, user: CurrentUser):
        exported = self.wallets.export_wallet(user.id)
        return [] if exported is None else exported[1]

    def ledger_matches_balances(self, user: CurrentUser) -> bool:
        """帳本是否完整解釋餘額：各筆增減量加總等於目前的兩個餘額，且序號從 1 連續無洞。"""
        entries = self.all_entries(user)
        free, paid = self.balances(user)
        return (
            sum(e.free_delta for e in entries) == free and sum(e.paid_delta for e in entries) == paid
            and [e.seq for e in entries] == list(range(1, len(entries) + 1))
        )
