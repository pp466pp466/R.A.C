"""account-deletion 規格：`/api/account/delete` 與 `/api/account/cancel-deletion` 端到端
（CSRF、確認本人、所有裝置登出、受限登入狀態只能撤銷、撤銷後完整功能恢復、通知信、不被快取）。"""
import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base, User
from app.models.user import STATUS_ACTIVE, STATUS_DELETION_PENDING
from tests.support.auth import GOOD_PASSWORD, FakeClock, add_user, sign_in_as
from tests.support.client import raw_client
from tests.support.config_env import AUTH_ENV
from tests.support.email_flow import FakeSender
from tests.support.idor import logged_in_client

CONFIRM = "刪除我的帳號"
BASE = "https://cs.example.com"


@pytest.fixture()
def clock():
    return FakeClock()


@pytest.fixture()
def app(clock):
    application = create_app(load_config(environ={"APP_ENV": "testing"}), clock=clock)
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


def db(app) -> Session:
    return Session(app.extensions["db_engine"])


def make_member(app, n: int = 1, **overrides) -> int:
    with db(app) as s:
        return add_user(s, n, age=30, **overrides).id


def row(app, user_id: int) -> User:
    with db(app) as s:
        user = s.get(User, user_id)
        s.expunge(user)
        return user


class _Signed:
    status_code = 200
    def get_json(self):
        return {}


def login(client, email="user1@example.com"):
    """代表「通過 Google 登入」：以該信箱目前的 (uid, token_version) 寫入登入 cookie。玩家沒有密碼登入；
    申請刪除後 token_version 已加 1，所以每次都讀最新的版本（等同重新登入）。"""
    with Session(client.application.extensions["db_engine"]) as s:
        user = s.query(User).filter(User.email == email).one()
        sign_in_as(client, user.id, user.token_version)
    return _Signed()


# ---- 申請 -------------------------------------------------------------------------

def test_requesting_deletion_requires_login(app):
    client = app.test_client()
    assert client.post("/api/account/delete", json={"password": GOOD_PASSWORD}).status_code == 401
    assert client.post("/api/account/cancel-deletion", json={}).status_code == 401


def test_requesting_without_a_csrf_token_is_refused_and_changes_nothing(app):
    user_id = make_member(app)
    attacker = logged_in_client(app, row(app, user_id))
    forged = raw_client(app)
    forged.set_cookie("session", attacker.get_cookie("session").value)
    response = forged.post("/api/account/delete", json={"password": GOOD_PASSWORD}, headers={"Origin": "http://localhost"})
    assert response.status_code == 403 and row(app, user_id).status == STATUS_ACTIVE


def test_a_successful_request_stops_the_account_and_logs_out_every_device(app):
    user_id = make_member(app)
    device_a, device_b = app.test_client(), app.test_client()
    assert login(device_a).status_code == 200 and login(device_b).status_code == 200

    response = device_a.post("/api/account/delete", json={"password": GOOD_PASSWORD})
    body = response.get_json()
    assert response.status_code == 200 and body["ok"] is True and body["scheduled_at"] == "2026-11-03T12:00:00Z"
    assert "7 天" in body["message"]
    assert response.headers["Cache-Control"] == "no-store"
    assert row(app, user_id).status == STATUS_DELETION_PENDING
    assert device_a.get("/api/auth/me").status_code == 401           # 這台的 cookie 已清除
    assert device_b.get("/api/auth/me").status_code == 401           # 其他裝置的 token_version 已失效


def test_a_wrong_password_is_a_400_field_error_and_nothing_changes(app):
    user_id = make_member(app)
    client = app.test_client()
    login(client)
    response = client.post("/api/account/delete", json={"password": "definitely-the-wrong-password"})
    assert response.status_code == 400 and "password" in response.get_data(as_text=True)
    assert row(app, user_id).status == STATUS_ACTIVE and row(app, user_id).failed_login_count == 1       # 失敗已計入（4xx 仍保留）
    assert client.get("/api/auth/me").status_code == 200              # 仍登入著


def test_a_google_only_account_confirms_with_the_text(app):
    user_id = make_member(app, password=None)
    client = logged_in_client(app, row(app, user_id))
    refused = client.post("/api/account/delete", json={"confirm_text": "刪除帳號"})
    assert refused.status_code == 400 and "confirm_text" in refused.get_data(as_text=True)
    assert row(app, user_id).status == STATUS_ACTIVE
    assert client.post("/api/account/delete", json={"confirm_text": CONFIRM}).status_code == 200
    assert row(app, user_id).status == STATUS_DELETION_PENDING


