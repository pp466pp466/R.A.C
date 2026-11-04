"""玩家自己的內容模式：查詢與切換（見 content-tier-modes 規格、AGENTS.md 第 7 節）。

- 玩家在個人頁面決定自己的內容模式偏好（安全、一般、成人）：預設最保守，成年玩家也可以自行選擇安全模式；
  滿 18 歲不會自動調高，介面提示玩家自行切換（查詢的回應讓介面能判斷「現在可以切換為一般模式」）。
- 能不能用哪一種模式由 `AccessPolicy` 依生日於當下計算的年齡決定；這裡不內嵌年齡數字。未滿成年門檻者只能選安全模式，
  要求其他模式得到 `ContentModeRestricted`（403）。
- 成人模式須同時滿足：部署開關開啟（否則 `AdultModeUnavailable`，403）、已成年、在目前的安全規範版本下完成成年確認。
  成年確認發生在切換為成人模式的當下：玩家須同時確認「我確定已滿 18 歲」與「同意安全規範」兩項，缺一得到 400 並指出缺哪一項；
  記錄確認時間、規範版本與方式（目前為自行聲明，不蒐集證件、不接第三方驗證）。規範版本改變後先前的確認失效、須重新確認；
  離開成人模式不清除確認紀錄。
- 回應只含偏好、生效模式、各模式是否可選、成人模式沒有啟用的原因與是否已完成目前版本的確認；**不含**生日、年齡區間、
  年齡確認時間或內部編號。變更成功只記錄事件名稱與使用者編號（事件名稱本身表達新的模式），不含任何輸入。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from app.models.user import CONTENT_TIER_ADULT, CONTENT_TIER_SAFE, CONTENT_TIER_STANDARD, CONTENT_TIERS
from app.observability import security_events
from app.repositories.user_repository import UserRepository
from app.services.access_policy import AccessPolicy, ContentAccess
from app.services.content_tier import ADULT_CONFIRM_SELF_DECLARED, ADULT_RULES_VERSION
from app.services.current_user import CurrentUser
from app.services.errors import AdultModeUnavailable, ContentModeRestricted, Unauthenticated, ValidationError

MODE_INVALID_MESSAGE = "內容模式只能是安全模式、一般模式或成人模式。"
CONFIRM_ADULT_MESSAGE = "請勾選「我確定已滿 18 歲」。"
CONFIRM_RULES_MESSAGE = "請勾選同意安全規範。"

_EVENT_FOR_MODE = {
    CONTENT_TIER_SAFE: security_events.CONTENT_MODE_SAFE,
    CONTENT_TIER_STANDARD: security_events.CONTENT_MODE_STANDARD,
    CONTENT_TIER_ADULT: security_events.CONTENT_MODE_ADULT,
}


class ContentModeService:
    def __init__(self, *, users: UserRepository, policy: AccessPolicy, clock: Callable[[], datetime]) -> None:
        self._users = users
        self._policy = policy
        self._clock = clock

    def get_state(self, current: CurrentUser) -> dict[str, Any]:
        """目前的內容模式狀態。先過年齡閘門：尚未完成年齡確認的管理員得到 `age_confirmation_required`
        （玩家在更早的登入檢查就被導向年齡確認頁）。"""
        self._policy.require_chat_access(current)
        return state_of(self._policy.content_access(current))

    def set_mode(
        self, current: CurrentUser, mode: object, *, confirm_adult: object = None, confirm_rules: object = None
    ) -> dict[str, Any]:
        """把偏好設為 `mode`，回傳最新的狀態。偏好只能在年齡上限之內調整，也不會因任何輸入被自動調高。"""
        self._policy.require_chat_access(current)
        if not isinstance(mode, str) or mode not in CONTENT_TIERS:
            raise ValidationError(fields={"mode": MODE_INVALID_MESSAGE})

        user = self._users.get_by_id(current.id)
        if user is None:
            raise Unauthenticated()
        access = self._policy.content_access(current)
        if mode != CONTENT_TIER_SAFE and not access.is_adult:
            raise ContentModeRestricted()
        if mode == CONTENT_TIER_ADULT:
            if not self._policy.adult_mode_enabled:
                raise AdultModeUnavailable()
            if not access.adult_confirmed:
                self._require_confirmations(confirm_adult, confirm_rules)
                self._users.record_adult_confirmation(
                    user, confirmed_at=self._clock(), rules_version=ADULT_RULES_VERSION, method=ADULT_CONFIRM_SELF_DECLARED
                )
        if not self._users.set_content_preference(user, mode):
            raise Unauthenticated()                  # 條件式更新沒有寫入：帳號已不存在或尚未完成年齡確認
        security_events.log_event(_EVENT_FOR_MODE[mode], user_id=current.id)
        return state_of(self._policy.content_access(CurrentUser.from_model(user)))

    @staticmethod
    def _require_confirmations(confirm_adult: object, confirm_rules: object) -> None:
        """兩項都必須是明確的 `true`（字串或數字不算）；缺哪一項就指出哪一項。"""
        missing: dict[str, str] = {}
        if confirm_adult is not True:
            missing["confirm_adult"] = CONFIRM_ADULT_MESSAGE
        if confirm_rules is not True:
            missing["confirm_rules"] = CONFIRM_RULES_MESSAGE
        if missing:
            raise ValidationError(fields=missing)


def state_of(access: ContentAccess) -> dict[str, Any]:
    """內容模式 API 的回應內容（只含列舉值與布林，沒有任何個資）。"""
    return {
        "preference": access.preference,
        "effective": access.effective,
        "selectable": {
            CONTENT_TIER_SAFE: True,
            CONTENT_TIER_STANDARD: access.is_adult,
            CONTENT_TIER_ADULT: access.adult_selectable,
        },
        "adult_reason": access.reason,               # null、disabled、not_adult 或 needs_confirmation（成人模式沒有啟用的原因）
        "adult_confirmed": access.adult_confirmed,
    }
