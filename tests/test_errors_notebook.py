"""rpg-notebook 規格：記事簿的三個業務錯誤（太長、被規則層擋下、版本衝突）的狀態碼、代碼與回應內容。

回應只帶它該帶的東西：太長只有整數（字數與容量）；被擋只有類別、不回顯內容；衝突附後端目前的記事簿（以後端為準）。
"""
import pytest

from app import create_app
from app.config import load_config
from app.services.errors import Conflict, NotebookBlocked, NotebookConflict, NotebookTooLong, Unprocessable

TRACE_MARKERS = ("Traceback", 'File "', "app\\", "app/", "sqlalchemy", "SELECT ")
LATEST = {"text": "後端最新的記事。", "version": 7, "length": 8, "capacity": 2000, "reserve": 500, "full": False}


@pytest.fixture()
def client():
    app = create_app(load_config(environ={"APP_ENV": "testing"}))

    def raiser(error):
        def view():
            raise error
        return view

    cases = {
        "too-long": NotebookTooLong(2240, 2000),
        "blocked": NotebookBlocked(("minor_sexual", "non_consensual")),
        "blocked-empty": NotebookBlocked(),
        "conflict": NotebookConflict(LATEST),
    }
    for name, error in cases.items():
        app.add_url_rule(f"/api/_t/{name}", f"api_{name}", raiser(error))
    return app.test_client()


def has_chinese(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


@pytest.mark.parametrize(
    ("name", "status", "code"),
    [("too-long", 422, "notebook_too_long"), ("blocked", 422, "notebook_blocked"), ("conflict", 409, "notebook_conflict")],
)
def test_each_notebook_error_has_its_status_and_code_with_a_chinese_message(client, name, status, code):
    response = client.get(f"/api/_t/{name}")
    assert response.status_code == status and response.mimetype == "application/json"
    error = response.get_json()["error"]
    assert error["code"] == code and has_chinese(error["message"])
    for marker in TRACE_MARKERS:
        assert marker not in response.get_data(as_text=True)


def test_too_long_carries_only_the_two_integers(client):
    body = client.get("/api/_t/too-long").get_json()
    assert (body["length"], body["capacity"]) == (2240, 2000)
    assert set(body) == {"error", "length", "capacity"} and set(body["error"]) == {"code", "message"}


def test_blocked_carries_only_the_categories_and_never_echoes_content(client):
    body = client.get("/api/_t/blocked").get_json()
    assert body["categories"] == ["minor_sexual", "non_consensual"] and set(body) == {"error", "categories"}
    assert client.get("/api/_t/blocked-empty").get_json()["categories"] == []


def test_conflict_carries_the_latest_notebook_from_the_backend(client):
    body = client.get("/api/_t/conflict").get_json()
    assert body["notebook"] == LATEST and set(body) == {"error", "notebook"}


def test_the_error_hierarchy_decides_the_status_and_a_message_can_be_overridden():
    assert issubclass(NotebookTooLong, Unprocessable) and issubclass(NotebookBlocked, Unprocessable)
    assert issubclass(NotebookConflict, Conflict) and not issubclass(NotebookConflict, NotebookTooLong)
    assert NotebookTooLong(1, 1, "自訂說明").message == "自訂說明" and NotebookConflict({}, "自訂").message == "自訂"


def test_the_numbers_are_kept_as_integers():
    error = NotebookTooLong("2240", 2000.0)
    assert (error.length, error.capacity) == (2240, 2000) and all(type(v) is int for v in (error.length, error.capacity))
