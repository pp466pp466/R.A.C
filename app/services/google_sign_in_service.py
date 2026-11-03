"""Google 登入的帳號對應與合併規則（見 AGENTS.md 第 10 節與 google-sign-in 規格）。

流程分兩段：
1. `begin()`：產生 state、nonce、PKCE 驗證碼並組出授權網址；回傳要綁定在瀏覽器上的交易內容（由 Controller 放進
   簽章的短效 cookie，10 分鐘有效、回呼結束一律刪除）。
2. `complete()`：驗證 state 與交易綁定一致 → 向 Google 換取並驗證 ID token → 對應帳號。

帳號對應（`_resolve`）：
- Google 身分（`sub`）已綁定 → 直接登入該帳號，不因 Google 端信箱變更而重新比對。
- 否則以信箱的重複判斷正規形式（gmail 去點與標籤）尋找本機帳號：
  - 信箱已驗證 → 綁定，原有密碼仍可使用；
  - 信箱未驗證（含信箱驗證關閉期間註冊的帳號）→ **清除其密碼憑證**、視為已驗證、`token_version` 加 1
    （撤銷任何以該密碼建立的登入狀態），再綁定。這是防止「預先佔用信箱」的帳號劫持；
  - 沒有帳號 → 建立新帳號（已驗證、沒有密碼），需要發起時已勾選同意條款；**不要求生日與年齡區間**：
    帳號建立時沒有年齡資料，並記錄 24 小時的補填期限（`age_deadline_at`），登入後只能到年齡確認頁補填
    （`AuthService.verify_age`，逾時由 `purge-unconfirmed-accounts` 刪除）。
- 該帳號已綁定另一個 Google 身分、或停用中 → 拒絕，不新增綁定。
- 已存在的帳號（已綁定、或以信箱合併）登入時不需要勾選條款，也**不會**被順便標記為已確認年齡：
  補確認只能在年齡確認頁明確完成。
所有拒絕都以固定的錯誤代碼表示（Controller 轉成 `/login?error=<代碼>`），不夾帶任何外部提供的文字。
"""
from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from app.models.lock_strike import STRIKE_IDENTITY_REJECTED
from app.models.user import (
    AGE_CONFIRMATION_DEADLINE, PROVIDER_GOOGLE, STATUS_ACTIVE, STATUS_DELETION_PENDING,
    STATUS_SUSPENDED, User, UserIdentity,
)
from app.observability import security_events
from app.repositories.errors import DuplicateIdentityError, DuplicateUserError
from app.repositories.user_identity_repository import UserIdentityRepository
from app.repositories.user_repository import UserRepository
from app.services.account_lock_service import AccountLockService
from app.services.current_user import CurrentUser
from app.services.google_oidc import (
    GoogleAuthError, GoogleIdentityProvider, GoogleProfile, code_challenge_for, new_code_verifier, new_state_or_nonce,
)
from app.services.input_cleaning import clean_single_line
from app.services.legal import CURRENT_TERMS_VERSION
from app.services.normalization import canonical_email, is_valid_email, normalize_email

MAX_DISPLAY_NAME_LENGTH = 50
_DISABLED = (STATUS_SUSPENDED,)

# 回到 /login?error=<代碼> 的固定代碼
ERROR_STATE = "google_state"
ERROR_UNAVAILABLE = "google_unavailable"
ERROR_REJECTED = "google_rejected"
ERROR_TERMS = "terms_required"
ERROR_DISABLED = "account_disabled"

OUTCOME_LOGIN = "login"
OUTCOME_REGISTERED = "registered"
OUTCOME_LINKED = "linked"
OUTCOME_LINKED_UNVERIFIED = "linked_unverified"


