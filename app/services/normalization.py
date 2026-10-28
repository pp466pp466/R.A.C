"""具唯一約束的文字（例如 email）的正規化規則（見 AGENTS.md 第 4 節）。

SQLite 的字串比對預設區分大小寫、SQL Server 預設不區分。為讓兩種資料庫的重複判斷一致，
具唯一約束的文字一律先經此函式正規化，再寫入與查詢。
"""
from __future__ import annotations

import re
import unicodedata

MAX_EMAIL_LENGTH = 254
MAX_EMAIL_LOCAL_LENGTH = 64
GMAIL_DOMAINS = ("gmail.com", "googlemail.com")

# 保守的 ASCII 格式：本機部分只允許英數與 ._%+-（點不得在頭尾或連續）；網域為至少兩段、以點分隔的標籤。
# 不做 DNS 檢查，也不接受非 ASCII 地址（避免 SMTPUTF8 與 IDN 的相容問題）。
_LOCAL_PART = re.compile(r"[a-z0-9_%+\-]+(?:\.[a-z0-9_%+\-]+)*")
_DOMAIN_LABEL = r"[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?"
_DOMAIN = re.compile(rf"{_DOMAIN_LABEL}(?:\.{_DOMAIN_LABEL})+")


def normalize_unique_text(value: str) -> str:
    """NFC 正規化、去除頭尾空白、轉為小寫。"""
    return unicodedata.normalize("NFC", value).strip().lower()


def normalize_email(value: str) -> str:
    """email 儲存與比對用的正規形式（與 `normalize_unique_text` 相同規則）。"""
    return normalize_unique_text(value)


def _split_email(normalized: str) -> tuple[str, str] | None:
    local, separator, domain = normalized.partition("@")
    if not separator or "@" in domain:
        return None
    return local, domain


def _gmail_canonical_local(local: str) -> str:
    return local.split("+", 1)[0].replace(".", "")


def is_valid_email(value: str) -> bool:
    """格式是否合法。先檢查原始輸入是否為 ASCII：有些非 ASCII 字元轉小寫後會變成 ASCII
    （例如克耳文符號 U+212A），不能讓它們在正規化後才「變合法」。"""
    stripped = value.strip()
    if not stripped.isascii() or len(stripped) > MAX_EMAIL_LENGTH:
        return False
    parts = _split_email(normalize_email(stripped))
    if parts is None:
        return False
    local, domain = parts
    if len(local) > MAX_EMAIL_LOCAL_LENGTH or not _LOCAL_PART.fullmatch(local) or not _DOMAIN.fullmatch(domain):
        return False
    if domain in GMAIL_DOMAINS and not _gmail_canonical_local(local):
        return False  # 例如 "+news@gmail.com"：去掉標籤後沒有任何本機部分
    return True


def canonical_email(value: str) -> str:
    """用於「是否為同一個信箱」的正規形式（見 AGENTS.md 第 10 節）。

    先套用 `normalize_email`；網域為 gmail.com 或 googlemail.com 時，另外去除本機部分的「.」
    與第一個「+」之後的標籤，並把網域統一為 gmail.com。其他網域不變。
    輸入必須先通過 `is_valid_email`。
    """
    normalized = normalize_email(value)
    parts = _split_email(normalized)
    if parts is None:
        raise ValueError("不是合法的 email")
    local, domain = parts
    if domain in GMAIL_DOMAINS:
        return f"{_gmail_canonical_local(local)}@gmail.com"
    return normalized
