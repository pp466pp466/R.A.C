"""登入後返回位置（`next`）的安全檢查（見 auth-pages 規格「轉址的安全」）。

只接受本站內、以單一 `/` 開頭的路徑。外部網址、協定相對網址（`//host`）、反斜線（部分瀏覽器會當成斜線）、
控制字元與過長的值，一律退回預設頁面，避免被利用來做開放轉址（open redirect）。
判斷在伺服器端完成，前端只拿到已經驗證過的值。
"""
from __future__ import annotations

from urllib.parse import unquote

DEFAULT_AFTER_LOGIN = "/account"   # C-3 完成後改成 /cs（單一常數）
MAX_NEXT_LENGTH = 200


def safe_next(value: object, default: str = DEFAULT_AFTER_LOGIN) -> str:
    """回傳可以安全轉址的站內路徑；不合法時回傳 `default`。"""
    if not isinstance(value, str) or not value or len(value) > MAX_NEXT_LENGTH:
        return default
    if not value.startswith("/") or value.startswith("//"):
        return default
    # 連同百分比編碼解開後再檢查一次：%2f%2f、%5c、%0d%0a 這類寫法不能繞過
    decoded = unquote(value)
    for candidate in (value, decoded):
        if "\\" in candidate or candidate.startswith("//"):
            return default
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in candidate):
            return default
    if "://" in decoded.split("?", 1)[0]:
        return default
    return value
