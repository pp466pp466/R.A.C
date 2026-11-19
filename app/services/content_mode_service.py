"""玩家自己的安全模式：查詢與切換（見 content-tier-modes 規格、AGENTS.md 第 7 節）。

- 內容模式只有兩種：`safe`（安全模式開，預設）與 `adult`（安全模式關：成年角色之間合意的露骨內容）。全站只限滿 18 歲者使用，
  所以沒有依年齡設定的上限，也不再要求「我確定已滿 18 歲」的勾選；偏好不會因任何輸入被自動調高，只有玩家自己切換才會改變。
- 關閉安全模式（`adult`）的條件只有：已完成年齡確認（`require_chat_access`）、緊急煞車未啟用（否則 `SafeModeForced`，403）、
  並在**目前版本**的安全規範下同意（`confirm_rules` 為明確的 `true`，字串或數字不算；缺少時 400 並指出 `confirm_rules`）。
  同意成功時記錄確認時間、規範版本與方式（目前為自行聲明，不蒐集證件、不接第三方驗證）；已在目前版本同意的玩家再次關閉不需要重新勾選；
  規範版本改變後先前的同意失效、須重新同意；回到 `safe` 不清除紀錄。切回 `safe` 永遠可以，緊急煞車時也一樣。
- 回應只含偏好、生效模式（不含劇本）、是否已在目前版本同意與緊急煞車是否啟用；**不含**生日、年齡區間、年齡確認時間、
  同意的時間戳記或內部編號。變更成功只記錄事件名稱與使用者編號（事件名稱本身表達新的模式），不含任何輸入。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from app.models.user import CONTENT_MODES, CONTENT_TIER_ADULT, CONTENT_TIER_SAFE
from app.observability import security_events
from app.repositories.user_repository import UserRepository
from app.services.access_policy import AccessPolicy, ModeState
from app.services.content_tier import ADULT_CONFIRM_SELF_DECLARED, SAFE_RULES_VERSION
from app.services.current_user import CurrentUser
from app.services.errors import SafeModeForced, Unauthenticated, ValidationError

MODE_INVALID_MESSAGE = "內容模式只能是 safe（安全模式開）或 adult（安全模式關）。"
CONFIRM_RULES_MESSAGE = "請勾選同意安全規範。"

_EVENT_FOR_MODE = {
    CONTENT_TIER_SAFE: security_events.CONTENT_MODE_SAFE,
    CONTENT_TIER_ADULT: security_events.CONTENT_MODE_ADULT,
}


class ContentModeService:
    def __init__(self, *, users: UserRepository, policy: AccessPolicy, clock: Callable[[], datetime]) -> None:
        self._users = users
        self._policy = policy
        self._clock = clock

    def get_state(self, current: CurrentUser) -> dict[str, Any]:
        """目前的安全模式狀態。先過年齡閘門：尚未完成年齡確認的管理員得到 `age_confirmation_required`
        （玩家在更早的登入檢查就被導向年齡確認頁）。"""
        self._policy.require_chat_access(current)
        return state_of(self._policy.mode_state(current))

    def set_mode(self, current: CurrentUser, mode: object, *, confirm_rules: object = None) -> dict[str, Any]:
        """把偏好設為 `mode`，回傳最新的狀態。偏好不會因任何輸入被自動調高；`standard` 等其他值是 400，偏好不變。"""
        self._policy.require_chat_access(current)
        if not isinstance(mode, str) or mode not in CONTENT_MODES:
            raise ValidationError(fields={"mode": MODE_INVALID_MESSAGE})

        user = self._users.get_by_id(current.id)
        if user is None:
            raise Unauthenticated()
        if mode == CONTENT_TIER_ADULT:
            if self._policy.safe_mode_forced:
                raise SafeModeForced()
            if not self._policy.rules_confirmed(current):
                if confirm_rules is not True:
                    raise ValidationError(fields={"confirm_rules": CONFIRM_RULES_MESSAGE})
                self._users.record_adult_confirmation(
                    user, confirmed_at=self._clock(), rules_version=SAFE_RULES_VERSION, method=ADULT_CONFIRM_SELF_DECLARED
                )
        if not self._users.set_content_preference(user, mode):
            raise Unauthenticated()                  # 條件式更新沒有寫入：帳號已不存在或尚未完成年齡確認
        security_events.log_event(_EVENT_FOR_MODE[mode], user_id=current.id)
        return state_of(self._policy.mode_state(CurrentUser.from_model(user)))


def state_of(state: ModeState) -> dict[str, Any]:
    """內容模式 API 的回應內容（只含列舉值與布林，沒有任何個資）。"""
    return {
        "preference": state.preference,
        "effective": state.effective,
        "confirmed": state.confirmed,
        "forced": state.forced,
    }
