"""權限判斷的唯一出處（見 AGENTS.md 第 10 節）。

端點與其他 Service 不自行寫「是不是擁有者」「是不是管理員」，一律問這裡：
- 非擁有者存取資源 → `NotFound`（404），與資源真的不存在完全相同，避免洩漏資源是否存在；
- 授權不依賴識別碼的不可猜測性（即使知道別人的 public_id 也一樣被擋下）；
- 會員等級（free、plus、vip）只影響額度，**不參與任何權限判斷**——這個類別刻意不讀 `member_tier`；
- 會呼叫 Gemini 的功能一律先問 `require_chat_access`：帳號可用、且已確認年滿 18 歲（管理員與各等級都不豁免）。
管理員存取他人資料必須走專用的 Repository 方法並寫入稽核紀錄（C-4），不是用這裡繞過擁有者檢查。
"""
from __future__ import annotations

from app.models.user import ROLE_ADMIN, STATUS_ACTIVE
from app.observability import security_events
from app.services.current_user import CurrentUser
from app.services.errors import AgeConfirmationRequired, Forbidden, NotFound


class AccessPolicy:
    def __init__(self, *, email_verification_required: bool) -> None:
        self._verification_required = email_verification_required

    def is_admin(self, user: CurrentUser) -> bool:
        """只有角色為 admin 且帳號啟用中才是管理員。"""
        return user.role == ROLE_ADMIN and user.status == STATUS_ACTIVE

    def is_owner(self, user: CurrentUser, owner_id: int | None) -> bool:
        return owner_id is not None and user.id == owner_id

    def require_owner(self, user: CurrentUser, owner_id: int | None) -> None:
        """不是擁有者就拋出 `NotFound`，不是 `Forbidden`：呼叫端無從分辨「別人的」與「不存在的」。"""
        if not self.is_owner(user, owner_id):
            raise NotFound()

    def _account_usable(self, user: CurrentUser) -> bool:
        """帳號必須啟用中；信箱驗證開啟時，信箱還必須已驗證（防止未驗證的帳號消耗 Gemini 額度）。"""
        if user.status != STATUS_ACTIVE:
            return False
        return user.email_verified or not self._verification_required

    def can_use_chat(self, user: CurrentUser) -> bool:
        """是否可使用客服與其他會呼叫 Gemini 的功能：帳號可用，且已確認年滿 18 歲。

        年齡閘門不看角色與會員等級：管理員與各等級都一樣要先確認。
        """
        return self._account_usable(user) and user.age_confirmed

    def require_chat_access(self, user: CurrentUser) -> None:
        """每個會呼叫 Gemini 的端點在做任何事之前先呼叫這裡；不允許時拋出錯誤。

        先檢查帳號狀態與信箱驗證，再檢查年齡：前兩者不允許時，補確認年齡也沒有幫助，不該引導使用者去補。
        只有年齡不足時才拋出 `AgeConfirmationRequired`（前端據此引導到帳號設定），並記錄一筆資訊層級的事件。
        """
        if not self._account_usable(user):
            raise Forbidden()
        if not user.age_confirmed:
            security_events.log_event(security_events.AGE_GATE_DENIED, user_id=user.id)
            raise AgeConfirmationRequired()
