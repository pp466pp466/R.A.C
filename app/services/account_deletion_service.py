"""使用者自助刪除帳號的申請與撤銷（見 account-deletion 規格與 privacy-lifecycle 的 design D3、D4、D7）。

- 申請需要確認本人：有密碼的帳號重新輸入密碼（沿用 `AuthService.confirm_password`，與登入共用失敗計數與鎖定）；
  只靠 Google 的帳號輸入固定的確認文字。端點依帳號是否有密碼決定要求哪一個，另一個欄位被忽略。
- 申請是單一條件式 UPDATE（`UserRepository.request_deletion`）：狀態改為 `deletion_pending`、記下申請時間與申請前的狀態、
  `token_version` 加 1（所有裝置登出）。管理員不可自助刪除。
- 撤銷也是單一條件式 UPDATE：只在寬限期（7 天）內成立，還原為申請前的狀態。
- 期滿後的抹除由排程指令走 `AccountErasureService`，不在這裡。
- 信箱驗證開啟時，申請成功後（提交之後）寄通知信；寄信失敗不影響申請結果。
"""
from __future__ import annotations

import unicodedata
from datetime import datetime
from typing import Callable, Protocol

from app.models.user import DELETION_GRACE_PERIOD, ROLE_ADMIN, STATUS_ACTIVE, STATUS_DELETION_PENDING, STATUS_PENDING_VERIFICATION, User
from app.observability import lifecycle_events
from app.repositories.user_repository import UserRepository
from app.services.current_user import CurrentUser
from app.services.errors import Conflict, Forbidden, Unauthenticated, ValidationError

# 只靠 Google 登入的帳號沒有密碼可輸入，改輸入這段固定文字；前端顯示的就是這個常數
CONFIRM_TEXT = "刪除我的帳號"

ADMIN_DELETE_FORBIDDEN_MESSAGE = "管理員帳號無法自行刪除。"
NOT_ALLOWED_MESSAGE = "目前的帳號狀態無法申請刪除。"
NOT_PENDING_MESSAGE = "您的帳號沒有申請刪除，不需要撤銷。"
GRACE_EXPIRED_MESSAGE = "撤銷期限（申請後 7 天）已過，帳號即將被刪除，無法撤銷。"


class DeletionMailer(Protocol):
    def send_deletion_notice(self, user: User, scheduled_at: datetime) -> None: ...


class AccountDeletionService:
    def __init__(
        self,
        *,
        users: UserRepository,
        confirm_password: Callable[[CurrentUser, object], None],
        clock: Callable[[], datetime],
        mailer: DeletionMailer | None = None,
    ) -> None:
        self._users = users
        self._confirm_password = confirm_password
        self._clock = clock
        self._mailer = mailer

    def request(self, current: CurrentUser, *, password: object = None, confirm_text: object = None) -> datetime:
        """申請刪除自己的帳號。成功回傳預定刪除的時間（申請時間加 7 天）。"""
        user = self._require_user(current)
        if user.role == ROLE_ADMIN:
            raise Forbidden(ADMIN_DELETE_FORBIDDEN_MESSAGE, code="admin_delete_forbidden")
        if user.status not in (STATUS_ACTIVE, STATUS_PENDING_VERIFICATION):
            raise Conflict(NOT_ALLOWED_MESSAGE, code="deletion_not_allowed")
        self._confirm(user, current, password, confirm_text)

        now = self._clock()
        if not self._users.request_deletion(user, now=now):
            raise Conflict(NOT_ALLOWED_MESSAGE, code="deletion_not_allowed")       # 並行的另一個請求搶先了
        scheduled_at = now + DELETION_GRACE_PERIOD
        lifecycle_events.log_deletion_requested(user_id=user.id)
        if self._mailer is not None:
            self._mailer.send_deletion_notice(user, scheduled_at)
        return scheduled_at

    def cancel(self, current: CurrentUser) -> CurrentUser:
        """撤銷刪除：寬限期內還原為申請前的狀態，回傳最新的快照（目前的登入狀態因此變成完整的）。"""
        user = self._require_user(current)
        if user.status != STATUS_DELETION_PENDING:
            raise Conflict(NOT_PENDING_MESSAGE, code="not_pending")
        if not self._users.cancel_deletion(user, now=self._clock()):
            expired = user.status == STATUS_DELETION_PENDING
            raise Conflict(GRACE_EXPIRED_MESSAGE if expired else NOT_PENDING_MESSAGE, code="grace_expired" if expired else "not_pending")
        lifecycle_events.log_deletion_cancelled(user_id=user.id)
        return CurrentUser.from_model(user)

    # ---- 內部 --------------------------------------------------------------------

    def _confirm(self, user: User, current: CurrentUser, password: object, confirm_text: object) -> None:
        if user.password_hash:
            self._confirm_password(current, password)            # 失敗已計入登入失敗次數（並保存），可能因此鎖定
            return
        if not isinstance(confirm_text, str) or unicodedata.normalize("NFC", confirm_text).strip() != CONFIRM_TEXT:
            raise ValidationError(fields={"confirm_text": f"請輸入「{CONFIRM_TEXT}」以確認。"})

    def _require_user(self, current: CurrentUser) -> User:
        user = self._users.get_by_id(current.id)
        if user is None:
            raise Unauthenticated()
        return user
