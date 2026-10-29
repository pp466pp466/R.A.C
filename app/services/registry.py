"""Service 的組裝處：把目前請求的資料庫工作階段接到 Repository，再交給 Service。

端點只呼叫這裡的 `get_*_service()`，不接觸 Repository 或工作階段（見 tests/architecture/test_layering.py）。
命令列指令沒有請求，因此另提供以明確 Session 建構的 `build_*` 函式。
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from sqlalchemy.orm import Session

from app.config import AppConfig, LlmSettings
from app.db.session import defer_until_commit, get_session, keep_changes_on_error
from app.llm.types import LLMProvider
from app.repositories.auth_token_repository import AuthTokenRepository
from app.repositories.chat_repository import ChatRepository
from app.repositories.deletion_tombstone_repository import DeletionTombstoneRepository
from app.repositories.health_repository import HealthRepository
from app.repositories.llm_call_log_repository import LlmCallLogRepository
from app.repositories.user_identity_repository import UserIdentityRepository
from app.repositories.user_repository import UserRepository
from app.runtime import (
    current_request_id, get_age_policy, get_clock, get_config, get_identity_provider, get_llm_provider, get_mail_sender,
    get_session_factory,
)
from app.services.access_policy import AccessPolicy
from app.services.age_policy import DEFAULT_AGE_POLICY, AgePolicy
from app.services.account_retention_service import AccountRetentionService
from app.services.age_stats_service import AgeStatsService
from app.services.auth_service import AccountMailer, AuthService
from app.services.chat_service import ChatService
from app.services.cs_prompt import leak_marker
from app.services.email_flow_service import EmailFlowService
from app.services.google_sign_in_service import GoogleSignInService
from app.services.health_service import HealthService
from app.services.llm_service import LlmService
from app.services.mail_sender import MailSender
from app.services.password_hasher import PasswordHasherService, parameters_for_env
from app.services.summary_service import SummaryService


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


def build_llm_service(
    session: Session, *, provider: LLMProvider, settings: LlmSettings, clock: Callable[[], datetime]
) -> LlmService:
    """`release` 是 `session.commit`：每次呼叫模型前結束目前的交易，等待最長 30 秒的期間不佔用連線與鎖。"""
    return LlmService(
        provider=provider, ledger=LlmCallLogRepository(session), settings=settings, clock=clock, release=session.commit
    )


def get_llm_service() -> LlmService | None:
    """目前請求的 LlmService；沒有模型供應商（本機開發沒有金鑰）時為 None。

    只有「需要呼叫模型」的操作才應該在 None 時回 503（`ServiceUnavailable`）；讀取對話、列表等不需要模型的操作照常運作。
    """
    provider = get_llm_provider()
    if provider is None:
        return None
    return build_llm_service(get_session(), provider=provider, settings=get_config().llm, clock=get_clock())


def build_summary_service(
    session: Session, *, provider: LLMProvider, settings: LlmSettings, clock: Callable[[], datetime], marker: str
) -> SummaryService:
    """背景摘要用：呼叫者提供自己的 Session（不是請求的）。"""
    return SummaryService(
        chats=ChatRepository(session),
        llm=build_llm_service(session, provider=provider, settings=settings, clock=clock),
        summary_model=settings.summary_model, commit=session.commit, marker=marker,
    )


def _summary_registrar(marker: str) -> Callable[[int, str], None]:
    """回覆提交後登記摘要折疊：動作在回應送出後以「自己的 Session」執行（請求的 Session 那時已關閉）。

    執行需要的東西（工廠、供應商、設定、時鐘、標記）都在登記時就綁好，動作裡不再碰 `g`、`current_app` 與請求的 Session。
    """
    factory, provider, settings, clock = get_session_factory(), get_llm_provider(), get_config().llm, get_clock()

    def register(user_id: int, public_id: str) -> None:
        def run() -> None:
            with factory() as session:
                build_summary_service(session, provider=provider, settings=settings, clock=clock, marker=marker).compact(
                    user_id, public_id
                )

        defer_until_commit(run)

    return register


def get_chat_service() -> ChatService:
    """目前請求的 ChatService。沒有模型供應商時 `llm` 為空：讀取與列表照常，送出訊息回 503。"""
    session = get_session()
    marker = leak_marker(get_config().secret_key.reveal())
    llm = get_llm_service()
    return ChatService(
        chats=ChatRepository(session), llm=llm, policy=get_access_policy(), clock=get_clock(),
        commit=session.commit, rollback=session.rollback, marker=marker,
        after_reply=_summary_registrar(marker) if llm is not None else None,
    )


def build_account_retention_service(session: Session, *, clock: Callable[[], datetime]) -> AccountRetentionService:
    """命令列用：每個帳號的「刪除＋刪除紀錄」在同一個交易，立刻提交。"""
    return AccountRetentionService(
        users=UserRepository(session), tombstones=DeletionTombstoneRepository(session), clock=clock, commit=session.commit
    )


def build_age_stats_service(session: Session, *, age_policy: AgePolicy = DEFAULT_AGE_POLICY) -> AgeStatsService:
    return AgeStatsService(users=UserRepository(session), age_policy=age_policy)
