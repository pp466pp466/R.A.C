"""登入後返回位置（`next`）的安全檢查（見 auth-pages 規格「轉址的安全」）。

只接受本站內、以單一 `/` 開頭的路徑。外部網址、協定相對網址（`//host`）、反斜線（部分瀏覽器會當成斜線）、
控制字元與過長的值，一律退回預設頁面，避免被利用來做開放轉址（open redirect）。
判斷在伺服器端完成，前端只拿到已經驗證過的值。

`landing_for` 決定「登入者該去哪裡」：受限登入狀態各去自己的頁面，否則去 `next`（已驗證）或預設頁面。
首頁、登入頁與 Google 回呼共用它，所以預設頁面只需要改 `DEFAULT_AFTER_LOGIN` 一個常數。
"""
from __future__ import annotations

from urllib.parse import quote, unquote

from app.security.session import AGE_PATH, RESTRICTION_PAGES
from app.services.current_user import RESTRICTION_AGE_PENDING, CurrentUser

DEFAULT_AFTER_LOGIN = "/game"      # RPG 的入口：選擇劇本與我的存檔（單一常數；不直接進入聊天，客服 `/cs` 改由導覽列進入，仍可直接開啟）
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


def landing_for(user: CurrentUser, next_url: object = None) -> str:
    """登入者該去的位置。

    受限登入狀態一律去各自的頁面（等待刪除 → `/account`、被鎖定 → `/appeal`），不依 `next`；
    待確認年齡的玩家去 `/age`，並帶著已驗證過的 `next`，補填成功後才回到原本要去的地方（沒有 `next` 或等於預設頁面時不帶）。
    沒有受限時去 `next`（不合法就用預設頁面）。
    """
    restriction = user.restriction
    if restriction == RESTRICTION_AGE_PENDING:
        destination = safe_next(next_url, DEFAULT_AFTER_LOGIN)
        return AGE_PATH if destination == DEFAULT_AFTER_LOGIN else f"{AGE_PATH}?next={quote(destination, safe='')}"
    if restriction is not None:
        return RESTRICTION_PAGES[restriction]
    return safe_next(next_url, DEFAULT_AFTER_LOGIN)
