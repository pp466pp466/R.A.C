"""玩家的公開暱稱與短代號（見 player-nickname 規格與 AGENTS.md 第 10 節「暱稱與筆名」）。

純函式，不碰資料庫與網路：
- `check_nickname(raw)`：NFC 正規化、去除控制字元與零寬度等格式字元、頭尾空白與多餘空白，長度 2 至 20 字（Unicode 字元），
  不得含網址、電子郵件或電話的樣式，也不得冒用平台的官方署名（含「官方」）。回傳（標準化後的暱稱, 錯誤說明）其中一個為 `None`。
  樣式比對在「全形轉半形、不分大小寫」之後進行，所以全形的網址或數字也擋得住；錯誤說明只說類別，不回顯使用者輸入。
- `new_code()`：短代號。第一次設定暱稱時亂數產生並保存（`secrets`），所以無法由內部編號或對外識別碼推得；固定 4 碼、
  不含容易混淆的字元；不要求唯一，碰撞無害（公開顯示為「暱稱#短代號」，兩者一起才是署名）。
"""
from __future__ import annotations

import re
import secrets
import unicodedata

from app.models.script import OFFICIAL_AUTHOR_NAME
from app.services.script_spec import normalize_single_line

NICKNAME_MIN = 2
NICKNAME_MAX = 20

CODE_LENGTH = 4
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"          # 不含 0、1、O、I、L

MESSAGE_REQUIRED = "請輸入暱稱。"
MESSAGE_LENGTH = f"暱稱需為 {NICKNAME_MIN} 至 {NICKNAME_MAX} 個字。"
MESSAGE_PATTERN = "暱稱不可包含網址、電子郵件或電話。"
MESSAGE_OFFICIAL = "暱稱不可冒用官方署名。"

# 常見的網域後綴（不用「任何字母加點」，否則 "Mr.Smith" 這種正常的署名會被誤擋）
_TLDS = "com|net|org|edu|gov|tw|cn|jp|hk|io|co|me|tv|cc|xyz|info|app|dev|ai|gg|ly|to|us|uk|de|fr|kr|biz|shop|site|online|top|link"
_URL = re.compile(rf"(?:[a-z][a-z0-9+.\-]*://|www\.|[a-z0-9\-]+\.(?:{_TLDS})(?![a-z0-9]))")
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+")
# 連續 8 位以上的數字，數字之間可夾常見的分隔符號（空白、連字號、括號、點、加號、底線）
_PHONE = re.compile(r"(?:\d[\s\-().+_]*){8,}")


def _compare_form(value: str) -> str:
    """比對用：NFKC（全形轉半形）、不分大小寫，並把中文句點視為點。"""
    text = unicodedata.normalize("NFKC", value).casefold()
    return text.replace("。", ".").replace("．", ".")


def check_nickname(raw: object) -> tuple[str | None, str | None]:
    """回傳（標準化後的暱稱, None）或（None, 錯誤說明）。"""
    if not isinstance(raw, str):
        return None, MESSAGE_REQUIRED
    text = normalize_single_line(raw)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return None, MESSAGE_PATTERN                      # 孤立的代理字元：無法安全儲存，與不合格的內容同樣處理
    if not text:
        return None, MESSAGE_REQUIRED
    if not NICKNAME_MIN <= len(text) <= NICKNAME_MAX:
        return None, MESSAGE_LENGTH
    compare = _compare_form(text)
    squeezed = re.sub(r"\s+", "", compare)               # 以空白拆開的網址也要擋住
    if _URL.search(compare) or _URL.search(squeezed) or _EMAIL.search(compare) or _PHONE.search(compare):
        return None, MESSAGE_PATTERN
    if _compare_form(OFFICIAL_AUTHOR_NAME) in squeezed:
        return None, MESSAGE_OFFICIAL
    return text, None


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def display_name(nickname: str, code: str) -> str:
    """公開顯示的署名：「暱稱#短代號」。"""
    return f"{nickname}#{code}"
