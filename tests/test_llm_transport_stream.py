"""llm-streaming／llm-openai-compatible 規格：傳輸層的串流讀取（`UrllibTransport.open_stream`）。

用本機 HTTP 伺服器（chunked 編碼，與真實的 SSE 相同）驗證：逐行讀取、第一個項目是狀態碼、保活註解仍是一行、
一行被拆成兩個 chunk 時不遺失資料、輪詢逾時回傳 None、`close()` 能中斷阻塞中的讀取、非 200 不讀本文、
斷線與逾時拋出不帶原始訊息的錯誤、佇列有反壓上限。
"""
import http.client
import json
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.llm.transport import STREAM_EOF, STREAM_QUEUE_SIZE, StreamHead, UrllibTransport

WAIT = 5.0


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        mode = self.server.mode
        try:
            if mode == "429":
                payload = b'{"error": {"message": "SECRET-ECHO"}}'
                self.send_response(429)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            if mode == "lines":
                self._chunk(b": keepalive\n\n")
                self._chunk('data: {"a": "你好"}\n\n'.encode("utf-8"))
                self._chunk(b"data: [DONE]\n\n")
                self.wfile.write(b"0\r\n\r\n")
            elif mode == "split":
                first = 'data: {"a": "你'.encode("utf-8")
                self._chunk(first[:-1])                                   # 在「你」的第三個位元組之前切開
                time.sleep(0.3)
                self._chunk(first[-1:] + '好"}\n\n'.encode("utf-8"))
                self.wfile.write(b"0\r\n\r\n")
            elif mode in ("openai", "openai_hang"):
                self._chunk(b": OPENROUTER PROCESSING\n\n")
                self._chunk('data: {"choices": [{"delta": {"reasoning": "想"}, "finish_reason": null}]}\n\n'.encode("utf-8"))
                self._chunk('data: {"choices": [{"delta": {"content": "清晨的"}, "finish_reason": null}]}\n\n'.encode("utf-8"))
                if mode == "openai_hang":
                    self.server.release.wait(WAIT * 2)
                    return
                self._chunk('data: {"choices": [{"delta": {"content": "海風。"}, "finish_reason": "stop"}]}\n\n'.encode("utf-8"))
                self._chunk(b'data: {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 4}}\n\n')
                self._chunk(b"data: [DONE]\n\n")
                self.wfile.write(b"0\r\n\r\n")
            elif mode == "hang":
                self._chunk(b"data: 1\n\n")
                self.server.release.wait(WAIT * 2)
            elif mode == "cut":
                self._chunk(b"data: 1\n\n")
                self.close_connection = True                              # 沒有結尾的 0 長度 chunk：連線被截斷
            elif mode == "reset":
                self._chunk(b"data: 1\n\n")
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                self.connection.close()                                   # 以 RST 結束：用戶端的讀取會失敗
                self.close_connection = True
            elif mode == "flood":
                for index in range(STREAM_QUEUE_SIZE * 4):
                    self._chunk(f"data: {index}\n\n".encode())
                self.server.release.wait(WAIT * 2)
        except OSError:
            pass


@pytest.fixture()
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.seen, httpd.mode, httpd.release = [], "lines", threading.Event()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd
    httpd.release.set()
    httpd.shutdown()
    httpd.server_close()


def url_of(httpd) -> str:
    return f"http://127.0.0.1:{httpd.server_port}/v1/chat/completions?x=1"


def drain(stream, *, limit: float = WAIT) -> list:
    """取到 EOF 為止的所有項目；超過 `limit` 秒還沒結束就失敗。"""
    items, deadline = [], time.monotonic() + limit
    while time.monotonic() < deadline:
        item = stream.poll(0.2)
        if item is None:
            continue
        items.append(item)
        if item is STREAM_EOF:
            return items
    raise AssertionError(f"串流沒有在 {limit} 秒內結束：{items!r}")


def reader_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "llm-stream-reader" and t.is_alive()]