def test_an_admin_cannot_delete_the_account(app):
    user_id = make_member(app, role="admin")
    client = app.test_client()
    sign_in_as(client, user_id)                                      # 管理員不能只靠密碼登入
    response = client.post("/api/account/delete", json={"password": GOOD_PASSWORD})
    assert response.status_code == 403 and "admin_delete_forbidden" in response.get_data(as_text=True)
    assert row(app, user_id).status == STATUS_ACTIVE


def test_privileged_fields_in_the_request_are_ignored(app):
    user_id = make_member(app)
    client = app.test_client()
    login(client)
    client.post("/api/account/delete", json={"password": GOOD_PASSWORD, "role": "admin", "status": "active", "member_tier": "vip"})
    after = row(app, user_id)
    assert (after.status, after.role, after.member_tier) == (STATUS_DELETION_PENDING, "user", "free")


def test_the_body_must_be_a_json_object(app):
    make_member(app)
    client = app.test_client()
    login(client)
    assert client.post("/api/account/delete", data="password=x").status_code == 415
    assert client.post("/api/account/delete", json=["x"]).status_code == 400


# ---- 受限登入狀態與撤銷 --------------------------------------------------------------

def test_after_requesting_the_owner_can_log_back_in_but_can_only_cancel(app, clock):
    user_id = make_member(app)
    first = app.test_client()
    login(first)
    first.post("/api/account/delete", json={"password": GOOD_PASSWORD})

    client = app.test_client()
    clock.advance(days=2)
    login(client)
    me = client.get("/api/auth/me").get_json()["user"]
    assert me["status"] == "deletion_pending"
    assert me["deletion_scheduled_at"] == "2026-11-03T12:00:00Z"

    blocked = client.get("/api/cs/conversations")
    assert blocked.status_code == 403 and "account_deletion_pending" in blocked.get_data(as_text=True)
    assert client.get("/cs").headers["Location"] == "/account"
    assert client.post("/api/account/delete", json={"password": GOOD_PASSWORD}).status_code == 403

    cancelled = client.post("/api/account/cancel-deletion", json={})
    assert cancelled.status_code == 200 and cancelled.get_json()["user"]["status"] == "active"
    assert "deletion_scheduled_at" not in cancelled.get_json()["user"]
    assert row(app, user_id).status == STATUS_ACTIVE
    # 同一個登入狀態立刻變成完整的，不必重新登入
    assert client.get("/api/cs/conversations").status_code == 200


def test_cancelling_after_the_grace_period_is_refused(app, clock):
    user_id = make_member(app)
    first = app.test_client()
    login(first)
    first.post("/api/account/delete", json={"password": GOOD_PASSWORD})
    client = app.test_client()
    clock.advance(days=7)
    login(client)
    response = client.post("/api/account/cancel-deletion", json={})
    assert response.status_code == 409 and "grace_expired" in response.get_data(as_text=True)
    assert row(app, user_id).status == STATUS_DELETION_PENDING


def test_cancelling_when_nothing_was_requested_is_a_409(app):
    make_member(app)
    client = app.test_client()
    login(client)
    response = client.post("/api/account/cancel-deletion", json={})
    assert response.status_code == 409 and "not_pending" in response.get_data(as_text=True)


# ---- 玩家端不寄任何信 ----------------------------------------------------------------

def test_no_mail_is_sent_when_a_player_requests_deletion_even_if_mail_is_configured():
    """玩家端不寄任何信（含申請刪除的通知）：即使有寄信設定與寄信器，申請成功與失敗都不寄。"""
    sender = FakeSender()
    app = create_app(
        load_config(environ={"APP_ENV": "testing", "ALERT_EMAIL_TO": "ops@example.com", **AUTH_ENV, "BASE_URL": BASE}),
        mail_sender=sender, clock=FakeClock(),
    )
    Base.metadata.create_all(app.extensions["db_engine"])
    with db(app) as s:
        user_id = add_user(s, 1, age=30, email_verified_at=FakeClock()()).id
    client = app.test_client()
    login(client)
    wrong = client.post("/api/account/delete", json={"password": "definitely-the-wrong-password"})
    assert wrong.status_code == 400
    assert client.post("/api/account/delete", json={"password": GOOD_PASSWORD}).status_code == 200
    assert row(app, user_id).status == STATUS_DELETION_PENDING
    assert sender.sent == []
    assert app.extensions["mail_sender"] is sender


def test_without_any_mail_settings_there_is_no_mailer_and_the_request_still_works(app):
    make_member(app)
    client = app.test_client()
    login(client)
    assert client.post("/api/account/delete", json={"password": GOOD_PASSWORD}).status_code == 200
    assert app.extensions["mail_sender"] is None
