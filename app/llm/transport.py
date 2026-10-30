"""模型供應商轉接器共用的 HTTP 傳輸（`urllib`，不引入 SDK）。

從 `gemini.py` 抽出，讓每個轉接器共用同一份 TLS 與逾時處理，而不是各自複製一份後慢慢漂移。轉接器之間不互相匯入。

- 驗證伺服器憑證（`ssl.create_default_context()`）。
- 供應商回了錯誤狀態碼仍是「有回應」，狀態碼與（可解析的）本文交給轉接器判斷；本文**只回給轉接器，不被記錄**。
- 逾時一律拋出 `TimeoutError`，其他連線問題一律拋出 `OSError`，且**不帶原始訊息**（它可能含網址與主機資訊）。
- 傳輸可注入（`HttpTransport`），測試不必連網。
"""
from __future__ import annotations

import json
import socket
import ssl
import urllib.error
import urllib.request
from typing import Any, Protocol


class HttpTransport(Protocol):
    """送出一個 JSON POST。回傳（狀態碼, 解析後的 JSON 或 None）；逾時拋出 `TimeoutError`，其他連線問題拋出 `OSError`。"""

    def post_json(self, url: str, headers: dict[str, str], body: dict[str, Any], timeout: float) -> tuple[int, Any]: ...


class UrllibTransport:
    """真實的傳輸：`urllib`，驗證伺服器憑證。供應商回了錯誤狀態碼仍是「有回應」，交給轉接器判斷。"""

    def post_json(self, url: str, headers: dict[str, str], body: dict[str, Any], timeout: float) -> tuple[int, Any]:
        request = urllib.request.Request(
            url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
                return response.status, parse_json(response.read())
        except urllib.error.HTTPError as error:
            try:
                return error.code, parse_json(error.read())
            finally:
                error.close()
        except urllib.error.URLError as error:
            if isinstance(error.reason, (socket.timeout, TimeoutError)):
                raise TimeoutError from None
            raise OSError("連線失敗") from None            # 不帶原始訊息：它可能含網址與主機資訊
        except (socket.timeout, TimeoutError):
            raise TimeoutError from None


def parse_json(raw: bytes) -> Any:
    """解析回應本文；不是合法的 JSON（或不是 UTF-8）時回傳 None，不讓壞掉的本文讓轉接器爆掉。"""
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
