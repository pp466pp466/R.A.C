"""玩家自己的故事偏好：查詢與更新（見 story-preference 規格、AGENTS.md 第 7 節）。

- 偏好只有 `all`（全讀者）、`male`（男頻）、`female`（女頻），預設全讀者；**只影響劇本清單的排序**，不影響內容分級、
  可看或可玩的劇本、敘事、提示詞、遊戲幣與額度。所以這裡不碰 `AccessPolicy` 的內容分級，也不要求通過年齡閘門
  （年齡閘門檢查的是「能不能呼叫模型」；玩家未完成年齡確認時，本來就在更早的登入檢查被擋下）。
- 輸入必須是**完全等於**列舉值的字串（不容忍大小寫、空白、數字或 `null`），否則 400 並指出 `story_preference` 欄位。
- 更新為冪等：設成目前的值同樣成功。只讀寫呼叫者本人的資料列，請求中沒有任何可指定他人的欄位。
- 回應只有列舉值，沒有任何個資。成功只記事件名稱與使用者編號，不含值。
"""
from __future__ import annotations

from app.observability import security_events
from app.repositories.user_repository import UserRepository
from app.services.current_user import CurrentUser
from app.services.errors import Unauthenticated, ValidationError
from app.services.story_preference import normalize_audience, parse_audience

PREFERENCE_INVALID_MESSAGE = "故事偏好只能是全讀者、男頻或女頻。"


class StoryPreferenceService:
    def __init__(self, *, users: UserRepository) -> None:
        self._users = users

    def get(self, current: CurrentUser) -> str:
        """目前的偏好。從資料庫讀取（不用登入快照），缺值視為全讀者。"""
        user = self._users.get_by_id(current.id)
        if user is None:
            raise Unauthenticated()
        return normalize_audience(user.story_preference)

    def set(self, current: CurrentUser, value: object) -> str:
        """把偏好設為 `value`，回傳更新後的偏好。不合法的值不改動任何東西。"""
        preference = parse_audience(value)
        if preference is None:
            raise ValidationError(fields={"story_preference": PREFERENCE_INVALID_MESSAGE})
        user = self._users.get_by_id(current.id)
        if user is None or not self._users.set_story_preference(user, preference):
            raise Unauthenticated()                  # 帳號已不存在
        security_events.log_event(security_events.STORY_PREFERENCE_CHANGED, user_id=current.id)
        return preference
