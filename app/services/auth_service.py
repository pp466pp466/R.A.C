"""管理員的密碼驗證、登入狀態辨識、年齡補填、變更密碼與建立管理員（見 AGENTS.md 第 10 節）。

玩家只用 Google 登入（`GoogleSignInService`），沒有密碼註冊與密碼登入；密碼只屬於管理員，用於 Google 通過之後的第二道驗證
與高風險操作的重新輸入。這裡的幾個保證是「結構性」的，不靠各端點自律：
- 密碼驗證對「中間狀態對不上帳號／密碼錯誤／鎖定中」做等價的運算並拋出同一個錯誤；
- 失敗計數與 `token_version` 一律用單一條件式 UPDATE 修改（Repository），並行請求不會互相覆蓋。
Service 不匯入 flask，也不知道 HTTP；時間由注入的時鐘提供。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, Protocol

from app.models.lock_strike import STRIKE_IDENTITY_REJECTED
from app.models.user import PROVIDER_GOOGLE, ROLE_ADMIN, STATUS_ACTIVE, STATUS_SUSPENDED, User
from app.observability import security_events
from app.repositories.errors import DuplicateUserError
from app.repositories.user_identity_repository import UserIdentityRepository
from app.repositories.user_repository import UserRepository
from app.services.account_erasure_service import AccountErasureService
from app.services.account_lock_service import AccountLockService
from app.services.current_user import CurrentUser
from app.services.errors import Conflict, Forbidden, UnderageAccountErased, Unauthenticated, ValidationError
from app.services.input_cleaning import clean_single_line
from app.services.nickname import check_nickname, new_code
from app.services.age_policy import DEFAULT_AGE_POLICY, AgePolicy, VerifiedAge, age_group, taipei_today, validate_age_input
from app.services.normalization import canonical_email, is_valid_email, normalize_email
from app.services.password_hasher import MAX_PASSWORD_LENGTH, PasswordHasherService, normalize_password, password_problem

MAX_FAILED_LOGINS = 5
LOCK_DURATION = timedelta(minutes=15)
MAX_DISPLAY_NAME_LENGTH = 50

# 所有「登入不成功」共用的訊息：不透露是信箱不存在、密碼錯誤，還是帳號被鎖定
INVALID_CREDENTIALS_MESSAGE = "信箱或密碼不正確，或登入嘗試過多而暫時鎖定，請稍後再試。"
ACCOUNT_DISABLED_MESSAGE = "此帳號已停用，無法登入。"
# 補填年齡時生日與區間對不起來：通用說明，不指出是哪個欄位，也不暗示可以改填其他值
AGE_STEP_MISMATCH_MESSAGE = "資料有誤，請重新填寫。"

# 停權的帳號不能登入；待刪除的帳號可以登入，但只得到受限的登入狀態（只能撤銷刪除，見 session.require_login）
_DISABLED_STATUSES = (STATUS_SUSPENDED,)


class AccountMailer(Protocol):
    """帳號相關的寄信動作（實作見 EmailFlowService）。沒有寄信設定時不需要，傳入 None。"""

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
        mailer: AccountMailer | None = None,
        persist_failures: Callable[[], None] | None = None,
        age_policy: AgePolicy = DEFAULT_AGE_POLICY,
        erasure: AccountErasureService | None = None,
        lock: AccountLockService | None = None,
    ) -> None:
        self._age_policy = age_policy
        self._erasure = erasure          # 補填年齡時發現未滿 13 歲的玩家：走單一抹除出口
        self._lock = lock                # 補填年齡時的不一致：計入帳號鎖定的計次
        self._users = users
        self._identities = identities
        self._hasher = hasher
        self._clock = clock
        self._mailer = mailer
        # 登入失敗會回 4xx，而請求級交易遇到 4xx 預設會回復——失敗次數若跟著回復就永遠不會累積、帳號永遠不會鎖定。
        # 記錄失敗後呼叫它，要求這個請求即使以 4xx 結束也保留變更（沒有請求的環境，例如命令列，不需要）。
        self._persist_failures = persist_failures or (lambda: None)

    # =========================================================================
    # 管理員的密碼驗證
    # =========================================================================

    def verify_admin_password(self, user_id: int, token_version: int, password: object) -> CurrentUser:
        """管理員通過 Google 之後的密碼驗證步驟。`user_id` 與 `token_version` 來自簽章的中間狀態 cookie。

        中間狀態對不上目前的帳號（不存在、不是管理員、`token_version` 已變、被停權）時回 401 `unauthorized`，
        要求重新以 Google 登入；密碼不對則與登入相同：通用訊息、失敗次數累計、連續 5 次鎖定 15 分鐘（計數在資料庫）。
        """
        if not isinstance(password, str) or not password:
            raise ValidationError("請輸入密碼。", fields={"password": "請輸入密碼。"})
        user = self._users.get_by_id(user_id)
        if user is None or user.role != ROLE_ADMIN or user.token_version != token_version or user.status in _DISABLED_STATUSES:
            self._hasher.burn(password)
            security_events.log_event(security_events.ADMIN_STEP_EXPIRED, user_id=user.id if user else None)
            raise Unauthenticated()
        return self._authenticate(user, password, self._clock())

    def _authenticate(self, user: User, password: str, now: datetime) -> CurrentUser:
        """密碼驗證與失敗鎖定（管理員的密碼驗證步驟）。失敗一律拋出同一個 `invalid_credentials`（401）。"""
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

    def verify_age(
        self, current: CurrentUser, *, birth_date: object, age_range: object, nickname: object = None
    ) -> CurrentUser:
        """已登入、尚未完成年齡確認的使用者補填生日與年齡區間（玩家首次以 Google 登入後的年齡確認頁、管理員的帳號設定頁）。

        - 已完成的帳號再次提交一律成功、完全不改動原本的紀錄，也不再記錄事件（即使內容不同）。
        - **玩家同時提交暱稱**（rpg-script-authoring）：暱稱與生日、年齡區間是同一個步驟、同一次提交，全有或全無。暱稱**先**驗證，
          不合格以暱稱欄位的錯誤回應，不保存任何資料，也**不**計入帳號鎖定的計次（不是年齡資料的不一致）。管理員在帳號設定頁
          補填時可不提供暱稱（提供了就一併驗證與保存）。
        - 驗證規則與註冊相同；不通過時不寫入任何年齡資料，日誌只記事件名稱。
        - **未滿 13 歲的玩家**（生日算出的年齡，或所選區間為未滿 13 歲）：立即抹除帳號與全部個資，不保存填寫的生日，
          拋出 `UnderageAccountErased`（400，Controller 據此清除目前裝置的登入狀態）。管理員只拒絕、不抹除。
        - **不一致**：回應通用的「資料有誤」，不指出是哪個欄位；帳號保留、可立即重填，並計入帳號鎖定的計次。
        - 回傳最新的帳號快照，讓前端立刻知道已完成。
        """
        user = self._require_user(current)
        if user.birth_date is not None and user.age_range is not None:
            return CurrentUser.from_model(user)
        clean_nickname: str | None = None
        nickname_error: str | None = None
        if user.role != ROLE_ADMIN or nickname not in (None, ""):
            clean_nickname, nickname_error = check_nickname(nickname)
        verified, error = self._check_age_input(birth_date, age_range)
        if nickname_error is not None:
            # 暱稱不合格：什麼都不保存、不計入鎖定計次（不一致與未滿 13 歲的處理在這之後，所以不會被觸發）。
            # 年齡欄位只是格式錯誤（沒有填、日期不合法）時，把欄位錯誤一併回報，使用者一次改完。
            fields = {"nickname": nickname_error}
            if error is not None and error.code == "validation_error":
                fields = {**error.fields, **fields}
            raise ValidationError(fields=fields)
        if error is not None:
            security_events.log_age_rejection(error.code)
            raise self._age_step_error(user, error)
        assert verified is not None
        if self._users.complete_age_verification_if_unset(
            user,
            birth_date=verified.birth_date,
            age_range=verified.age_range,
            is_adult=verified.is_adult,
            confirmed_at=self._clock(),
            policy_version=self._age_policy.version,
            nickname=clean_nickname,
            nickname_code=new_code() if clean_nickname else None,
        ):
            security_events.log_event(security_events.AGE_CONFIRMED_LATER, user_id=user.id)
        return CurrentUser.from_model(user)

    def _age_step_error(self, user: User, error: ValidationError) -> ValidationError:
        """補填年齡不通過時要拋出的錯誤；未滿 13 歲的玩家會在這裡被抹除，不一致會在這裡計次。"""
        if error.code == "age_not_eligible" and user.role != ROLE_ADMIN:
            if self._erasure is None:
                raise RuntimeError("沒有設定帳號抹除元件，無法處理未滿 13 歲的帳號。")
            self._erasure.erase_underage(user.id, now=self._clock())
            return UnderageAccountErased(f"本服務僅供 {self._age_policy.min_register_age} 歲以上使用，您的帳號與相關資料已刪除。")
        if error.code == "age_mismatch":
            if self._lock is not None:
                self._lock.register_strike(user.id, STRIKE_IDENTITY_REJECTED)
            return ValidationError(AGE_STEP_MISMATCH_MESSAGE, code="age_mismatch")
        return error

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
        if self._mailer is not None:
            self._mailer.send_password_changed_notice(user)
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
        clean_email, clean_name = self._validate_admin_input(email, password, display_name)
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

    def _validate_admin_input(self, email: object, password: object, display_name: object) -> tuple[str, str]:
        """驗證 `create-admin` 的輸入。欄位錯誤一次回報。"""
        fields: dict[str, str] = {}
        if not isinstance(email, str) or not is_valid_email(email):
            fields["email"] = "請輸入有效的電子郵件地址。"
        problem = password_problem(password)
        if problem:
            fields["password"] = problem
        cleaned_name = clean_single_line(display_name) if isinstance(display_name, str) else None
        if not cleaned_name or len(cleaned_name) > MAX_DISPLAY_NAME_LENGTH:
            fields["display_name"] = f"顯示名稱須為 1 至 {MAX_DISPLAY_NAME_LENGTH} 字。"
        if fields:
            raise ValidationError(fields=fields)
        assert isinstance(email, str) and cleaned_name is not None
        return email, cleaned_name

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

