"""模型供應商轉接器共用的 HTTP 傳輸（`urllib`，不引入 SDK）。

從 `gemini.py` 抽出，讓每個轉接器共用同一份 TLS 與逾時處理，而不是各自複製一份後慢慢漂移。轉接器之間不互相匯入。

- 驗證伺服器憑證（`ssl.create_default_context()`）。
- 供應商回了錯誤狀態碼仍是「有回應」，狀態碼與（可解析的）本文交給轉接器判斷；本文**只回給轉接器，不被記錄**。
- 逾時一律拋出 `TimeoutError`，其他連線問題一律拋出 `OSError`，且**不帶原始訊息**（它可能含網址與主機資訊）。
- 傳輸可注入（`HttpTransport`、`StreamingTransport`），測試不必連網。
- 串流（`open_stream`）：一條背景讀取執行緒逐行讀取、放進有上限的佇列，呼叫端以短逾時輪詢，所以等待上游的期間仍能檢查期限、
  送出心跳；`close()` 關閉底層連線，使阻塞中的讀取立刻失敗。串流不保留任何回應本文（非 200 只回報狀態碼）。
"""
from __future__ import annotations

import _socket
import http.client
import json
import queue
import socket
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

STREAM_QUEUE_SIZE = 64                    # 佇列滿時讀取執行緒阻塞（反壓），不無限制地緩衝
STREAM_MAX_LINE_BYTES = 1024 * 1024       # 單行上限：超過視為連線異常，避免無換行的串流吃光記憶體
_PUT_RETRY_SECONDS = 0.2                  # 反壓等待時檢查是否已關閉的間隔


class HttpTransport(Protocol):
    """送出一個 JSON POST。回傳（狀態碼, 解析後的 JSON 或 None）；逾時拋出 `TimeoutError`，其他連線問題拋出 `OSError`。"""

    def post_json(self, url: str, headers: dict[str, str], body: dict[str, Any], timeout: float) -> tuple[int, Any]: ...


@dataclass(frozen=True)
class StreamHead:
    """串流的第一個項目：HTTP 狀態碼。非 200 時之後不會再有項目（不讀取回應本文）。"""

    status: int


class _StreamEof:
    def __repr__(self) -> str:
        return "STREAM_EOF"


STREAM_EOF = _StreamEof()                 # 上游正常結束（或我方已關閉）
StreamItem = StreamHead | bytes | _StreamEof


class LineStream(Protocol):
    """逐行讀取的上游連線。第一個項目是 `StreamHead`，其後是去掉行尾換行的 `bytes`，最後是 `STREAM_EOF`。"""

    def poll(self, timeout: float) -> StreamItem | None:
        """最多等 `timeout` 秒；沒有新項目回傳 None。逾時拋出 `TimeoutError`，其他連線問題拋出 `OSError`（皆不帶原始訊息）。"""
        ...

    def close(self) -> None:
        """關閉底層連線並使讀取結束；可重複呼叫，關閉後 `poll` 一律回傳 `STREAM_EOF`。"""
        ...


class StreamingTransport(Protocol):
    def open_stream(self, url: str, headers: dict[str, str], body: dict[str, Any], timeout: float) -> LineStream:
        """送出 JSON POST 並回傳逐行讀取的串流。立即回傳，連線在背景進行；`timeout` 是連線與每次讀取的逾時。"""
        ...


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


    def open_stream(self, url: str, headers: dict[str, str], body: dict[str, Any], timeout: float) -> LineStream:
        return _ThreadedLineStream(url, headers, dict(body), timeout)


