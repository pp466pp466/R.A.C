"""各會員等級的用量限制，以及「今天」的界線（見 cs-usage-limits 規格與 AGENTS.md 第 7、10 節）。

限制的數值只有這一份：`UsageService` 用它判斷、客服 system prompt 用它說明額度，兩者因此不會分歧。
數值是 AGENTS.md 第 7 節的規格（不是部署差異），所以是程式常數而不是環境變數；會隨模型與方案變動的
全站上限才是環境變數（`USAGE_DAILY_TOKEN_LIMIT`、`USAGE_DAILY_CALL_LIMIT`，見 `app/config.py`）。

會員等級只影響額度、不影響權限，所以這個模組只被 `UsageService` 與提示詞使用，`AccessPolicy` 不匯入它。
日界一律以 Asia/Taipei 計（台灣沒有夏令時間，固定 UTC+8 即正確，沿用 `age_policy.TAIPEI`），資料庫存 UTC。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.models.user import MEMBER_TIERS, TIER_FREE, TIER_PLUS, TIER_VIP
from app.services.age_policy import TAIPEI


@dataclass(frozen=True)
class TierLimits:
    daily_turns: int         # 每日對話輪數（一則使用者訊息加一則正常的 AI 回覆）
    per_minute: int          # 每分鐘可被接受的訊息數
    conversations: int       # 對話數上限


TIER_LIMITS: dict[str, TierLimits] = {
    TIER_FREE: TierLimits(daily_turns=30, per_minute=6, conversations=20),
    TIER_PLUS: TierLimits(daily_turns=100, per_minute=10, conversations=100),
    TIER_VIP: TierLimits(daily_turns=300, per_minute=20, conversations=500),
}

RATE_WINDOW = timedelta(seconds=60)

# 同一位使用者一日（台灣日）內對話呼叫被內容安全過濾阻擋達這個次數，即暫停對話一段時間（AGENTS.md 第 10 節）
SAFETY_BLOCK_LIMIT = 3
SAFETY_SUSPENSION = timedelta(hours=1)

# 全站每日用量達上限的這個比例時記錄警告（AGENTS.md 第 10 節：達 80% 告警）
BUDGET_WARNING_RATIO = 0.8

assert tuple(TIER_LIMITS) == MEMBER_TIERS, "每個會員等級都必須有限制"


def limits_for(tier: object) -> TierLimits:
    """該等級的限制；不認得的等級（資料異常）套用最保守的 free，而不是放行。"""
    return TIER_LIMITS.get(tier, TIER_LIMITS[TIER_FREE]) if isinstance(tier, str) else TIER_LIMITS[TIER_FREE]


def tier_facts() -> str:
    """給客服 system prompt 的額度說明，數字全部來自 `TIER_LIMITS`。"""
    def by_tier(field: str) -> str:
        return "、".join(f"{tier} {getattr(limits, field)}" for tier, limits in TIER_LIMITS.items())

    return (
        f"會員等級的額度——每日對話輪數：{by_tier('daily_turns')}；每分鐘訊息數：{by_tier('per_minute')}；"
        f"對話數上限：{by_tier('conversations')}；單則訊息上限一律是 1000 字。"
    )


def _as_utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


def taipei_day_bounds(now: datetime) -> tuple[datetime, datetime]:
    """`now` 所在的台灣日的起訖（含起、不含訖），以帶時區的 UTC 表示；前一台灣日的 16:00 UTC 是台灣 00:00。"""
    local = _as_utc(now).astimezone(TAIPEI)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)
