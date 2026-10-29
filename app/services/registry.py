"""Service 的組裝處：把目前請求的資料庫工作階段接到 Repository，再交給 Service。

端點只呼叫這裡的 `get_*_service()`，不接觸 Repository 或工作階段（見 tests/architecture/test_layering.py）。
命令列指令沒有請求，因此另提供以明確 Session 建構的 `build_*` 函式。
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from sqlalchemy.orm import Session

from app.config import AppConfig
from app.db.session import defer_until_commit, get_session, keep_changes_on_error
from app.repositories.auth_token_repository import AuthTokenRepository
from app.repositories.deletion_tombstone_repository import DeletionTombstoneRepository
from app.repositories.health_repository import HealthRepository
from app.repositories.user_identity_repository import UserIdentityRepository
from app.repositories.user_repository import UserRepository
from app.runtime import (
    current_request_id, get_age_policy, get_clock, get_config, get_identity_provider, get_mail_sender,
)
from app.services.access_policy import AccessPolicy
from app.services.age_policy import DEFAULT_AGE_POLICY, AgePolicy
from app.services.account_retention_service import AccountRetentionService
from app.services.age_stats_service import AgeStatsService
from app.services.auth_service import AccountMailer, AuthService
from app.services.email_flow_service import EmailFlowService
from app.services.google_sign_in_service import GoogleSignInService
from app.services.health_service import HealthService
from app.services.mail_sender import MailSender
from app.services.password_hasher import PasswordHasherService, parameters_for_env


def get_health_service() -> HealthService:
    return HealthService(HealthRepository(get_session()))


def build_auth_service(
    session: Session,
    *,
    config: AppConfig,
    clock: Callable[[], datetime],
    mailer: AccountMailer | None = None,
    persist_failures: Callable[[], None] | None = None,
    age_policy: AgePolicy = DEFAULT_AGE_POLICY,
) -> AuthService:
    return AuthService(
        users=UserRepository(session),
        identities=UserIdentityRepository(session),
        hasher=PasswordHasherService(parameters_for_env(config.env)),
        clock=clock,
        email_verification_required=config.email_verification_required,
        mailer=mailer,
        persist_failures=persist_failures,
        age_policy=age_policy,
    )


def get_google_sign_in_service() -> GoogleSignInService:
    """目前請求的 GoogleSignInService。Google 登入未啟用（沒有身分供應商）時不應被呼叫。"""
    provider = get_identity_provider()
    if provider is None:
        raise RuntimeError("Google 登入未啟用，沒有身分供應商。")
    session = get_session()
    return GoogleSignInService(
        users=UserRepository(session), identities=UserIdentityRepository(session), provider=provider, clock=get_clock(),
        age_policy=get_age_policy(),
    )


def get_access_policy() -> AccessPolicy:
    return AccessPolicy(
        email_verification_required=get_config().email_verification_required,
        age_policy=get_age_policy(),
        clock=get_clock(),
    )


def build_email_flow_service(
    session: Session,
    *,
    config: AppConfig,
    clock: Callable[[], datetime],
    mail_sender: MailSender,
    defer: Callable[[Callable[[], None]], None],
    request_id: Callable[[], str | None] = lambda: None,
) -> EmailFlowService:
    if not config.base_url:
        raise RuntimeError("信箱驗證需要設定對外網址 BASE_URL。")
    return EmailFlowService(
        users=UserRepository(session),
        tokens=AuthTokenRepository(session),
        hasher=PasswordHasherService(parameters_for_env(config.env)),
        clock=clock,
        base_url=config.base_url,
        mail_sender=mail_sender,
        defer=defer,
        request_id=request_id,
    )


def get_email_flow_service() -> EmailFlowService:
    """目前請求的 EmailFlowService（信箱驗證與重設密碼）。信箱驗證關閉時沒有寄信器，不應被呼叫。"""
    sender = get_mail_sender()
    if sender is None:
        raise RuntimeError("信箱驗證未啟用，沒有寄信器。")
    return build_email_flow_service(
        get_session(), config=get_config(), clock=get_clock(), mail_sender=sender,
        defer=defer_until_commit, request_id=current_request_id,
    )


def get_auth_service() -> AuthService:
    """目前請求的 AuthService；信箱驗證開啟時，註冊與變更密碼會經由 EmailFlowService 寄信。"""
    config = get_config()
    mailer = get_email_flow_service() if config.email_verification_required else None
    return build_auth_service(
        get_session(), config=config, clock=get_clock(), mailer=mailer, persist_failures=keep_changes_on_error,
        age_policy=get_age_policy(),
    )


def build_account_retention_service(session: Session, *, clock: Callable[[], datetime]) -> AccountRetentionService:
    """命令列用：每個帳號的「刪除＋刪除紀錄」在同一個交易，立刻提交。"""
    return AccountRetentionService(
        users=UserRepository(session), tombstones=DeletionTombstoneRepository(session), clock=clock, commit=session.commit
    )


def build_age_stats_service(session: Session, *, age_policy: AgePolicy = DEFAULT_AGE_POLICY) -> AgeStatsService:
    return AgeStatsService(users=UserRepository(session), age_policy=age_policy)
