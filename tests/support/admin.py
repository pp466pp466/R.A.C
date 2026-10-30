"""後台測試共用的組裝：真的 Repository、真的 `AuthService.confirm_password`、真的 `UsageService`，假的只有時鐘。

用真的密碼確認，是為了讓「密碼錯誤計入失敗、鎖定、保存稽核」這些跨元件的行為被實際走過，而不是被假物件掩蓋。
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from app.config import UsageSettings
from app.repositories.admin_audit_repository import AdminAuditRepository
from app.repositories.admin_repository import AdminRepository
from app.repositories.chat_repository import ChatRepository
from app.repositories.llm_call_log_repository import LlmCallLogRepository
from app.repositories.user_identity_repository import UserIdentityRepository
from app.repositories.user_repository import UserRepository
from app.services.access_policy import AccessPolicy
from app.services.admin_service import AdminService
from app.services.age_stats_service import AgeStatsService
from app.services.auth_service import AuthService
from app.services.current_user import CurrentUser
from app.services.usage_service import UsageService
from tests.support.auth import FAST_HASHER, GOOD_PASSWORD, FakeClock, add_user

STATE = {"member_tier": "free", "last_intent": None, "pending_entities": {}, "open_issue": None}
WRONG_PASSWORD = "definitely-not-the-password"


class Persisted:
    """記錄「這個請求即使以 4xx 結束也要保留變更」被要求了幾次。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


class AdminKit:
    """一個後台 Service 與它周邊的東西。"""

    def __init__(self, session: Session, *, verification: bool = False, settings: UsageSettings | None = None) -> None:
        self.session = session
        self.clock = FakeClock()
        self.persisted = Persisted()
        self.settings = settings or UsageSettings()
        self.audit = AdminAuditRepository(session)
        self.admins = AdminRepository(session)
        self.chats = ChatRepository(session)
        self.ledger = LlmCallLogRepository(session)
        self.auth = AuthService(
            users=UserRepository(session), identities=UserIdentityRepository(session), hasher=FAST_HASHER, clock=self.clock,
            email_verification_required=verification, persist_failures=self.persisted,
        )
        usage = UsageService(
            ledger=self.ledger, chats=self.chats, settings=self.settings, clock=self.clock, processing_ttl=timedelta(seconds=60)
        )
        self.service = AdminService(
            policy=AccessPolicy(email_verification_required=verification, clock=self.clock), admins=self.admins, audit=self.audit,
            usage=usage, age_stats=AgeStatsService(users=UserRepository(session)), usage_settings=self.settings,
            email_verification_required=verification, confirm_password=self.auth.confirm_password,
            persist_on_error=self.persisted, clock=self.clock,
        )
        self._n = 100

    # ---- 人 -------------------------------------------------------------------------------

    def admin(self, n: int = 1) -> CurrentUser:
        return CurrentUser.from_model(add_user(self.session, n, role="admin", age=30))

    def member(self, n: int = 2, **overrides):
        """回傳 ORM 的 User（測試要讀它的狀態）。"""
        return add_user(self.session, n, age=30, **overrides)

    def current(self, user) -> CurrentUser:
        self.session.expire_all()
        return CurrentUser.from_model(self.session.get(type(user), user.id))

    def reload(self, user):
        self.session.expire_all()
        return self.session.get(type(user), user.id)

    # ---- 資料 -----------------------------------------------------------------------------

    def conversation(self, user, *, turns=(), at=None):
        """`turns` 是助理回覆的意圖（None 代表降級回覆）。"""
        at = at or self.clock()
        self._n += 1
        conv = self.chats.create_conversation(user.id, client_id=f"{self._n:036d}", state=STATE, now=at)
        for index, intent in enumerate(turns):
            self._n += 1
            self.chats.append_message(user.id, conv, client_msg_id=f"{self._n:036d}", role="user", content=f"使用者說的話{index}", now=at)
            self._n += 1
            self.chats.append_message(
                user.id, conv, client_msg_id=f"{self._n:036d}", role="assistant", content=f"客服的回覆{index}", intent=intent,
                msg_status="ok" if intent else "fallback", now=at,
            )
        self.session.commit()
        return conv

    def block(self, user, conversation, *, at=None, kind="chat"):
        self.ledger.add(
            user_id=user.id, conversation_id=None if conversation is None else conversation.id, kind=kind, provider="f",
            model="m", attempt=1, status="blocked", counted=False, latency_ms=1, created_at=at or self.clock(),
        )
        self.session.commit()

    def call(self, *, at=None, status="ok", kind="chat", counted=False, user=None, model="m", **extra):
        self.ledger.add(
            user_id=None if user is None else user.id, conversation_id=None, kind=kind, provider="f", model=model, attempt=1,
            status=status, counted=counted, latency_ms=1, created_at=at or self.clock(), **extra,
        )
        self.session.commit()

    def audit_rows(self):
        return self.audit.list(limit=100, offset=0)


def wrong_password() -> str:
    return WRONG_PASSWORD


__all__ = ["AdminKit", "GOOD_PASSWORD", "WRONG_PASSWORD", "wrong_password"]
