"""權限判斷的唯一出處（見 AGENTS.md 第 10 節）。

端點與其他 Service 不自行寫「是不是擁有者」「是不是管理員」，一律問這裡：
- 非擁有者存取資源 → `NotFound`（404），與資源真的不存在完全相同，避免洩漏資源是否存在；
- 授權不依賴識別碼的不可猜測性（即使知道別人的 public_id 也一樣被擋下）；
- 會員等級（free、plus、vip）只影響額度，**不參與任何權限判斷**——這個類別刻意不讀 `member_tier`；
- 會呼叫 Gemini 的功能一律先問 `require_chat_access`：帳號可用、已完成年齡確認、且年齡不低於最低使用年齡
  （管理員與各等級都不豁免）；
- 內容上限由 `content_ceiling` 依「當下」的年齡決定：未滿成年門檻一律是安全模式，成年者最高到 2（3 維持鎖定）。
  年齡一律由生日於當下計算，年齡數字只來自 `AgePolicy`，這裡不內嵌任何年齡數字。
管理員存取他人資料必須走專用的 Repository 方法並寫入稽核紀錄（C-4），不是用這裡繞過擁有者檢查。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from app.models.base import utcnow
from app.models.user import ROLE_ADMIN, STATUS_ACTIVE
from app.observability import security_events
from app.services.age_policy import DEFAULT_AGE_POLICY, AgePolicy, age_on, taipei_today
from app.services.current_user import CurrentUser
from app.services.errors import AgeConfirmationRequired, AgeRestricted, Forbidden, NotFound

# 成年者的內容上限：sexual_content_level 3（成年角色之間合意的露骨內容）在取得模型供應商的書面許可與法務意見之前，
# 程式上不得開啟（AGENTS.md 第 7 節），所以上限是 2。要開啟 3 必須同時修改規範與這個常數。
MAX_ADULT_SEXUAL_CONTENT_LEVEL = 2
SAFE_MODE_SEXUAL_CONTENT_LEVEL = 0


@dataclass(frozen=True)
class ContentCeiling:
    """目前這位使用者能使用的內容上限。`safe_mode` 為真時介面須顯示安全模式的提示。"""

    sexual_content_level: int
    safe_mode: bool


class AccessPolicy:
    def __init__(
        self,
        *,
        email_verification_required: bool,
        age_policy: AgePolicy = DEFAULT_AGE_POLICY,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._verification_required = email_verification_required
        self._age_policy = age_policy
        self._clock = clock

    def is_admin(self, user: CurrentUser) -> bool:
        """只有角色為 admin 且帳號啟用中才是管理員。"""
        return user.role == ROLE_ADMIN and user.status == STATUS_ACTIVE

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
        """內容上限。尚未完成年齡確認者保守地視為安全模式（他們本來也不能使用 AI 功能）。"""
        age = self._age(user)
        if age is None or age < self._age_policy.adult_age:
            return ContentCeiling(SAFE_MODE_SEXUAL_CONTENT_LEVEL, safe_mode=True)
        return ContentCeiling(MAX_ADULT_SEXUAL_CONTENT_LEVEL, safe_mode=False)

    # ---- 是否可使用 AI 功能 -------------------------------------------------

    def _account_usable(self, user: CurrentUser) -> bool:
        """帳號必須啟用中；信箱驗證開啟時，信箱還必須已驗證（防止未驗證的帳號消耗 Gemini 額度）。"""
        if user.status != STATUS_ACTIVE:
            return False
        return user.email_verified or not self._verification_required

    def can_use_chat(self, user: CurrentUser) -> bool:
        """是否可使用客服與其他會呼叫 Gemini 的功能：帳號可用、已完成年齡確認，且年齡不低於最低使用年齡。

        年齡閘門不看角色與會員等級：管理員與各等級都一樣要先完成。
        """
        if not self._account_usable(user):
            return False
        age = self._age(user)
        return age is not None and age >= self._age_policy.min_use_age

    def require_chat_access(self, user: CurrentUser) -> None:
        """每個會呼叫 Gemini 的端點在做任何事之前先呼叫這裡；不允許時拋出錯誤。

        先檢查帳號狀態與信箱驗證，再檢查年齡：前兩者不允許時，補填年齡也沒有幫助，不該引導使用者去補。
        年齡的兩種拒絕不同：尚未完成確認 → `AgeConfirmationRequired`（前端引導到帳號設定補填）；
        年齡低於最低使用年齡 → `AgeRestricted`（補填也沒有用，所以不引導）。兩者都記錄一筆資訊層級的事件。
        """
        if not self._account_usable(user):
            raise Forbidden()
        age = self._age(user)
        if age is None:
            security_events.log_event(security_events.AGE_GATE_DENIED, user_id=user.id)
            raise AgeConfirmationRequired()
        if age < self._age_policy.min_use_age:
            security_events.log_event(security_events.AGE_GATE_DENIED, user_id=user.id)
            raise AgeRestricted()
