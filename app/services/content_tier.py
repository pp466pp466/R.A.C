"""內容模式的順序與比較（content-tier-modes 規格、AGENTS.md 第 7 節）。

三種模式由保守到寬鬆：`safe`（安全模式）、`standard`（一般模式）、`adult`（成人模式）。這裡是**純邏輯**：沒有資料庫、
設定與時間的依賴，也不內嵌任何年齡數字——「誰可以用哪一種模式」由 `AccessPolicy` 依生日於當下計算的年齡決定，
這裡只回答「哪一個比較保守」。

- 兩個模式「取較保守者」一律以這裡的順序決定（`most_conservative`）；
- 無法判定（缺少、空字串或不是三種之一）一律視為最保守的 `safe`（`parse_tier`），不拋出錯誤：
  預設最保守是安全模式的底線，寧可少給也不要因為髒資料而放寬。
"""
from __future__ import annotations

from typing import Any

from app.models.user import (
    ADULT_CONFIRM_SELF_DECLARED,
    CONTENT_TIER_ADULT,
    CONTENT_TIER_SAFE,
    CONTENT_TIER_STANDARD,
    CONTENT_TIERS,
)

__all__ = [
    "ADULT_CONFIRM_SELF_DECLARED",
    "ADULT_RULES_VERSION",
    "CONTENT_TIERS",
    "CONTENT_TIER_ADULT",
    "CONTENT_TIER_SAFE",
    "CONTENT_TIER_STANDARD",
    "is_tier",
    "most_conservative",
    "parse_tier",
    "tier_rank",
]

# 成人模式的「安全規範」版本。玩家切換為成人模式時勾選同意的是這一版；改版後所有人的先前確認視為失效、須重新確認
# （規格：安全規範的版本改變後，先前的確認視為失效）。規範內容由法務覆核後才會改版，改版時必須一併更新對外的規範頁面。
ADULT_RULES_VERSION = "2026-11-03-draft-1"

_RANK = {tier: rank for rank, tier in enumerate(CONTENT_TIERS)}


def is_tier(value: Any) -> bool:
    """是不是三種內容模式之一（只認小寫的列舉值，不做任何寬鬆比對）。"""
    return isinstance(value, str) and value in _RANK


def parse_tier(value: Any) -> str:
    """把任意輸入轉為三種模式之一；缺少或不合法一律回傳最保守的 `safe`。"""
    return value if is_tier(value) else CONTENT_TIER_SAFE


def tier_rank(value: Any) -> int:
    """順序值：`safe` 0、`standard` 1、`adult` 2；無法判定視為 `safe`（0）。"""
    return _RANK[parse_tier(value)]


def most_conservative(*tiers: Any) -> str:
    """取最保守的一個。沒有任何輸入、或任一輸入無法判定，結果都是 `safe`。"""
    if not tiers:
        return CONTENT_TIER_SAFE
    return min((parse_tier(tier) for tier in tiers), key=_RANK.__getitem__)
