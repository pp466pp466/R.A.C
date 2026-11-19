"""權限判斷的唯一出處（見 AGENTS.md 第 10 節）。

端點與其他 Service 不自行寫「是不是擁有者」「是不是管理員」，一律問這裡：
- 非擁有者存取資源 → `NotFound`（404），與資源真的不存在完全相同，避免洩漏資源是否存在；
- 授權不依賴識別碼的不可猜測性（即使知道別人的 public_id 也一樣被擋下）；
- 會員等級（free、plus、vip）只影響額度，**不參與任何權限判斷**——這個類別刻意不讀 `member_tier`；
- 會呼叫 Gemini 的功能一律先問 `require_chat_access`：帳號可用（狀態為 active）、已完成年齡確認、且年齡不低於最低使用年齡
  （管理員與各等級都不豁免）；
- 內容模式（`effective_mode`，見 AGENTS.md 第 7 節與 content-tier-modes 規格）只有兩種：`safe`（安全模式開）與 `adult`
  （安全模式關）。生效的模式每次現算，是玩家偏好（缺少目前安全規範版本下的同意時視為 `safe`）、劇本是否綁定安全模式、
  緊急煞車三者中最保守的一個；全站只限滿 18 歲者使用，不再有依年齡設定的上限，存檔也不釘住模式。管理員與各會員等級都不豁免。
  存檔的生效模式用 `save_mode`（劇本綁定取自複製進存檔的劇本）。年齡一律由生日於當下計算，年齡數字只來自 `AgePolicy`，
  這裡不內嵌任何年齡數字。
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
from app.services.content_tier import CONTENT_TIER_SAFE, SAFE_RULES_VERSION, most_conservative, parse_tier
from app.services.current_user import CurrentUser
from app.services.errors import AgeConfirmationRequired, AgeRestricted, Forbidden, NotFound, PublishSuspended


@dataclass(frozen=True)
class ModeState:
    """一位使用者目前的安全模式狀態（內容模式 API 的回應內容，只含列舉值與布林，不含劇本）。

    - `preference`：玩家存的偏好（`safe` 或 `adult`；舊值 `standard` 讀作 `adult`，無法判定為 `safe`）；
    - `effective`：生效模式（不含劇本）：偏好（缺少目前版本的同意時視為 `safe`）與緊急煞車中較保守者；
    - `confirmed`：已在目前的安全規範版本下完成同意；
    - `forced`：緊急煞車是否啟用。
    """

    preference: str
    effective: str
    confirmed: bool
    forced: bool


class AccessPolicy:
    def __init__(
        self,
        *,
        age_policy: AgePolicy = DEFAULT_AGE_POLICY,
        clock: Callable[[], datetime] = utcnow,
        safe_mode_forced: bool = False,
    ) -> None:
        if not isinstance(safe_mode_forced, bool):
            raise ValueError("safe_mode_forced 必須是布林值。")
        self._age_policy = age_policy
        self._clock = clock
        self._safe_mode_forced = safe_mode_forced

    def is_admin(self, user: CurrentUser) -> bool:
        """只有角色為 admin 且帳號啟用中才是管理員。"""
        return user.is_active_admin

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

    @property
    def adult_age(self) -> int:
        """成年門檻（來自年齡規則）；規則層判斷「小於成年門檻的年齡」時使用，不在別處寫死數字。"""
        return self._age_policy.adult_age

    # ---- 內容模式 -----------------------------------------------------------

    @property
    def safe_mode_forced(self) -> bool:
        """緊急煞車（由組裝處依環境設定 `SAFE_MODE_FORCED` 傳入；預設未啟用）：啟用時所有生效模式一律是 `safe`。"""
        return self._safe_mode_forced

    def rules_confirmed(self, user: CurrentUser) -> bool:
        """玩家是否已在**目前版本**的安全規範下完成同意；版本改變後先前的同意失效。"""
        return user.adult_confirmed_at is not None and user.adult_rules_version == SAFE_RULES_VERSION

    def effective_mode(self, user: CurrentUser, script_binding: str | None = None) -> str:
        """實際生效的內容模式，每次現算：玩家偏好、劇本是否綁定安全模式、緊急煞車三者中最保守的一個。

        - 偏好缺少目前版本的同意時視為 `safe`（不是錯誤）；舊值 `standard` 讀作 `adult`，仍須有同意才生效；
        - `script_binding` 是劇本（存檔複本）宣告的 `content_tier`：`safe` 為綁定、`adult`（含舊值 `standard`）為未綁定；
          沒有劇本時不傳（`None`，不參與），傳入了卻無法判定的值一律視為綁定，不會被忽略；
        - 緊急煞車啟用時一律是 `safe`；
        - 全站只限滿 18 歲者使用，這裡沒有年齡上限；管理員與各會員等級都不豁免。
        存檔不記錄、不釘住自己的模式：玩家調整設定後，下一次呼叫就是新的結果，也不會因任何輸入被調高。
        """
        if self._safe_mode_forced:
            return CONTENT_TIER_SAFE
        modes = [parse_tier(user.content_tier_preference) if self.rules_confirmed(user) else CONTENT_TIER_SAFE]
        if script_binding is not None:
            modes.append(script_binding)
        return most_conservative(*modes)

    def save_mode(self, user: CurrentUser, script_snapshot: object) -> str:
        """某個存檔目前的生效模式：`effective_mode`，劇本綁定取自**複製進存檔的劇本**（`script_snapshot_json` 的 `content_tier`），
        所以劇本日後的下架、改版或刪除不影響進行中的存檔；快照缺少或無法判定該欄位時視為綁定（取保守，不是忽略）。
        存檔本身不記錄也不釘住模式（`sessions.content_tier` 新版程式不讀）：玩家調整設定後，下一次呼叫就是新的結果。"""
        binding = script_snapshot.get("content_tier") if isinstance(script_snapshot, dict) else None
        return self.effective_mode(user, binding if binding is not None else "")

    def upgrade_hint_eligible(self, user: CurrentUser, script_snapshot: object) -> bool:
        """遊戲內「可以嘗試關閉安全模式」提示的前提（safe-mode-upgrade-hint）：玩家目前自己的設定是安全模式開（缺少目前版本的同意視為開，
        與個人頁的開關一致）、存檔的劇本**未綁定**安全模式（取自複製進存檔的快照；缺少或無法判定視為綁定，與 `save_mode` 一致）、
        緊急煞車未啟用。三者缺一不可：綁定的劇本關閉安全模式也不會改變它，緊急煞車時玩家無法關閉，已關閉的玩家不需要提示。
        只回答「有沒有資格被提示」，不看玩家輸入或模型輸出（觸發條件由規則層與審查層判定）。"""
        state = self.mode_state(user)
        binding = script_snapshot.get("content_tier") if isinstance(script_snapshot, dict) else None
        bound = binding is None or parse_tier(binding) == CONTENT_TIER_SAFE
        return state.effective == CONTENT_TIER_SAFE and not state.forced and not bound

    def mode_state(self, user: CurrentUser) -> ModeState:
        """個人頁與內容模式 API 用的狀態（不含劇本）。"""
        return ModeState(
            preference=parse_tier(user.content_tier_preference),
            effective=self.effective_mode(user),
            confirmed=self.rules_confirmed(user),
            forced=self._safe_mode_forced,
        )

    def require_can_submit(self, user: CurrentUser) -> None:
        """送審的權限：被暫停發佈（連續被自動拒絕）的玩家不能送審，其餘功能不受影響；管理員不會被暫停。"""
        if user.role != ROLE_ADMIN and user.publish_suspended_at is not None:
            raise PublishSuspended()

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
