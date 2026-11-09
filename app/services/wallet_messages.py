"""遊戲幣記錄頁的顯示文字（繁體中文；見 wallet-api 規格「遊戲幣記錄」）。

帳本的種類是機器可讀的固定列舉，玩家看到的是這裡對應的說明；沒有任何英文代碼或內部編號會出現在畫面上。
尚未結算的預扣顯示為「處理中」。
"""
from __future__ import annotations

from app.models.wallet import (
    ENTRY_ADMIN_ADJUST, ENTRY_DAILY_CHECKIN, ENTRY_KINDS, ENTRY_SIGNUP_GRANT, ENTRY_TURN_HOLD, ENTRY_TURN_REFUND, RESOLUTION_SETTLED,
)

LABEL_PENDING = "處理中"

_LABELS = {
    ENTRY_SIGNUP_GRANT: "註冊贈送",
    ENTRY_DAILY_CHECKIN: "每日簽到",
    ENTRY_TURN_HOLD: "冒險回合",          # 已結算的預扣；尚未結算的顯示為「處理中」
    ENTRY_TURN_REFUND: "退回",
    ENTRY_ADMIN_ADJUST: "管理員調整",
}

assert tuple(_LABELS) == ENTRY_KINDS, "每個帳本種類都必須有繁體中文說明"


def label(kind: str, resolution: str | None) -> str:
    """一筆帳本的說明：尚未結算的預扣是「處理中」，其餘依種類；不認得的種類（資料異常）顯示為「其他」。"""
    if kind == ENTRY_TURN_HOLD and resolution != RESOLUTION_SETTLED:
        return LABEL_PENDING
    return _LABELS.get(kind, "其他")
