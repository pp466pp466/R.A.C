"""database-access 規格：提交成功後才執行的延後動作。"""
import logging

import pytest
from flask import abort, g, has_request_context
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.db.session import defer_until_commit, get_session
from app.models import Base, User
from app.models.user import STATUS_ACTIVE
from tests.support.client import buffered_client


@pytest.fixture()
def app():
    app = create_app(load_config(environ={"APP_ENV": "testing"}))
    Base.metadata.create_all(app.extensions["db_engine"])
    app.calls = []          # 延後動作執行的紀錄

    def add_user(n: int):
        get_session().add(User(email=f"u{n}@example.com", email_canonical=f"u{n}@example.com",
                               display_name=f"u{n}", status=STATUS_ACTIVE))

    def visible_users() -> int:
        """從另一條 Session 看到的使用者數：用來證明動作執行時資料已經提交。"""
        with Session(app.extensions["db_engine"]) as other:
            return other.query(User).count()

    @app.post("/ok")
    def ok():
        add_user(1)
        defer_until_commit(lambda: app.calls.append(("first", visible_users())))
        defer_until_commit(lambda: app.calls.append(("second", visible_users())))
        return {"ok": True}

    @app.post("/error-status")
    def error_status():
        add_user(2)
        defer_until_commit(lambda: app.calls.append(("never", None)))
        abort(400)

    @app.post("/raises")
    def raises():
        add_user(3)
        defer_until_commit(lambda: app.calls.append(("never", None)))
        raise RuntimeError("boom")

    @app.post("/failing-action")
    def failing_action():
        add_user(4)

        def explode():
            raise ValueError("secret-value-should-not-be-logged")

        defer_until_commit(lambda: app.calls.append(("before", None)))
        defer_until_commit(explode)
        defer_until_commit(lambda: app.calls.append(("after", None)))
        return {"ok": True}

    @app.post("/environment")
    def environment():
        defer_until_commit(lambda: app.calls.append(("env", has_request_context())))
        return {"ok": True}

    @app.post("/commit-fails")
    def commit_fails():
        defer_until_commit(lambda: app.calls.append(("never", None)))
        session = get_session()
        session.add(User(email="dup@example.com", email_canonical="dup@example.com", display_name="d", status=STATUS_ACTIVE))
        session.add(User(email="dup@example.com", email_canonical="dup2@example.com", display_name="d", status=STATUS_ACTIVE))
        return {"ok": True}

    @app.get("/read-only")
    def read_only():
        defer_until_commit(lambda: app.calls.append(("get", None)))
        return {"ok": True}

    return app


@pytest.fixture()
def client(app):
    return buffered_client(app)


def test_actions_run_in_order_after_the_commit_is_visible_to_other_connections(app, client):
    assert client.post("/ok").status_code == 200
    assert app.calls == [("first", 1), ("second", 1)]      # 另一條連線已經看得到這筆資料


def test_actions_are_dropped_when_the_response_is_an_error(app, client):
    assert client.post("/error-status").status_code == 400
    assert app.calls == []


def test_actions_are_dropped_when_the_request_raises(app, client):
    assert client.post("/raises").status_code == 500
    assert app.calls == []


def test_actions_are_dropped_when_the_commit_itself_fails(app, client):
    assert client.post("/commit-fails").status_code == 500
    assert app.calls == []


def test_a_failing_action_does_not_change_the_response_or_stop_the_others(app, client, caplog):
    with caplog.at_level(logging.ERROR, logger="app.db.session"):
        response = client.post("/failing-action")
    assert response.status_code == 200 and response.get_json() == {"ok": True}
    assert app.calls == [("before", None), ("after", None)]
    record = next(r for r in caplog.records if r.getMessage() == "延後動作失敗")
    assert record.error_type == "ValueError"
    assert "secret-value-should-not-be-logged" not in record.getMessage()
    assert "secret-value-should-not-be-logged" not in str(getattr(record, "error_location", ""))


def test_a_failure_log_carries_the_original_request_id(app, client, caplog):
    with caplog.at_level(logging.ERROR, logger="app.db.session"):
        client.post("/failing-action", headers={"X-Request-ID": "req-abcdef123456"})
    record = next(r for r in caplog.records if r.getMessage() == "延後動作失敗")
    assert record.request_id == "req-abcdef123456"


def test_actions_run_after_the_request_and_its_session_are_gone(app, client):
    client.post("/environment")
    assert app.calls == [("env", False)]


def test_a_get_request_can_also_defer_actions(app, client):
    client.get("/read-only")
    assert app.calls == [("get", None)]


def test_actions_do_not_leak_into_the_next_request(app, client):
    client.post("/ok")
    app.calls.clear()
    client.post("/environment")
    assert app.calls == [("env", False)]          # 只有第二個請求自己登記的動作


def test_the_runtime_module_exposes_the_same_function_for_services():
    from app import runtime
    assert runtime.defer_until_commit is defer_until_commit


def test_without_a_buffered_client_the_actions_do_not_run_until_the_response_is_closed(app):
    """說明為什麼測試要用 buffered client：動作是掛在回應的 close 上。"""
    plain = app.test_client()
    response = plain.post("/ok", buffered=False)
    assert app.calls == []
    response.close()
    assert [name for name, _ in app.calls] == ["first", "second"]
