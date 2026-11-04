"""劇本相關測試的共用輔助：一份合法的劇本內容、標準化後的結果，以及資料庫裡的劇本與版本。"""
from __future__ import annotations

from typing import Any

from app.services.script_spec import ValidatedScript, validate_script


def valid(**changes: Any) -> dict[str, Any]:
    """一份欄位齊全、全部在上限之內的劇本內容（原始輸入）；`changes` 的值為 `...` 表示移除該欄位。"""
    content: dict[str, Any] = {
        "title": "青雲問道", "summary": "少年入宗門，一步步走向自己的道。", "world_name": "乾歷朝仙俠世界",
        "content_tier": "safe", "background": "靈氣復甦的年代，宗門林立。\n凡人亦可修行。", "opening": "山門前，晨霧未散。",
        "rules": "境界分為煉氣、築基、金丹。", "tags": ["仙俠", "成長"], "calendar": {"name": "乾歷朝", "start_year": 1200},
        "ending_mode": "none",
        "characters": [{"name": "林曉", "personality": "活潑好動", "age": 16, "appearance": "身形瘦小，眼神明亮"}],
        "locations": ["古劍山莊", "青雲宗山門"],
        "milestones": [{"title": "宗門大測", "trigger": {"kind": "age", "value": 20}}],
    }
    for key, value in changes.items():
        if value is ...:
            content.pop(key, None)
        else:
            content[key] = value
    return content


def validated(max_tier: str = "adult", **changes: Any) -> ValidatedScript:
    """驗證並標準化 `valid(**changes)`（作者上限預設為最寬鬆，讓測試可以自由宣告內容模式）。"""
    return validate_script(valid(**changes), max_tier=max_tier)
