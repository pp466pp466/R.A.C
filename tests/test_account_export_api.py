"""personal-data-export 規格：`POST /api/account/export` 端到端（登入、CSRF、格式、只匯出本人、受限狀態、標頭、日誌）。"""
import io
import json
import zipfile
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base
from app.models.user import STATUS_DELETION_PENDING
from app.repositories.chat_repository import ChatRepository
from tests.support.age import birth_date_for_age
from tests.support.auth import GOOD_PASSWORD, START, FakeClock, add_user
from tests.support.idor import logged_in_client


@pytest.fixture()
def app():
    application = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


def make_member(app, n: int, **overrides):
    with Session(app.extensions["db_engine"]) as s:
        user = add_user(s, n, age=30, **overrides)
        s.refresh(user)
        s.expunge(user)
        return user


def add_conversation(app, user_id: int, text: str) -> None:
    with Session(app.extensions["db_engine"]) as s:
        chats = ChatRepository(s)
        conv = chats.create_conversation(user_id, client_id=f"{user_id:036d}", state={}, now=START)
        chats.append_message(user_id, conv, client_msg_id="a" * 36, role="user", content=text, now=START + timedelta(seconds=1))
        s.commit()


def test_export_requires_login(app):
    assert app.test_client().post("/api/account/export", json={"format": "json"}).status_code == 401


def test_export_without_a_csrf_token_is_refused(app):
    from tests.support.client import raw_client

    owner = make_member(app, 1)
    cookie = logged_in_client(app, owner).get_cookie("session").value
    forged = raw_client(app)
    forged.set_cookie("session", cookie)
    response = forged.post("/api/account/export", json={"format": "json"}, headers={"Origin": "http://localhost"})
    assert response.status_code == 403


def test_json_export_is_an_attachment_that_is_never_cached_and_contains_only_the_owner(app):
    owner, other = make_member(app, 1, display_name="王小明"), make_member(app, 2)
    add_conversation(app, owner.id, "我的對話內容")
    add_conversation(app, other.id, "別人的對話內容")
    client = logged_in_client(app, owner)

    response = client.post("/api/account/export", json={"format": "json", "user_id": other.id, "public_id": other.public_id})

    assert response.status_code == 200
    assert response.headers["Content-Disposition"] == 'attachment; filename=my-data-20261027.json'
    assert response.headers["Cache-Control"] == "no-store"
    assert response.mimetype == "application/json" and "utf-8" in response.headers["Content-Type"].lower()
    body = response.get_data()
    assert not body.startswith(b"\xef\xbb\xbf")
    data = json.loads(body.decode("utf-8"))
    assert data["account"]["display_name"] == "王小明" and data["account"]["birth_date"] == birth_date_for_age(30).isoformat()
    assert [c["messages"][0]["content"] for c in data["conversations"]] == ["我的對話內容"]
    assert "別人" not in body.decode("utf-8")


def test_csv_export_is_a_zip_with_the_documented_files(app):
    owner = make_member(app, 1)
    add_conversation(app, owner.id, "=1+1 我的內容")
    response = logged_in_client(app, owner).post("/api/account/export", json={"format": "csv"})
    assert response.status_code == 200 and response.mimetype == "application/zip"
    assert response.headers["Content-Disposition"] == "attachment; filename=my-data-20261027.zip"
    assert response.headers["Cache-Control"] == "no-store"
    with zipfile.ZipFile(io.BytesIO(response.get_data())) as archive:
        assert sorted(archive.namelist()) == ["account.csv", "appeals.csv", "messages.csv", "script_submissions.csv", "scripts.csv", "sessions.csv", "turns.csv", "wallet_entries.csv"]
        messages = archive.read("messages.csv")
    assert messages.startswith(b"\xef\xbb\xbf") and "'=1+1 我的內容".encode("utf-8") in messages


@pytest.mark.parametrize("body", [{"format": "xml"}, {"format": ""}, {}, {"format": ["json"]}])
def test_an_unknown_or_missing_format_is_a_400_field_error(app, body):
    owner = make_member(app, 1)
    response = logged_in_client(app, owner).post("/api/account/export", json=body)
    assert response.status_code == 400 and "format" in response.get_data(as_text=True)


def test_the_body_must_be_a_json_object(app):
    owner = make_member(app, 1)
    client = logged_in_client(app, owner)
    assert client.post("/api/account/export", data="format=json").status_code == 415
    assert client.post("/api/account/export", json=["json"]).status_code == 400


def test_an_account_awaiting_deletion_cannot_export(app):
    owner = make_member(app, 1, status=STATUS_DELETION_PENDING, deletion_requested_at=START, status_before_deletion="active")
    add_conversation(app, owner.id, "不應被匯出的內容")
    response = logged_in_client(app, owner).post("/api/account/export", json={"format": "json"})
    assert response.status_code == 403 and "account_deletion_pending" in response.get_data(as_text=True)
    assert "不應被匯出" not in response.get_data(as_text=True)


def test_the_log_has_the_event_name_the_id_and_the_format_only(app, log_lines):
    owner = make_member(app, 1, display_name="機密姓名")
    add_conversation(app, owner.id, "機密對話內容")
    logged_in_client(app, owner).post("/api/account/export", json={"format": "json"})
    events = [line for line in log_lines() if line.get("event") == "account.exported"]
    assert [(e["user_id"], e["reason"]) for e in events] == [(owner.id, "json")]
    for secret in ("機密姓名", "機密對話內容", "user1@example.com", birth_date_for_age(30).isoformat(), GOOD_PASSWORD):
        assert secret not in log_lines.raw()
