"""預先簽出的測試玩家登入狀態（load-testing 規格「使用者行為情境」）。

玩家只用 Google 登入，負載測試的虛擬使用者沒有註冊或密碼登入可用，所以改為：測試開始前，用
`scripts/mint_test_sessions.py` 在目標環境內建立測試玩家並簽出 session cookie，寫成一個 JSON 檔；這裡讀取它，每位虛擬使用者取一組。
檔案內容是有效的登入憑證（只屬於 `loadtest.invalid` 的測試帳號），不進版本庫、不寫入日誌、不印出。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_POOL_FILE = "test-sessions.json"


class PoolError(Exception):
    """登入狀態檔不存在、格式不對，或數量不足。訊息可以直接顯示（不含 cookie 的值）。"""


@dataclass(frozen=True)
class SessionPool:
    cookie_name: str
    values: tuple[str, ...]

    def __len__(self) -> int:
        return len(self.values)

    def value_for(self, number: int) -> str:
        """第 `number` 位（從 1 開始）虛擬使用者的 cookie 值。"""
        if not 1 <= number <= len(self.values):
            raise PoolError(f"沒有第 {number} 組登入狀態（檔案只有 {len(self.values)} 組）。")
        return self.values[number - 1]


def load_pool(path: str | Path) -> SessionPool:
    target = Path(path)
    if not target.is_file():
        raise PoolError(f"找不到測試玩家的登入狀態檔 {target}：請先執行 scripts/mint_test_sessions.py（見 load-testing 規格）。")
    try:
        data = json.loads(target.read_text(encoding="utf-8-sig"))
        name = data["cookie_name"]
        values = tuple(entry["value"] for entry in data["sessions"])
    except (ValueError, KeyError, TypeError):
        raise PoolError(f"{target} 的格式不對（需要 cookie_name 與 sessions[].value）。") from None
    if not isinstance(name, str) or not name or not values or not all(isinstance(v, str) and v for v in values):
        raise PoolError(f"{target} 的格式不對（需要 cookie_name 與 sessions[].value）。")
    return SessionPool(name, values)


def require(pool: SessionPool, needed: int) -> None:
    """虛擬使用者數多於登入狀態數時，在送出任何請求之前失敗並說明缺少的數量。"""
    if needed > len(pool):
        raise PoolError(f"測試玩家的登入狀態不足：需要 {needed} 組，只有 {len(pool)} 組，還缺 {needed - len(pool)} 組。請以更大的 --count 重新執行 scripts/mint_test_sessions.py。")
