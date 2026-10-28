"""會員驗證測試的共用輔助：假時鐘、記錄用的寄信元件、AuthService 工廠、建立使用者。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.user import STATUS_ACTIVE, User
from app.repositories.user_identity_repository import UserIdentityRepository
from app.repositories.user_repository import UserRepository
from app.services.auth_service import AuthService
from app.services.password_hasher import TESTING_PARAMETERS, PasswordHasherService

FAST_HASHER = PasswordHasherService(TESTING_PARAMETERS)
GOOD_PASSWORD = "correct-horse-battery"
START = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    """可手動前進的時鐘，讓鎖定到期、權杖過期不必真的等待。"""

    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


class RecordingMailer:
    """代替 EmailFlowService：只記錄被要求寄出的信（對象為使用者的內部編號）。"""

    def __init__(self) -> None:
        self.verifications: list[int] = []
        self.notices: list[int] = []

    def send_verification(self, user: User) -> None:
        self.verifications.append(user.id)

    def send_password_changed_notice(self, user: User) -> None:
        self.notices.append(user.id)


def make_auth_service(
    session: Session,
    *,
    verification_required: bool = False,
    clock: FakeClock | None = None,
    mailer: RecordingMailer | None = None,
    hasher: PasswordHasherService | None = None,
) -> AuthService:
    if verification_required and mailer is None:
        mailer = RecordingMailer()
    return AuthService(
        users=UserRepository(session),
        identities=UserIdentityRepository(session),
        hasher=hasher or FAST_HASHER,
        clock=clock or FakeClock(),
        email_verification_required=verification_required,
        mailer=mailer,
    )


def add_user(session: Session, n: int = 1, *, password: str | None = GOOD_PASSWORD, **overrides) -> User:
    """直接寫入一位使用者（不經過 Service）。email 預設為 userN@example.com。"""
    values = dict(
        email=f"user{n}@example.com",
        email_canonical=f"user{n}@example.com",
        display_name=f"使用者{n}",
        status=STATUS_ACTIVE,
        password_hash=FAST_HASHER.hash(password) if password is not None else None,
    )
    values.update(overrides)
    user = User(**values)
    UserRepository(session).add_user(user)
    session.commit()
    return user