def wait_until(condition, limit: float = WAIT) -> bool:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def test_lines_arrive_in_order_with_the_status_first_and_eof_last_and_the_request_is_sent_as_utf8_json(server):
    stream = UrllibTransport().open_stream(url_of(server), {"Content-Type": "application/json", "x-test": "1"}, {"問": "你好"}, 5)
    items = drain(stream)
    assert items == [
        StreamHead(200), b": keepalive", b"", 'data: {"a": "你好"}'.encode("utf-8"), b"", b"data: [DONE]", b"", STREAM_EOF,
    ]
    path, headers, body = server.seen[0]
    assert path == "/v1/chat/completions?x=1" and headers["x-test"] == "1"
    assert json.loads(body.decode("utf-8")) == {"問": "你好"}


def test_a_line_split_across_two_chunks_in_the_middle_of_a_character_is_not_lost(server):
    server.mode = "split"
    items = drain(UrllibTransport().open_stream(url_of(server), {}, {}, 5))
    assert 'data: {"a": "你好"}'.encode("utf-8") in items and items[-1] is STREAM_EOF


def test_polling_returns_none_when_nothing_arrives_and_the_connection_stays_open(server):
    server.mode = "hang"
    stream = UrllibTransport().open_stream(url_of(server), {}, {}, 5)
    try:
        assert wait_until(lambda: stream.poll(0.05) == b"data: 1")               # 先把狀態碼與第一行讀掉
        assert wait_until(lambda: stream.poll(0.05) == b"")                      # 事件結尾的空行
        started = time.monotonic()
        assert [stream.poll(0.1) for _ in range(3)] == [None, None, None]        # 上游沉默：逾時回傳 None，不拋錯
        assert time.monotonic() - started < 2
        assert reader_threads()                                                  # 連線沒有被關閉
    finally:
        stream.close()


def test_close_interrupts_a_blocked_read_and_stops_the_reader_thread(server):
    server.mode = "hang"
    stream = UrllibTransport().open_stream(url_of(server), {}, {}, 30)
    assert wait_until(lambda: stream.poll(0.05) == b"data: 1")                   # 讀取執行緒現在阻塞在下一次讀取
    assert reader_threads()
    started = time.monotonic()
    stream.close()
    assert wait_until(lambda: not reader_threads(), 3)                           # 阻塞中的讀取立刻失敗並結束
    assert time.monotonic() - started < 3
    assert stream.poll(1) is STREAM_EOF                                          # 關閉後不再讀取
    stream.close()                                                               # 可重複呼叫


def test_a_non_200_reports_only_the_status_and_never_reads_the_body(server):
    server.mode = "429"
    items = drain(UrllibTransport().open_stream(url_of(server), {}, {}, 5))
    assert items == [StreamHead(429), STREAM_EOF]
    assert "SECRET-ECHO" not in repr(items)


def test_a_truncated_chunked_body_ends_like_a_clean_eof_so_the_sse_layer_must_require_done_or_a_finish_reason(server):
    """`http.client` 把沒有結尾 chunk 的回應當成正常結束；因此「串流是否完整」由轉接器以 `[DONE]` 或結束原因判斷。"""
    server.mode = "cut"
    items = drain(UrllibTransport().open_stream(url_of(server), {}, {}, 5))
    assert items == [StreamHead(200), b"data: 1", b"", STREAM_EOF]


def test_a_connection_reset_in_the_middle_raises_oserror_without_the_original_message(server):
    server.mode = "reset"
    stream = UrllibTransport().open_stream(url_of(server), {}, {}, 5)
    deadline = time.monotonic() + WAIT
    with pytest.raises(OSError) as raised:
        while time.monotonic() < deadline:
            if stream.poll(0.2) is STREAM_EOF:
                break
    # 收到 RST 時，作業系統可能丟棄尚未讀取的資料（Windows 會），所以不斷言錯誤之前讀到了什麼
    assert not isinstance(raised.value, TimeoutError)
    assert str(raised.value) == "連線失敗" and "127.0.0.1" not in str(raised.value) and raised.value.__cause__ is None


