"""database-access 規格：請求級交易預設在 4xx 時回復，但「失敗的記帳」可以明確要求保留。

這是一個只有端到端才會暴露的問題：登入失敗回 401，若隨之回復，失敗次數永遠不會累積。
"""
import pytest
from flask import abort
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.db.session import defer_until_commit, get_session, keep_changes_on_error
from app.models import Base, User
from app.models.user import STATUS_ACTIVE


@pytest.fixture()
def app():
    app = create_app(load_config(environ={"APP_ENV": "testing"}))
    Base.metadata.create_all(app.extensions["db_engine"])
    app.calls = []

    def add(n):
        get_session().add(User(email=f"u{n}@example.com", email_canonical=f"u{n}@example.com", display_name="x", status=STATUS_ACTIVE))

    @app.post("/client-error-keep")
    def client_error_keep():
        add(1)
        keep_changes_on_error()
        defer_until_commit(lambda: app.calls.append("deferred"))
        abort(401)

    @app.post("/client-error-default")
    def client_error_default():
        add(2)
        abort(401)

    @app.post("/server-error-keep")
    def server_error_keep():
        add(3)
        keep_changes_on_error()
        abort(503)

    @app.post("/exception-keep")
    def exception_keep():
        add(4)
        keep_changes_on_error()
        raise RuntimeError("boom")

    @app.post("/plain-ok")
    def plain_ok():
        add(5)
        return {"ok": True}

    return app


def emails(app) -> set[str]:
    with Session(app.extensions["db_engine"]) as s:
        return set(s.scalars(select(User.email)))


def test_a_4xx_response_keeps_the_changes_only_when_asked(app):
    client = app.test_client()
    assert client.post("/client-error-keep").status_code == 401
    assert client.post("/client-error-default").status_code == 401
    assert emails(app) == {"u1@example.com"}


def test_the_deferred_actions_of_a_kept_request_still_run(app):
    app.test_client().post("/client-error-keep")
    assert app.calls == ["deferred"]


@pytest.mark.parametrize("path", ["/server-error-keep", "/exception-keep"])
def test_a_5xx_response_or_an_exception_always_rolls_back_even_when_asked(app, path):
    assert app.test_client().post(path).status_code >= 500
    assert emails(app) == set()


def test_the_request_is_not_affected_by_an_earlier_one_that_asked_to_keep(app):
    client = app.test_client()
    client.post("/client-error-keep")
    client.post("/client-error-default")
    assert emails(app) == {"u1@example.com"}          # 第二個請求沒有要求保留，仍然回復


def test_a_successful_request_commits_as_before(app):
    assert app.test_client().post("/plain-ok").status_code == 200
    assert emails(app) == {"u5@example.com"}
    with Session(app.extensions["db_engine"]) as s:
        assert s.scalar(select(func.count(User.id))) == 1
