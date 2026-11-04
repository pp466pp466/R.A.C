"""被鎖定或被暫停發佈玩家的申訴（見 lockout-appeals 規格與 google-first-login 的 design D8、rpg-script-authoring 的 D13）。

- 只有「目前被鎖定」或「目前被暫停發佈」的玩家能申訴；其他人（含管理員）得到與不存在相同的 404。兩種狀態共用同一套上限與處理流程。
- 內容視為個資：NFC 正規化、去除控制字元，上限 1000 字（以清理之後的 Unicode 字元計），空白內容在這裡擋下。
- 頻率：同一帳號每小時最多 3 則、同一時間最多 1 則待處理（先判斷待處理，因為那時再送也不會成功）；拒絕時不寫入。
- 申訴內容絕不進日誌或稽核：稽核（`appeal.submit`，管理員欄位為空）只有對外識別碼與字數，日誌只有事件名稱與內部編號。
- 管理員檢視申訴、解除鎖定與解除暫停發佈在 `AdminService`（後台專用 Repository），不在這裡。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from app.models.admin_audit_log import ACTION_APPEAL_SUBMIT
from app.models.lock_appeal import MAX_APPEAL_CHARS, LockAppeal
from app.observability import security_events
from app.repositories.lock_appeal_repository import LockAppealRepository
from app.repositories.user_repository import UserRepository
from app.services.account_lock_service import AuditSink
from app.services.current_user import CurrentUser
from app.services.errors import AppealPending, AppealRateLimited, NotFound, Unauthenticated, ValidationError
from app.services.input_cleaning import clean_message

APPEAL_HOURLY_LIMIT = 3
APPEAL_WINDOW = timedelta(hours=1)
MAX_APPEALS_LISTED = 20

STATUS_PENDING = "pending"
STATUS_HANDLED = "handled"


@dataclass(frozen=True)
class AppealView:
    """玩家自己的申訴：只有自己送出的內容與處理狀態。"""

    public_id: str
    content: str
    created_at: datetime
    status: str

    def to_dict(self) -> dict:
        return {
            "id": self.public_id, "content": self.content, "status": self.status,
            "created_at": self.created_at.isoformat(timespec="seconds"),
        }


def _view(appeal: LockAppeal) -> AppealView:
    return AppealView(
        public_id=appeal.public_id, content=appeal.content, created_at=appeal.created_at,
        status=STATUS_HANDLED if appeal.handled_at is not None else STATUS_PENDING,
    )


class AppealService:
    def __init__(
        self,
        *,
        users: UserRepository,
        appeals: LockAppealRepository,
        audit: AuditSink,
        clock: Callable[[], datetime],
    ) -> None:
        self._users = users
        self._appeals = appeals
        self._audit = audit
        self._clock = clock

    def submit(self, current: CurrentUser, content: object) -> AppealView:
        user_id, public_id = self._require_appealable(current)
        text = self._clean(content)
        now = self._clock()
        if self._appeals.has_pending(user_id):
            raise AppealPending()
        if self._appeals.count_since(user_id, now - APPEAL_WINDOW) >= APPEAL_HOURLY_LIMIT:
            raise AppealRateLimited()
        appeal = self._appeals.create(user_id, text, now)
        self._audit(ACTION_APPEAL_SUBMIT, public_id, {"length": len(text)})
        security_events.log_event(security_events.APPEAL_SUBMITTED, user_id=user_id)
        return _view(appeal)

    def mine(self, current: CurrentUser) -> list[AppealView]:
        """自己的申訴，由新到舊（最多 20 則）。"""
        user_id, _ = self._require_appealable(current)
        appeals = self._appeals.list_for_user(user_id)
        return [_view(appeal) for appeal in reversed(appeals[-MAX_APPEALS_LISTED:])]

    # ---- 內部 ----------------------------------------------------------------------

    def _require_appealable(self, current: CurrentUser) -> tuple[int, str]:
        user = self._users.get_by_id(current.id)
        if user is None:
            raise Unauthenticated()
        if user.locked_at is None and user.publish_suspended_at is None:
            raise NotFound()                            # 沒有被鎖定、也沒有被暫停發佈：與不存在完全相同的回應
        return user.id, user.public_id

    @staticmethod
    def _clean(content: object) -> str:
        if not isinstance(content, str):
            raise ValidationError(fields={"content": "請輸入申訴內容。"})
        text = clean_message(content)
        if text is None:
            raise ValidationError(fields={"content": "申訴內容含有無法儲存的字元，請修改後再送出。"})
        if not text:
            raise ValidationError(fields={"content": "請輸入申訴內容。"})
        if len(text) > MAX_APPEAL_CHARS:
            raise ValidationError(fields={"content": f"申訴內容最多 {MAX_APPEAL_CHARS} 字。"})
        return text
