"""管理員的重設密碼（見 AGENTS.md 第 10 節與 email-token-flows 規格）。

玩家只用 Google 登入：沒有信箱驗證、沒有玩家的忘記密碼，也不寄任何信給玩家。這裡只剩管理員在密碼驗證步驟按
「忘記密碼」的重設流程，以及密碼已變更的通知信。

- 權杖：32 位元組隨機值（`secrets.token_urlsafe(32)`），資料庫只存 SHA-256 雜湊；單次使用，使用的判定與標記是同一個
  條件式 UPDATE（Repository），並行最多一次成功；重設密碼 1 小時失效；
  為同一使用者發放新權杖時，舊的未使用權杖立即失效。
- 連結帶的是網址片段（`#token=`）：伺服器與入口層的存取日誌看不到它。GET 只顯示確認頁，由 POST 完成動作。
- 寄信**不在請求的交易內**：以 `defer` 登記，提交成功、回應送出之後才寄；寄信失敗只記錄事件，不影響回應與已提交的資料。
- 寄信頻率限制（同一使用者同一用途：間隔至少 60 秒、每小時最多 5 封）由資料表本身計算，存在資料庫，符合多 worker 規範。
- 重設只接受管理員的權杖（遷移前遺留的玩家權杖一律視為無效）；對象由簽章的中間狀態決定，不是請求內容。
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Callable

from app.models.user import ROLE_ADMIN, STATUS_DELETION_PENDING, STATUS_SUSPENDED, TOKEN_RESET_PASSWORD, User
from app.observability import security_events
from app.repositories.auth_token_repository import AuthTokenRepository
from app.repositories.user_repository import UserRepository
from app.services import mail_messages
from app.services.errors import ValidationError
from app.services.mail_sender import MailSender
from app.services.password_hasher import PasswordHasherService, password_problem

RESET_TTL = timedelta(hours=1)
SEND_COOLDOWN = timedelta(seconds=60)
SEND_WINDOW = timedelta(hours=1)
MAX_SENDS_PER_WINDOW = 5
MAX_TOKEN_LENGTH = 200

INVALID_RESET_LINK_MESSAGE = "這個連結無效或已經過期，請重新申請重設密碼。"

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
    # 管理員的忘記密碼與重設密碼
    # =========================================================================

    def request_admin_password_reset(self, user_id: int, token_version: int) -> None:
        """管理員在密碼驗證步驟按「忘記密碼」。對象由簽章的中間狀態（`user_id`、`token_version`）決定，不是請求內容；
        中間狀態對不上目前的帳號（不是管理員、`token_version` 已變）時什麼也不寄。回應與結果無關（沒有回傳值）。"""
        user = self._users.get_by_id(user_id)
        if user is not None and (user.role != ROLE_ADMIN or user.token_version != token_version):
            user = None
        self._send_reset_link(user)

    def _send_reset_link(self, user: User | None) -> None:
        """寄重設密碼的信：帳號不存在、被停用或被頻率限制時不寄，結果一樣不外露。"""
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
        """以權杖設定新密碼。權杖所屬的帳號必須是管理員，否則與無效權杖相同。

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
        if user is None or user.role != ROLE_ADMIN:
            security_events.log_event(security_events.EMAIL_TOKEN_REJECTED, user_id=user.id if user else None)
            raise ValidationError(INVALID_RESET_LINK_MESSAGE, code="invalid_token")
        user.password_hash = self._hasher.hash(new_password)
        self._users.save(user)
        self._users.reset_login_failures(user)
        self._users.bump_token_version(user)
        self._tokens.invalidate_unused(user_id=user.id, purpose=TOKEN_RESET_PASSWORD, now=self._clock())
        security_events.log_event(security_events.PASSWORD_RESET, user_id=user.id)
        self.send_password_changed_notice(user)

    def send_password_changed_notice(self, user: User) -> None:
        self._queue_mail(user.email, mail_messages.password_changed_mail(user.display_name))

    # =========================================================================
    # 內部
    # =========================================================================

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
