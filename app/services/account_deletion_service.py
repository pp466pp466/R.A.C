"""使用者自助刪除帳號的申請與撤銷（見 account-deletion 規格與 privacy-lifecycle 的 design D3、D4、D7）。

- 申請需要確認本人：有密碼的帳號重新輸入密碼（沿用 `AuthService.confirm_password`，與登入共用失敗計數與鎖定）；
  只靠 Google 的帳號輸入固定的確認文字。端點依帳號是否有密碼決定要求哪一個，另一個欄位被忽略。
- 申請是單一條件式 UPDATE（`UserRepository.request_deletion`）：狀態改為 `deletion_pending`、記下申請時間與申請前的狀態、
  `token_version` 加 1（所有裝置登出）。管理員不可自助刪除。
- 申請時可順便帶選填的布林 `keep_published_scripts`（刪除視窗的「匿名保留我已發佈的劇本」核取方塊，見 script-anonymous-retention 規格）：
  只在確認本人通過、**而且**條件式 UPDATE 成功之後才寫入偏好，所以確認失敗或被並行的另一個請求搶先時偏好不變；
  兩者在同一個交易內，寫入失敗時整個回復。沒有提供（含 `null`）時不碰偏好；提供了卻不是布林就是 400，且不計入登入失敗。
  偏好真正被採用是在期滿抹除的當下（`AccountErasureService` 於刪除當下讀取），不是在這裡。
- 撤銷也是單一條件式 UPDATE：只在寬限期（7 天）內成立，還原為申請前的狀態。
- 期滿後的抹除由排程指令走 `AccountErasureService`，不在這裡。
- 玩家端不寄任何信（含申請刪除的通知）：寬限期與撤銷方式由申請成功後的畫面與 `/account` 說明。
"""
from __future__ import annotations

import unicodedata
from datetime import datetime
from typing import Callable

from app.models.user import DELETION_GRACE_PERIOD, ROLE_ADMIN, STATUS_ACTIVE, STATUS_DELETION_PENDING, User
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
KEEP_INVALID_MESSAGE = "請以「開」或「關」指定是否匿名保留已發佈的劇本。"


class AccountDeletionService:
    def __init__(
        self,
        *,
        users: UserRepository,
        confirm_password: Callable[[CurrentUser, object], None],
        clock: Callable[[], datetime],
    ) -> None:
        self._users = users
        self._confirm_password = confirm_password
        self._clock = clock

    def request(
        self,
        current: CurrentUser,
        *,
        password: object = None,
        confirm_text: object = None,
        keep_published_scripts: object = None,
    ) -> datetime:
        """申請刪除自己的帳號。成功回傳預定刪除的時間（申請時間加 7 天）。

        `keep_published_scripts` 為 `None`（沒提供）時不碰偏好；是布林時，在申請成立的同一個交易內寫入；其他型別是 400。
        """
        user = self._require_user(current)
        if user.role == ROLE_ADMIN:
            raise Forbidden(ADMIN_DELETE_FORBIDDEN_MESSAGE, code="admin_delete_forbidden")
        if user.status != STATUS_ACTIVE:
            raise Conflict(NOT_ALLOWED_MESSAGE, code="deletion_not_allowed")
        if keep_published_scripts is not None and not isinstance(keep_published_scripts, bool):
            raise ValidationError(fields={"keep_published_scripts": KEEP_INVALID_MESSAGE})     # 早於確認本人：不計入登入失敗
        self._confirm(user, current, password, confirm_text)

        now = self._clock()
        if not self._users.request_deletion(user, now=now):
            raise Conflict(NOT_ALLOWED_MESSAGE, code="deletion_not_allowed")       # 並行的另一個請求搶先了
        if keep_published_scripts is not None and not self._users.set_keep_published_scripts(user, keep_published_scripts):
            raise Conflict(NOT_ALLOWED_MESSAGE, code="deletion_not_allowed")       # 帳號此刻不存在：整個交易回復
        scheduled_at = now + DELETION_GRACE_PERIOD
        lifecycle_events.log_deletion_requested(user_id=user.id)
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
