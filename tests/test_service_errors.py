"""application-layering 規格：Service 的業務錯誤統一對應 HTTP 回應；API 請求本文的格式與大小。"""
import pytest
from flask import jsonify

from app import create_app
from app.config import load_config
from app.controllers.request_helpers import read_json_object
from app.services.errors import (
    Conflict, Forbidden, NotFound, ServiceError, TooManyRequests, Unauthenticated, ValidationError,
)

TRACE_MARKERS = ("Traceback", 'File "', "app\\", "app/", "sqlalchemy", "SELECT ")


@pytest.fixture()
def app():
    app = create_app(load_config(environ={"APP_ENV": "testing"}))

    def raiser(error):
        def view():
            raise error
        return view

    cases = {
        "validation": ValidationError(fields={"password": "密碼長度須為 8 至 128 字"}),
        "validation-plain": ValidationError("請輸入內容。"),
        "unauthenticated": Unauthenticated(),
        "forbidden": Forbidden(),
        "notfound": NotFound(),
        "conflict": Conflict(),
        "toomany": TooManyRequests(),
        "custom": Unauthenticated("信箱或密碼不正確。", code="invalid_credentials"),
        "unknown-subclass": type("Odd", (ServiceError,), {})("特殊錯誤"),
    }
    for name, error in cases.items():
        app.add_url_rule(f"/api/_t/{name}", f"api_{name}", raiser(error))
        app.add_url_rule(f"/_t/{name}", f"page_{name}", raiser(error))

    @app.post("/api/_t/body")
    def body():
        return jsonify(received=read_json_object())

    return app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.mark.parametrize(
    ("name", "status", "code"),
    [
        ("validation", 400, "validation_error"), ("unauthenticated", 401, "unauthorized"),
        ("forbidden", 403, "forbidden"), ("notfound", 404, "not_found"),
        ("conflict", 409, "conflict"), ("toomany", 429, "too_many_requests"),
    ],
)
def test_each_service_error_maps_to_its_status_with_a_json_body(client, name, status, code):
    response = client.get(f"/api/_t/{name}")
    assert response.status_code == status
    assert response.mimetype == "application/json"
    error = response.get_json()["error"]
    assert error["code"] == code
    assert any("一" <= ch <= "鿿" for ch in error["message"]), "訊息應為繁體中文"


def test_validation_error_carries_the_field_messages(client):
    error = client.get("/api/_t/validation").get_json()["error"]
    assert error["fields"] == {"password": "密碼長度須為 8 至 128 字"}


def test_validation_error_without_fields_has_no_fields_key(client):
    error = client.get("/api/_t/validation-plain").get_json()["error"]
    assert "fields" not in error and error["message"] == "請輸入內容。"


def test_an_error_can_override_its_code_and_message(client):
    response = client.get("/api/_t/custom")
    assert response.status_code == 401
    assert response.get_json()["error"] == {"code": "invalid_credentials", "message": "信箱或密碼不正確。"}


def test_an_unclassified_service_error_is_a_400_not_a_500(client):
    response = client.get("/api/_t/unknown-subclass")
    assert response.status_code == 400
    assert response.get_json()["error"]["message"] == "特殊錯誤"


def test_not_found_matches_a_genuinely_missing_route_in_status_and_shape(client):
    """非擁有者與不存在必須得到相同的回應：同樣的狀態碼、代碼與訊息。"""
    from_policy = client.get("/api/_t/notfound").get_json()
    from_router = client.get("/api/_t/this-route-does-not-exist").get_json()
    assert from_policy == from_router


@pytest.mark.parametrize("name", ["validation", "unauthenticated", "forbidden", "notfound", "conflict", "toomany"])
def test_error_responses_never_leak_internals(client, name):
    text = client.get(f"/api/_t/{name}").get_data(as_text=True)
    for marker in TRACE_MARKERS:
        assert marker not in text


def test_pages_get_a_chinese_error_page_instead_of_json(client):
    response = client.get("/_t/notfound")
    assert response.status_code == 404
    assert response.mimetype == "text/html"
    html = response.get_data(as_text=True)
    assert "找不到您要的內容" in html and 'lang="zh-Hant-TW"' in html
    assert client.get("/_t/forbidden").status_code == 403


# ---- API 請求本文 --------------------------------------------------------------

def test_a_json_object_body_is_read(client):
    response = client.post("/api/_t/body", json={"email": "a@example.com", "名稱": "王小明"})
    assert response.status_code == 200
    assert response.get_json()["received"] == {"email": "a@example.com", "名稱": "王小明"}


def test_a_form_body_is_a_415_json_error(client):
    response = client.post("/api/_t/body", data={"email": "a@example.com"})
    assert response.status_code == 415
    assert response.mimetype == "application/json"
    assert response.get_json()["error"]["code"] == "unsupported_media_type"


@pytest.mark.parametrize("raw", ["[1, 2]", "42", '"text"', "null", "", "{not json"])
def test_json_that_is_not_an_object_is_a_400_json_error(client, raw):
    response = client.post("/api/_t/body", data=raw, content_type="application/json")
    assert response.status_code == 400
    assert response.mimetype == "application/json"
    assert response.get_json()["error"]["code"] == "bad_request"


def test_a_body_over_64_kb_is_a_413_json_error(client):
    huge = '{"x": "' + "a" * (64 * 1024 + 10) + '"}'
    response = client.post("/api/_t/body", data=huge, content_type="application/json")
    assert response.status_code == 413
    assert response.mimetype == "application/json"
    assert response.get_json()["error"]["code"] == "payload_too_large"


def test_a_body_just_under_the_limit_is_accepted(client):
    payload = '{"x": "' + "a" * (64 * 1024 - 100) + '"}'
    assert client.post("/api/_t/body", data=payload, content_type="application/json").status_code == 200
