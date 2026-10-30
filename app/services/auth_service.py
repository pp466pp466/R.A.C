"""註冊、登入、登入狀態辨識、變更密碼與建立管理員（見 AGENTS.md 第 10 節）。

這裡的幾個保證是「結構性」的，不靠各端點自律：
- 註冊對「信箱已存在」與「新信箱」回傳同樣的結果，且兩者都先雜湊密碼；
- 登入對「信箱不存在／密碼錯誤／無密碼／鎖定中」做等價的運算並拋出同一個錯誤；
- 失敗計數與 `token_version` 一律用單一條件式 UPDATE 修改（Repository），並行請求不會互相覆蓋。
Service 不匯入 flask，也不知道 HTTP；時間由注入的時鐘提供。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, Protocol

from app.models.user import (
    PROVIDER_GOOGLE, ROLE_ADMIN, STATUS_ACTIVE, STATUS_DELETION_PENDING, STATUS_PENDING_VERIFICATION,
    STATUS_SUSPENDED, User,
)
from app.observability import security_events
from app.repositories.errors import DuplicateUserError
from app.repositories.user_identity_repository import UserIdentityRepository
from app.repositories.user_repository import UserRepository
from app.services.current_user import CurrentUser
from app.services.errors import Conflict, Forbidden, Unauthenticated, ValidationError
from app.services.input_cleaning import clean_single_line
from app.services.age_policy import DEFAULT_AGE_POLICY, AgePolicy, VerifiedAge, age_group, taipei_today, validate_age_input
from app.services.legal import CURRENT_TERMS_VERSION
from app.services.normalization import canonical_email, is_valid_email, normalize_email
from app.services.password_hasher import MAX_PASSWORD_LENGTH, PasswordHasherService, normalize_password, password_problem

MAX_FAILED_LOGINS = 5
LOCK_DURATION = timedelta(minutes=15)
MAX_DISPLAY_NAME_LENGTH = 50

# 所有「登入不成功」共用的訊息：不透露是信箱不存在、密碼錯誤，還是帳號被鎖定
INVALID_CREDENTIALS_MESSAGE = "信箱或密碼不正確，或登入嘗試過多而暫時鎖定，請稍後再試。"
ACCOUNT_DISABLED_MESSAGE = "此帳號已停用，無法登入。"

_DISABLED_STATUSES = (STATUS_SUSPENDED, STATUS_DELETION_PENDING)


class AccountMailer(Protocol):
    """帳號相關的寄信動作（實作見 EmailFlowService）。信箱驗證關閉時不需要，傳入 None。"""

    def send_verification(self, user: User) -> None: ...

    def send_password_changed_notice(self, user: User) -> None: ...


def _invalid_credentials() -> Unauthenticated:
    return Unauthenticated(INVALID_CREDENTIALS_MESSAGE, code="invalid_credentials")


class AuthService:
    def __init__(
        self,
        *,
        users: UserRepository,
        identities: UserIdentityRepository,
        hasher: PasswordHasherService,
        clock: Callable[[], datetime],
        email_verification_required: bool,
        mailer: AccountMailer | None = None,
        persist_failures: Callable[[], None] | None = None,
        age_policy: AgePolicy = DEFAULT_AGE_POLICY,
    ) -> None:
        self._age_policy = age_policy
        self._users = users
        self._identities = identities
        self._hasher = hasher
        self._clock = clock
        self._verification_required = email_verification_required
        self._mailer = mailer
        # 登入失敗會回 4xx，而請求級交易遇到 4xx 預設會回復——失敗次數若跟著回復就永遠不會累積、帳號永遠不會鎖定。
        # 記錄失敗後呼叫它，要求這個請求即使以 4xx 結束也保留變更（沒有請求的環境，例如命令列，不需要）。
        self._persist_failures = persist_failures or (lambda: None)

    # =========================================================================
    # 註冊
    # =========================================================================

    def register(
        self,
        *,
        email: object,
        password: object,
        display_name: object,
        accept_terms: object,
        birth_date: object,
        age_range: object,
    ) -> None:
        """以信箱加密碼註冊。不論信箱是否已有帳號，回傳與副作用以外的行為完全相同（沒有回傳值）。

        - 同意條款（獨立的明確勾選）與生日加年齡區間（規則見 `age_policy.validate_age_input`）都要通過；驗證發生在
          查詢信箱是否存在之前，所以輸入不合法時，已存在與不存在的信箱得到同樣的錯誤。
        - 生日與年齡區間不一致、或未滿最低註冊年齡：不建立帳號、不保存任何年齡資料、**不封鎖或標記該信箱**，
          也不累計次數，使用者可以立刻以同一個信箱與密碼再註冊；日誌只記事件名稱。
        - 信箱驗證開啟：新帳號為 pending_verification 並寄驗證信；關閉：直接 active，不寄信。
        - 信箱已存在：不建立帳號，也不覆蓋既有的密碼、資料與年齡確認紀錄；若該帳號尚未驗證信箱且驗證開啟，
          重新寄驗證信（受寄信頻率限制）。
        - 不自動登入。
        """
        clean_email, clean_name, verified = self._validate_new_account(
            email, password, display_name, require_consents=True, accept_terms=accept_terms,
            birth_date=birth_date, age_range=age_range,
        )
        assert verified is not None
        password_hash = self._hasher.hash(str(password))      # 不論信箱是否存在都先雜湊，使耗時一致
        canonical = canonical_email(clean_email)

        existing = self._users.get_by_canonical_email(canonical)
        if existing is None:
            now = self._clock()
            user = User(
                email=normalize_email(clean_email),
                email_canonical=canonical,
                display_name=clean_name,
                password_hash=password_hash,
                status=STATUS_PENDING_VERIFICATION if self._verification_required else STATUS_ACTIVE,
                terms_version=CURRENT_TERMS_VERSION,
                terms_accepted_at=now,
                birth_date=verified.birth_date,
                age_range=verified.age_range,
                is_adult_at_confirmation=verified.is_adult,
                age_confirmed_at=now,
                age_policy_version=self._age_policy.version,
            )
            try:
                self._users.add_user(user)
            except DuplicateUserError:
                existing = self._users.get_by_canonical_email(canonical)   # 並行註冊同一信箱：輸家當作「已存在」
            else:
                security_events.log_event(security_events.REGISTER, user_id=user.id)
                security_events.log_event(security_events.AGE_CONFIRMED_REGISTER, user_id=user.id)
                if self._verification_required:
                    self._require_mailer().send_verification(user)
                return

        if existing is None:  # 唯一索引說有重複，卻查不到：理論上不會發生，保守地當作成功
            return
        security_events.log_event(security_events.REGISTER_EXISTING, user_id=existing.id)
        if (
            self._verification_required
            and existing.email_verified_at is None
            and existing.status in (STATUS_PENDING_VERIFICATION, STATUS_ACTIVE)
        ):
            self._require_mailer().send_verification(existing)

    # =========================================================================
    # 登入
    # =========================================================================

    def login(self, *, email: object, password: object) -> CurrentUser:
        """驗證信箱與密碼。失敗一律拋出同一個 `invalid_credentials`（401）；帳號被停用時拋出 403。"""
        fields: dict[str, str] = {}
        if not isinstance(email, str) or not email.strip():
            fields["email"] = "請輸入電子郵件。"
        if not isinstance(password, str) or not password:
            fields["password"] = "請輸入密碼。"
        if fields:
            raise ValidationError("請輸入信箱與密碼。", fields=fields)
        assert isinstance(email, str) and isinstance(password, str)

        now = self._clock()
        user = self._users.get_by_email(normalize_email(email)) if is_valid_email(email) else None
        if user is None:
            self._hasher.burn(password)
            security_events.log_event(security_events.LOGIN_FAILED_UNKNOWN_USER)
            raise _invalid_credentials()

        if user.locked_until is not None:
            if user.locked_until > now:
                self._hasher.burn(password)
                security_events.log_event(security_events.LOGIN_FAILED_LOCKED, user_id=user.id)
                raise _invalid_credentials()
            self._users.release_expired_lock(user, now=now)          # 鎖定已到期：開啟新的計數週期

        if len(normalize_password(password)) > MAX_PASSWORD_LENGTH:
            matched = False                                         # 超過長度上限的密碼不可能正確，不浪費一次雜湊
        else:
            matched = self._hasher.verify(user.password_hash, password)
        if not matched:
            self._register_failure(user, now, had_password=bool(user.password_hash))
            raise _invalid_credentials()

        if user.status in _DISABLED_STATUSES:
            security_events.log_event(security_events.LOGIN_REFUSED_DISABLED, user_id=user.id)
            raise Forbidden(ACCOUNT_DISABLED_MESSAGE, code="account_disabled")

        if user.failed_login_count or user.locked_until is not None:
            self._users.reset_login_failures(user)
        if user.password_hash and self._hasher.needs_rehash(user.password_hash):
            user.password_hash = self._hasher.hash(password)
        user.last_login_at = now
        self._users.save(user)
        security_events.log_event(security_events.LOGIN_SUCCESS, user_id=user.id)
        return CurrentUser.from_model(user)

    def _register_failure(self, user: User, now: datetime, *, had_password: bool) -> None:
        """登入失敗：記錄事件（失敗原因寫在事件名稱）並計入失敗次數。"""
        reason = security_events.LOGIN_FAILED_BAD_PASSWORD if had_password else security_events.LOGIN_FAILED_NO_PASSWORD
        security_events.log_event(reason, user_id=user.id)
        self._count_failure(user, now)

    def _count_failure(self, user: User, now: datetime) -> None:
        """失敗次數加 1（單一 UPDATE）；達門檻時鎖定帳號。變更密碼時輸入錯誤的目前密碼也走這裡。"""
        count = self._users.increment_failed_login(user)
        if count >= MAX_FAILED_LOGINS and self._users.lock_account(
            user, locked_until=now + LOCK_DURATION, now=now, min_failures=MAX_FAILED_LOGINS
        ):
            security_events.log_event(security_events.ACCOUNT_LOCKED, user_id=user.id)
        self._persist_failures()

    # =========================================================================
    # 登入狀態
    # =========================================================================

    def resolve_session(self, user_id: int, token_version: int) -> CurrentUser | None:
        """登入 cookie 內的 (user_id, token_version) 是否仍有效。使用者不存在、版本不符或帳號被停用都回傳 None。"""
        user = self._users.get_by_id(user_id)
        if user is None or user.token_version != token_version or user.status in _DISABLED_STATUSES:
            return None
        return CurrentUser.from_model(user)

    def profile(self, current: CurrentUser) -> dict:
        """`/api/auth/me` 的內容：使用者本人的資料，另含是否已綁定 Google 與年齡群組（不含生日與年齡區間）。"""
        linked = self._identities.get_for_user(current.id, PROVIDER_GOOGLE) is not None
        group = age_group(current.birth_date, taipei_today(self._clock()), self._age_policy)
        return current.public_dict(age_group=group, google_linked=linked)

    def verify_age(self, current: CurrentUser, *, birth_date: object, age_range: object) -> CurrentUser:
        """已登入、尚未完成年齡確認的使用者補填生日與年齡區間（管理員指令建立的帳號、勾選版時代的帳號都走這裡）。

        - 已完成的帳號再次提交一律成功、完全不改動原本的紀錄，也不再記錄事件（即使內容不同）。
        - 驗證規則與註冊相同；不通過時不寫入任何東西，日誌只記事件名稱。
        - 回傳最新的帳號快照，讓前端立刻知道已完成。
        """
        user = self._require_user(current)
        if user.birth_date is not None and user.age_range is not None:
            return CurrentUser.from_model(user)
        verified, error = self._check_age_input(birth_date, age_range)
        if error is not None:
            security_events.log_age_rejection(error.code)
            raise error
        assert verified is not None
        if self._users.complete_age_verification_if_unset(
            user,
            birth_date=verified.birth_date,
            age_range=verified.age_range,
            is_adult=verified.is_adult,
            confirmed_at=self._clock(),
            policy_version=self._age_policy.version,
        ):
            security_events.log_event(security_events.AGE_CONFIRMED_LATER, user_id=user.id)
        return CurrentUser.from_model(user)

    def record_logout(self, current: CurrentUser) -> None:
        security_events.log_event(security_events.LOGOUT, user_id=current.id)

    def logout_all(self, current: CurrentUser) -> None:
        """登出所有裝置（含目前這台）：`token_version` 加 1，既有的登入狀態全部失效。"""
        user = self._require_user(current)
        self._users.bump_token_version(user)
        security_events.log_event(security_events.LOGOUT_ALL, user_id=user.id)

    # =========================================================================
    # 變更密碼
    # =========================================================================

    def change_password(self, current: CurrentUser, *, current_password: object, new_password: object) -> CurrentUser:
        """已登入且已設定密碼的使用者，在提供目前密碼的前提下變更密碼。

        成功後 `token_version` 加 1（其他裝置登出），回傳新的快照讓目前裝置以新版本維持登入。
        """
        user = self._require_user(current)
        if not user.password_hash:
            raise ValidationError("目前的帳號沒有設定密碼，無法以這個方式變更。", code="no_password")

        fields: dict[str, str] = {}
        if not isinstance(current_password, str) or not current_password:
            fields["current_password"] = "請輸入目前的密碼。"
        problem = password_problem(new_password)
        if problem:
            fields["new_password"] = problem
        if fields:
            raise ValidationError(fields=fields)
        assert isinstance(current_password, str) and isinstance(new_password, str)

        now = self._clock()
        locked = user.locked_until is not None and user.locked_until > now
        if locked or not self._hasher.verify(user.password_hash, current_password):
            if not locked:
                self._count_failure(user, now)
            security_events.log_event(security_events.PASSWORD_CHANGE_REFUSED, user_id=user.id)
            raise ValidationError(fields={"current_password": "目前的密碼不正確。"})
        if normalize_password(new_password) == normalize_password(current_password):
            raise ValidationError(fields={"new_password": "新密碼不可與目前的密碼相同。"})

        user.password_hash = self._hasher.hash(new_password)
        self._users.save(user)
        self._users.reset_login_failures(user)
        self._users.bump_token_version(user)
        security_events.log_event(security_events.PASSWORD_CHANGED, user_id=user.id)
        if self._verification_required:
            self._require_mailer().send_password_changed_notice(user)
        return CurrentUser.from_model(user)

    # =========================================================================
    # 重新輸入密碼（危險操作的二次確認）
    # =========================================================================

    def confirm_password(self, current: CurrentUser, password: object) -> None:
        """已登入者以自己的密碼確認一個危險操作（後台的停權、調整等級、刪除對話……）。通過就回傳，否則拋出錯誤。

        - 沒有設定密碼（只靠 Google 的帳號）→ `ValidationError`，代碼 `no_password`。
        - 缺少或不正確 → `ValidationError`，欄位 `password`。**與登入共用失敗計數與鎖定**（連續 5 次鎖定 15 分鐘），
          所以這個確認不會成為暴力猜測密碼的捷徑。鎖定中即使密碼正確也一律拒絕，且不洩漏是否被鎖定（同一個訊息）。
        - 成功**不**重設失敗計數：只有登入成功才重設，否則攻擊者可以交錯一次正確與多次錯誤。
        - 不改變 `token_version` 與登入狀態。
        """
        user = self._require_user(current)
        if not user.password_hash:
            raise ValidationError("目前的帳號沒有設定密碼，無法執行這個操作。", code="no_password")
        if not isinstance(password, str) or not password:
            raise ValidationError(fields={"password": "請輸入您的密碼。"})

        now = self._clock()
        if user.locked_until is not None and user.locked_until <= now:
            self._users.release_expired_lock(user, now=now)           # 鎖定已到期：開啟新的計數週期（與登入相同）
        locked = user.locked_until is not None and user.locked_until > now
        matched = (
            not locked
            and len(normalize_password(password)) <= MAX_PASSWORD_LENGTH
            and self._hasher.verify(user.password_hash, password)
        )
        if not matched:
            if not locked:
                self._count_failure(user, now)
            raise ValidationError(fields={"password": "密碼不正確，操作未執行。"})

    # =========================================================================
    # 管理員
    # =========================================================================

    def create_admin(self, *, email: object, display_name: object, password: object) -> CurrentUser:
        """由部署者以一次性指令建立管理員：角色 admin、狀態 active、信箱視為已驗證。信箱已存在時拒絕且不改動既有帳號。"""
        clean_email, clean_name, _ = self._validate_new_account(email, password, display_name, require_consents=False)
        canonical = canonical_email(clean_email)
        if self._users.get_by_canonical_email(canonical) is not None:
            raise Conflict("這個信箱已經有帳號了。", code="email_exists")
        user = User(
            email=normalize_email(clean_email),
            email_canonical=canonical,
            display_name=clean_name,
            password_hash=self._hasher.hash(str(password)),
            role=ROLE_ADMIN,
            status=STATUS_ACTIVE,
            email_verified_at=self._clock(),
        )
        try:
            self._users.add_user(user)
        except DuplicateUserError as exc:
            raise Conflict("這個信箱已經有帳號了。", code="email_exists") from exc
        security_events.log_event(security_events.ADMIN_CREATED, user_id=user.id)
        return CurrentUser.from_model(user)

    # =========================================================================
    # 內部輔助
    # =========================================================================

    def _validate_new_account(
        self,
        email: object,
        password: object,
        display_name: object,
        *,
        require_consents: bool,
        accept_terms: object = True,
        birth_date: object = None,
        age_range: object = None,
    ) -> tuple[str, str, VerifiedAge | None]:
        """驗證新帳號的輸入。欄位錯誤一次回報；年齡的語意錯誤（未滿最低年齡、不一致）只在沒有欄位錯誤時才拋出，
        因為欄位錯誤可以就地修正，而語意錯誤要求使用者重新填寫。管理員指令（`require_consents=False`）不涉及年齡。"""
        fields: dict[str, str] = {}
        if not isinstance(email, str) or not is_valid_email(email):
            fields["email"] = "請輸入有效的電子郵件地址。"
        problem = password_problem(password)
        if problem:
            fields["password"] = problem
        cleaned_name = clean_single_line(display_name) if isinstance(display_name, str) else None
        if not cleaned_name or len(cleaned_name) > MAX_DISPLAY_NAME_LENGTH:
            fields["display_name"] = f"顯示名稱須為 1 至 {MAX_DISPLAY_NAME_LENGTH} 字。"
        verified: VerifiedAge | None = None
        age_error: ValidationError | None = None
        if require_consents:
            if accept_terms is not True:
                fields["accept_terms"] = "請先勾選同意服務條款與隱私權政策。"
            verified, age_error = self._check_age_input(birth_date, age_range)
            if age_error is not None and age_error.code == ValidationError.code:    # 缺少或格式不合法：欄位錯誤，一併回報
                fields.update(age_error.fields)
                age_error = None
        if fields:
            raise ValidationError(fields=fields)
        if age_error is not None:
            security_events.log_age_rejection(age_error.code)
            raise age_error
        assert isinstance(email, str) and cleaned_name is not None
        return email, cleaned_name, verified

    def _check_age_input(self, birth_date: object, age_range: object) -> tuple[VerifiedAge | None, ValidationError | None]:
        """以「今天（Asia/Taipei）」驗證生日與年齡區間；回傳（結果, 錯誤），不拋出、不記錄。"""
        try:
            return validate_age_input(birth_date, age_range, taipei_today(self._clock()), self._age_policy), None
        except ValidationError as error:
            return None, error

    def _require_user(self, current: CurrentUser) -> User:
        user = self._users.get_by_id(current.id)
        if user is None:  # 已通過 resolve_session 的使用者不應消失；保守地當作未登入
            raise Unauthenticated()
        return user

    def _require_mailer(self) -> AccountMailer:
        if self._mailer is None:
            raise RuntimeError("信箱驗證已開啟，但沒有設定寄信元件。")
        return self._mailer
