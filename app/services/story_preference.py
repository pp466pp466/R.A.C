"""故事偏好與讀者頻道的值域與排序（story-preference 規格、AGENTS.md 第 7 節）。

**純邏輯**：沒有資料庫、設定與時間的依賴。玩家的故事偏好與劇本版本的讀者頻道共用同一組值：
`all`（全讀者）、`male`（男頻）、`female`（女頻）。

- 偏好**只影響劇本清單的排序**，不影響內容分級、敘事與權限；排序從不隱藏任何劇本。
- 排序分三層（`rank`，0 最前）：偏好為 `male` 時 男頻、全讀者、女頻；`female` 時 女頻、全讀者、男頻；
  偏好為 `all` 時所有劇本同一層（不重排）。劇本的頻道為空值（遷移之前的舊版本）視同全讀者。
- 值域常數放在 `app/models/user.py`（資料模型不得匯入 Service），這裡再匯出供 Service、Repository 與測試共用，
  不各自重寫字串。與 `content_tier.py` 同一種做法，所以不在「RPG 純模組」的匯入守門清單內（那份清單禁止匯入模型）。
"""
from __future__ import annotations

from typing import Any

from app.models.user import AUDIENCE_ALL, AUDIENCE_FEMALE, AUDIENCE_MALE, AUDIENCES

__all__ = [
    "AUDIENCES",
    "AUDIENCE_ALL",
    "AUDIENCE_FEMALE",
    "AUDIENCE_MALE",
    "LAYER_SAME",
    "LAYER_NEUTRAL",
    "LAYER_OTHER",
    "normalize_audience",
    "parse_audience",
    "rank",
]

LAYER_SAME = 0       # 與偏好同頻
LAYER_NEUTRAL = 1    # 全讀者（含空值）
LAYER_OTHER = 2      # 另一頻


def parse_audience(value: Any) -> str | None:
    """嚴格解析：必須是完全等於三個列舉值之一的字串，否則回傳 `None`（不容忍大小寫、空白或其他型別）。"""
    if isinstance(value, str) and value in AUDIENCES:
        return value
    return None


def normalize_audience(value: Any) -> str:
    """讀取已儲存或標準化內容時的容錯解析：空值或無法辨識一律視為全讀者（缺值視為全讀者）。"""
    return parse_audience(value) or AUDIENCE_ALL


def rank(preference: Any, audience: Any) -> int:
    """劇本在清單中的層級（0 最前）。偏好為 `all`（或無法辨識）時一律 0，也就是不重排。"""
    wanted = normalize_audience(preference)
    if wanted == AUDIENCE_ALL:
        return LAYER_SAME
    actual = normalize_audience(audience)
    if actual == wanted:
        return LAYER_SAME
    if actual == AUDIENCE_ALL:
        return LAYER_NEUTRAL
    return LAYER_OTHER
