"""內容模式的順序與比較（content-tier-modes 規格、AGENTS.md 第 7 節）。

內容模式只有兩種，由保守到寬鬆：`safe`（安全模式開）、`adult`（安全模式關）。這裡是**純邏輯**：沒有資料庫、
設定與時間的依賴，也不內嵌任何年齡數字——全站只限滿 18 歲者使用，沒有依年齡設定的上限，這裡只回答「哪一個比較保守」。

- 兩個模式「取較保守者」一律以這裡的順序決定（`most_conservative`）；
- 舊值 `standard`（原一般模式）一律讀作 `adult`（`parse_tier`），不論出現在玩家偏好、劇本內容或其他儲存的資料，
  所以既有資料**不必改寫**；系統不再寫入 `standard`；
- 無法判定（缺少、空字串或不是兩種之一）一律視為最保守的 `safe`（`parse_tier`），不拋出錯誤：
  預設最保守是安全模式的底線，寧可少給也不要因為髒資料而放寬。

檔案最下方的「過渡相容層」是 safe-mode-toggle 第 1 組與第 2 組之間的暫時產物（讓每個 commit 的測試都是綠的），
第 2 組的 2.5 移除並以守門測試鎖住；新程式請勿使用它。
"""
from __future__ import annotations

from typing import Any

from app.models.user import (
    ADULT_CONFIRM_SELF_DECLARED,
    CONTENT_MODES,
    CONTENT_TIER_ADULT,
    CONTENT_TIER_SAFE,
    CONTENT_TIER_STANDARD,
    CONTENT_TIERS,
)

__all__ = [
    "ADULT_CONFIRM_SELF_DECLARED",
    "CONTENT_MODES",
    "CONTENT_TIERS",
    "CONTENT_TIER_ADULT",
    "CONTENT_TIER_SAFE",
    "CONTENT_TIER_STANDARD",
    "SAFE_RULES_VERSION",
    "is_tier",
    "legacy_most_conservative",
    "legacy_parse_tier",
    "legacy_tier_rank",
    "most_conservative",
    "parse_tier",
    "tier_rank",
]

# 「安全規範」的版本。玩家關閉安全模式時同意的是這一版；改版後所有人的先前同意視為失效、須重新同意
# （規格：安全規範的版本改變後，先前的同意視為失效）。規範內容由法務覆核後才會改版，改版時必須一併更新對外的規範頁面。
# 欄位仍沿用 `users.adult_confirmed_at`／`adult_rules_version`／`adult_confirm_method`（不改名，免一次 contract change）。
SAFE_RULES_VERSION = "2026-11-19-draft-2"

_RANK = {mode: rank for rank, mode in enumerate(CONTENT_MODES)}


def is_tier(value: Any) -> bool:
    """是不是兩種內容模式之一（只認小寫的列舉值，不做任何寬鬆比對；舊值 `standard` 不算，它只在 `parse_tier` 被讀作 `adult`）。"""
    return isinstance(value, str) and value in _RANK


def parse_tier(value: Any) -> str:
    """把任意輸入轉為兩種模式之一：舊值 `standard` 讀作 `adult`；缺少或不合法一律回傳最保守的 `safe`。"""
    if value == CONTENT_TIER_STANDARD and isinstance(value, str):
        return CONTENT_TIER_ADULT
    return value if is_tier(value) else CONTENT_TIER_SAFE


def tier_rank(value: Any) -> int:
    """順序值：`safe` 0、`adult` 1；無法判定視為 `safe`（0），舊值 `standard` 同 `adult`。"""
    return _RANK[parse_tier(value)]


def most_conservative(*tiers: Any) -> str:
    """取最保守的一個。沒有任何輸入、或任一輸入無法判定，結果都是 `safe`。"""
    if not tiers:
        return CONTENT_TIER_SAFE
    return min((parse_tier(tier) for tier in tiers), key=_RANK.__getitem__)


# ---- 過渡相容層（safe-mode-toggle 2.5 移除）-----------------------------------------------------------------
# 三值的判定：第 2 組改完目錄、開存檔與審查之前，這些呼叫端仍依「safe、standard、adult」三種運作。

_LEGACY_RANK = {tier: rank for rank, tier in enumerate(CONTENT_TIERS)}


def legacy_parse_tier(value: Any) -> str:
    """三值版的 `parse_tier`：`standard` 維持 `standard`；缺少或不合法回傳 `safe`。"""
    return value if isinstance(value, str) and value in _LEGACY_RANK else CONTENT_TIER_SAFE


def legacy_tier_rank(value: Any) -> int:
    """三值版的順序值：`safe` 0、`standard` 1、`adult` 2；無法判定視為 0。"""
    return _LEGACY_RANK[legacy_parse_tier(value)]


def legacy_most_conservative(*tiers: Any) -> str:
    """三值版的取最保守者；沒有任何輸入、或任一輸入無法判定，結果都是 `safe`。"""
    if not tiers:
        return CONTENT_TIER_SAFE
    return min((legacy_parse_tier(tier) for tier in tiers), key=_LEGACY_RANK.__getitem__)
