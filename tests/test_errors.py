"""application-layering 規格：API 一律 JSON、錯誤統一為繁體中文且不洩漏內部資訊。"""
import logging

import pytest

from app import create_app
from app.config import load_config

SECRET_IN_EXCEPTION = "資料庫密碼是 S3cretPw-leak-check"


@pytest.fixture()
def app():
    application = create_app(load_config(environ={"APP_ENV": "testing"}))

    def api_boom():
        raise RuntimeError(SECRET_IN_EXCEPTION)

    def page_boom():
        raise RuntimeError(SECRET_IN_EXCEPTION)

    application.add_url_rule("/api/boom", "api_boom", api_boom)
    application.add_url_rule("/boom", "page_boom", page_boom)
    application.add_url_rule("/api/post-only", "post_only", lambda: {"ok": True}, methods=["POST"])
    application.testing = False  # 讓未預期例外走錯誤處理器，而不是重新拋出
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


def _assert_clean(text: str):
    for forbidden in ("Traceback", "File \"", ".py", "S3cretPw", "sqlalchemy", "SELECT "):
        assert forbidden not in text, f"回應不應包含：{forbidden}"


def test_api_success_is_json(app):
    app.add_url_rule("/api/ping", "ping", lambda: {"ok": True})
    response = app.test_client().get("/api/ping")
    assert response.status_code == 200
    assert response.is_json


def test_unknown_api_path_is_a_json_404_not_an_html_page(client):
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.is_json
    error = response.get_json()["error"]
    assert error["code"] == "not_found"
    assert error["message"] == "找不到您要的內容。"


def test_wrong_method_on_api_is_a_json_405(client):
    response = client.get("/api/post-only")
    assert response.status_code == 405
    assert response.is_json
    assert response.get_json()["error"]["code"] == "method_not_allowed"


def test_unexpected_api_exception_is_a_clean_json_500(client, caplog):
    with caplog.at_level(logging.ERROR):
        response = client.get("/api/boom")
    assert response.status_code == 500
    assert response.is_json
    error = response.get_json()["error"]
    assert error["code"] == "internal_error"
    assert error["message"] == "系統發生錯誤，請稍後再試。"
    _assert_clean(response.get_data(as_text=True))


def test_unexpected_exception_message_never_reaches_the_logs(client, caplog):
    with caplog.at_level(logging.DEBUG):
        client.get("/api/boom")
    assert "S3cretPw" not in caplog.text
    # 仍然留下可追查的線索：例外類型與位置
    record = next(r for r in caplog.records if r.getMessage() == "未預期的例外")
    assert record.error_type == "RuntimeError"
    assert ":" in record.error_location


def test_unknown_page_is_a_chinese_404_page(client):
    response = client.get("/no-such-page")
    assert response.status_code == 404
    assert response.content_type.startswith("text/html")
    html = response.get_data(as_text=True)
    assert "找不到您要的內容" in html
    assert 'lang="zh-Hant-TW"' in html
    _assert_clean(html)


def test_unexpected_page_exception_is_a_clean_chinese_500_page(client):
    response = client.get("/boom")
    assert response.status_code == 500
    html = response.get_data(as_text=True)
    assert "系統發生錯誤，請稍後再試" in html
    _assert_clean(html)
