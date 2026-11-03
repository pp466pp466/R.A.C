"""玩家帳號的鎖定計次與自動鎖定（見 account-lockout 規格與 google-first-login 的 design D4）。

兩類計次，各自在 15 分鐘的滑動視窗內計算（帳號為單位）：
- `identity_rejected`（身分被拒）：已辨識出帳號卻被拒絕的 Google 登入，與補填年齡時的不一致，合計達 5 次；
- `rate_limited`（訊息被限流）：被每分鐘訊息數上限擋下，達 10 次。
任一類達門檻就鎖定；鎖定不會自動解除，只有管理員能解除（見 `admin_service`）。

- 只有 `user_id` 已知的失敗才計次：無法辨識身分的失敗與還沒有帳號的 Google 身分不計，呼叫端也就不會呼叫這裡。
- 管理員永遠不被鎖定（不計次）；已鎖定的帳號不再計次。
- 計次發生在會以 4xx 結束的請求裡（限流 429、不一致 400），預設的「4xx 就回復」會讓計次永遠累積不起來，
  所以寫入計次後呼叫 `persist`（`keep_changes_on_error`）。
- 鎖定以條件式 UPDATE（`WHERE locked_at IS NULL`）達成：並行達門檻時只有一個請求鎖定成功，也只有它寫稽核與事件。
- 稽核紀錄的管理員欄位為空（系統觸發），細節只有固定列舉的原因類別；日誌只有事件名稱與內部編號。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable, Mapping

from app.models.admin_audit_log import ACTION_ACCOUNT_LOCK
from app.models.lock_strike import STRIKE_IDENTITY_REJECTED, STRIKE_KINDS, STRIKE_RATE_LIMITED
from app.models.user import ROLE_ADMIN
from app.observability import security_events
from app.repositories.lock_strike_repository import LockStrikeRepository
from app.repositories.user_repository import UserRepository

STRIKE_WINDOW = timedelta(minutes=15)
STRIKE_LIMITS = {STRIKE_IDENTITY_REJECTED: 5, STRIKE_RATE_LIMITED: 10}

# 寫稽核的函式：(動作, 目標使用者的對外識別碼, 細節)。由 registry 提供，讓這個 Service 不直接依賴後台專用的 Repository
AuditSink = Callable[[str, str, Mapping[str, Any]], None]


class AccountLockService:
    def __init__(
        self,
        *,
        users: UserRepository,
        strikes: LockStrikeRepository,
        audit: AuditSink,
        clock: Callable[[], datetime],
        persist: Callable[[], None] = lambda: None,
    ) -> None:
        self._users = users
        self._strikes = strikes
        self._audit = audit
        self._clock = clock
        self._persist = persist

    def register_strike(self, user_id: int, kind: str) -> bool:
        """記一次計次；達門檻就鎖定。回傳這次呼叫是否使帳號被鎖定（並行時只有一個呼叫者得到真）。"""
        if kind not in STRIKE_KINDS:
            raise ValueError(f"未知的計次類別：{kind!r}")
        user = self._users.get_by_id(user_id)
        if user is None or user.role == ROLE_ADMIN or user.locked_at is not None:
            return False
        now = self._clock()
        self._strikes.record(user.id, kind, now)
        self._persist()
        if self._strikes.count_since(user.id, (kind,), now - STRIKE_WINDOW) < STRIKE_LIMITS[kind]:
            return False
        if not self._users.lock_player(user, reason=kind, now=now):
            return False
        self._audit(ACTION_ACCOUNT_LOCK, user.public_id, {"reason": kind})
        security_events.log_event(security_events.PLAYER_LOCKED, user_id=user.id)
        return True
