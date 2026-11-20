"""劇本目錄的查詢參數解析與游標（rpg-script-catalog 規格；design D6）。

**純邏輯**：沒有資料庫、設定、時間與 HTTP 的依賴，輸入是查詢字串轉出的值，輸出是已驗證的 `CatalogQuery`；
任何一個欄位不合法就拋 `ValidationError`，**一次指出所有不合法的欄位**（`q`、`tag`、`audience`、`limit`、`cursor`）。

- 關鍵字 `q`：NFC 正規化、控制字元（含換行與 Tab）視為空白、不分大小寫（`casefold`），以空白切成至多 `Q_MAX_TOKENS` 個詞，
  整體（詞之間以單一空白相連）至多 `Q_MAX_CHARS` 字；空字串視為沒有關鍵字。**`%`、`_` 與跳脫字元在這裡原樣保留**，
  由 Repository 以 ORM 的 `contains(autoescape=True)` 當成一般字元比對（兩處職責分開，這裡不處理 LIKE 的萬用字元）。
- `tag`：單一標籤、不分大小寫的完整比對，長度上限與劇本規格的標籤相同（`TAG_MAX`）。
- `audience`：`all`、`male`、`female`（嚴格比對，不容忍大小寫或空白）；沒給就不篩選。
- `limit`：1 至 `MAX_LIMIT` 的整數（布林不算），預設 `DEFAULT_LIMIT`。
- `cursor`：不透明的位置標記 `[偏好層, 發佈時間, 版本編號]`，編成 URL-safe base64 的 JSON。**不簽章**：它只表示「從哪個位置之後」，
  不含祕密、也不授權任何東西；解碼時嚴格驗證型別與範圍，並要求重新編碼後與輸入逐字相同（拒絕非標準寫法），所以被竄改的游標只會得到
  400 `cursor` 或一個合法的起點，不會造成 500。`offset` 不在這裡，目錄不再讀取它。
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.services.errors import ValidationError
from app.services.script_spec import TAG_MAX, fold
from app.services.story_preference import parse_audience

DEFAULT_LIMIT = 12
MAX_LIMIT = 24
Q_MAX_TOKENS = 5
Q_MAX_CHARS = 50

# 游標的範圍：偏好層 0 至 2（見 story_preference 的三層）、版本編號為正的 32 位元整數（SQL Server 的 INT）。
CURSOR_MAX_CHARS = 128
CURSOR_MAX_RANK = 2
CURSOR_MAX_ID = 2_147_483_647
_CURSOR_ALPHABET = re.compile(r"[A-Za-z0-9_-]+")
_CURSOR_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z")
_CURSOR_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_CURSOR_MESSAGE = "分頁位置不正確，請重新載入。"


@dataclass(frozen=True)
class CatalogCursor:
    """下一頁從哪裡開始：排序鍵 `(偏好層, 發佈時間, 版本編號)` 之後。發佈時間是帶 UTC 時區的 `datetime`。"""

    rank: int
    published_at: datetime
    version_id: int


@dataclass(frozen=True)
class CatalogQuery:
    """已驗證的目錄查詢。`terms` 是已正規化、去重（保留順序）的關鍵字詞，全部要符合；空元組＝沒有關鍵字。"""

    terms: tuple[str, ...]
    tag: str | None
    audience: str | None
    limit: int
    cursor: CatalogCursor | None


def parse_catalog_query(
    *, q: object = None, tag: object = None, audience: object = None, limit: object = DEFAULT_LIMIT, cursor: object = None,
) -> CatalogQuery:
    """驗證並正規化目錄的全部查詢參數；任何欄位不合法就拋 `ValidationError`，`fields` 含所有不合法的欄位。"""
    errors: dict[str, str] = {}

    terms: tuple[str, ...] = ()
    try:
        terms = _parse_terms(q)
    except _Invalid as exc:
        errors["q"] = str(exc)

    tag_key: str | None = None
    try:
        tag_key = _parse_tag(tag)
    except _Invalid as exc:
        errors["tag"] = str(exc)

    audience_key: str | None = None
    if audience not in (None, ""):
        audience_key = parse_audience(audience)
        if audience_key is None:
            errors["audience"] = "讀者頻道只能是 all、male 或 female。"

    size = DEFAULT_LIMIT
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        errors["limit"] = f"每次筆數須為 1 至 {MAX_LIMIT}。"
    else:
        size = limit

    position: CatalogCursor | None = None
    if cursor not in (None, ""):
        try:
            position = decode_cursor(cursor)
        except ValidationError as exc:
            errors["cursor"] = exc.fields["cursor"]

    if errors:
        raise ValidationError(fields=errors)
    return CatalogQuery(terms=terms, tag=tag_key, audience=audience_key, limit=size, cursor=position)


class _Invalid(Exception):
    """欄位層級的驗證失敗（訊息就是回給使用者的說明）；只在本模組內使用。"""


def _parse_terms(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, str):
        raise _Invalid("關鍵字必須是文字。")
    text = unicodedata.normalize("NFC", value)
    # 空白類字元（含換行、Tab）都當成詞的分隔；其餘控制字元直接去除
    text = "".join(" " if char.isspace() else char for char in text if char.isspace() or unicodedata.category(char) != "Cc")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise _Invalid("關鍵字含有無法處理的字元。") from None
    words = text.split()
    if len(" ".join(words)) > Q_MAX_CHARS:
        raise _Invalid(f"關鍵字最長 {Q_MAX_CHARS} 字。")
    if len(words) > Q_MAX_TOKENS:
        raise _Invalid(f"關鍵字最多 {Q_MAX_TOKENS} 個詞，請用空白分隔。")
    return tuple(dict.fromkeys(fold(word) for word in words))


def _parse_tag(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _Invalid("標籤必須是文字。")
    text = unicodedata.normalize("NFC", value)
    text = "".join(char for char in text if unicodedata.category(char) != "Cc").strip()
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise _Invalid("標籤含有無法處理的字元。") from None
    if not text:
        return None
    if len(text) > TAG_MAX:
        raise _Invalid(f"標籤最長 {TAG_MAX} 字。")
    return fold(text)


# ---- 游標 -----------------------------------------------------------------------------

def encode_cursor(rank: int, published_at: datetime, version_id: int) -> str:
    """把一頁最後一筆的排序鍵編成游標。發佈時間不帶時區時視為 UTC（`UTCDateTime` 讀出時一律帶 UTC）。"""
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    stamp = published_at.astimezone(timezone.utc).strftime(_CURSOR_STAMP_FORMAT)
    payload = json.dumps([rank, stamp, version_id], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(raw: object) -> CatalogCursor:
    """解出游標；任何不合（型別、長度、字元、結構、範圍、非標準寫法）一律拋 `ValidationError(fields={"cursor": …})`。"""
    if not isinstance(raw, str) or not 0 < len(raw) <= CURSOR_MAX_CHARS or not _CURSOR_ALPHABET.fullmatch(raw):
        raise _bad_cursor()
    try:
        payload = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        data = json.loads(payload.decode("utf-8"))
    except (binascii.Error, ValueError):                         # UnicodeDecodeError 與 JSONDecodeError 都是 ValueError
        raise _bad_cursor() from None
    if not isinstance(data, list) or len(data) != 3:
        raise _bad_cursor()
    rank, stamp, version_id = data
    if not _is_int(rank) or not 0 <= rank <= CURSOR_MAX_RANK:
        raise _bad_cursor()
    if not _is_int(version_id) or not 1 <= version_id <= CURSOR_MAX_ID:
        raise _bad_cursor()
    if not isinstance(stamp, str) or not _CURSOR_STAMP.fullmatch(stamp):
        raise _bad_cursor()
    try:
        published_at = datetime.strptime(stamp, _CURSOR_STAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:                                           # 例如月份 13
        raise _bad_cursor() from None
    if encode_cursor(rank, published_at, version_id) != raw:     # 同一份內容只有一種寫法
        raise _bad_cursor()
    return CatalogCursor(rank=rank, published_at=published_at, version_id=version_id)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _bad_cursor() -> ValidationError:
    return ValidationError(fields={"cursor": _CURSOR_MESSAGE})
