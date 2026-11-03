"""lockout-appeals 規格：被鎖定玩家的申訴 API 與 `/appeal` 頁面（長度、頻率、待處理、404、個資不進日誌與稽核）。"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import AdminAuditLog, Base, LockAppeal, User
from app.repositories.lock_appeal_repository import LockAppealRepository
from app.security.redirects import DEFAULT_AFTER_LOGIN
from tests.support.auth import START, FakeClock, add_user
from tests.support.client import raw_client
from tests.support.idor import logged_in_client

LOCKED = dict(locked_at=START, lock_reason="rate_limited")
SECRET = "只該出現在申訴裡的私密內容SECRET-APPEAL-TEXT"
STATE_PATTERN = re.compile(r'<script type="application/json" id="initial-state">(.*?)</script>', re.S)


@pytest.fixture()
def app():
    application = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    with Session(application.extensions["db_engine"]) as s:
        application.ids = {
            "locked": add_user(s, 1, **LOCKED).id,
            "other_locked": add_user(s, 2, **LOCKED).id,
            "free": add_user(s, 3).id,
            "admin": add_user(s, 4, role="admin").id,
            "locked_no_age": add_user(s, 5, age=None, **LOCKED).id,
        }
    return application


def client_for(app, name):
    with Session(app.extensions["db_engine"]) as s:
        return logged_in_client(app, s.get(User, app.ids[name]))


def clock_of(app):
    return app.extensions["clock"]


def appeals_in(app) -> list[LockAppeal]:
    with Session(app.extensions["db_engine"]) as s:
        return list(s.scalars(select(LockAppeal).order_by(LockAppeal.id)))


def seed_appeal(app, name, *, minutes_ago, handled=False, content="之前的申訴"):
    with Session(app.extensions["db_engine"]) as s:
        appeal = LockAppealRepository(s).create(app.ids[name], content, clock_of(app)() - timedelta(minutes=minutes_ago))
        if handled:
            appeal.handled_at = clock_of(app)()
        s.commit()


def post(app, name, content):
    return client_for(app, name).post("/api/appeals", json={"content": content})


# ---- 提交 ---------------------------------------------------------------------------------------------

def test_a_locked_player_submits_an_appeal_and_gets_its_pending_status(app):
    response = post(app, "locked", "我只是不小心連按了很多次，請解除鎖定。")
    assert response.status_code == 201
    appeal = response.get_json()["appeal"]
    assert set(appeal) == {"id", "content", "status", "created_at"}
    assert appeal["status"] == "pending" and appeal["content"] == "我只是不小心連按了很多次，請解除鎖定。"
    (stored,) = appeals_in(app)
    assert stored.public_id == appeal["id"] and stored.user_id == app.ids["locked"] and stored.handled_at is None


def test_a_locked_player_who_has_not_confirmed_the_age_can_still_appeal(app):
    assert post(app, "locked_no_age", "申訴").status_code == 201


@pytest.mark.parametrize("length", [1, 200, 1000])
def test_lengths_up_to_a_thousand_characters_are_accepted(app, length):
    assert post(app, "locked", "字" * length).status_code == 201


def test_one_character_over_the_limit_is_refused_and_nothing_is_written(app):
    response = post(app, "locked", "字" * 1001)
    assert response.status_code == 400 and set(response.get_json()["error"]["fields"]) == {"content"}
    assert "1000" in response.get_json()["error"]["fields"]["content"] and appeals_in(app) == []


@pytest.mark.parametrize("content", ["", "   ", "\n\t \n", "\x00\x01\x02", None, 5, ["a"], {"a": 1}])
def test_empty_or_non_text_content_is_refused(app, content):
    response = post(app, "locked", content)
    assert response.status_code == 400 and set(response.get_json()["error"]["fields"]) == {"content"}
    assert appeals_in(app) == []


def test_the_length_is_counted_after_cleaning_nfc_and_control_characters(app):
    # 1000 個字加上 20 個控制字元：清理之後剛好 1000 字
    assert post(app, "locked", "字" * 1000 + "\x00" * 20).status_code == 201
    # 分解形式的 é（e + 組合重音）正規化為 1 個字元
    decomposed = "é" * 600                                       # 原始 1200 個碼位，NFC 之後 600 字
    response = post(app, "other_locked", decomposed)
    assert response.status_code == 201
    assert response.get_json()["appeal"]["content"] == unicodedata.normalize("NFC", decomposed)


def test_the_text_comes_back_exactly_as_plain_text(app):
    text = "<script>alert(1)</script> & \"引號\""
    assert post(app, "locked", text).get_json()["appeal"]["content"] == text


# ---- 頻率與待處理 -------------------------------------------------------------------------------------

def test_a_second_appeal_while_one_is_pending_is_a_409_and_writes_nothing(app):
    assert post(app, "locked", "第一則").status_code == 201
    response = post(app, "locked", "第二則")
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "appeal_pending"
    assert len(appeals_in(app)) == 1


def test_the_fourth_appeal_within_an_hour_is_a_429_even_when_the_earlier_ones_are_handled(app):
    for minutes_ago in (50, 30, 10):
        seed_appeal(app, "locked", minutes_ago=minutes_ago, handled=True)
    response = post(app, "locked", "第四則")
    assert response.status_code == 429 and response.get_json()["error"]["code"] == "appeal_rate_limited"
    assert len(appeals_in(app)) == 3


def test_appeals_older_than_an_hour_do_not_count_toward_the_hourly_limit(app):
    for minutes_ago in (90, 80, 70):
        seed_appeal(app, "locked", minutes_ago=minutes_ago, handled=True)
    seed_appeal(app, "locked", minutes_ago=10, handled=True)
    assert post(app, "locked", "新的").status_code == 201


def test_the_limits_are_per_account(app):
    assert post(app, "locked", "我的").status_code == 201
    assert post(app, "other_locked", "另一個人的").status_code == 201


# ---- 誰能用 -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["free", "admin"])
def test_an_account_that_is_not_locked_gets_the_same_404_as_a_missing_resource(app, name):
    for response in (post(app, name, "我沒有被鎖定"), client_for(app, name).get("/api/appeals/mine")):
        assert response.status_code == 404 and response.get_json()["error"]["code"] == "not_found"
    assert appeals_in(app) == []


def test_signed_out_requests_are_401(app):
    client = app.test_client()
    assert client.post("/api/appeals", json={"content": "x"}).status_code == 401
    assert client.get("/api/appeals/mine").status_code == 401


def test_a_post_without_the_csrf_token_is_refused(app):
    with Session(app.extensions["db_engine"]) as s:
        user = s.get(User, app.ids["locked"])
        client = raw_client(app)
        with client.session_transaction() as session:
            session["uid"], session["tv"] = user.id, user.token_version
    response = client.post("/api/appeals", json={"content": "x"}, headers={"Origin": "http://localhost"})
    assert response.status_code == 403 and appeals_in(app) == []


def test_the_body_must_be_a_json_object(app):
    assert client_for(app, "locked").post("/api/appeals", data="content=x").status_code == 415


# ---- 自己的申訴 ---------------------------------------------------------------------------------------

def test_mine_lists_only_my_appeals_newest_first_with_their_status(app):
    seed_appeal(app, "locked", minutes_ago=300, handled=True, content="很久以前的")
    seed_appeal(app, "other_locked", minutes_ago=5, content="別人的")
    assert post(app, "locked", "最新的").status_code == 201
    appeals = client_for(app, "locked").get("/api/appeals/mine").get_json()["appeals"]
    assert [(a["content"], a["status"]) for a in appeals] == [("最新的", "pending"), ("很久以前的", "handled")]
    assert "別人的" not in json.dumps(appeals, ensure_ascii=False)


def test_mine_is_empty_before_the_first_appeal(app):
    assert client_for(app, "locked").get("/api/appeals/mine").get_json() == {"appeals": []}


# ---- 個資不進日誌與稽核 -------------------------------------------------------------------------------

def test_the_audit_row_and_the_log_carry_only_ids_and_the_length(app, log_lines):
    assert post(app, "locked", SECRET).status_code == 201
    with Session(app.extensions["db_engine"]) as s:
        (audit,) = s.scalars(select(AdminAuditLog).where(AdminAuditLog.action == "appeal.submit"))
        locked = s.get(User, app.ids["locked"])
        assert audit.admin_user_id is None and audit.target_user_public_id == locked.public_id
        assert audit.detail_json == {"length": len(SECRET)} and audit.reason is None
        assert SECRET not in repr((audit.detail_json, audit.reason, audit.action))
    events = [line for line in log_lines() if line.get("event") == "account.appeal_submitted"]
    assert len(events) == 1 and events[0]["user_id"] == app.ids["locked"]
    assert SECRET not in log_lines.raw()


def test_refused_submissions_leave_no_audit_row_and_no_content_in_the_log(app, log_lines):
    post(app, "locked", SECRET)
    post(app, "locked", SECRET + "第二則")                                 # 409
    post(app, "free", SECRET)                                              # 404
    post(app, "locked", "字" * 1001 + SECRET)                              # 400
    assert SECRET not in log_lines.raw()
    with Session(app.extensions["db_engine"]) as s:
        assert s.query(AdminAuditLog).filter_by(action="appeal.submit").count() == 1


# ---- /appeal 頁面 -------------------------------------------------------------------------------------

def test_a_locked_player_opens_the_appeal_page_with_the_limits_and_nothing_personal(app):
    response = client_for(app, "locked").get("/appeal")
    assert response.status_code == 200
    state = json.loads(STATE_PATTERN.search(response.get_data(as_text=True)).group(1))
    assert state["appeal"] == {"maxChars": 1000, "hourlyLimit": 3} and "user" not in state
    assert "user1@example.com" not in response.get_data(as_text=True)


def test_a_player_who_is_not_locked_is_sent_to_the_default_page(app):
    response = client_for(app, "free").get("/appeal")
    assert response.status_code == 302 and response.headers["Location"] == DEFAULT_AFTER_LOGIN


def test_an_anonymous_visitor_is_sent_to_the_login_page(app):
    response = app.test_client().get("/appeal")
    assert response.status_code == 302 and response.headers["Location"].startswith("/login?next=")


def test_a_player_still_waiting_for_the_age_step_is_sent_to_it_not_to_the_appeal_page(app):
    with Session(app.extensions["db_engine"]) as s:
        pending = add_user(s, 9, age=None)
        client = logged_in_client(app, pending)
    response = client.get("/appeal")
    assert response.status_code == 302 and response.headers["Location"] == "/age"
