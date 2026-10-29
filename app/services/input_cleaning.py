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


def clean_message(value: str) -> str | None:
    """客服訊息的清理：NFC 正規化、統一換行為 `\\n`、去除控制字元（保留換行）與頭尾空白。

    Tab 換成一個空格（直接刪掉會讓貼上的表格或程式碼的字黏在一起）。回傳 None 代表文字無法被安全地儲存
    （例如孤立的代理字元，無法編碼為 UTF-8）。字數上限與「空白訊息」由呼叫端在清理之後判斷，
    因為清理本身會改變字數（NFC 與去控制字元）。
    """
    text = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    text = "".join(ch for ch in text if ch == "\n" or unicodedata.category(ch) != "Cc")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return text.strip()
