"""信箱驗證與重設密碼（見 AGENTS.md 第 10 節與 email-token-flows 規格）。

- 權杖：32 位元組隨機值（`secrets.token_urlsafe(32)`），資料庫只存 SHA-256 雜湊；單次使用，使用的判定與標記是同一個
  條件式 UPDATE（Repository），並行最多一次成功；信箱驗證 24 小時、重設密碼 1 小時失效；
  為同一使用者、同一用途發放新權杖時，舊的未使用權杖立即失效。
- 連結帶的是網址片段（`#token=`）：伺服器與入口層的存取日誌看不到它。GET 只顯示確認頁，由 POST 完成動作。
- 寄信**不在請求的交易內**：以 `defer` 登記，提交成功、回應送出之後才寄；寄信失敗只記錄事件，不影響回應與已提交的資料。
- 寄信頻率限制（同一使用者同一用途：間隔至少 60 秒、每小時最多 5 封）由資料表本身計算，存在資料庫，符合多 worker 規範。
- 忘記密碼對「信箱是否存在」永遠回應相同；重設成功代表使用者控制該信箱，因此未驗證者同時視為已驗證。
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Callable

from app.models.user import (
    ROLE_ADMIN, STATUS_ACTIVE, STATUS_DELETION_PENDING, STATUS_PENDING_VERIFICATION, STATUS_SUSPENDED, TOKEN_RESET_PASSWORD,
    TOKEN_VERIFY_EMAIL, User,
)
from app.observability import security_events
from app.repositories.auth_token_repository import AuthTokenRepository
from app.repositories.user_repository import UserRepository
from app.services import mail_messages
from app.services.current_user import CurrentUser
from app.services.errors import TooManyRequests, Unauthenticated, ValidationError
from app.services.mail_sender import MailSender
from app.services.normalization import canonical_email, is_valid_email
from app.services.password_hasher import PasswordHasherService, password_problem

VERIFY_TTL = timedelta(hours=24)
RESET_TTL = timedelta(hours=1)
SEND_COOLDOWN = timedelta(seconds=60)
SEND_WINDOW = timedelta(hours=1)
MAX_SENDS_PER_WINDOW = 5
MAX_TOKEN_LENGTH = 200

INVALID_VERIFY_LINK_MESSAGE = "這個連結無效或已經過期。若您尚未完成驗證，請登入後重新寄送驗證信。"
INVALID_RESET_LINK_MESSAGE = "這個連結無效或已經過期，請重新申請重設密碼。"
RESEND_LIMITED_MESSAGE = "寄送得太頻繁了，請稍候幾分鐘再試。"

_BLOCKED_FROM_RESET = (STATUS_SUSPENDED, STATUS_DELETION_PENDING)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class EmailFlowService:
    def __init__(
        self,
        *,
        users: UserRepository,
        tokens: AuthTokenRepository,
        hasher: PasswordHasherService,
        clock: Callable[[], datetime],
        base_url: str,
        mail_sender: MailSender,
        defer: Callable[[Callable[[], None]], None],
        request_id: Callable[[], str | None] = lambda: None,
    ) -> None:
        self._users = users
        self._tokens = tokens
        self._hasher = hasher
        self._clock = clock
        self._base_url = base_url
        self._sender = mail_sender
        self._defer = defer
        self._request_id = request_id

    # =========================================================================
    # 信箱驗證
    # =========================================================================

    def send_verification(self, user: User) -> bool:
        """為這位使用者發放驗證權杖並登記寄信。超過寄信頻率限制時靜默略過（回傳 False），不拋出錯誤。"""
        if self._send_limited(user.id, TOKEN_VERIFY_EMAIL):
            security_events.log_event(security_events.VERIFICATION_RESEND_LIMITED, user_id=user.id)
            return False
        token = self._issue(user.id, TOKEN_VERIFY_EMAIL, VERIFY_TTL)
        content = mail_messages.verification_mail(user.display_name, mail_messages.verification_link(self._base_url, token))
        self._queue_mail(user.email, content)
        security_events.log_event(security_events.VERIFICATION_SENT, user_id=user.id)
        return True

    def resend_verification(self, current: CurrentUser) -> None:
        """已登入且信箱尚未驗證的使用者重寄驗證信。已驗證回 400，太頻繁回 429。"""
        user = self._require_user(current)
        if user.email_verified_at is not None:
            raise ValidationError("您的信箱已經完成驗證了。", code="already_verified")
        if not self.send_verification(user):
            raise TooManyRequests(RESEND_LIMITED_MESSAGE)

    def verify_email(self, token: object) -> None:
        """以權杖完成信箱驗證。待驗證帳號轉為 active；成功後 token_version 加 1，要求重新登入。"""
        user_id = self._consume(token, TOKEN_VERIFY_EMAIL)
        if user_id is None:
            security_events.log_event(security_events.EMAIL_TOKEN_REJECTED)
            raise ValidationError(INVALID_VERIFY_LINK_MESSAGE, code="invalid_token")
        user = self._users.get_by_id(user_id)
        if user is None:
            raise ValidationError(INVALID_VERIFY_LINK_MESSAGE, code="invalid_token")
        self._mark_verified(user)
        self._users.save(user)
        self._users.bump_token_version(user)
        security_events.log_event(security_events.EMAIL_VERIFIED, user_id=user.id)

    # =========================================================================
    # 忘記密碼與重設密碼
    # =========================================================================

    def request_password_reset(self, email: object) -> None:
        """忘記密碼。不論信箱是否存在、是否被頻率限制，都回傳相同的結果（沒有回傳值）。"""
        if not isinstance(email, str) or not is_valid_email(email):
            raise ValidationError(fields={"email": "請輸入有效的電子郵件地址。"})
        user = self._users.get_by_canonical_email(canonical_email(email))
        self._send_reset_link(user)

    def request_admin_password_reset(self, user_id: int, token_version: int) -> None:
        """管理員在密碼驗證步驟按「忘記密碼」。對象由簽章的中間狀態（`user_id`、`token_version`）決定，不是請求內容；
        中間狀態對不上目前的帳號（不是管理員、`token_version` 已變）時什麼也不寄。回應與結果無關（沒有回傳值）。"""
        user = self._users.get_by_id(user_id)
        if user is not None and (user.role != ROLE_ADMIN or user.token_version != token_version):
            user = None
        self._send_reset_link(user)

    def _send_reset_link(self, user: User | None) -> None:
        """寄重設密碼的信（玩家的忘記密碼與管理員的忘記密碼共用）：帳號不存在、被停用或被頻率限制時不寄，結果一樣不外露。"""
        if user is None or user.status in _BLOCKED_FROM_RESET:
            security_events.log_event(security_events.PASSWORD_RESET_REQUESTED, user_id=user.id if user else None)
            return
        if self._send_limited(user.id, TOKEN_RESET_PASSWORD):
            security_events.log_event(security_events.PASSWORD_RESET_LIMITED, user_id=user.id)
            return
        token = self._issue(user.id, TOKEN_RESET_PASSWORD, RESET_TTL)
        content = mail_messages.reset_password_mail(user.display_name, mail_messages.reset_link(self._base_url, token))
        self._queue_mail(user.email, content)
        security_events.log_event(security_events.PASSWORD_RESET_REQUESTED, user_id=user.id)

    def reset_password(self, token: object, new_password: object) -> None:
        """以權杖設定新密碼。純 Google 帳號也能藉此設定密碼；未驗證的信箱同時視為已驗證。

        先檢查新密碼再消耗權杖：密碼不合規時權杖不會被浪費。
        成功後 token_version 加 1（所有既有登入失效）、清除登入失敗計數與鎖定，並寄出密碼已變更的通知信。
        """
        problem = password_problem(new_password)
        if problem:
            raise ValidationError(fields={"new_password": problem})
        assert isinstance(new_password, str)
        user_id = self._consume(token, TOKEN_RESET_PASSWORD)
        if user_id is None:
            security_events.log_event(security_events.EMAIL_TOKEN_REJECTED)
            raise ValidationError(INVALID_RESET_LINK_MESSAGE, code="invalid_token")
        user = self._users.get_by_id(user_id)
        if user is None:
            raise ValidationError(INVALID_RESET_LINK_MESSAGE, code="invalid_token")
        user.password_hash = self._hasher.hash(new_password)
        self._mark_verified(user)
        self._users.save(user)
        self._users.reset_login_failures(user)
        self._users.bump_token_version(user)
        self._tokens.invalidate_unused(user_id=user.id, purpose=TOKEN_RESET_PASSWORD, now=self._clock())
        security_events.log_event(security_events.PASSWORD_RESET, user_id=user.id)
        self.send_password_changed_notice(user)

    def send_password_changed_notice(self, user: User) -> None:
        self._queue_mail(user.email, mail_messages.password_changed_mail(user.display_name))

    def send_deletion_notice(self, user: User, scheduled_at: datetime) -> None:
        """申請刪除帳號的通知信（提交成功後才寄；寄信失敗只記錄事件，不影響申請結果）。"""
        self._queue_mail(user.email, mail_messages.deletion_notice_mail(user.display_name, scheduled_at))

    # =========================================================================
    # 內部
    # =========================================================================

    def _mark_verified(self, user: User) -> None:
        if user.email_verified_at is None:
            user.email_verified_at = self._clock()
        if user.status == STATUS_PENDING_VERIFICATION:
            user.status = STATUS_ACTIVE

    def _issue(self, user_id: int, purpose: str, ttl: timedelta) -> str:
        now = self._clock()
        self._tokens.invalidate_unused(user_id=user_id, purpose=purpose, now=now)    # 新的取代舊的
        token = secrets.token_urlsafe(32)
        self._tokens.add_token(
            user_id=user_id, purpose=purpose, token_hash=hash_token(token), created_at=now, expires_at=now + ttl
        )
        return token

    def _consume(self, token: object, purpose: str) -> int | None:
        if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LENGTH:
            return None
        return self._tokens.consume(token_hash=hash_token(token), purpose=purpose, now=self._clock())

    def _send_limited(self, user_id: int, purpose: str) -> bool:
        now = self._clock()
        latest = self._tokens.latest_created_at(user_id=user_id, purpose=purpose)
        if latest is not None and now - latest < SEND_COOLDOWN:
            return True
        recent = self._tokens.count_created_since(user_id=user_id, purpose=purpose, since=now - SEND_WINDOW)
        return recent >= MAX_SENDS_PER_WINDOW

    def _queue_mail(self, to: str, content: mail_messages.MailContent) -> None:
        """登記提交成功後才寄的信。收件人、內容與請求識別碼都在此時綁定（執行時請求與資料庫都已關閉）。"""
        sender, request_id = self._sender, self._request_id()

        def deliver() -> None:
            try:
                sender.send(to=to, subject=content.subject, body=content.body)
            except Exception:  # noqa: BLE001 - 寄信失敗不影響已提交的資料；只記錄事件，不記錄地址、權杖與例外訊息
                security_events.log_event(security_events.MAIL_SEND_FAILED, request_id=request_id)

        self._defer(deliver)

    def _require_user(self, current: CurrentUser) -> User:
        user = self._users.get_by_id(current.id)
        if user is None:
            raise Unauthenticated()
        return user
