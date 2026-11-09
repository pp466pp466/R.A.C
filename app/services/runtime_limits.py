"""存放在資料庫的營運限制（見 runtime-limits 規格與 rpg-script-authoring 的 design D10）。

兩組共十五個限制。**劇本與主角組**（十個）：每位作者可擁有的劇本數（依會員等級）、每個台灣日的送審次數（依會員等級）、
每個台灣日的主角生成與重新生成次數（依會員等級，rpg-character-creation）、連續被自動拒絕幾次就暫停發佈。
**遊戲幣組**（五個，game-wallet）：每輪幣價（以模型鍵區分）、首次註冊贈幣、每日簽到、免費幣上限、預扣逾時秒數。
兩組由各自的指令修改（`set-script-limit`／`set-wallet-setting`），指令只接受自己那一組的鍵。
- **每次使用時現讀**，沒有模組層快取、也不放進行程內變數（AGENTS.md 第 9 節）：改了資料庫，下一個請求就用新值，多個 worker 一致。
- 缺值、不是整數或在合法範圍外時使用內建的保底預設（與遷移 `0013` 寫入的初始值相同，由測試比對），並記一筆只含鍵名的警告
  事件；不會因此放行無上限，也不會讓創作與送審全部失敗。不認得的會員等級套用最保守的 free（同 `quota_policy`）。
- 只有部署者能以 flask 指令修改（`set-script-limit`），每次寫一筆 `limit.set` 稽核（管理員欄位為空，細節只有鍵與新舊數值）；
  沒有任何 HTTP 端點可以寫入。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

from app.models.admin_audit_log import ACTION_LIMIT_SET
from app.models.user import MEMBER_TIERS, TIER_FREE, TIER_PLUS, TIER_VIP
from app.observability import script_events
from app.repositories.runtime_limit_repository import RuntimeLimitRepository

SCRIPT_LIMIT_PREFIX = "script_limit"                 # 每位作者可擁有的劇本數
SUBMIT_DAILY_PREFIX = "script_submit_daily"          # 每個台灣日的送審次數
SUGGEST_DAILY_PREFIX = "protagonist_suggest_daily"   # 每個台灣日的主角生成與重新生成次數
REJECT_STREAK_KEY = "script_reject_streak"           # 連續被自動拒絕幾次就暫停發佈

GROUP_SCRIPT = "script"                              # 劇本與主角組：set-script-limit／show-script-limits
GROUP_WALLET = "wallet"                              # 遊戲幣組：set-wallet-setting／show-wallet-settings
GROUPS = (GROUP_SCRIPT, GROUP_WALLET)
GROUP_COMMANDS = {GROUP_SCRIPT: "flask set-script-limit", GROUP_WALLET: "flask set-wallet-setting"}

COIN_TURN_COST_PREFIX = "coin.turn_cost"             # 每輪幣價，後面接模型鍵
COIN_SIGNUP_GRANT_KEY = "coin.signup_grant"          # 首次註冊贈幣（免費幣）
COIN_CHECKIN_GRANT_KEY = "coin.checkin_grant"        # 每日簽到（免費幣）
COIN_FREE_CAP_KEY = "coin.free_cap"                  # 免費幣上限（只限制新發放）
COIN_HOLD_TIMEOUT_KEY = "coin.hold_timeout_seconds"  # 預扣逾時秒數：超過仍未結算者在下次存取錢包時退回
# 可以計價的模型鍵（常數，不是設定：新增模型是程式與設定表一起新增的事件，見 W3）
WALLET_MODEL_KEYS = ("standard",)


@dataclass(frozen=True)
class LimitSpec:
    key: str
    default: int
    minimum: int
    maximum: int
    group: str = GROUP_SCRIPT


def _per_tier(prefix: str, defaults: Mapping[str, int], minimum: int, maximum: int) -> dict[str, LimitSpec]:
    return {f"{prefix}.{tier}": LimitSpec(f"{prefix}.{tier}", defaults[tier], minimum, maximum) for tier in MEMBER_TIERS}


LIMIT_SPECS: dict[str, LimitSpec] = {
    **_per_tier(SCRIPT_LIMIT_PREFIX, {TIER_FREE: 3, TIER_PLUS: 10, TIER_VIP: 30}, 0, 500),
    **_per_tier(SUBMIT_DAILY_PREFIX, {TIER_FREE: 3, TIER_PLUS: 5, TIER_VIP: 10}, 0, 100),
    **_per_tier(SUGGEST_DAILY_PREFIX, {TIER_FREE: 5, TIER_PLUS: 15, TIER_VIP: 40}, 0, 100),
    REJECT_STREAK_KEY: LimitSpec(REJECT_STREAK_KEY, 3, 1, 20),
    **{
        f"{COIN_TURN_COST_PREFIX}.{model}": LimitSpec(f"{COIN_TURN_COST_PREFIX}.{model}", 30, 1, 10_000, GROUP_WALLET)
        for model in WALLET_MODEL_KEYS
    },
    COIN_SIGNUP_GRANT_KEY: LimitSpec(COIN_SIGNUP_GRANT_KEY, 200, 0, 100_000, GROUP_WALLET),
    COIN_CHECKIN_GRANT_KEY: LimitSpec(COIN_CHECKIN_GRANT_KEY, 200, 0, 100_000, GROUP_WALLET),
    COIN_FREE_CAP_KEY: LimitSpec(COIN_FREE_CAP_KEY, 1000, 0, 1_000_000, GROUP_WALLET),
    COIN_HOLD_TIMEOUT_KEY: LimitSpec(COIN_HOLD_TIMEOUT_KEY, 180, 60, 1800, GROUP_WALLET),
}
LIMIT_KEYS = tuple(LIMIT_SPECS)
SCRIPT_LIMIT_KEYS = tuple(key for key, spec in LIMIT_SPECS.items() if spec.group == GROUP_SCRIPT)
WALLET_LIMIT_KEYS = tuple(key for key, spec in LIMIT_SPECS.items() if spec.group == GROUP_WALLET)

# 寫稽核的函式：(動作, 目標使用者的對外識別碼或 None, 細節)。由 registry 提供，讓這個 Service 不直接依賴後台專用的 Repository
AuditSink = Callable[[str, "str | None", Mapping[str, Any]], None]


class LimitError(ValueError):
    """修改限制時的鍵或值不合法（訊息為繁體中文，可直接顯示給部署者）。"""


@dataclass(frozen=True)
class LimitRow:
    key: str
    value: int              # 目前實際生效的值（資料庫中合法的值，否則保底預設）
    from_database: bool     # False 表示資料庫缺值或不合法，目前用的是保底預設
    default: int
    minimum: int
    maximum: int


class RuntimeLimits:
    def __init__(
        self, *, limits: RuntimeLimitRepository, clock: Callable[[], datetime], audit: AuditSink | None = None
    ) -> None:
        self._limits = limits
        self._clock = clock
        self._audit = audit

    # ---- 讀取（每次都問資料庫）-------------------------------------------------------

    def get(self, key: str) -> int:
        spec = LIMIT_SPECS.get(key)
        if spec is None:
            raise LimitError(f"未知的限制：{key}")
        return self._effective(spec, self._limits.get_value(key))

    def script_limit(self, tier: object) -> int:
        """該會員等級可擁有的劇本數；不認得的等級套用 free。"""
        return self.get(f"{SCRIPT_LIMIT_PREFIX}.{_tier_or_free(tier)}")

    def submit_daily(self, tier: object) -> int:
        """該會員等級每個台灣日可送審的次數；不認得的等級套用 free。"""
        return self.get(f"{SUBMIT_DAILY_PREFIX}.{_tier_or_free(tier)}")

    def suggest_daily(self, tier: object) -> int:
        """該會員等級每個台灣日可請 AI 生成主角的次數（含重新生成）；不認得的等級套用 free。"""
        return self.get(f"{SUGGEST_DAILY_PREFIX}.{_tier_or_free(tier)}")

    def reject_streak(self) -> int:
        return self.get(REJECT_STREAK_KEY)

    # ---- 遊戲幣（game-wallet）：每次都現讀 ------------------------------------------------

    def coin_turn_cost(self, model_key: object) -> int:
        """該模型鍵一輪的幣價。模型鍵不在 `WALLET_MODEL_KEYS` 內拋出 `LimitError`（呼叫端據此拒絕，不扣任何幣）。"""
        if not isinstance(model_key, str) or model_key not in WALLET_MODEL_KEYS:
            raise LimitError(f"未知的模型鍵：{model_key!r}")
        return self.get(f"{COIN_TURN_COST_PREFIX}.{model_key}")

    def coin_signup_grant(self) -> int:
        return self.get(COIN_SIGNUP_GRANT_KEY)

    def coin_checkin_grant(self) -> int:
        return self.get(COIN_CHECKIN_GRANT_KEY)

    def coin_free_cap(self) -> int:
        return self.get(COIN_FREE_CAP_KEY)

    def coin_hold_timeout(self) -> int:
        return self.get(COIN_HOLD_TIMEOUT_KEY)

    def rows(self, group: str | None = None) -> list[LimitRow]:
        """目前的限制狀態（一次查詢）；`group` 為 None 列全部，否則只列該組（`show-script-limits`、`show-wallet-settings` 使用）。"""
        stored = self._limits.all_values()
        rows = []
        for spec in LIMIT_SPECS.values():
            if group is not None and spec.group != group:
                continue
            value = self._effective(spec, stored.get(spec.key), warn=False)
            valid = spec.key in stored and self._valid(spec, stored[spec.key])
            rows.append(LimitRow(spec.key, value, valid, spec.default, spec.minimum, spec.maximum))
        return rows

    # ---- 修改（只由 flask 指令使用）----------------------------------------------------

    def set(self, key: str, value: object, *, group: str | None = None) -> tuple[int, int]:
        """驗證後寫入並留下稽核；回傳（修改前實際生效的值, 新值）。鍵或值不合法拋出 `LimitError`。

        `group` 不是 None 時只接受該組的鍵（指令各管一組）；鍵屬於另一組時，錯誤訊息指出該用哪個指令。"""
        spec = LIMIT_SPECS.get(key)
        if spec is None:
            available = LIMIT_KEYS if group is None else tuple(k for k, s in LIMIT_SPECS.items() if s.group == group)
            raise LimitError(f"未知的限制：{key}。可用的限制：{'、'.join(available)}")
        if group is not None and spec.group != group:
            raise LimitError(f"{key} 不屬於這個指令管轄的限制，請改用 {GROUP_COMMANDS[spec.group]}。")
        if isinstance(value, bool) or not isinstance(value, int):
            raise LimitError(f"{key} 的值必須是整數。")
        if not spec.minimum <= value <= spec.maximum:
            raise LimitError(f"{key} 的值必須在 {spec.minimum} 至 {spec.maximum} 之間。")
        if self._audit is None:
            raise RuntimeError("沒有設定稽核寫入函式，無法修改限制。")
        previous = self._effective(spec, self._limits.get_value(key), warn=False)
        self._limits.set_value(key, value, self._clock())
        self._audit(ACTION_LIMIT_SET, None, {"limit_key": key, "old_value": previous, "new_value": value})
        script_events.log_limit_changed(key=key)
        return previous, value

    # ---- 內部 ------------------------------------------------------------------------

    @staticmethod
    def _valid(spec: LimitSpec, value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and spec.minimum <= value <= spec.maximum

    def _effective(self, spec: LimitSpec, stored: object, *, warn: bool = True) -> int:
        if self._valid(spec, stored):
            return stored                                             # type: ignore[return-value]
        if warn:
            script_events.log_limit_invalid(key=spec.key)
        return spec.default


def _tier_or_free(tier: object) -> str:
    return tier if isinstance(tier, str) and tier in MEMBER_TIERS else TIER_FREE
