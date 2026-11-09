"""Service 的組裝處：把目前請求的資料庫工作階段接到 Repository，再交給 Service。

端點只呼叫這裡的 `get_*_service()`，不接觸 Repository 或工作階段（見 tests/architecture/test_layering.py）。
命令列指令沒有請求，因此另提供以明確 Session 建構的 `build_*` 函式。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from sqlalchemy.orm import Session

from app.config import AppConfig, LlmSettings, UsageSettings
from app.db.session import defer_until_commit, get_session, keep_changes_on_error
from app.llm.types import LLMProvider
from app.repositories.admin_audit_repository import AdminAuditRepository
from app.repositories.admin_repository import AdminRepository
from app.repositories.auth_token_repository import AuthTokenRepository
from app.repositories.chat_repository import ChatRepository
from app.repositories.deletion_tombstone_repository import DeletionTombstoneRepository
from app.repositories.health_repository import HealthRepository
from app.repositories.llm_call_log_repository import LlmCallLogRepository
from app.repositories.lock_appeal_repository import LockAppealRepository
from app.repositories.lock_strike_repository import LockStrikeRepository
from app.repositories.alert_state_repository import AlertStateRepository
from app.repositories.retention_repository import RetentionRepository
from app.repositories.runtime_limit_repository import RuntimeLimitRepository
from app.repositories.script_repository import ScriptRepository
from app.repositories.script_submission_repository import ScriptSubmissionRepository
from app.repositories.session_repository import SessionRepository
from app.repositories.user_identity_repository import UserIdentityRepository
from app.repositories.user_repository import UserRepository
from app.repositories.wallet_repository import WalletRepository
from app.observability import wallet_events
from app.runtime import (
    current_request_id, get_age_policy, get_clock, get_config, get_identity_provider, get_llm_provider, get_mail_sender,
    get_rpg_llm_provider, get_session_factory,
)
from app.services.access_policy import AccessPolicy
from app.services.admin_service import AdminService
from app.services.age_policy import DEFAULT_AGE_POLICY, AgePolicy
from app.services.account_deletion_executor import AccountDeletionExecutor
from app.services.account_deletion_service import AccountDeletionService
from app.services.account_erasure_service import AccountErasureService
from app.services.account_lock_service import AccountLockService
from app.services.deletion_replay_service import DeletionReplayService
from app.services.account_retention_service import AccountRetentionService
from app.services.alert_service import AlertService
from app.services.appeal_service import AppealService
from app.services.data_retention_service import DataRetentionService
from app.services.personal_export_service import PersonalExportService
from app.services.age_stats_service import AgeStatsService
from app.services.content_mode_service import ContentModeService
from app.services.auth_service import AccountMailer, AuthService
from app.services.chat_service import PROCESSING_STALE_AFTER, ChatService
from app.services.cs_prompt import leak_marker
from app.services.email_flow_service import EmailFlowService
from app.services.google_sign_in_service import GoogleSignInService
from app.services.health_service import HealthService
from app.services.nickname_service import NicknameService
from app.services.llm_service import LlmService
from app.services.mail_sender import MailSender
from app.services.password_hasher import PasswordHasherService, parameters_for_env
from app.services.runtime_limits import RuntimeLimits
from app.services.script_authoring_service import ScriptAuthoringService
from app.services.script_submission_service import ScriptSubmissionService
from app.services.script_review_prompt import review_marker
from app.services.script_review_service import ScriptReviewService
from app.services.script_seed_service import SEED_DIRECTORY, ScriptSeedService
from app.services.protagonist_prompt import protagonist_marker
from app.services.protagonist_review_service import ProtagonistReviewService
from app.services.protagonist_service import ProtagonistService
from app.services.wallet_service import WalletService
from app.services.session_service import SessionService
from app.services.startable import StartableVersions
from app.services.summary_service import SummaryService
from app.services.usage_service import UsageService


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
    users = UserRepository(session)
    return AuthService(
        users=users,
        identities=UserIdentityRepository(session),
        hasher=PasswordHasherService(parameters_for_env(config.env)),
        clock=clock,
        mailer=mailer,
        persist_failures=persist_failures,
        age_policy=age_policy,
        erasure=AccountErasureService(
            users=users, tombstones=DeletionTombstoneRepository(session), commit=session.commit, rollback=session.rollback
        ),
        lock=build_account_lock_service(session, clock=clock, persist=persist_failures),
        on_age_verified=_grant_signup_coins_on_age_verified(session, clock),
    )


def build_wallet_service(session: Session, *, clock: Callable[[], datetime]) -> WalletService:
    """遊戲幣錢包（餘額與帳本只由它的 Repository 寫入；數值每次現讀自設定表）。"""
    return WalletService(wallets=WalletRepository(session), limits=build_runtime_limits(session, clock=clock), clock=clock)


def get_wallet_service() -> WalletService:
    return build_wallet_service(get_session(), clock=get_clock())


def _grant_signup_coins_on_age_verified(session: Session, clock: Callable[[], datetime]):
    """年齡確認成功之後發放首次註冊贈幣的回呼。包在 SAVEPOINT 內：失敗只復原這一筆，年齡確認照常完成；
    贈幣會在玩家第一次存取錢包時補發（冪等鍵相同，不會重複）。"""
    wallet = build_wallet_service(session, clock=clock)

    def grant(current) -> None:
        try:
            with session.begin_nested():
                wallet.ensure_signup_grant(current)
        except Exception:                                                  # noqa: BLE001 — 不影響年齡確認，事件只含內部編號
            wallet_events.log_grant_deferred(user_id=current.id)

    return grant


def build_account_lock_service(
    session: Session, *, clock: Callable[[], datetime], persist: Callable[[], None] | None = None
) -> AccountLockService:
    """鎖定計次與自動鎖定；稽核（管理員欄位為空）以注入的函式寫入，所以這個 Service 不直接依賴後台專用的 Repository。"""
    return AccountLockService(
        users=UserRepository(session), strikes=LockStrikeRepository(session), audit=_system_audit(session, clock), clock=clock,
        persist=persist or (lambda: None),
    )


def _system_audit(session: Session, clock: Callable[[], datetime]):
    """系統或玩家觸發的稽核寫入（管理員欄位為空）：鎖定與申訴提交。傳給 Service 的是函式，不是後台專用的 Repository。"""
    audits = AdminAuditRepository(session)

    def write_audit(action: str, target_user_public_id: str | None, detail: Mapping[str, Any]) -> None:
        audits.add(admin_user_id=None, action=action, now=clock(), target_user_public_id=target_user_public_id, detail=detail)

    return write_audit


def get_appeal_service() -> AppealService:
    session, clock = get_session(), get_clock()
    return AppealService(
        users=UserRepository(session), appeals=LockAppealRepository(session), audit=_system_audit(session, clock), clock=clock,
    )


def get_account_lock_service() -> AccountLockService:
    return build_account_lock_service(get_session(), clock=get_clock(), persist=keep_changes_on_error)


def get_google_sign_in_service() -> GoogleSignInService:
    """目前請求的 GoogleSignInService。Google 登入未啟用（沒有身分供應商）時不應被呼叫。"""
    provider = get_identity_provider()
    if provider is None:
        raise RuntimeError("Google 登入未啟用，沒有身分供應商。")
    session = get_session()
    clock = get_clock()
    return GoogleSignInService(
        users=UserRepository(session), identities=UserIdentityRepository(session), provider=provider, clock=clock,
        lock=build_account_lock_service(session, clock=clock, persist=keep_changes_on_error),
    )


def get_access_policy() -> AccessPolicy:
    config = get_config()
    return AccessPolicy(
        age_policy=get_age_policy(),
        clock=get_clock(),
        # 成人模式的部署開關只能以環境設定開啟（預設關閉）；啟動時已驗證過條件（見 app/config.py）
        adult_mode_enabled=config.content.adult_mode_enabled,
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
        raise RuntimeError("管理員的重設密碼需要設定對外網址 BASE_URL。")
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
    """目前請求的 EmailFlowService（管理員的重設密碼）。沒有寄信設定時沒有寄信器，不應被呼叫。"""
    sender = get_mail_sender()
    if sender is None:
        raise RuntimeError("沒有寄信設定，沒有寄信器。")
    return build_email_flow_service(
        get_session(), config=get_config(), clock=get_clock(), mail_sender=sender,
        defer=defer_until_commit, request_id=current_request_id,
    )


def admin_password_reset_available() -> bool:
    """管理員的「忘記密碼」（寄重設信）是否可用：只需要寄信設定（寄信器與對外網址）。"""
    return get_mail_sender() is not None and bool(get_config().base_url)


def get_auth_service() -> AuthService:
    """目前請求的 AuthService；有寄信設定時，變更密碼會經由 EmailFlowService 寄出通知信。"""
    config = get_config()
    mailer = get_email_flow_service() if admin_password_reset_available() else None
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


def get_rpg_llm_service() -> LlmService | None:
    """RPG 敘事用途的 LlmService；沒有設定這個用途的供應商時為 None（只有 RPG 的功能回 503，客服完全不受影響）。

    與 `get_llm_service()` 是同一個流程（預算、備援、斷路、帳本），只是供應商與模型設定不同。帳本與斷路都以模型名稱為鍵，
    兩個用途的模型名稱不同，所以彼此的失敗不會互相影響。
    """
    provider = get_rpg_llm_provider()
    if provider is None:
        return None
    config = get_config()
    settings = config.rpg_llm.llm if config.rpg_llm else config.llm        # 沒有設定卻注入了假供應商（測試）時沿用客服的設定
    return build_llm_service(get_session(), provider=provider, settings=settings, clock=get_clock())


def build_usage_service(
    session: Session, *, settings: UsageSettings, clock: Callable[[], datetime], lock: AccountLockService | None = None
) -> UsageService:
    """用量檢查：帳本與訊息表的彙總都用同一個 Session（讀取為主，不寫入任何資料）。"""
    return UsageService(
        ledger=LlmCallLogRepository(session), chats=ChatRepository(session), settings=settings, clock=clock,
        processing_ttl=PROCESSING_STALE_AFTER, lock=lock,
    )


def get_usage_service() -> UsageService:
    session, clock = get_session(), get_clock()
    return build_usage_service(
        session, settings=get_config().usage, clock=clock,
        lock=build_account_lock_service(session, clock=clock, persist=keep_changes_on_error),
    )


def build_summary_service(
    session: Session, *, provider: LLMProvider, settings: LlmSettings, clock: Callable[[], datetime], marker: str,
    usage: UsageSettings | None = None,
) -> SummaryService:
    """背景摘要用：呼叫者提供自己的 Session（不是請求的）。給了 `usage` 就在全站用量達上限時延後摘要。"""
    budget = build_usage_service(session, settings=usage, clock=clock).global_budget_exhausted if usage is not None else None
    return SummaryService(
        chats=ChatRepository(session),
        llm=build_llm_service(session, provider=provider, settings=settings, clock=clock),
        summary_model=settings.summary_model, commit=session.commit, marker=marker, budget_exhausted=budget,
    )


def _summary_registrar(marker: str) -> Callable[[int, str], None]:
    """回覆提交後登記摘要折疊：動作在回應送出後以「自己的 Session」執行（請求的 Session 那時已關閉）。

    執行需要的東西（工廠、供應商、設定、時鐘、標記）都在登記時就綁好，動作裡不再碰 `g`、`current_app` 與請求的 Session。
    """
    factory, provider, settings, clock = get_session_factory(), get_llm_provider(), get_config().llm, get_clock()
    usage = get_config().usage

    def register(user_id: int, public_id: str) -> None:
        def run() -> None:
            with factory() as session:
                build_summary_service(
                    session, provider=provider, settings=settings, clock=clock, marker=marker, usage=usage
                ).compact(user_id, public_id)

        defer_until_commit(run)

    return register


def get_chat_service() -> ChatService:
    """目前請求的 ChatService。沒有模型供應商時 `llm` 為空：讀取與列表照常，送出訊息回 503。"""
    session = get_session()
    marker = leak_marker(get_config().secret_key.reveal())
    llm = get_llm_service()
    return ChatService(
        chats=ChatRepository(session), llm=llm, policy=get_access_policy(), usage=get_usage_service(), clock=get_clock(),
        commit=session.commit, rollback=session.rollback, marker=marker,
        after_reply=_summary_registrar(marker) if llm is not None else None,
    )


def get_admin_service() -> AdminService:
    """目前請求的後台 Service。後台專用的 Repository 只在這裡組裝（見 tests/architecture/test_admin_boundaries.py）。

    重新輸入密碼用 `AuthService.confirm_password`（與登入共用失敗計數與鎖定）；`persist_on_error` 讓 4xx 的回應仍保留
    「密碼失敗的計數」與「`reauth.failed` 稽核」，與登入失敗同一個機制。
    """
    session = get_session()
    config = get_config()
    return AdminService(
        policy=get_access_policy(), admins=AdminRepository(session), audit=AdminAuditRepository(session),
        usage=get_usage_service(), age_stats=build_age_stats_service(session, age_policy=get_age_policy()),
        usage_settings=config.usage,
        confirm_password=get_auth_service().confirm_password, persist_on_error=keep_changes_on_error, clock=get_clock(),
        wallet=get_wallet_service(),
    )


def get_account_deletion_service() -> AccountDeletionService:
    """目前請求的自助刪除帳號服務（玩家端不寄任何信，包含申請刪除的通知）。"""
    return AccountDeletionService(
        users=UserRepository(get_session()), confirm_password=get_auth_service().confirm_password, clock=get_clock(),
    )


def get_content_mode_service() -> ContentModeService:
    """目前請求的內容模式服務：只讀寫呼叫者本人的偏好與成年確認紀錄。"""
    return ContentModeService(users=UserRepository(get_session()), policy=get_access_policy(), clock=get_clock())


def get_script_authoring_service() -> ScriptAuthoringService:
    """目前請求的創作服務：只讀寫呼叫者本人的劇本與版本（Repository 的方法都以 `user_id` 為第一個參數）。"""
    session, clock = get_session(), get_clock()
    return ScriptAuthoringService(
        scripts=ScriptRepository(session), users=UserRepository(session), submissions=ScriptSubmissionRepository(session),
        policy=get_access_policy(), limits=get_runtime_limits(), clock=clock,
    )


def get_script_submission_service() -> ScriptSubmissionService:
    """目前請求的送審服務：自動拒絕與暫停發佈的寫入要在 4xx 之後保留（`persist`），稽核由系統寫入（管理員欄位為空）。"""
    session, clock = get_session(), get_clock()
    return ScriptSubmissionService(
        scripts=ScriptRepository(session), users=UserRepository(session), submissions=ScriptSubmissionRepository(session),
        policy=get_access_policy(), limits=get_runtime_limits(), reviews=get_script_review_service(),
        authoring=get_script_authoring_service(), audit=_system_audit(session, clock), clock=clock, persist=keep_changes_on_error,
    )


def get_nickname_service() -> NicknameService:
    """目前請求的暱稱服務：只讀寫呼叫者本人的暱稱與短代號。"""
    return NicknameService(users=UserRepository(get_session()), policy=get_access_policy())


def get_script_review_service() -> ScriptReviewService:
    """目前請求的劇本審查（模型層）：RPG 用途的 `LlmService`（沒有設定供應商時為 None，審查一律回報不可用）與全站用量上限的檢查。"""
    session, config, clock = get_session(), get_config(), get_clock()
    usage = build_usage_service(session, settings=config.usage, clock=clock)
    return ScriptReviewService(
        scripts=ScriptRepository(session), llm=get_rpg_llm_service(), marker=review_marker(config.secret_key.reveal()),
        budget_exhausted=usage.global_budget_exhausted, clock=clock,
    )


def get_protagonist_review_service() -> ProtagonistReviewService:
    """目前請求的主角審查（模型層）：RPG 用途的 `LlmService`（沒有設定供應商時為 None，審查一律回報不可用）與全站用量上限的檢查。"""
    session, config, clock = get_session(), get_config(), get_clock()
    usage = build_usage_service(session, settings=config.usage, clock=clock)
    return ProtagonistReviewService(
        llm=get_rpg_llm_service(), marker=protagonist_marker(config.secret_key.reveal()), budget_exhausted=usage.global_budget_exhausted,
    )


def get_session_service() -> SessionService:
    """目前請求的存檔服務：開存檔（含主角的驗證與兩層審查）與開始回合（兩者都只處理呼叫者本人的資料）。"""
    session, config = get_session(), get_config()
    return SessionService(
        scripts=ScriptRepository(session), sessions=SessionRepository(session), policy=get_access_policy(),
        reviews=get_script_review_service(), protagonist_reviews=get_protagonist_review_service(),
        protagonist_marker=protagonist_marker(config.secret_key.reveal()), clock=get_clock(),
    )


def get_protagonist_service() -> ProtagonistService:
    """目前請求的主角服務：創角所需的資料與 AI 生成預覽（無狀態；唯一的寫入是用量帳本）。"""
    session, config, clock = get_session(), get_config(), get_clock()
    policy = get_access_policy()
    usage = build_usage_service(session, settings=config.usage, clock=clock)
    return ProtagonistService(
        startable=StartableVersions(scripts=ScriptRepository(session), policy=policy), policy=policy, limits=get_runtime_limits(),
        ledger=LlmCallLogRepository(session), llm=get_rpg_llm_service(), marker=protagonist_marker(config.secret_key.reveal()),
        budget_exhausted=usage.global_budget_exhausted, clock=clock,
    )


def get_personal_export_service() -> PersonalExportService:
    """目前請求的匯出服務：只讀取呼叫者本人的帳號與對話（Repository 的方法都以 `user_id` 為第一個參數）。"""
    session = get_session()
    return PersonalExportService(
        users=UserRepository(session), identities=UserIdentityRepository(session), chats=ChatRepository(session),
        appeals=LockAppealRepository(session), scripts=ScriptRepository(session), sessions=SessionRepository(session),
        submissions=ScriptSubmissionRepository(session), clock=get_clock(), wallets=WalletRepository(session),
    )


def build_alert_service(
    session: Session, *, config: AppConfig, clock: Callable[[], datetime], mail_sender: MailSender | None
) -> AlertService:
    """命令列用：每項告警的狀態搶佔與還原都立刻提交；沒有設定收件人時只評估、不寄信。"""
    return AlertService(
        states=AlertStateRepository(session), ledger=LlmCallLogRepository(session), users=UserRepository(session),
        usage_settings=config.usage, recipients=config.alerts.recipients, mail_sender=mail_sender,
        clock=clock, commit=session.commit, rollback=session.rollback,
    )


def build_data_retention_service(session: Session, *, clock: Callable[[], datetime]) -> DataRetentionService:
    """命令列用：每一類、每一批各自提交；某一類失敗時回復該批並繼續其餘類別。"""
    return DataRetentionService(
        repository=RetentionRepository(session), clock=clock, commit=session.commit, rollback=session.rollback
    )


def build_account_deletion_executor(session: Session, *, clock: Callable[[], datetime]) -> AccountDeletionExecutor:
    """命令列用：寬限期已滿的帳號逐一抹除；每個帳號一個交易，條件不成立時整個回復。"""
    users = UserRepository(session)
    erasure = AccountErasureService(
        users=users, tombstones=DeletionTombstoneRepository(session), commit=session.commit, rollback=session.rollback
    )
    return AccountDeletionExecutor(users=users, erasure=erasure, clock=clock)


def build_deletion_replay_service(session: Session) -> DeletionReplayService:
    """命令列用：備份還原後依刪除紀錄重新抹除；走同一個單一抹除出口，每個帳號一個交易。"""
    users = UserRepository(session)
    tombstones = DeletionTombstoneRepository(session)
    erasure = AccountErasureService(users=users, tombstones=tombstones, commit=session.commit, rollback=session.rollback)
    return DeletionReplayService(users=users, tombstones=tombstones, erasure=erasure)


def build_account_retention_service(session: Session, *, clock: Callable[[], datetime]) -> AccountRetentionService:
    """命令列用：每個帳號的「清帳本對話參照＋刪除＋刪除紀錄」在同一個交易，立刻提交；條件不成立時整個回復。"""
    return AccountRetentionService(
        users=UserRepository(session), tombstones=DeletionTombstoneRepository(session), clock=clock,
        commit=session.commit, rollback=session.rollback,
    )


def build_script_seed_service(
    session: Session, *, clock: Callable[[], datetime], directory: Path | None = None, forbidden_markers: tuple[str, ...] = (),
) -> ScriptSeedService:
    """命令列用：內建劇本全部驗證通過才在單一交易內寫入（全有或全無），失敗時整批回復。"""
    return ScriptSeedService(
        scripts=ScriptRepository(session), clock=clock, commit=session.commit, rollback=session.rollback,
        directory=directory if directory is not None else SEED_DIRECTORY, forbidden_markers=forbidden_markers,
    )


def build_runtime_limits(session: Session, *, clock: Callable[[], datetime]) -> RuntimeLimits:
    """營運限制（每次使用時現讀，不快取）；稽核（管理員欄位為空）以注入的函式寫入，只有命令列的修改用得到。"""
    return RuntimeLimits(limits=RuntimeLimitRepository(session), clock=clock, audit=_system_audit(session, clock))


def get_runtime_limits() -> RuntimeLimits:
    return build_runtime_limits(get_session(), clock=get_clock())


def build_age_stats_service(session: Session, *, age_policy: AgePolicy = DEFAULT_AGE_POLICY) -> AgeStatsService:
    return AgeStatsService(users=UserRepository(session), age_policy=age_policy)
