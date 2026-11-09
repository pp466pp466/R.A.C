"""會員驗證測試的共用輔助：假時鐘、記錄用的寄信元件、AuthService 工廠、建立使用者。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.user import STATUS_ACTIVE, User
from app.repositories.deletion_tombstone_repository import DeletionTombstoneRepository
from app.repositories.user_identity_repository import UserIdentityRepository
from app.repositories.user_repository import UserRepository
from app.services.account_erasure_service import AccountErasureService
from app.services.age_policy import DEFAULT_AGE_POLICY, AgePolicy
from app.services.auth_service import AuthService
from app.services.registry import build_account_lock_service
from app.services.password_hasher import TESTING_PARAMETERS, PasswordHasherService
from tests.support.age import age_columns

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
    """代替 EmailFlowService：只記錄被要求寄出的信（對象為使用者的內部編號）。玩家端不寄信，只剩管理員的密碼已變更通知。"""

    def __init__(self) -> None:
        self.notices: list[int] = []

    def send_password_changed_notice(self, user: User) -> None:
        self.notices.append(user.id)


def make_auth_service(
    session: Session,
    *,
    clock: FakeClock | None = None,
    mailer: RecordingMailer | None = None,
    hasher: PasswordHasherService | None = None,
    age_policy: AgePolicy = DEFAULT_AGE_POLICY,
    on_age_verified=None,
) -> AuthService:
    clock = clock or FakeClock()
    users = UserRepository(session)
    return AuthService(
        users=users,
        identities=UserIdentityRepository(session),
        hasher=hasher or FAST_HASHER,
        clock=clock,
        mailer=mailer,
        age_policy=age_policy,
        # 與正式組裝相同的協作者：補填年齡時的抹除與鎖定計次
        erasure=AccountErasureService(
            users=users, tombstones=DeletionTombstoneRepository(session), commit=session.commit, rollback=session.rollback
        ),
        lock=build_account_lock_service(session, clock=clock),
        on_age_verified=on_age_verified,
    )


def sign_in_as(client, user_id: int, token_version: int = 1) -> None:
    """直接寫入登入 cookie（不經過任何登入流程）。管理員不能只靠密碼登入（要先 Google 再密碼），所以管理員的測試用這個。"""
    with client.session_transaction() as session:
        session["uid"], session["tv"] = user_id, token_version


DEFAULT_ADULT_AGE = 30
_UNSET = object()


def add_user(
    session: Session, n: int = 1, *, password: str | None = GOOD_PASSWORD, age: int | None | object = _UNSET, **overrides
) -> User:
    """直接寫入一位使用者（不經過 Service）。email 預設為 userN@example.com。

    預設**已完成年齡確認、剛好 30 歲**（google-first-login 之後，待確認年齡的玩家除了年齡確認與登出什麼都不能做，
    所以幾乎所有測試都需要一位已確認的使用者）；傳入 `age=N` 代表已完成、且剛好 N 歲；
    明確傳入 `age=None` 才是「尚未完成年齡確認」（與管理員指令建立的帳號、剛以 Google 建立的帳號相同）。
    """
    if age is _UNSET:
        age = DEFAULT_ADULT_AGE
    values = dict(
        email=f"user{n}@example.com",
        email_canonical=f"user{n}@example.com",
        display_name=f"使用者{n}",
        status=STATUS_ACTIVE,
        password_hash=FAST_HASHER.hash(password) if password is not None else None,
    )
    if age is not None:
        values.update(age_columns(age))
    values.update(overrides)
    user = User(**values)
    UserRepository(session).add_user(user)
    session.commit()
    return user
