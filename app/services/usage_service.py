"""客服的用量檢查（見 cs-usage-limits、global-usage-budget、cs-safety-suspension 規格與 design D1、D3 至 D7）。

這個類別只回答「這一次可不可以」，拒絕時拋出 `services/errors.py` 的用量錯誤（控制層統一轉成 429／409／503／403）：
- 所有計數都從資料庫導出——帳本（`llm_call_logs`）與訊息表——沒有任何計數器欄位，也不依賴行程內記憶體，所以多個
  worker 看到同一個數字，重啟也不會讓暫停或額度失效。
- 檢查在取得對話的處理標記**之前**，拒絕時不寫入訊息、不設標記、不呼叫模型、不新增帳本（呼叫端依序呼叫 `check_send`）。
- 順序：暫停 → 每分鐘 → 每日輪數 → 全站用量。同時違反多項時，使用者看到的是最優先的那一項。
- 檢查與提交之間有時間差，並行請求可能一起通過；超額的幅度不超過並行數（處理中的其他對話已視為預留名額），
  全站用量上限是第二道保護。需要硬上限時須改用資料庫中的原子計數列（另案）。

會員等級只影響這裡的額度，不影響權限：`AccessPolicy` 不匯入 `quota_policy`。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from app.config import UsageSettings
from app.observability import usage_events
from app.repositories.chat_repository import ChatRepository
from app.repositories.llm_call_log_repository import LlmCallLogRepository
from app.services.age_policy import TAIPEI
from app.services.current_user import CurrentUser
from app.services.errors import (
    ChatSuspended, ConversationLimitReached, DailyQuotaExceeded, RateLimited, SystemBusy,
)
from app.services.quota_policy import BUDGET_WARNING_RATIO, RATE_WINDOW, limits_for, suspension_until, taipei_day_bounds


@dataclass(frozen=True)
class UsageSnapshot:
    """使用者自己的用量，給 `GET /api/cs/usage`。不含全站用量、token 數或任何其他使用者的資料。"""

    tier: str
    daily_used: int
    daily_limit: int
    resets_at: datetime
    per_minute_limit: int
    conversations: int
    conversations_limit: int
    suspended_until: datetime | None

    def to_dict(self) -> dict[str, object]:
        return {
            "tier": self.tier,
            "daily": {"used": self.daily_used, "limit": self.daily_limit, "resets_at": self.resets_at.isoformat(timespec="seconds")},
            "per_minute_limit": self.per_minute_limit,
            "conversations": {"count": self.conversations, "limit": self.conversations_limit},
            "suspended_until": None if self.suspended_until is None else self.suspended_until.isoformat(timespec="seconds"),
        }


def _seconds_until(target: datetime, now: datetime) -> int:
    """距離 `target` 的整數秒（進位），至少 1：`Retry-After` 不該是 0。"""
    return max(1, math.ceil((target - now).total_seconds()))


def _aware(moment: datetime) -> datetime:
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


class UsageService:
    def __init__(
        self,
        *,
        ledger: LlmCallLogRepository,
        chats: ChatRepository,
        settings: UsageSettings,
        clock: Callable[[], datetime],
        processing_ttl: timedelta,
    ) -> None:
        self._ledger = ledger
        self._chats = chats
        self._settings = settings
        self._clock = clock
        self._processing_ttl = processing_ttl            # 處理標記多久內視為仍在處理（與 ChatService 的過期時間相同）

    # ---- 暫停 ---------------------------------------------------------------------

    def suspended_until(self, user: CurrentUser) -> datetime | None:
        """今日（台灣日）對話呼叫被安全過濾阻擋達 3 次，暫停到最近一次被擋的時間加 1 小時；沒有暫停為 None。

        規則本身在 `quota_policy.suspension_until`（後台的被標記清單共用同一份）。摘要呼叫被擋不計入（由 Repository 的條件排除）。
        """
        now = self._clock()
        start, end = taipei_day_bounds(now)
        count, latest = self._ledger.blocked_chat_calls(user.id, start=start, end=end)
        return suspension_until(count, latest, now)

    def record_block_outcome(self, user: CurrentUser) -> None:
        """一則訊息剛因安全過濾被擋：若因此達到暫停門檻就記錄事件。必須在帳本的那一列提交之後呼叫。"""
        if self.suspended_until(user) is not None:
            usage_events.log_suspended(user_id=user.id)

    def _check_not_suspended(self, user: CurrentUser) -> None:
        until = self.suspended_until(user)
        if until is None:
            return
        usage_events.log_denied(user_id=user.id, reason=usage_events.DENIED_SUSPENDED)
        resume = until.astimezone(TAIPEI).strftime("%H:%M")
        raise ChatSuspended(until=until, message=f"由於多次觸發內容安全限制，客服對話已暫停，將於 {resume}（台灣時間）恢復。")

    # ---- 送出訊息 -----------------------------------------------------------------

    def check_send(self, user: CurrentUser, *, conversation_id: int, already_accepted: bool = False) -> None:
        """一則新的（或需要補上回覆的）訊息可不可以送出；不可以就拋出對應的用量錯誤。

        `already_accepted`：這則訊息先前已被接受並儲存、只是沒有得到回覆（上次中斷）。它不再受每分鐘限制，
        因為已經被接受過一次；其餘的檢查照常，因為它仍要呼叫模型。
        """
        self._check_not_suspended(user)
        now = self._clock()
        limits = limits_for(user.member_tier)
        if not already_accepted:
            self._check_rate(user, now, limits.per_minute)
        self._check_daily(user, now, limits.daily_turns, conversation_id)
        self._check_global(user, now)

    def _check_rate(self, user: CurrentUser, now: datetime, per_minute: int) -> None:
        recent = self._chats.recent_user_message_times(user.id, since=now - RATE_WINDOW, limit=per_minute)
        if len(recent) < per_minute:
            return
        usage_events.log_denied(user_id=user.id, reason=usage_events.DENIED_RATE_LIMITED)
        oldest_in_window = _aware(recent[-1])               # 第 `per_minute` 新的那一則；它離開視窗就空出一個名額
        raise RateLimited(retry_after=_seconds_until(oldest_in_window + RATE_WINDOW, now))

    def _check_daily(self, user: CurrentUser, now: datetime, daily_limit: int, conversation_id: int) -> None:
        start, end = taipei_day_bounds(now)
        used = self._ledger.count_counted_chat_calls(user.id, start=start, end=end)
        in_flight = self._chats.count_processing(
            user.id, fresh_since=now - self._processing_ttl, exclude_conversation_id=conversation_id
        )
        if used + in_flight < daily_limit:
            return
        usage_events.log_denied(user_id=user.id, reason=usage_events.DENIED_DAILY_QUOTA)
        raise DailyQuotaExceeded(retry_after=_seconds_until(end, now))

    # ---- 全站用量 -----------------------------------------------------------------

    def _global_usage(self, now: datetime) -> tuple[int, int]:
        start, end = taipei_day_bounds(now)
        return self._ledger.usage_totals(start=start, end=end)

    def global_budget_exhausted(self) -> bool:
        """全站今日的 token 或呼叫次數是否已達上限（背景的摘要折疊用；不記錄事件）。"""
        tokens, calls = self._global_usage(self._clock())
        return tokens >= self._settings.daily_token_limit or calls >= self._settings.daily_call_limit

    def _check_global(self, user: CurrentUser, now: datetime) -> None:
        tokens, calls = self._global_usage(now)
        token_limit, call_limit = self._settings.daily_token_limit, self._settings.daily_call_limit
        if tokens >= token_limit or calls >= call_limit:
            usage_events.log_denied(user_id=user.id, reason=usage_events.DENIED_GLOBAL_BUDGET)
            raise SystemBusy()
        # 越過 80% 只記錄、不阻擋；沒有跨 worker 的共享狀態可去重，所以越過後的每個請求都記一筆，由告警層以時間窗去重
        if tokens >= token_limit * BUDGET_WARNING_RATIO:
            usage_events.log_budget_warning(resource=usage_events.BUDGET_TOKENS, used=tokens, limit=token_limit)
        if calls >= call_limit * BUDGET_WARNING_RATIO:
            usage_events.log_budget_warning(resource=usage_events.BUDGET_CALLS, used=calls, limit=call_limit)

    # ---- 建立對話與查看用量 ---------------------------------------------------------

    def check_create_conversation(self, user: CurrentUser) -> None:
        """新對話會超過對話數上限就拒絕。呼叫端須先確認這不是「同一識別碼的冪等重送」。"""
        if self._chats.count_conversations(user.id) < limits_for(user.member_tier).conversations:
            return
        usage_events.log_denied(user_id=user.id, reason=usage_events.DENIED_CONVERSATION_LIMIT)
        raise ConversationLimitReached()

    def snapshot(self, user: CurrentUser) -> UsageSnapshot:
        now = self._clock()
        start, end = taipei_day_bounds(now)
        limits = limits_for(user.member_tier)
        return UsageSnapshot(
            tier=user.member_tier,
            daily_used=self._ledger.count_counted_chat_calls(user.id, start=start, end=end),
            daily_limit=limits.daily_turns,
            resets_at=end,
            per_minute_limit=limits.per_minute,
            conversations=self._chats.count_conversations(user.id),
            conversations_limit=limits.conversations,
            suspended_until=self.suspended_until(user),
        )