class _ThreadedLineStream:
    """背景執行緒讀取、佇列交接。

    為什麼不用 `select`：要自己處理 TLS 的內部緩衝、chunked 編碼與 HTTP 解析；為什麼不在 `socket` 上設短逾時輪詢：逾時發生在
    一行的中間時，緩衝區內已讀取的資料可能遺失。讀取執行緒用「長逾時的阻塞讀取」，短逾時只發生在佇列上。
    """

    def __init__(self, url: str, headers: dict[str, str], body: dict[str, Any], timeout: float) -> None:
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=STREAM_QUEUE_SIZE)
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._connection: http.client.HTTPConnection | None = None
        self._finished = False                      # 呼叫端已取得終點（EOF 或錯誤）
        threading.Thread(
            target=self._read, args=(url, headers, body, timeout), daemon=True, name="llm-stream-reader"
        ).start()

    # ---- 呼叫端 -----------------------------------------------------------------------------

    def poll(self, timeout: float) -> StreamItem | None:
        if self._closed.is_set() or self._finished:
            return STREAM_EOF
        try:
            item = self._queue.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            return None
        if isinstance(item, BaseException):
            self._finished = True
            raise item from None
        if item is STREAM_EOF:
            self._finished = True
        return item

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            connection, self._connection = self._connection, None
        if connection is not None:
            _interrupt(connection)

    # ---- 讀取執行緒 -------------------------------------------------------------------------

    def _put(self, item: Any) -> bool:
        while not self._closed.is_set():
            try:
                self._queue.put(item, timeout=_PUT_RETRY_SECONDS)
                return True
            except queue.Full:
                continue
        return False

    def _read(self, url: str, headers: dict[str, str], body: dict[str, Any], timeout: float) -> None:
        connection: http.client.HTTPConnection | None = None
        try:
            parts = urllib.parse.urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise OSError("連線失敗")
            if parts.scheme == "https":
                connection = http.client.HTTPSConnection(
                    parts.hostname, parts.port, timeout=timeout, context=ssl.create_default_context()
                )
            else:
                connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
            with self._lock:
                if self._closed.is_set():
                    return
                self._connection = connection
            target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            connection.request("POST", target, body=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers)
            response = connection.getresponse()
            if self._closed.is_set() or not self._put(StreamHead(response.status)):
                return
            if response.status != 200:                  # 不讀取回應本文（它可能回聲請求內容）；直接結束
                self._put(STREAM_EOF)
                return
            while not self._closed.is_set():
                line = response.readline(STREAM_MAX_LINE_BYTES)
                if not line:
                    self._put(STREAM_EOF)
                    return
                if len(line) >= STREAM_MAX_LINE_BYTES and not line.endswith(b"\n"):
                    raise OSError("連線失敗")
                if not self._put(line.rstrip(b"\r\n")):
                    return
        except BaseException as error:              # noqa: BLE001 - 執行緒不得讓任何例外逃出；一律轉成不帶原始訊息的分類
            if not self._closed.is_set():
                self._put(TimeoutError() if isinstance(error, (socket.timeout, TimeoutError)) else OSError("連線失敗"))
        finally:
            with self._lock:
                self._connection = None
            if connection is not None:
                _hard_close(connection)


def _interrupt(connection: http.client.HTTPConnection) -> None:
    """從另一條執行緒中斷阻塞中的讀取（可在讀取執行緒之外呼叫）。

    實測（Windows 與 Linux 的行為不同）：`shutdown` 在 Linux 能喚醒阻塞的 `recv`，Windows 不行；`socket.close()` 在有
    `makefile` 的參照時只是標記、不會真的關閉檔案描述符；`HTTPConnection.close()` 則會卡在讀取執行緒持有的緩衝鎖上。
    所以先 `shutdown`（Linux），再用底層的 `_socket.socket.close` 直接關閉檔案描述符（Windows 上會讓阻塞的 `recv` 立刻失敗），
    不在這裡呼叫 `HTTPConnection.close()`——讀取執行緒醒來之後自己收尾。
    """
    sock = connection.sock
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        _socket.socket.close(sock)
    except OSError:
        pass


def _hard_close(connection: http.client.HTTPConnection) -> None:
    """讀取執行緒收尾：確保連線完全關閉（此時沒有別的執行緒在讀，`close()` 不會卡住）。"""
    _interrupt(connection)
    try:
        connection.close()
    except OSError:
        pass


def parse_json(raw: bytes) -> Any:
    """解析回應本文；不是合法的 JSON（或不是 UTF-8）時回傳 None，不讓壞掉的本文讓轉接器爆掉。"""
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
