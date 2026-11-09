"""管理員後台的業務邏輯（見 admin-console 規格與 design D1 至 D15）。

三條不靠自律、由測試守住的規則（tests/architecture/test_admin_boundaries.py）：
1. **每個公開方法的第一件事是 `AccessPolicy.require_admin`**——非管理員得到與「不存在」相同的 404，後面什麼都不會發生。
2. **後台專用的 Repository 只在這裡使用**；一般使用者的 Repository 與端點不碰它們。
3. **每個讀取與改變都留下稽核**，且與動作同一個交易（請求成功才一起提交）；讀取類先寫稽核再回傳內容，稽核寫入失敗就不回傳。
   重新輸入密碼失敗（`reauth.failed`）與查無此人的查詢是 4xx，請求交易預設會回復，所以明確以 `persist_on_error` 保存。

可讀範圍最小：清單只有識別碼、次數與時間；只有「意見回饋」與「被內容安全過濾標記」兩類對話可讀全文；使用者資料沒有任何
個別的年齡資料（生日、年齡區間、年齡群組），年齡只有總覽的各區間人數彙總。回傳的資料一律是明確組出來的字典，不是 ORM 物件，
所以內部編號、密碼雜湊與 `token_version` 不可能意外外露。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

from app.config import UsageSettings
from app.models.admin_audit_log import (
    ACTION_ACCOUNT_UNLOCK, ACTION_APPEAL_READ, ACTION_CONVERSATION_DELETE, ACTION_CONVERSATION_READ, ACTION_FAILURES_READ,
    ACTION_PUBLISH_LIFT,
    ACTION_REAUTH_FAILED, ACTION_USER_CHANGE_TIER, ACTION_USER_LOOKUP, ACTION_USER_SUSPEND, ACTION_USER_UNSUSPEND,
    ACTION_WALLET_ADJUST, MAX_REASON_CHARS,
)
from app.models.user import (
    MEMBER_TIERS, ROLE_USER, STATUS_ACTIVE, STATUS_DELETION_PENDING, STATUS_SUSPENDED,
    USER_STATUSES, User,
)
from app.models.wallet import BUCKET_FREE, BUCKETS
from app.observability import admin_events, script_events, security_events
from app.repositories.admin_audit_repository import AdminAuditRepository
from app.repositories.admin_repository import AdminRepository, ReviewableConversation
from app.services.access_policy import AccessPolicy
from app.services.age_policy import TAIPEI
from app.services.age_stats_service import AgeStatsService
from app.services.cs_intents import INTENT_COMPLAINT_FEEDBACK, INTENT_HUMAN_HANDOFF
from app.services.current_user import CurrentUser
from app.services.errors import AdminTargetForbidden, Conflict, NotFound, NotSuspended, ValidationError
from app.services.input_cleaning import clean_single_line
from app.services.normalization import canonical_email, is_valid_email
from app.services.quota_policy import suspension_until, taipei_day_bounds
from app.services.usage_service import UsageService
from app.services.wallet_service import WalletService

# 客服在這兩種情況都會告訴使用者「管理員會看到」，所以兩者都算意見回饋（使用者的決定；只有 `unknown` 的對話不算）
FEEDBACK_INTENTS = (INTENT_COMPLAINT_FEEDBACK, INTENT_HUMAN_HANDOFF)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
MAX_MESSAGES = 200
FAILURE_WINDOW = timedelta(days=14)
MAX_FAILURES = 100
OVERVIEW_DAYS = 7

VIA_EMAIL = "email"
VIA_ID = "id"

_SUSPENDABLE = (STATUS_ACTIVE,)


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat(timespec="seconds")


def _ratio(used: int, limit: int) -> float:
    return round(used / limit, 4) if limit else 0.0


class AdminService:
    def __init__(
        self,
        *,
        policy: AccessPolicy,
        admins: AdminRepository,
        audit: AdminAuditRepository,
        usage: UsageService,
        age_stats: AgeStatsService,
        usage_settings: UsageSettings,
        confirm_password: Callable[[CurrentUser, object], None],
        persist_on_error: Callable[[], None],
        clock: Callable[[], datetime],
        wallet: WalletService | None = None,
    ) -> None:
        self._wallet = wallet                              # 調整遊戲幣餘額用（game-wallet）；沒有組裝時該操作不可用
        self._policy = policy
        self._admins = admins
        self._audit = audit
        self._usage = usage
        self._age_stats = age_stats
        self._usage_settings = usage_settings
        self._confirm_password = confirm_password
        self._persist_on_error = persist_on_error          # 「這個請求即使以 4xx 結束也保留變更」（與登入失敗計數同一個機制）
        self._clock = clock

    def authorize(self, admin: CurrentUser) -> None:
        """只做管理員檢查。控制器在讀取請求本文與查詢參數**之前**先呼叫它：非管理員對後台的任何請求（含格式錯誤的本文）
        都得到與「不存在」相同的 404，而不是 400 或 415 這種會透露端點存在的回應。"""
        self._policy.require_admin(admin)

    # =========================================================================
    # 總覽與稽核檢視
    # =========================================================================

    def overview(self, admin: CurrentUser) -> dict[str, Any]:
        """全站狀況：只有彙總。沒有任何個別使用者的 email、姓名、識別碼、生日或對話內容。"""
        self._policy.require_admin(admin)
        now = self._clock()
        start, end = taipei_day_bounds(now)
        limits = self._usage_settings
        today = self._admins.usage_for_day(start=start, end=end)

        days = []
        for offset in range(OVERVIEW_DAYS - 1, -1, -1):                       # 由舊到新，最後一筆是今天
            day_start = start - timedelta(days=offset)
            usage = today if offset == 0 else self._admins.usage_for_day(start=day_start, end=day_start + timedelta(days=1))
            days.append({
                "date": day_start.astimezone(TAIPEI).date().isoformat(), "tokens": usage.tokens, "calls": usage.calls,
                "turns": usage.turns, "blocked_calls": usage.blocked_calls,
            })

        blocked = self._admins.blocked_by_user(start=start, end=end)
        suspended_now = sum(1 for count, latest in blocked.values() if suspension_until(count, latest, now) is not None)
        by_status = self._admins.count_users_by_status()
        by_tier = self._admins.count_users_by_tier()
        ages = self._age_stats.summary()
        return {
            "users": {
                "total": sum(by_status.values()),
                "by_status": {status: by_status.get(status, 0) for status in USER_STATUSES},
                "by_tier": {tier: by_tier.get(tier, 0) for tier in MEMBER_TIERS},
            },
            "usage": {
                "today": days[-1],
                "days": days,
                "budget": {
                    "tokens": {"used": today.tokens, "limit": limits.daily_token_limit, "ratio": _ratio(today.tokens, limits.daily_token_limit)},
                    "calls": {"used": today.calls, "limit": limits.daily_call_limit, "ratio": _ratio(today.calls, limits.daily_call_limit)},
                },
            },
            "warnings": {
                "blocked_calls": sum(count for count, _ in blocked.values()),
                "blocked_users": len(blocked),
                "suspended_users": suspended_now,
            },
            "ages": {"by_range": [{"label": label, "count": count} for label, count in ages.by_range], "not_completed": ages.not_completed, "total": ages.total},
        }

    def audit_log(self, admin: CurrentUser, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0) -> list[dict[str, Any]]:
        """稽核紀錄（由新到舊）。查看稽核本身不再寫稽核。管理員只有顯示名稱，沒有 email。"""
        self._policy.require_admin(admin)
        limit, offset = self._page(limit, offset)
        return [
            {
                "id": e.id, "created_at": _iso(e.created_at), "action": e.action, "admin": e.admin_display_name,
                "target_user": e.target_user_public_id, "target_conversation": e.target_conversation_public_id,
                "reason": e.reason, "detail": e.detail,
            }
            for e in self._audit.list(limit=limit, offset=offset)
        ]

    # =========================================================================
    # 使用者
    # =========================================================================

    def lookup_user(self, admin: CurrentUser, email: object) -> dict[str, Any]:
        """以信箱精確查詢（次要入口：客服反應時使用）。找到與找不到都寫稽核（不含被查詢的信箱）。"""
        self._policy.require_admin(admin)
        if not isinstance(email, str) or not is_valid_email(email.strip()):
            raise ValidationError(fields={"email": "請輸入有效的電子郵件地址。"})
        user = self._admins.find_user_by_canonical_email(canonical_email(email.strip()))
        if user is None:
            self._record(admin, ACTION_USER_LOOKUP, detail={"via": VIA_EMAIL, "found": 0})
            self._persist_on_error()                      # 回應是 404，請求交易預設會回復，但「查過」這件事要留下
            raise NotFound()
        self._record(admin, ACTION_USER_LOOKUP, target_user=user, detail={"via": VIA_EMAIL, "found": 1})
        return self._user_view(user)

    def get_user(self, admin: CurrentUser, public_id: str) -> dict[str, Any]:
        """以對外識別碼檢視（從被標記清單進入的主要入口）。"""
        self._policy.require_admin(admin)
        user = self._find_user(public_id)
        self._record(admin, ACTION_USER_LOOKUP, target_user=user, detail={"via": VIA_ID, "found": 1})
        return self._user_view(user)

    def suspend_user(self, admin: CurrentUser, public_id: str, *, reason: object, password: object) -> dict[str, Any]:
        self._policy.require_admin(admin)
        clean_reason = self._reason(reason)
        user = self._manageable_target(public_id)
        if user.status == STATUS_SUSPENDED:
            raise Conflict("這個帳號已經是停權狀態。", code="already_suspended")
        self._confirm(admin, password, ACTION_USER_SUSPEND, target_user=user)
        previous = user.status
        if not self._admins.suspend_user(user, allowed_from=_SUSPENDABLE):
            raise Conflict("這個帳號的狀態已經改變，請重新整理後再試。", code="state_changed")
        self._record(admin, ACTION_USER_SUSPEND, target_user=user, reason=clean_reason, detail={"from_status": previous}, changed=True)
        return self._user_view(user)

    def unsuspend_user(self, admin: CurrentUser, public_id: str, *, password: object) -> dict[str, Any]:
        self._policy.require_admin(admin)
        user = self._manageable_target(public_id)
        if user.status != STATUS_SUSPENDED:
            raise Conflict("這個帳號目前不是停權狀態。", code="not_suspended")
        self._confirm(admin, password, ACTION_USER_UNSUSPEND, target_user=user)
        if not self._admins.unsuspend_user(user):
            raise Conflict("這個帳號的狀態已經改變，請重新整理後再試。", code="state_changed")
        self._record(admin, ACTION_USER_UNSUSPEND, target_user=user, detail={"to_status": STATUS_ACTIVE}, changed=True)
        return self._user_view(user)

    def change_tier(self, admin: CurrentUser, public_id: str, *, tier: object, password: object) -> dict[str, Any]:
        self._policy.require_admin(admin)
        if not isinstance(tier, str) or tier not in MEMBER_TIERS:
            raise ValidationError(fields={"tier": "會員等級必須是 " + "、".join(MEMBER_TIERS) + " 其中之一。"})
        user = self._manageable_target(public_id)
        if user.member_tier == tier:
            raise Conflict("這位使用者已經是這個等級。", code="tier_unchanged")
        self._confirm(admin, password, ACTION_USER_CHANGE_TIER, target_user=user)
        previous = user.member_tier
        if not self._admins.change_tier(user, tier=tier):
            raise Conflict("這位使用者的等級已經改變，請重新整理後再試。", code="state_changed")
        self._record(admin, ACTION_USER_CHANGE_TIER, target_user=user, detail={"from_tier": previous, "to_tier": tier}, changed=True)
        return self._user_view(user)

    def adjust_wallet(
        self, admin: CurrentUser, public_id: str, *, bucket: object, delta: object, reason: object, password: object
    ) -> dict[str, Any]:
        """調整一位玩家的遊戲幣餘額（高風險：重新輸入密碼；稽核與調整同一個交易）。調整不受免費幣上限限制；
        該餘額會因此低於 0 時拋出 `InsufficientCoins`（409），餘額與稽核都不變。"""
        self._policy.require_admin(admin)
        if self._wallet is None:
            raise RuntimeError("沒有設定錢包元件，無法調整遊戲幣。")
        errors: dict[str, str] = {}
        if bucket not in BUCKETS:
            errors["bucket"] = "餘額種類必須是 free 或 paid。"
        if isinstance(delta, bool) or not isinstance(delta, int) or delta == 0:
            errors["delta"] = "增減量必須是不為 0 的整數。"
        cleaned_reason = ""
        try:
            cleaned_reason = self._reason(reason)
        except ValidationError as error:
            errors.update(error.fields)
        if errors:
            raise ValidationError(fields=errors)
        user = self._manageable_target(public_id)
        self._confirm(admin, password, ACTION_WALLET_ADJUST, target_user=user)
        free, paid = self._wallet.adjust(user.id, bucket=bucket, delta=delta)                     # type: ignore[arg-type]
        self._record(
            admin, ACTION_WALLET_ADJUST, target_user=user, reason=cleaned_reason,
            detail={"bucket": bucket, "delta": delta, "balance_after": free if bucket == BUCKET_FREE else paid}, changed=True,
        )
        return self._user_view(user)

    # =========================================================================
    # 帳號鎖定與申訴（google-first-login）
    # =========================================================================

    def locked_accounts(self, admin: CurrentUser, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0) -> list[dict[str, Any]]:
        """目前被鎖定的玩家帳號：只有對外識別碼、顯示名稱、鎖定時間與原因類別（固定列舉）。"""
        self._policy.require_admin(admin)
        limit, offset = self._page(limit, offset)
        return [
            {"public_id": row.user_public_id, "display_name": row.display_name, "locked_at": _iso(row.locked_at), "reason": row.lock_reason}
            for row in self._admins.locked_accounts(limit=limit, offset=offset)
        ]

    def appeals(self, admin: CurrentUser, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0) -> list[dict[str, Any]]:
        """**只有「目前被鎖定」或「目前被暫停發佈」的帳號送出的申訴**（兩種都解除之後不再出現；這不開放讀取任何對話）。`restriction`
        說明該帳號目前是哪一種（鎖定優先），後台據此提供解除鎖定或解除暫停。內容是個資：只出現在這個回應，
        稽核只記筆數。先寫稽核再回傳。"""
        self._policy.require_admin(admin)
        limit, offset = self._page(limit, offset)
        rows = self._admins.appeals_of_restricted_accounts(limit=limit, offset=offset)
        self._record(admin, ACTION_APPEAL_READ, detail={"count": len(rows)})
        return [
            {
                "id": row.appeal_public_id, "user_public_id": row.user_public_id, "display_name": row.display_name,
                "content": row.content, "created_at": _iso(row.created_at), "status": "handled" if row.handled_at else "pending",
                "restriction": row.restriction,
            }
            for row in rows
        ]

    def unlock_account(self, admin: CurrentUser, public_id: str, *, password: object) -> dict[str, Any]:
        """解除玩家帳號的鎖定（高風險操作：重新輸入密碼）。成功時清除該帳號的計次、把待處理的申訴標為已處理，
        目標之後的登入狀態立刻恢復（不需要重新登入）。"""
        self._policy.require_admin(admin)
        user = self._find_user(public_id)
        if user.role != ROLE_USER:
            raise AdminTargetForbidden()
        if user.locked_at is None:
            raise Conflict("這個帳號目前沒有被鎖定。", code="not_locked")
        self._confirm(admin, password, ACTION_ACCOUNT_UNLOCK, target_user=user)
        handled = self._admins.unlock_account(user, now=self._clock())
        if handled is None:
            raise Conflict("這個帳號的狀態已經改變，請重新整理後再試。", code="state_changed")
        self._record(admin, ACTION_ACCOUNT_UNLOCK, target_user=user, detail={"handled_appeals": handled}, changed=True)
        security_events.log_event(security_events.PLAYER_UNLOCKED, user_id=user.id)
        return {"public_id": user.public_id, "display_name": user.display_name, "locked": False}

    def publish_suspended_accounts(self, admin: CurrentUser, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0) -> list[dict[str, Any]]:
        """目前被暫停發佈的玩家帳號：只有對外識別碼、顯示名稱與暫停時間（不含 email、暱稱與內部編號）。"""
        self._policy.require_admin(admin)
        limit, offset = self._page(limit, offset)
        return [
            {"public_id": row.user_public_id, "display_name": row.display_name, "suspended_at": _iso(row.suspended_at)}
            for row in self._admins.publish_suspended_accounts(limit=limit, offset=offset)
        ]

    def lift_publish_suspension(self, admin: CurrentUser, public_id: str, *, password: object) -> dict[str, Any]:
        """解除玩家的暫停發佈（高風險操作：重新輸入密碼）。成功時補一筆 `lifted` 送審紀錄、把待處理的申訴標為已處理，目標立刻可以再送審。"""
        self._policy.require_admin(admin)
        user = self._find_user(public_id)
        if user.role != ROLE_USER:
            raise AdminTargetForbidden()
        if user.publish_suspended_at is None:
            raise NotSuspended()
        self._confirm(admin, password, ACTION_PUBLISH_LIFT, target_user=user)
        handled = self._admins.lift_publish_suspension(user, now=self._clock())
        if handled is None:
            raise Conflict("這個帳號的狀態已經改變，請重新整理後再試。", code="state_changed")
        self._record(admin, ACTION_PUBLISH_LIFT, target_user=user, detail={"handled_appeals": handled}, changed=True)
        script_events.log_publish_lifted(user_id=user.id)
        return {"public_id": user.public_id, "display_name": user.display_name, "publish_suspended": False}

    # =========================================================================
    # 對話與模型失敗的原文
    # =========================================================================

    def feedback_conversations(self, admin: CurrentUser, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0) -> list[dict[str, Any]]:
        """意見回饋的清單：只有識別碼、數量與時間，沒有任何訊息內容。"""
        self._policy.require_admin(admin)
        limit, offset = self._page(limit, offset)
        return [
            {"conversation": r.conversation_public_id, "user": r.user_public_id, "count": r.count, "latest_at": _iso(r.latest_at)}
            for r in self._admins.feedback_conversations(intents=FEEDBACK_INTENTS, limit=limit, offset=offset)
        ]

    def flagged_conversations(self, admin: CurrentUser, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0) -> list[dict[str, Any]]:
        """被內容安全過濾標記的清單（警告）：每筆帶該使用者今日被擋的次數與客服對話暫停的截止時間，讓管理員判斷輕重。"""
        self._policy.require_admin(admin)
        limit, offset = self._page(limit, offset)
        rows = self._admins.flagged_conversations(limit=limit, offset=offset)
        now = self._clock()
        start, end = taipei_day_bounds(now)
        today = self._admins.blocked_by_user(start=start, end=end, user_ids=sorted({r.user_id for r in rows}))
        items = []
        for row in rows:
            count, latest = today.get(row.user_id, (0, None))
            items.append({
                "conversation": row.conversation_public_id, "user": row.user_public_id, "blocked_count": row.blocked_count,
                "latest_at": _iso(row.latest_at), "user_blocked_today": count,
                "user_chat_suspended_until": _iso(suspension_until(count, latest, now)),
            })
        return items

    def read_conversation(self, admin: CurrentUser, public_id: str) -> dict[str, Any]:
        """讀取一個可讀的對話的全文（先寫稽核再回傳）。其他對話與不存在的識別碼得到完全相同的 404，且沒有任何寫入。"""
        self._policy.require_admin(admin)
        found = self._reviewable(public_id)
        self._audit.add(
            admin_user_id=admin.id, action=ACTION_CONVERSATION_READ, now=self._clock(),
            target_user_public_id=found.user_public_id, target_conversation_public_id=found.public_id,
        )
        messages, has_more = self._admins.read_messages(found.id, limit=MAX_MESSAGES)
        return {
            "conversation": {"id": found.public_id, "user": found.user_public_id, "is_feedback": found.is_feedback, "is_flagged": found.is_flagged},
            "messages": [
                {"seq": m.seq, "role": m.role, "content": m.content, "msg_status": m.msg_status, "created_at": _iso(m.created_at)}
                for m in messages
            ],
            "has_more": has_more,
        }

    def delete_conversation(self, admin: CurrentUser, public_id: str, *, reason: object, password: object) -> None:
        """永久刪除一個可讀的對話（含訊息）；帳本保留。不提供刪除單則訊息的方式。"""
        self._policy.require_admin(admin)
        clean_reason = self._reason(reason)
        found = self._reviewable(public_id)
        self._confirm(admin, password, ACTION_CONVERSATION_DELETE, target_user_public_id=found.user_public_id, target_conversation=found.public_id)
        if not self._admins.delete_conversation(found.id):
            raise NotFound()
        self._audit.add(
            admin_user_id=admin.id, action=ACTION_CONVERSATION_DELETE, now=self._clock(), target_user_public_id=found.user_public_id,
            target_conversation_public_id=found.public_id, reason=clean_reason,
        )
        admin_events.log_action(user_id=admin.id, action=ACTION_CONVERSATION_DELETE)

    def failures(self, admin: CurrentUser) -> list[dict[str, Any]]:
        """最近 14 天模型輸出驗證失敗的原文（僅管理員可讀）。期限由查詢本身限制，不依賴清除排程。"""
        self._policy.require_admin(admin)
        now = self._clock()
        rows = self._admins.recent_failures(since=now - FAILURE_WINDOW, limit=MAX_FAILURES)
        self._audit.add(admin_user_id=admin.id, action=ACTION_FAILURES_READ, now=now, detail={"count": len(rows)})
        return [{"created_at": _iso(r.created_at), "kind": r.kind, "model": r.model, "detail": r.error_detail} for r in rows]

    # =========================================================================
    # 內部輔助
    # =========================================================================

    @staticmethod
    def _page(limit: int, offset: int) -> tuple[int, int]:
        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValidationError(fields={"limit": f"每頁筆數須為 1 至 {MAX_PAGE_SIZE}。"})
        if offset < 0:
            raise ValidationError(fields={"offset": "起始位置不可為負數。"})
        return limit, offset

    @staticmethod
    def _reason(reason: object) -> str:
        cleaned = clean_single_line(reason) if isinstance(reason, str) else None
        if not cleaned or len(cleaned) > MAX_REASON_CHARS:
            raise ValidationError(fields={"reason": f"請填寫原因（1 至 {MAX_REASON_CHARS} 字，請勿填寫個人資料）。"})
        return cleaned

    def _find_user(self, public_id: object) -> User:
        user = self._admins.find_user_by_public_id(public_id) if isinstance(public_id, str) else None
        if user is None:
            raise NotFound()
        return user

    def _manageable_target(self, public_id: object) -> User:
        """停權、解除與調整等級的目標：只能是一般使用者（管理員含自己一律 403），且不在待刪除的寬限期。"""
        user = self._find_user(public_id)
        if user.role != ROLE_USER:
            raise AdminTargetForbidden()
        if user.status == STATUS_DELETION_PENDING:
            raise Conflict("這個帳號已申請刪除，無法執行這個操作。", code="target_pending_deletion")
        return user

    def _reviewable(self, public_id: object) -> ReviewableConversation:
        found = self._admins.find_reviewable(public_id, intents=FEEDBACK_INTENTS) if isinstance(public_id, str) else None
        if found is None:
            raise NotFound()
        return found

    def _confirm(
        self, admin: CurrentUser, password: object, action: str, *, target_user: User | None = None,
        target_user_public_id: str | None = None, target_conversation: str | None = None,
    ) -> None:
        """危險操作先重新輸入密碼。失敗時寫 `reauth.failed`（含嘗試的動作）並保存，再把錯誤往外傳；沒有設定密碼不算失敗。"""
        try:
            self._confirm_password(admin, password)
        except ValidationError as error:
            if error.code == "no_password":
                raise
            self._audit.add(
                admin_user_id=admin.id, action=ACTION_REAUTH_FAILED, now=self._clock(),
                target_user_public_id=target_user.public_id if target_user is not None else target_user_public_id,
                target_conversation_public_id=target_conversation, detail={"attempted": action},
            )
            admin_events.log_reauth_failed(user_id=admin.id, action=action)
            self._persist_on_error()
            raise

    def _record(
        self, admin: CurrentUser, action: str, *, target_user: User | None = None, reason: str | None = None,
        detail: dict[str, Any] | None = None, changed: bool = False,
    ) -> None:
        self._audit.add(
            admin_user_id=admin.id, action=action, now=self._clock(),
            target_user_public_id=target_user.public_id if target_user is not None else None, reason=reason, detail=detail,
        )
        if changed:
            admin_events.log_action(user_id=admin.id, action=action)

    def _user_view(self, user: User) -> dict[str, Any]:
        """管理員看到的使用者資料：最小範圍。**沒有**密碼雜湊、`token_version`、權杖與任何個別的年齡資料。"""
        usage = self._usage.snapshot(CurrentUser.from_model(user))
        return {
            "public_id": user.public_id, "display_name": user.display_name, "email": user.email, "role": user.role,
            "status": user.status, "tier": user.member_tier, "email_verified": user.email_verified_at is not None,
            "created_at": _iso(user.created_at), "last_login_at": _iso(user.last_login_at),
            "usage": usage.to_dict(),
            "wallet": self._wallet_view(user),
        }

    def _wallet_view(self, user: User) -> dict[str, int]:
        """兩個餘額（唯讀；沒有錢包為 0，不建立錢包）。沒有帳本、沒有任何其他錢包資料。"""
        free, paid = self._admins.wallet_balances(user.id)
        return {"free_balance": free, "paid_balance": paid}
