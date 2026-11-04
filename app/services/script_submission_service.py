"""送審：草稿 -> 待審（見 script-submission 規格與 rpg-script-authoring 的 design D8、D9）。

`POST /api/scripts/versions/<id>/submit` 依序檢查，並在第一個不通過處停止（後面的步驟都不執行，包括不呼叫模型）：
1. 帳號與年齡閘門；2. 擁有者（別人的版本是 `NotFound`）；3. 版本為草稿（`NotDraft`）；4. 沒有被暫停發佈（`PublishSuspended`）；
5. 今日（台灣日）送審次數未達會員等級的上限（`SubmissionLimit`；上限來自資料庫的 `runtime_limits`）；
6. 固定規格重新驗證與**規則層**；7. **模型層**（`ScriptReviewService.ensure_reviewed`；不可用一律擋下）；
8. 合併規則層與模型層的 `flag` 類別記在版本上、條件式轉為待審、寫一筆 `sent`。

- **自動拒絕**（規則層或模型層明確拒絕）：寫一筆 `auto_rejected`（只有使用者、類別列舉與時間，沒有版本識別碼與任何內容）、評估連續被拒，
  回 422 與類別；版本維持草稿。因為請求以 4xx 結束，預設會回復交易，所以寫入後呼叫 `persist`（`keep_changes_on_error`）。
- **連續被拒**：最近 N 筆（N 是 `script_reject_streak`）紀錄全為 `auto_rejected` 才暫停發佈；成功送審（`sent`）與管理員解除（`lifted`）使
  連續中斷。暫停是單一條件式 UPDATE，並行時只有一個請求成功並寫稽核（管理員欄位為空）。管理員不會被暫停。
- **審查不可用**（模型層無法給出結果）：回 503，**不寫任何紀錄**，不計入次數與連續被拒。審查期間草稿被修改（結果沒有被記錄）：回 `DraftChanged`。
- 每日次數不做鎖定：兩個並行的請求可能多送一次，上限是營運節流，不是安全邊界（design 的 Risks）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Mapping

from app.models.admin_audit_log import ACTION_PUBLISH_SUSPEND
from app.models.script import REVIEW_FLAG, REVIEW_PASS, REVIEW_REJECT, VERSION_DRAFT
from app.models.script_submission import SUBMISSION_AUTO_REJECTED, SUBMISSION_SENT
from app.observability import script_events
from app.repositories.errors import InvalidVersionTransitionError
from app.repositories.script_repository import ScriptRepository
from app.repositories.script_submission_repository import ScriptSubmissionRepository
from app.repositories.user_repository import UserRepository
from app.services.access_policy import AccessPolicy
from app.services.current_user import CurrentUser
from app.services.errors import (
    DraftChanged, NotDraft, NotFound, ReviewUnavailable, ScriptAutoRejected, SubmissionLimit, Unauthenticated,
)
from app.services.quota_policy import taipei_day_bounds
from app.services.runtime_limits import RuntimeLimits
from app.services.script_authoring_service import ScriptAuthoringService
from app.services.script_review_service import ScriptReviewService
from app.services.script_rules import check_rules
from app.services.script_spec import validate_script

AuditSink = Callable[[str, "str | None", Mapping[str, Any]], None]


class ScriptSubmissionService:
    def __init__(
        self,
        *,
        scripts: ScriptRepository,
        users: UserRepository,
        submissions: ScriptSubmissionRepository,
        policy: AccessPolicy,
        limits: RuntimeLimits,
        reviews: ScriptReviewService,
        authoring: ScriptAuthoringService,
        audit: AuditSink,
        clock: Callable[[], datetime],
        persist: Callable[[], None] = lambda: None,
    ) -> None:
        self._scripts = scripts
        self._users = users
        self._submissions = submissions
        self._policy = policy
        self._limits = limits
        self._reviews = reviews
        self._authoring = authoring
        self._audit = audit
        self._clock = clock
        self._persist = persist

    def submit(self, current: CurrentUser, version_public_id: str) -> dict[str, Any]:
        self._policy.require_chat_access(current)
        user = self._users.get_by_id(current.id)
        if user is None:
            raise Unauthenticated()
        version = self._scripts.get_owned_version(user.id, version_public_id) if isinstance(version_public_id, str) else None
        if version is None:
            raise NotFound()
        if version.status != VERSION_DRAFT:
            raise NotDraft()
        self._policy.require_can_submit(CurrentUser.from_model(user))          # 讀剛查到的使用者列，不是請求開始時的快照

        now = self._clock()
        start, end = taipei_day_bounds(now)
        if self._submissions.count_counted_between(user.id, start=start, end=end) >= self._limits.submit_daily(user.member_tier):
            raise SubmissionLimit(retry_after=max(1, int((end - now).total_seconds())))

        content = self._scripts.content_of(version)
        validate_script(content, max_tier=self._policy.author_content_cap(current))      # 規格不變；上限只會隨年齡變寬鬆
        rules = check_rules(content, tier=version.content_tier, adult_age=self._policy.adult_age)
        if rules.verdict == REVIEW_REJECT:
            self._reject(user.id, user.public_id, "rules", rules.categories, now)

        review = self._reviews.ensure_reviewed(user.id, version, content)
        if review.verdict is None:
            raise ReviewUnavailable()
        if review.stale:
            raise DraftChanged()
        if review.verdict == REVIEW_REJECT:
            self._reject(user.id, user.public_id, "model", review.categories, now)

        flags = sorted(
            (set(rules.categories) if rules.verdict == REVIEW_FLAG else set()) | (set(review.categories) if review.verdict == REVIEW_FLAG else set())
        )
        merged = REVIEW_FLAG if flags else REVIEW_PASS
        if not self._scripts.record_review(version.id, version.content_hash, merged, flags, now):
            raise DraftChanged()                                      # 內容在這之間被改了：舊結果不蓋在新內容上
        try:
            self._scripts.submit_for_review(user.id, version_public_id)
        except InvalidVersionTransitionError:
            raise NotDraft() from None                                # 並行的另一個請求已送審（或撤回）
        self._submissions.record(user.id, SUBMISSION_SENT, flags, now)
        script_events.log_submitted(user_id=user.id, flagged=bool(flags))
        refreshed = self._scripts.get_owned_version(user.id, version_public_id)
        assert refreshed is not None
        return self._authoring.version_detail(refreshed)

    # ---- 內部 -------------------------------------------------------------------------

    def _reject(self, user_id: int, user_public_id: str, layer: str, categories: tuple[str, ...], now: datetime) -> None:
        """自動拒絕：記一筆（只有類別與時間）、評估連續被拒、保留這些寫入，然後回 422。永不回傳。"""
        self._submissions.record(user_id, SUBMISSION_AUTO_REJECTED, categories, now)
        script_events.log_auto_rejected(user_id=user_id, stage="submit", layer=layer, categories=categories)
        self._suspend_when_streak(user_id, user_public_id, now)
        self._persist()
        raise ScriptAutoRejected(categories)

    def _suspend_when_streak(self, user_id: int, user_public_id: str, now: datetime) -> None:
        """最近 N 筆全為自動拒絕才暫停發佈；條件式更新，並行時只有一個請求成功並寫稽核。"""
        streak = self._limits.reject_streak()
        recent = self._submissions.recent_outcomes(user_id, streak)
        if len(recent) < streak or any(outcome != SUBMISSION_AUTO_REJECTED for outcome in recent):
            return
        user = self._users.get_by_id(user_id)
        if user is not None and self._users.suspend_publish_if_clear(user, now):
            self._audit(ACTION_PUBLISH_SUSPEND, user_public_id, {"streak": streak})
            script_events.log_publish_suspended(user_id=user_id)
