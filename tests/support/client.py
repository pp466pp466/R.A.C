"""測試用的 Flask client。

兩個行為：
1. 請求以 `buffered=True` 開啟：Werkzeug 會在讀完回應本文後呼叫應用程式回傳物件的 `close()`，
   這正是「回應送出之後才執行」的延後動作（例如寄信）被觸發的時機。沒有這樣做的 client 看不到寄信的結果。
   **不要**用 `with client:` 包住請求：那會讓請求內容在請求結束後繼續存在，改變延後動作實際執行時的環境。
2. 非 GET 請求自動帶上同源的 `Origin` 與目前的 CSRF 權杖（沒有權杖時先 GET `/terms` 取得），
   讓一般測試不必每次重寫；專門測試 CSRF 的測試請用 `raw_client()`，它不做任何自動處理。
`tests/conftest.py` 把它設為所有 `app.test_client()` 的預設 client。
"""
from __future__ import annotations

from flask import Flask
from flask.testing import FlaskClient

from app.security.cookies import cookie_name

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CSRF_HEADER = "X-CSRF-Token"


class BufferedClient(FlaskClient):
    auto_csrf = True

    def csrf_token(self) -> str:
        """目前 cookie jar 裡的 CSRF 權杖；沒有就先請求一個永遠公開的頁面（`/terms`）取得。

        不能用首頁：已登入的 client 打首頁會被導向預設頁面，那個回應沒有發放 CSRF cookie。
        """
        name = cookie_name(self.application.extensions["app_config"], "csrf")
        cookie = self.get_cookie(name)
        if cookie is None:
            super().open("/terms", buffered=True)
            cookie = self.get_cookie(name)
        assert cookie is not None, "公開頁面沒有發放 CSRF cookie"
        return cookie.value

    def open(self, *args, **kwargs):  # type: ignore[override]
        kwargs.setdefault("buffered", True)
        method = str(kwargs.get("method", "GET")).upper()
        # 只有 create_app 建立的應用才有 CSRF 檢查；測試自己組的最小應用沒有，不需要也不能取得權杖
        has_csrf = "signed_cookies" in self.application.extensions
        if self.auto_csrf and has_csrf and method not in SAFE_METHODS:
            headers = dict(kwargs.get("headers") or {})
            headers.setdefault("Origin", "http://localhost")
            if CSRF_HEADER not in headers:
                headers[CSRF_HEADER] = self.csrf_token()
            kwargs["headers"] = headers
        return super().open(*args, **kwargs)


def buffered_client(app: Flask) -> BufferedClient:
    app.test_client_class = BufferedClient
    return app.test_client()  # type: ignore[return-value]


def raw_client(app: Flask) -> FlaskClient:
    """不做任何自動處理的原始 client（不自動帶來源與權杖、不緩衝），給 CSRF 與 cookie 行為的測試用。"""
    return FlaskClient(app, app.response_class, use_cookies=True)
