"""使用者輸入的清理（見 AGENTS.md 第 10 節：去除控制字元並做 NFC 正規化）。"""
from __future__ import annotations

import unicodedata


def clean_single_line(value: str) -> str | None:
    """NFC 正規化、去除控制字元（含換行與 Tab）與頭尾空白。

    回傳 None 代表文字無法被安全地儲存（例如孤立的代理字元，無法編碼為 UTF-8）。
    """
    text = unicodedata.normalize("NFC", value)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cc")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return text.strip()
