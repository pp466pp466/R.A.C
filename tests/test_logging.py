"""service-observability 規格：結構化日誌、敏感資料不得寫入、請求識別碼。"""
import io
import json
import logging

import pytest
from flask import g

from app import create_app
from app.config import load_config
from app.observability.json_logging import (
    ALLOWED_FIELDS,
    JsonFormatter,
    RequestContextFilter,
    redact,
)

BASE_KEYS = {"time", "level", "logger", "message"}
LEAKS = ["hunter2", "cookiesecret", "bearer-token-xyz", "S3cretPw", "alice@example.com", "abc123secret"]


@pytest.fixture()
def log_lines():
    """把 JSON 日誌導到記憶體，回傳讀取已解析日誌行的函式。"""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestContextFilter())
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    def read() -> list[dict]:
        return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]

    read.raw = stream.getvalue  # type: ignore[attr-defined]
    yield read
    root.removeHandler(handler)
    root.setLevel(previous_level)


@pytest.fixture()
def app():
    application = create_app(load_config(environ={"APP_ENV": "testing"}))

    application.add_url_rule("/api/submit", "submit", lambda: {"ok": True}, methods=["POST"])

    def boom():
        raise RuntimeError("password=hunter2 連線字串 mssql://sa:S3cretPw@localhost/project")

    def with_user():
        g.user_id = 7
        return {"ok": True}

    application.add_url_rule("/api/boom", "boom", boom)
    application.add_url_rule("/api/with-user", "with_user", with_user)
    application.add_url_rule("/verify/<token>", "verify", lambda token: "ok")
    application.testing = False
    return application


def _request_lines(lines):
    return [line for line in lines if line["message"] == "請求完成"]


# ---- 結構化日誌 ---------------------------------------------------------------

def test_one_request_produces_one_valid_json_line_with_allowed_fields_only(app, log_lines):
    app.test_client().get("/")
    lines = _request_lines(log_lines())
    assert len(lines) == 1
    line = lines[0]
    assert set(line) <= BASE_KEYS | set(ALLOWED_FIELDS)
    assert line["method"] == "GET" and line["path"] == "/" and line["status"] == 200
    assert isinstance(line["latency_ms"], (int, float)) and line["latency_ms"] >= 0
    assert line["level"] == "INFO"


def test_chinese_message_is_not_escaped(app, log_lines):
    app.test_client().get("/")
    assert "請求完成" in log_lines.raw()
    assert "\\u" not in log_lines.raw()


def test_path_never_contains_the_query_string(app, log_lines):
    app.test_client().get("/?token=abc123secret&user=alice@example.com")
    output = log_lines.raw()
    assert "abc123secret" not in output
    assert "alice@example.com" not in output
    assert "?" not in _request_lines(log_lines())[0]["path"]


def test_path_tokens_are_logged_as_the_route_rule_not_the_actual_value(app, log_lines):
    app.test_client().get("/verify/abc123secret")
    line = _request_lines(log_lines())[0]
    assert line["path"] == "/verify/<token>"
    assert "abc123secret" not in log_lines.raw()


def test_unmatched_paths_are_logged_without_query(app, log_lines):
    app.test_client().get("/no-such-page?x=abc123secret")
    line = _request_lines(log_lines())[0]
    assert line["status"] == 404 and line["path"] == "/no-such-page"
    assert "abc123secret" not in log_lines.raw()


def test_user_id_is_logged_when_known(app, log_lines):
    app.test_client().get("/api/with-user")
    assert _request_lines(log_lines())[0]["user_id"] == 7


# ---- 敏感資料不得寫入日誌 -------------------------------------------------------

def test_sensitive_headers_cookies_and_form_fields_never_appear(app, log_lines):
    client = app.test_client()
    client.set_cookie("session", "cookiesecret")
    client.post(
        "/api/submit",
        data={"password": "hunter2", "email": "alice@example.com"},
        headers={"Authorization": "Bearer bearer-token-xyz"},
    )
    output = log_lines.raw()
    for leak in LEAKS:
        assert leak not in output, f"日誌不應包含 {leak}"


def test_exception_logs_only_type_and_location_never_the_message(app, log_lines):
    app.test_client().get("/api/boom")
    output = log_lines.raw()
    for leak in ("hunter2", "S3cretPw", "mssql://"):
        assert leak not in output
    error_line = next(line for line in log_lines() if line["message"] == "未預期的例外")
    assert error_line["error_type"] == "RuntimeError"
    assert ":" in error_line["error_location"]
    assert "Traceback" not in output


def test_logger_exception_calls_from_other_code_are_also_reduced_to_type_and_location(log_lines):
    try:
        raise ValueError("api_key=sk-very-secret")
    except ValueError:
        logging.getLogger("some.library").exception("外部函式庫的錯誤")
    output = log_lines.raw()
    assert "sk-very-secret" not in output and "Traceback" not in output
    assert json.loads(output.splitlines()[-1])["error_type"] == "ValueError"


def test_extra_fields_outside_the_allowlist_are_dropped(log_lines):
    logging.getLogger("app.test").info(
        "測試", extra={"password": "hunter2", "email": "alice@example.com", "request_id": "abcdefgh1234", "authorization": "x"}
    )
    line = log_lines()[-1]
    assert "password" not in line and "email" not in line and "authorization" not in line
    assert line["request_id"] == "abcdefgh1234"


def test_free_text_is_redacted_as_defence_in_depth(log_lines):
    logging.getLogger("app.test").info("連線 mssql+pyodbc://sa:S3cretPw@localhost/db 失敗 password=hunter2 Bearer bearer-token-xyz")
    output = log_lines.raw()
    for leak in ("S3cretPw", "hunter2", "bearer-token-xyz"):
        assert leak not in output


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("postgres://user:pw12345@host/db", "pw12345"),
        ("password: topsecret", "topsecret"),
        ("API_KEY=AIzaFakeKey", "AIzaFakeKey"),
        ("Authorization: Basic Zm9vOmJhcg==", "Zm9vOmJhcg=="),
        ("set-cookie token=abc", "abc"),
    ],
)
def test_redact_masks_common_secret_shapes(text, secret):
    assert secret not in redact(text)


# ---- 請求識別碼 ---------------------------------------------------------------

def test_missing_request_id_is_generated_and_used_everywhere(app, log_lines):
    response = app.test_client().get("/")
    request_id = response.headers["X-Request-ID"]
    assert len(request_id) == 32 and request_id.isalnum()
    assert _request_lines(log_lines())[0]["request_id"] == request_id


def test_valid_upstream_request_id_is_reused(app, log_lines):
    response = app.test_client().get("/", headers={"X-Request-ID": "trace-1234_ABCD"})
    assert response.headers["X-Request-ID"] == "trace-1234_ABCD"
    assert _request_lines(log_lines())[0]["request_id"] == "trace-1234_ABCD"


@pytest.mark.parametrize("bad", ["short", "a" * 65, "has spaces in it", "slash/not/allowed", "引號和中文識別碼測試用"])
def test_invalid_upstream_request_id_is_replaced(app, bad):
    response = app.test_client().get("/", headers={"X-Request-ID": bad})
    assert response.headers["X-Request-ID"] != bad
    assert len(response.headers["X-Request-ID"]) == 32


def test_error_responses_also_carry_the_request_id(app):
    response = app.test_client().get("/api/boom")
    assert response.status_code == 500
    assert response.headers.get("X-Request-ID")
