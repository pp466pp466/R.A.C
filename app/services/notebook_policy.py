"""RPG 記事簿的容量、清理、視窗與追加規則（見 rpg-notebook、rpg-notebook-capture 規格與 rpg-notebook 的 design D2、D3）。

**純函式**：沒有資料庫、網路、時間與隨機的依賴，不匯入資料模型、Repository 或 Flask（`tests/architecture/test_rpg_pure_modules.py` 守住）；
只依賴 `quota_policy` 的容量常數與 `input_cleaning` 的清理函式。

- **容量只有一個來源**：`quota_policy.TIER_LIMITS[...].notebook_capacity`（free 2000、plus 5000、vip 10000）。容量由目前的會員等級推得、不儲存，
  所以升級立即有新容量、降級後超過新容量的內容不刪除而是視同已滿。預備空間是容量的 25%（容量 ÷ 4 取整），只供 AI 寫入，玩家編輯沒有。
- **字數 = NFC 正規化後的 Unicode 字元數**。長度**大於**容量才是「已滿」；恰等於容量仍是正常。
- **視窗**：玩家送出第 11、21、31… 輪、該輪完成之後，才抓取前一個 10 輪（`window_for`）；第 10 輪完成時不觸發。
- **追加只加在末尾**：`merge_entries` 清理條目、去除空的與重複的，並算出追加後的文字與長度，**不改寫既有內容**；條數與字數的「要求」在提示詞
  （`rpg_notebook_prompt`），這裡不因為條數或字數丟棄或截斷任何條目（rpg-notebook-capture：提示詞要求每次最多 5 條且每條 50 字內）。
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Iterable

from app.services.input_cleaning import clean_message, clean_single_line
from app.services.quota_policy import limits_for

WINDOW_TURNS = 10                 # 每完成幾輪抓取一次，也是抓取視窗的輪數
RESERVE_DIVISOR = 4               # 預備空間 = 容量的 25%
LINE_SEPARATOR = "\n"             # 條目之間（與既有內容之後）的分隔，一條一行


# ---- 容量與已滿 -------------------------------------------------------------------------------


def capacity_for(tier: object) -> int:
    """該等級的記事簿容量；不認得的等級（資料異常）套用 free（`limits_for` 的規則）。"""
    return limits_for(tier).notebook_capacity


def reserve_for(tier: object) -> int:
    """預備空間（容量的 25%，取整）：free 500、plus 1250、vip 2500。只供 AI 寫入。"""
    return capacity_for(tier) // RESERVE_DIVISOR


def ai_limit_for(tier: object) -> int:
    """AI 追加後總長的上限 = 容量 + 預備空間。"""
    return capacity_for(tier) + reserve_for(tier)


def length_of(text: str | None) -> int:
    """記事簿的字數：NFC 正規化後的 Unicode 字元數。"""
    return len(unicodedata.normalize("NFC", text)) if text else 0


def is_full(length: int, tier: object) -> bool:
    """長度大於容量即為已滿（等於容量不算）。降級後超過新容量視同已滿，內容不動。"""
    return length > capacity_for(tier)


def room_for(existing: str | None, tier: object) -> int:
    """AI 還能追加的字數上限（容量 + 預備空間 − 目前長度）；含接在既有內容之後需要補的那一個換行。不會小於 0。"""
    base = existing or ""
    used = length_of(base) + (0 if not base or base.endswith(LINE_SEPARATOR) else len(LINE_SEPARATOR))
    return max(0, ai_limit_for(tier) - used)


# ---- 清理 -------------------------------------------------------------------------------------


def clean_notebook_text(value: object) -> str | None:
    """玩家儲存的記事簿文字的清理：NFC、統一換行為 `\\n`、Tab 換成空格、去除除換行外的控制字元、頭尾空白。

    回傳 None 代表無法安全儲存（不是字串，或含孤立的代理字元而無法編碼為 UTF-8）。字數與空白由呼叫端在清理之後判斷（玩家可以清空）。
    """
    return clean_message(value) if isinstance(value, str) else None


# ---- 視窗 -------------------------------------------------------------------------------------


def window_for(completed: int) -> int | None:
    """完成 `completed` 輪（中斷、被擋與失敗的回合不計）之後待抓取視窗的邊界 B = ⌊(完成輪數 − 1) ÷ 10⌋ × 10；B 為 0 回 None（還沒有視窗）。

    視窗是第 B−9 至 B 輪。第 10 輪完成時 B = 0（None），第 11 輪完成時 B = 10；第 20 輪完成時 B 仍是 10（前一個視窗），第 21 輪完成時 B = 20。
    B 是否大於記事簿已處理到的輪次（`notebook_upto_turn`），由呼叫端判斷：不大於就沒有待抓取的視窗。
    """
    if not isinstance(completed, int) or completed <= 0:
        return None
    boundary = (completed - 1) // WINDOW_TURNS * WINDOW_TURNS
    return boundary or None


# ---- 追加 -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class NotebookMerge:
    """把新條目追加到既有記事簿之後的結果（純資料，不寫入任何地方）。"""

    entries: tuple[str, ...]      # 實際會追加的條目（已清理、已去除空的與重複的）；沒有則為空
    text: str                     # 追加後的完整文字；沒有條目時等於既有文字
    length: int                   # 追加後的字數（NFC）
    added_length: int             # 追加的字數（含接在既有內容之後的那個換行）；沒有條目時為 0
    longest_entry: int            # 最長一條的字數；沒有條目時為 0


def merge_entries(existing: str | None, entries: Iterable[object]) -> NotebookMerge:
    """清理新條目並計算追加後的結果。

    每條條目：NFC、去除控制字元（含換行與 Tab）與頭尾空白，成為單行；空的、無法編碼的、非字串的丟棄；與既有記事簿的某一行（比對去除頭尾空白後的整行）
    或與前面已收下的條目完全相同者去除。收下的條目以 `\\n` 連接，接在既有文字之後（既有文字非空且尾端沒有換行時補一個換行，並計入長度）。
    既有的文字原封不動。
    """
    base = existing or ""
    seen = {line.strip() for line in base.split(LINE_SEPARATOR) if line.strip()}
    accepted: list[str] = []
    for raw in entries:
        entry = clean_single_line(raw) if isinstance(raw, str) else None
        if not entry or entry in seen:
            continue
        seen.add(entry)
        accepted.append(entry)
    if not accepted:
        return NotebookMerge((), base, length_of(base), 0, 0)
    separator = "" if not base or base.endswith(LINE_SEPARATOR) else LINE_SEPARATOR
    text = base + separator + LINE_SEPARATOR.join(accepted)
    length = length_of(text)
    return NotebookMerge(tuple(accepted), text, length, length - length_of(base), max(len(entry) for entry in accepted))
