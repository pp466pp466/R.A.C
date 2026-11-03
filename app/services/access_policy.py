"""權限判斷的唯一出處（見 AGENTS.md 第 10 節）。

端點與其他 Service 不自行寫「是不是擁有者」「是不是管理員」，一律問這裡：
- 非擁有者存取資源 → `NotFound`（404），與資源真的不存在完全相同，避免洩漏資源是否存在；
- 授權不依賴識別碼的不可猜測性（即使知道別人的 public_id 也一樣被擋下）；
- 會員等級（free、plus、vip）只影響額度，**不參與任何權限判斷**——這個類別刻意不讀 `member_tier`；
- 會呼叫 Gemini 的功能一律先問 `require_chat_access`：帳號可用（狀態為 active）、已完成年齡確認、且年齡不低於最低使用年齡
  （管理員與各等級都不豁免）；
- 內容上限由 `content_ceiling` 依「當下」的年齡決定：未滿成年門檻（或尚未完成年齡確認）一律是安全模式；成年者最高到 2，
  只有組裝時以環境設定開啟等級 3（`SEXUAL_CONTENT_LEVEL3_ENABLED`，預設關閉，見 AGENTS.md 第 7 節）才是 3。
  年齡一律由生日於當下計算，年齡數字只來自 `AgePolicy`，這裡不內嵌任何年齡數字；開關不影響未成年者。
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
from app.services.current_user import CurrentUser
from app.services.errors import AgeConfirmationRequired, AgeRestricted, Forbidden, NotFound

# 成年者的內容上限。預設是 2（浪漫、曖昧與感官但不露骨）；sexual_content_level 3（成年角色之間合意的露骨內容）
# 只能以環境設定開啟（AGENTS.md 第 7 節：預設關閉、須已取得書面許可、staging 與 production 在強化的成年驗證完成前不得開啟），
# 由組裝處依設定傳入 `adult_ceiling`；這裡不依任何設定自行放寬，也不把 3 寫死為預設。
MAX_ADULT_SEXUAL_CONTENT_LEVEL = 2
LEVEL3_SEXUAL_CONTENT_LEVEL = 3
SAFE_MODE_SEXUAL_CONTENT_LEVEL = 0
_ALLOWED_ADULT_CEILINGS = (MAX_ADULT_SEXUAL_CONTENT_LEVEL, LEVEL3_SEXUAL_CONTENT_LEVEL)


@dataclass(frozen=True)
class ContentCeiling:
    """目前這位使用者能使用的內容上限。`safe_mode` 為真時介面須顯示安全模式的提示。"""

    sexual_content_level: int
    safe_mode: bool


class AccessPolicy:
    def __init__(
        self,
        *,
        age_policy: AgePolicy = DEFAULT_AGE_POLICY,
        clock: Callable[[], datetime] = utcnow,
        adult_ceiling: int = MAX_ADULT_SEXUAL_CONTENT_LEVEL,
    ) -> None:
        if adult_ceiling not in _ALLOWED_ADULT_CEILINGS:
            raise ValueError("成年者的內容上限只能是 2 或 3。")
        self._age_policy = age_policy
        self._clock = clock
        self._adult_ceiling = adult_ceiling

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

    def content_ceiling(self, user: CurrentUser) -> ContentCeiling:
        """內容上限。尚未完成年齡確認者保守地視為安全模式（他們本來也不能使用 AI 功能）；
        未成年者不論等級 3 開關為何都是安全模式，管理員與各會員等級都不豁免。"""
        age = self._age(user)
        if age is None or age < self._age_policy.adult_age:
            return ContentCeiling(SAFE_MODE_SEXUAL_CONTENT_LEVEL, safe_mode=True)
        return ContentCeiling(self._adult_ceiling, safe_mode=False)

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
