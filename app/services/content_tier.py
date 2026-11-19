"""內容模式的順序與比較（content-tier-modes 規格、AGENTS.md 第 7 節）。

內容模式只有兩種，由保守到寬鬆：`safe`（安全模式開）、`adult`（安全模式關）。這裡是**純邏輯**：沒有資料庫、
設定與時間的依賴，也不內嵌任何年齡數字——全站只限滿 18 歲者使用，沒有依年齡設定的上限，這裡只回答「哪一個比較保守」。

- 兩個模式「取較保守者」一律以這裡的順序決定（`most_conservative`）；
- 舊值 `standard`（原一般模式）一律讀作 `adult`（`parse_tier`），不論出現在玩家偏好、劇本內容或其他儲存的資料，
  所以既有資料**不必改寫**；系統不再寫入 `standard`；
- 無法判定（缺少、空字串或不是兩種之一）一律視為最保守的 `safe`（`parse_tier`），不拋出錯誤：
  預設最保守是安全模式的底線，寧可少給也不要因為髒資料而放寬。

"""
from __future__ import annotations

from typing import Any

from app.models.user import (
    ADULT_CONFIRM_SELF_DECLARED,
    CONTENT_MODES,
    CONTENT_TIER_ADULT,
    CONTENT_TIER_SAFE,
)

__all__ = [
    "ADULT_CONFIRM_SELF_DECLARED",
    "CONTENT_MODES",
    "CONTENT_TIER_ADULT",
    "CONTENT_TIER_SAFE",
    "LEGACY_TIER_STANDARD",
    "SAFE_RULES_VERSION",
    "is_readable_tier",
    "is_tier",
    "most_conservative",
    "parse_tier",
    "tier_rank",
]

# 舊值（原一般模式）：只認得、不寫入，一律讀作 `adult`。**這個字面值只允許出現在這個檔案**（全碼庫的守門測試 `test_no_legacy_tier_checks`）：
# 其他程式要判斷「是不是可讀的內容模式值」請用 `is_readable_tier`，要取得模式請用 `parse_tier`。
LEGACY_TIER_STANDARD = "standard"

# 「安全規範」的版本。玩家關閉安全模式時同意的是這一版；改版後所有人的先前同意視為失效、須重新同意
# （規格：安全規範的版本改變後，先前的同意視為失效）。規範內容由法務覆核後才會改版，改版時必須一併更新對外的規範頁面。
# 欄位仍沿用 `users.adult_confirmed_at`／`adult_rules_version`／`adult_confirm_method`（不改名，免一次 contract change）。
SAFE_RULES_VERSION = "2026-11-19-draft-2"

_RANK = {mode: rank for rank, mode in enumerate(CONTENT_MODES)}


def is_tier(value: Any) -> bool:
    """是不是兩種內容模式之一（只認小寫的列舉值，不做任何寬鬆比對；舊值 `standard` 不算，它只在 `parse_tier` 被讀作 `adult`）。"""
    return isinstance(value, str) and value in _RANK


def is_readable_tier(value: Any) -> bool:
    """是不是可以被讀取的內容模式值：兩種現行模式，或舊值 `standard`（讀作 `adult`）。劇本內容的驗證用它決定要不要接受該值。"""
    return is_tier(value) or (isinstance(value, str) and value == LEGACY_TIER_STANDARD)


def parse_tier(value: Any) -> str:
    """把任意輸入轉為兩種模式之一：舊值 `standard` 讀作 `adult`；缺少或不合法一律回傳最保守的 `safe`。"""
    if isinstance(value, str) and value == LEGACY_TIER_STANDARD:
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

