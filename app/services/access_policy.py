"""權限判斷的唯一出處（見 AGENTS.md 第 10 節）。

端點與其他 Service 不自行寫「是不是擁有者」「是不是管理員」，一律問這裡：
- 非擁有者存取資源 → `NotFound`（404），與資源真的不存在完全相同，避免洩漏資源是否存在；
- 授權不依賴識別碼的不可猜測性（即使知道別人的 public_id 也一樣被擋下）；
- 會員等級（free、plus、vip）只影響額度，**不參與任何權限判斷**——這個類別刻意不讀 `member_tier`；
- 會呼叫 Gemini 的功能一律先問 `require_chat_access`：帳號可用（狀態為 active）、已完成年齡確認、且年齡不低於最低使用年齡
  （管理員與各等級都不豁免）；
- 內容模式（`content_access`、`effective_tier`，見 AGENTS.md 第 7 節與 content-tier-modes 規格）依「當下」的年齡決定上限：
  未滿成年門檻（或尚未完成年齡確認）一律是安全模式，不論任何設定都不得提高；成年者最高到一般模式，只有成人模式「啟用中」
  （部署開關開啟，且玩家已在目前的安全規範版本下完成成年確認）才到成人模式。實際生效的模式是玩家偏好、年齡上限、劇本宣告與
  存檔記錄中最保守的一個。年齡一律由生日於當下計算，年齡數字只來自 `AgePolicy`，這裡不內嵌任何年齡數字；
  管理員與各會員等級都不豁免。
管理員存取他人資料必須走專用的 Repository 方法並寫入稽核紀錄（`AdminService`，見 admin-console 規格），不是用這裡繞過擁有者檢查；
`require_admin` 是後台功能的唯一入口，非管理員得到與「不存在」相同的 404。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from app.models.base import utcnow
from app.models.user import ROLE_ADMIN, STATUS_ACTIVE
from app.observability import admin_events, security_events
from app.services.age_policy import DEFAULT_AGE_POLICY, AgePolicy, age_on, taipei_today
from app.services.content_tier import (
    ADULT_RULES_VERSION, CONTENT_TIER_ADULT, CONTENT_TIER_SAFE, CONTENT_TIER_STANDARD, most_conservative, parse_tier,
)
from app.services.current_user import CurrentUser
from app.services.errors import AgeConfirmationRequired, AgeRestricted, Forbidden, NotFound

# 成人模式目前沒有啟用的原因（`ContentAccess.reason`，也是內容模式 API 回應的列舉值）
REASON_NOT_ADULT = "not_adult"                  # 未滿成年門檻，或尚未完成年齡確認
REASON_DISABLED = "disabled"                    # 部署層的開關為關閉
REASON_NEEDS_CONFIRMATION = "needs_confirmation"  # 尚未確認，或確認的安全規範版本已不是目前的版本


@dataclass(frozen=True)
class ContentAccess:
    """一位使用者目前的內容模式狀態（不含劇本與存檔，那兩者只在 `effective_tier` 加入）。

    - `preference`：玩家存的偏好（無法判定時為 `safe`）；
    - `age_cap`：年齡上限，由當下的年齡與成人模式是否啟用決定；
    - `effective`：偏好與年齡上限中較保守者；
    - `is_adult`：當下已滿成年門檻（介面據此顯示「現在可以切換為一般模式」）；
    - `adult_selectable`：玩家可以切換為成人模式（已成年且部署開關開啟；尚未確認時，切換須同時勾選兩項確認）；
    - `adult_confirmed`：已在目前的安全規範版本下完成成年確認；
    - `reason`：成人模式目前**沒有啟用**的原因，已啟用時為 `None`。
    """

    preference: str
    age_cap: str
    effective: str
    is_adult: bool
    adult_selectable: bool
    adult_confirmed: bool
    reason: str | None

    @property
    def selectable(self) -> tuple[str, ...]:
        """目前可以切換成的模式（由保守到寬鬆）。安全模式永遠可選，成年玩家也可以自行選擇它。"""
        modes = [CONTENT_TIER_SAFE]
        if self.is_adult:
            modes.append(CONTENT_TIER_STANDARD)
        if self.adult_selectable:
            modes.append(CONTENT_TIER_ADULT)
        return tuple(modes)


class AccessPolicy:
    def __init__(
        self,
        *,
        age_policy: AgePolicy = DEFAULT_AGE_POLICY,
        clock: Callable[[], datetime] = utcnow,
        adult_mode_enabled: bool = False,
    ) -> None:
        if not isinstance(adult_mode_enabled, bool):
            raise ValueError("adult_mode_enabled 必須是布林值。")
        self._age_policy = age_policy
        self._clock = clock
        self._adult_mode_enabled = adult_mode_enabled

    def is_admin(self, user: CurrentUser) -> bool:
        """只有角色為 admin 且帳號啟用中才是管理員。"""
        return user.role == ROLE_ADMIN and user.status == STATUS_ACTIVE

    def require_admin(self, user: CurrentUser) -> None:
        """所有後台功能的唯一入口：不是「角色為 `admin` 且帳號為 `active`」就以 `NotFound`（404）拒絕。

        與資源不存在完全相同的回應，不洩漏後台存在（與「非擁有者一律 404」同一個原則），會員等級也不影響。
        每次被拒絕記錄一筆只含內部使用者編號的事件，可用來察覺有人在試探後台。
        """
        if not self.is_admin(user):
            admin_events.log_access_denied(user_id=user.id)
            raise NotFound()

    def is_owner(self, user: CurrentUser, owner_id: int | None) -> bool:
        return owner_id is not None and user.id == owner_id

    def require_owner(self, user: CurrentUser, owner_id: int | None) -> None:
        """不是擁有者就拋出 `NotFound`，不是 `Forbidden`：呼叫端無從分辨「別人的」與「不存在的」。"""
        if not self.is_owner(user, owner_id):
            raise NotFound()

    # ---- 年齡 ---------------------------------------------------------------

    def _age(self, user: CurrentUser) -> int | None:
        """當下的年齡；尚未完成年齡確認（沒有生日）為 None。"""
        if user.birth_date is None:
            return None
        return age_on(user.birth_date, taipei_today(self._clock()))

    # ---- 內容模式 -----------------------------------------------------------

    @property
    def adult_mode_enabled(self) -> bool:
        """成人模式的部署開關（由組裝處依環境設定傳入；預設關閉）。"""
        return self._adult_mode_enabled

    def _adult_confirmed(self, user: CurrentUser) -> bool:
        return user.adult_confirmed_at is not None and user.adult_rules_version == ADULT_RULES_VERSION

    def content_access(self, user: CurrentUser) -> ContentAccess:
        """這位使用者目前的內容模式狀態。

        尚未完成年齡確認者保守地視為未成年（他們本來也不能使用 AI 功能）；未成年者不論開關與偏好為何都是安全模式，
        管理員與各會員等級都不豁免。成年者的上限是一般模式，成人模式啟用中才到成人模式；偏好為成人模式但尚未啟用時
        退為一般模式，不是錯誤。
        """
        age = self._age(user)
        is_adult = age is not None and age >= self._age_policy.adult_age
        confirmed = self._adult_confirmed(user)
        preference = parse_tier(user.content_tier_preference)
        if not is_adult:
            return ContentAccess(preference, CONTENT_TIER_SAFE, CONTENT_TIER_SAFE, False, False, confirmed, REASON_NOT_ADULT)
        selectable = self._adult_mode_enabled
        if not selectable:
            reason = REASON_DISABLED
        elif not confirmed:
            reason = REASON_NEEDS_CONFIRMATION
        else:
            reason = None
        age_cap = CONTENT_TIER_ADULT if reason is None else CONTENT_TIER_STANDARD
        return ContentAccess(preference, age_cap, most_conservative(preference, age_cap), True, selectable, confirmed, reason)

    def effective_tier(self, user: CurrentUser, *, script_tier: str | None = None, save_tier: str | None = None) -> str:
        """實際生效的內容模式：玩家偏好、年齡上限、劇本宣告（有劇本時）與存檔記錄（有存檔時）中最保守的一個。

        沒有劇本或存檔時不傳（`None`）；傳入了卻無法判定的值一律視為安全模式，不會被忽略。存檔的模式只能在上限之內調整，
        這裡不會因為任何輸入把模式調高。
        """
        tiers = [self.content_access(user).effective]
        tiers += [tier for tier in (script_tier, save_tier) if tier is not None]
        return most_conservative(*tiers)

    # ---- 是否可使用 AI 功能 -------------------------------------------------

    def _account_usable(self, user: CurrentUser) -> bool:
        """帳號必須啟用中。玩家的信箱在建立帳號時就由 Google 驗證，所以不再另看信箱驗證時間。"""
        return user.status == STATUS_ACTIVE

    def can_use_chat(self, user: CurrentUser) -> bool:
        """是否可使用客服與其他會呼叫 Gemini 的功能：帳號可用、已完成年齡確認，且年齡不低於最低使用年齡。

        年齡閘門不看角色與會員等級：管理員與各等級都一樣要先完成。
        """
        return self.chat_gate(user) is None

    def chat_gate(self, user: CurrentUser) -> str | None:
        """閘門的結果，不拋例外也不記錄事件：`None` = 可使用，否則是拒絕的代碼。

        頁面載入時就要知道結果（列表端點不過閘門），所以另外提供這個純查詢；`require_chat_access` 以它判斷，
        兩者因此不會分歧。先檢查帳號狀態，再檢查年齡：帳號不可用時，補填年齡也沒有幫助，不該引導使用者去補。
        """
        if not self._account_usable(user):
            return Forbidden.code
        age = self._age(user)
        if age is None:
            return AgeConfirmationRequired.code
        if age < self._age_policy.min_use_age:
            return AgeRestricted.code
        return None

    def require_chat_access(self, user: CurrentUser) -> None:
        """每個會呼叫 Gemini 的端點在做任何事之前先呼叫這裡；不允許時拋出錯誤。

        年齡的兩種拒絕不同：尚未完成確認 → `AgeConfirmationRequired`；年齡低於最低使用年齡 → `AgeRestricted`
        （補填也沒有用，所以不引導）。兩者都記錄一筆資訊層級的事件。
        玩家沒完成年齡確認時，在登入檢查（`CurrentUser.restriction` 的待確認年齡狀態）就被擋下並導向 `/age`，不會走到這裡；
        走到這裡而被這個代碼拒絕的是管理員（沒有受限狀態，到帳號設定頁補填）。
        """
        denied = self.chat_gate(user)
        if denied is None:
            return
        if denied == Forbidden.code:
            raise Forbidden()
        security_events.log_event(security_events.AGE_GATE_DENIED, user_id=user.id)
        raise AgeConfirmationRequired() if denied == AgeConfirmationRequired.code else AgeRestricted()
