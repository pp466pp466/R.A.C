"""具唯一約束的文字（例如 email）的正規化規則（見 AGENTS.md 第 4 節）。

SQLite 的字串比對預設區分大小寫、SQL Server 預設不區分。為讓兩種資料庫的重複判斷一致，
具唯一約束的文字一律先經此函式正規化，再寫入與查詢。
"""
from __future__ import annotations

import unicodedata


def normalize_unique_text(value: str) -> str:
    """NFC 正規化、去除頭尾空白、轉為小寫。"""
    return unicodedata.normalize("NFC", value).strip().lower()