class GoogleLoginRefused(Exception):
    """Google 登入被拒絕。`code` 是固定的錯誤代碼（見上方 ERROR_*）。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class GoogleStart:
    authorization_url: str
    transaction: dict[str, Any]       # 由 Controller 放進簽章 cookie；包含 PKCE 驗證碼，不可傳給前端


@dataclass(frozen=True)
class GoogleLoginResult:
    user: CurrentUser
    outcome: str


class GoogleSignInService:
    def __init__(
        self,
        *,
        users: UserRepository,
        identities: UserIdentityRepository,
        provider: GoogleIdentityProvider,
        clock: Callable[[], datetime],
        lock: AccountLockService | None = None,
    ) -> None:
        self._lock = lock                # 已辨識出帳號卻被拒絕時計入帳號鎖定的計次（沒有帳號的身分與無法辨識的失敗不計）
        self._users = users
        self._identities = identities
        self._provider = provider
        self._clock = clock

    # ---- 發起 ----------------------------------------------------------------

    def begin(self, *, accept_terms: object) -> GoogleStart:
        """發起只收條款旗標 `t`（只有明確為 `True` 才成立）；回訪的使用者不需要任何欄位，空的請求也可以。

        生日與年齡區間不在這裡處理：新帳號建立之後才在年齡確認頁補填。交易 cookie（簽章、10 分鐘有效、
        回呼結束即刪除）只放 state、nonce、PKCE 驗證碼與條款旗標。
        """
        state, nonce, verifier = new_state_or_nonce(), new_state_or_nonce(), new_code_verifier()
        url = self._provider.build_authorization_url(
            state=state, nonce=nonce, code_challenge=code_challenge_for(verifier)
        )
        security_events.log_event(security_events.GOOGLE_STARTED)
        transaction: dict[str, Any] = {"s": state, "n": nonce, "v": verifier, "t": accept_terms is True}
        return GoogleStart(authorization_url=url, transaction=transaction)

    # ---- 回呼 ----------------------------------------------------------------

    def complete(
        self,
        *,
        code: object,
        state: object,
        provider_error: object,
        transaction: dict[str, Any] | None,
    ) -> GoogleLoginResult:
        self._check_state(state, transaction)
        assert transaction is not None
        if provider_error:                              # 使用者在 Google 按了取消，或 Google 回報錯誤
            self._refuse(ERROR_REJECTED, security_events.GOOGLE_REJECTED_TOKEN)
        if not isinstance(code, str) or not code:
            self._refuse(ERROR_REJECTED, security_events.GOOGLE_REJECTED_TOKEN)
        assert isinstance(code, str)
        try:
            profile = self._provider.exchange_code(
                code=code, code_verifier=transaction["v"], expected_nonce=transaction["n"]
            )
        except GoogleAuthError as error:
            if error.reason == "unavailable":
                self._refuse(ERROR_UNAVAILABLE, security_events.GOOGLE_REJECTED_UNAVAILABLE)
            self._refuse(ERROR_REJECTED, security_events.GOOGLE_REJECTED_TOKEN)
        # 更新前發起、尚未回呼的交易可能還帶著生日 `b` 與區間 `r`：一律忽略，不讀取也不保存
        return self._resolve(profile, accepted_terms=transaction.get("t") is True)

    def _check_state(self, state: object, transaction: dict[str, Any] | None) -> None:
        valid = (
            isinstance(transaction, dict)
            and all(isinstance(transaction.get(key), str) and transaction[key] for key in ("s", "n", "v"))
            and isinstance(state, str)
            and bool(state)
            and hmac.compare_digest(state.encode("utf-8"), transaction["s"].encode("utf-8"))
        )
        if not valid:
            self._refuse(ERROR_STATE, security_events.GOOGLE_REJECTED_STATE)

    def _refuse(self, code: str, event: str, *, user_id: int | None = None) -> None:
        security_events.log_event(event, user_id=user_id)
        if user_id is not None and self._lock is not None:
            self._lock.register_strike(user_id, STRIKE_IDENTITY_REJECTED)     # 回應是 302，交易照常提交，計次會保留
        raise GoogleLoginRefused(code)

    # ---- 帳號對應 ------------------------------------------------------------

    def _resolve(self, profile: GoogleProfile, *, accepted_terms: bool) -> GoogleLoginResult:
        now = self._clock()

        identity = self._identities.get_by_subject(PROVIDER_GOOGLE, profile.subject)
        if identity is not None:
            user = self._users.get_by_id(identity.user_id)
            if user is None:
                self._refuse(ERROR_REJECTED, security_events.GOOGLE_REJECTED_CONFLICT)
            assert user is not None
            # 已綁定 Google 的待刪除帳號可以登入（只得到受限登入狀態，讓使用者撤銷刪除）
            self._ensure_enabled(user, allow_pending_deletion=True)
            return self._finish(user, now, OUTCOME_LOGIN, security_events.GOOGLE_LOGIN)

        if not is_valid_email(profile.email):
            self._refuse(ERROR_REJECTED, security_events.GOOGLE_REJECTED_TOKEN)   # 例如非 ASCII 的地址：系統不支援
        canonical = canonical_email(profile.email)
        user = self._users.get_by_canonical_email(canonical)

        if user is None:
            if not accepted_terms:
                self._refuse(ERROR_TERMS, security_events.GOOGLE_REJECTED_TERMS)
            user = self._create_account(profile, canonical, now)
            if user is not None:
                self._bind(user, profile.subject)
                return self._finish(user, now, OUTCOME_REGISTERED, security_events.GOOGLE_REGISTERED)
            user = self._users.get_by_canonical_email(canonical)      # 並行註冊同一信箱：輸家改走「合併」
            if user is None:
                self._refuse(ERROR_REJECTED, security_events.GOOGLE_REJECTED_CONFLICT)
        assert user is not None

        self._ensure_enabled(user)
        existing_binding = self._identities.get_for_user(user.id, PROVIDER_GOOGLE)
        if existing_binding is not None:                              # 這個帳號已綁定「另一個」Google 身分
            self._refuse(ERROR_REJECTED, security_events.GOOGLE_REJECTED_CONFLICT, user_id=user.id)

        if user.email_verified_at is None:
            # 預先佔用信箱的防護：清除未驗證的密碼憑證，並撤銷任何以它建立的登入狀態。
            # 先綁定（唯一可能因競爭而失敗的寫入）：拒絕的回應是 302，交易仍會提交，不能留下半套變更。
            self._bind(user, profile.subject)
            user.password_hash = None
            user.email_verified_at = now
            self._users.save(user)
            self._users.bump_token_version(user)
            return self._finish(user, now, OUTCOME_LINKED_UNVERIFIED, security_events.GOOGLE_LINKED_UNVERIFIED)

        self._bind(user, profile.subject)
        return self._finish(user, now, OUTCOME_LINKED, security_events.GOOGLE_LINKED)

    def _ensure_enabled(self, user: User, *, allow_pending_deletion: bool = False) -> None:
        """停權的帳號一律拒絕。待刪除的帳號只有「本來就綁定這個 Google 身分」時才放行；以信箱找到、尚未綁定的待刪除帳號
        仍拒絕（不在即將刪除的帳號上新增綁定或改動憑證）。"""
        if user.status == STATUS_DELETION_PENDING and not allow_pending_deletion:
            self._refuse(ERROR_DISABLED, security_events.GOOGLE_REJECTED_DISABLED, user_id=user.id)
        if user.status in _DISABLED:
            self._refuse(ERROR_DISABLED, security_events.GOOGLE_REJECTED_DISABLED, user_id=user.id)

    def _create_account(self, profile: GoogleProfile, canonical: str, now: datetime) -> User | None:
        user = User(
            email=normalize_email(profile.email),
            email_canonical=canonical,
            display_name=self._display_name(profile),
            password_hash=None,
            email_verified_at=now,
            status=STATUS_ACTIVE,
            terms_version=CURRENT_TERMS_VERSION,
            terms_accepted_at=now,
            age_deadline_at=now + AGE_CONFIRMATION_DEADLINE,       # 沒有年齡資料；逾時未補填由排程刪除
        )
        try:
            self._users.add_user(user)
        except DuplicateUserError:
            return None
        return user

    @staticmethod
    def _display_name(profile: GoogleProfile) -> str:
        for candidate in (profile.name, normalize_email(profile.email).split("@", 1)[0]):
            cleaned = clean_single_line(candidate) if candidate else None
            if cleaned:
                return cleaned[:MAX_DISPLAY_NAME_LENGTH]
        return "會員"

    def _bind(self, user: User, subject: str) -> None:
        try:
            self._identities.add_identity(UserIdentity(user_id=user.id, provider=PROVIDER_GOOGLE, provider_subject=subject))
        except DuplicateIdentityError:
            self._refuse(ERROR_REJECTED, security_events.GOOGLE_REJECTED_CONFLICT, user_id=user.id)

    def _finish(self, user: User, now: datetime, outcome: str, event: str) -> GoogleLoginResult:
        user.last_login_at = now
        self._users.save(user)
        security_events.log_event(event, user_id=user.id)
        return GoogleLoginResult(user=CurrentUser.from_model(user), outcome=outcome)
