"""rpg-turn-revision 規格：修訂的三個業務錯誤（版本衝突、不可修訂、內容不合規定）的狀態碼、代碼與回應內容。

回應只帶它該帶的東西：不可修訂只有固定列舉的原因；內容不合規定只有欄位說明（不回顯玩家填的內容）、沒有審查類別。
"""
import pytest

from app import create_app
from app.config import load_config
from app.services.errors import (
    NOT_REVISABLE_MESSAGES, Conflict, NotRevisable, RevisionConflict, RevisionInvalid, Unprocessable, ValidationError,
)

TRACE_MARKERS = ("Traceback", 'File "', "app\\", "app/", "sqlalchemy", "SELECT ")
REASONS = ("ended", "not_last", "in_progress", "no_snapshot")


@pytest.fixture()
def client():
    app = create_app(load_config(environ={"APP_ENV": "testing"}))

    def raiser(error):
        def view():
            raise error
        return view

    cases = {
        "conflict": RevisionConflict(),
        "invalid": RevisionInvalid({"reason": "請簡短說明原因，至少 5 字。", "category": "請選擇一個分類。"}),
        "turn-in-progress": Conflict("這個回合仍在處理中，請稍候。", code="turn_in_progress"),
        **{f"not-revisable-{reason}": NotRevisable(reason) for reason in REASONS},
    }
    for name, error in cases.items():
        app.add_url_rule(f"/api/_t/{name}", f"api_{name}", raiser(error))
    return app.test_client()


def has_chinese(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def test_a_revision_conflict_is_a_409_with_its_own_code_and_a_chinese_message(client):
    response = client.get("/api/_t/conflict")
    assert response.status_code == 409 and response.mimetype == "application/json"
    body = response.get_json()
    assert body["error"]["code"] == "revision_conflict" and has_chinese(body["error"]["message"]) and set(body) == {"error"}


@pytest.mark.parametrize("reason", REASONS)
def test_not_revisable_is_a_409_carrying_only_the_enumerated_reason(client, reason):
    response = client.get(f"/api/_t/not-revisable-{reason}")
    assert response.status_code == 409
    body = response.get_json()
    assert body["error"]["code"] == "not_revisable" and body["error"]["message"] == NOT_REVISABLE_MESSAGES[reason]
    assert has_chinese(body["error"]["message"]) and body["reason"] == reason and set(body) == {"error", "reason"}
    for marker in TRACE_MARKERS:
        assert marker not in response.get_data(as_text=True)


def test_the_four_reasons_have_distinct_messages(client):
    assert set(NOT_REVISABLE_MESSAGES) == set(REASONS) and len(set(NOT_REVISABLE_MESSAGES.values())) == 4


def test_invalid_revision_content_is_a_422_with_field_messages_and_no_categories(client):
    response = client.get("/api/_t/invalid")
    assert response.status_code == 422
    body = response.get_json()
    assert body["error"]["code"] == "validation_error" and set(body["error"]) == {"code", "message", "fields"}
    assert set(body["error"]["fields"]) == {"reason", "category"} and set(body) == {"error"}


def test_the_new_errors_sit_in_the_hierarchy_that_decides_their_status():
    assert issubclass(RevisionConflict, Conflict) and issubclass(NotRevisable, Conflict) and not issubclass(RevisionConflict, NotRevisable)
    assert issubclass(RevisionInvalid, Unprocessable) and not issubclass(RevisionInvalid, ValidationError)
    assert NotRevisable("ended", "自訂說明").message == "自訂說明" and NotRevisable("unknown-reason").message == NotRevisable.default_message


def test_turn_in_progress_keeps_its_existing_conflict_shape(client):
    body = client.get("/api/_t/turn-in-progress").get_json()
    assert client.get("/api/_t/turn-in-progress").status_code == 409 and body["error"]["code"] == "turn_in_progress" and set(body) == {"error"}
