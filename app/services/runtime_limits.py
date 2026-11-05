"""存放在資料庫的營運限制（見 runtime-limits 規格與 rpg-script-authoring 的 design D10）。

十個限制：每位作者可擁有的劇本數（依會員等級）、每個台灣日的送審次數（依會員等級）、每個台灣日的主角生成與重新生成次數
（依會員等級，rpg-character-creation）、連續被自動拒絕幾次就暫停發佈。
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


@dataclass(frozen=True)
class LimitSpec:
    key: str
    default: int
    minimum: int
    maximum: int


def _per_tier(prefix: str, defaults: Mapping[str, int], minimum: int, maximum: int) -> dict[str, LimitSpec]:
    return {f"{prefix}.{tier}": LimitSpec(f"{prefix}.{tier}", defaults[tier], minimum, maximum) for tier in MEMBER_TIERS}


LIMIT_SPECS: dict[str, LimitSpec] = {
    **_per_tier(SCRIPT_LIMIT_PREFIX, {TIER_FREE: 3, TIER_PLUS: 10, TIER_VIP: 30}, 0, 500),
    **_per_tier(SUBMIT_DAILY_PREFIX, {TIER_FREE: 3, TIER_PLUS: 5, TIER_VIP: 10}, 0, 100),
    **_per_tier(SUGGEST_DAILY_PREFIX, {TIER_FREE: 5, TIER_PLUS: 15, TIER_VIP: 40}, 0, 100),
    REJECT_STREAK_KEY: LimitSpec(REJECT_STREAK_KEY, 3, 1, 20),
}
LIMIT_KEYS = tuple(LIMIT_SPECS)

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

    def rows(self) -> list[LimitRow]:
        """全部限制的目前狀態（一次查詢），給 `show-script-limits` 使用。"""
        stored = self._limits.all_values()
        rows = []
        for spec in LIMIT_SPECS.values():
            value = self._effective(spec, stored.get(spec.key), warn=False)
            valid = spec.key in stored and self._valid(spec, stored[spec.key])
            rows.append(LimitRow(spec.key, value, valid, spec.default, spec.minimum, spec.maximum))
        return rows

    # ---- 修改（只由 flask 指令使用）----------------------------------------------------

    def set(self, key: str, value: object) -> tuple[int, int]:
        """驗證後寫入並留下稽核；回傳（修改前實際生效的值, 新值）。鍵或值不合法拋出 `LimitError`。"""
        spec = LIMIT_SPECS.get(key)
        if spec is None:
            raise LimitError(f"未知的限制：{key}。可用的限制：{'、'.join(LIMIT_KEYS)}")
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