@pytest.mark.parametrize("failure, expected", [(ConnectionRefusedError("127.0.0.1:9"), OSError), (TimeoutError("x"), TimeoutError)])
def test_connection_failures_and_timeouts_are_classified_without_host_details(monkeypatch, failure, expected):
    def failing_connect(self):
        raise failure

    monkeypatch.setattr(http.client.HTTPConnection, "connect", failing_connect)
    stream = UrllibTransport().open_stream("http://example.invalid:9/v1", {}, {}, 2)
    deadline = time.monotonic() + WAIT
    with pytest.raises(expected) as raised:
        while time.monotonic() < deadline:
            stream.poll(0.2)
    assert (isinstance(raised.value, TimeoutError)) == (expected is TimeoutError)
    assert "example.invalid" not in str(raised.value) and "127.0.0.1" not in str(raised.value)


def test_an_unsupported_scheme_is_a_connection_failure_not_a_crash():
    stream = UrllibTransport().open_stream("file:///etc/passwd", {}, {}, 2)
    deadline = time.monotonic() + WAIT
    with pytest.raises(OSError):
        while time.monotonic() < deadline:
            stream.poll(0.2)


def test_the_queue_is_bounded_so_a_fast_upstream_cannot_grow_memory_and_close_still_ends_the_reader(server):
    server.mode = "flood"
    stream = UrllibTransport().open_stream(url_of(server), {}, {}, 30)
    assert wait_until(lambda: stream._queue.full())                              # 讀取執行緒在佇列滿時阻塞（反壓）
    time.sleep(0.3)
    assert stream._queue.qsize() <= STREAM_QUEUE_SIZE and reader_threads()
    stream.close()
    assert wait_until(lambda: not reader_threads(), 3)


# ---- 轉接器接真實傳輸（本機 SSE 伺服器）-----------------------------------------------------------------------

def _provider(httpd):
    from app.llm.openai_compatible import OpenAICompatibleProvider

    return OpenAICompatibleProvider("sk-or-v1-fake-key-for-tests-0123456789", base_url=f"http://127.0.0.1:{httpd.server_port}/v1")


def _request(**overrides):
    from app.llm.types import LLMContent, LLMRequest

    values = dict(
        model="vendor/narrator", system_instruction="你是敘事者。", contents=(LLMContent("user", ("開始",)),),
        response_schema=None, timeout_seconds=10.0, max_output_tokens=100,
    )
    values.update(overrides)
    return LLMRequest(**values)


def test_the_provider_streams_end_to_end_over_the_real_transport(server):
    from app.llm.types import StreamEnd, StreamText

    server.mode = "openai"
    events = list(_provider(server).stream(_request()))
    assert [e.text for e in events if isinstance(e, StreamText)] == ["清晨的", "海風。"]
    end = events[-1]
    assert isinstance(end, StreamEnd) and (end.finish_reason, end.prompt_tokens, end.completion_tokens) == ("stop", 9, 4)
    path, headers, body = server.seen[0]
    assert path == "/v1/chat/completions" and headers["authorization"].startswith("Bearer ") and headers["accept"] == "text/event-stream"
    sent = json.loads(body.decode("utf-8"))
    assert sent["stream"] is True and sent["stream_options"] == {"include_usage": True} and sent["reasoning"] == {"enabled": False}


def test_closing_the_provider_stream_midway_stops_the_reader_thread_and_reads_nothing_more(server):
    from app.llm.types import StreamText

    server.mode = "openai_hang"
    handle = _provider(server).stream(_request(timeout_seconds=30.0))
    iterator = iter(handle)
    first = next(event for event in iterator if isinstance(event, StreamText))
    assert first.text == "清晨的" and reader_threads()
    handle.close()
    assert wait_until(lambda: not reader_threads(), 3)
    assert list(iterator) == []


def test_a_real_429_before_the_stream_is_a_rate_limited_error_with_no_body_kept(server):
    from app.llm.types import LLMTransientError

    server.mode = "429"
    with pytest.raises(LLMTransientError) as caught:
        list(_provider(server).stream(_request()))
    assert caught.value.kind == "rate_limited" and "SECRET-ECHO" not in f"{caught.value!r} {vars(caught.value)!r}"
