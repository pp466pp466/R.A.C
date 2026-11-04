"""player-nickname 規格的 API 與服務行為：年齡確認步驟的暱稱（全有或全無）、`PUT /api/account/nickname`、短代號、
只回給本人、日誌與管理員檢視不含暱稱。

純格式規則在 `test_nickname.py`；這裡驗證它們在端點與資料庫上的行為。
"""
import codecs
import csv
import io
import json
import zipfile
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base, LockStrike, User
from app.services.nickname import CODE_ALPHABET
from app.repositories.deletion_tombstone_repository import DeletionTombstoneRepository
from app.repositories.user_repository import UserRepository
from app.services.account_erasure_service import AccountErasureService
from tests.support.age import TEST_NICKNAME, age_fields
from tests.support.auth import FakeClock, add_user, sign_in_as
from tests.support.client import raw_client

VALID = "雲遊者"


@pytest.fixture()
def app():
    application = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


def db(app) -> Session:
    return Session(app.extensions["db_engine"])


def client_for(app, n: int = 1, **overrides):
    with db(app) as s:
        user = add_user(s, n, **overrides)
        uid, tv = user.id, user.token_version
    client = app.test_client()
    sign_in_as(client, uid, tv)
    return client


def user_row(app, n: int = 1) -> User:
    with db(app) as s:
        return s.scalar(select(User).where(User.email == f"user{n}@example.com"))


def put_nickname(client, value):
    return client.put("/api/account/nickname", json={"nickname": value})


# ---- 年齡確認步驟：全有或全無 ---------------------------------------------------------------


def test_a_new_player_saves_the_nickname_together_with_the_age(app):
    client = client_for(app, age=None)
    response = client.post("/api/auth/verify-age", json=age_fields(30, nickname="  雲遊者 "))
    assert response.status_code == 200
    user = response.get_json()["user"]
    assert user["nickname"] == VALID and len(user["nickname_code"]) == 4 and set(user["nickname_code"]) <= set(CODE_ALPHABET)
    stored = user_row(app)
    assert stored.birth_date is not None and stored.nickname == VALID and stored.nickname_code == user["nickname_code"]


def test_an_invalid_nickname_saves_nothing_and_is_not_counted_as_a_lock_strike(app):
    client = client_for(app, age=None)
    response = client.post("/api/auth/verify-age", json=age_fields(30, nickname="https://x.io"))
    assert response.status_code == 400
    error = response.get_json()["error"]
    assert error["code"] == "validation_error" and set(error["fields"]) == {"nickname"}
    stored = user_row(app)
    assert stored.birth_date is None and stored.age_range is None and stored.nickname is None and stored.nickname_code is None
    with db(app) as s:
        assert s.query(LockStrike).count() == 0


def test_a_missing_nickname_is_refused_and_nothing_is_saved(app):
    client = client_for(app, age=None)
    body = age_fields(30)
    del body["nickname"]
    response = client.post("/api/auth/verify-age", json=body)
    assert response.status_code == 400 and set(response.get_json()["error"]["fields"]) == {"nickname"}
    assert user_row(app).birth_date is None


def test_a_bad_nickname_and_a_malformed_birth_date_are_reported_together(app):
    client = client_for(app, age=None)
    response = client.post("/api/auth/verify-age", json=age_fields(30, birth_date="2026-02-30", nickname="a@b.com"))
    assert set(response.get_json()["error"]["fields"]) == {"birth_date", "nickname"}


def test_a_bad_nickname_with_a_mismatched_age_reports_only_the_nickname_and_counts_nothing(app):
    """暱稱不合格時，年齡不一致的處理（計入鎖定計次）不會被觸發。"""
    client = client_for(app, age=None)
    for _ in range(6):                                              # 超過 5 次的鎖定門檻也不會鎖定
        response = client.post("/api/auth/verify-age", json=age_fields(30, age_range="13_17", nickname="官方客服"))
        assert response.status_code == 400 and set(response.get_json()["error"]["fields"]) == {"nickname"}
    with db(app) as s:
        assert s.query(LockStrike).count() == 0 and s.scalar(select(User.locked_at)) is None


def test_a_player_under_13_is_erased_and_the_nickname_is_not_kept(app):
    client = client_for(app, age=None)
    response = client.post("/api/auth/verify-age", json=age_fields(10, age_range="13_17", nickname=VALID))
    assert response.status_code == 400
    with db(app) as s:
        assert s.query(User).count() == 0


def test_a_mismatch_still_counts_once_when_the_nickname_is_fine(app):
    client = client_for(app, age=None)
    response = client.post("/api/auth/verify-age", json=age_fields(30, age_range="13_17"))
    assert response.get_json()["error"]["code"] == "age_mismatch"
    with db(app) as s:
        assert s.query(LockStrike).count() == 1
    assert user_row(app).nickname is None


def test_resubmitting_after_completion_changes_nothing_including_the_nickname(app):
    client = client_for(app, age=None)
    first = client.post("/api/auth/verify-age", json=age_fields(30, nickname=VALID)).get_json()["user"]
    again = client.post("/api/auth/verify-age", json=age_fields(40, nickname="別的名字")).get_json()["user"]
    assert (again["nickname"], again["nickname_code"]) == (VALID, first["nickname_code"])


def test_an_admin_may_complete_the_age_without_a_nickname_or_with_one(app):
    admin = client_for(app, 1, role="admin", age=None)
    response = admin.post("/api/auth/verify-age", json={k: v for k, v in age_fields(30).items() if k != "nickname"})
    assert response.status_code == 200 and response.get_json()["user"]["nickname"] is None
    other = client_for(app, 2, role="admin", age=None)
    response = other.post("/api/auth/verify-age", json=age_fields(30, nickname=VALID))
    assert response.status_code == 200 and response.get_json()["user"]["nickname"] == VALID
    bad = client_for(app, 3, role="admin", age=None)
    assert bad.post("/api/auth/verify-age", json=age_fields(30, nickname="官方")).status_code == 400
    assert user_row(app, 3).birth_date is None


# ---- PUT /api/account/nickname --------------------------------------------------------------


def test_an_existing_player_without_a_nickname_sets_it_and_gets_a_code(app):
    client = client_for(app)
    assert user_row(app).nickname is None
    response = put_nickname(client, VALID)
    assert response.status_code == 200
    user = response.get_json()["user"]
    assert user["nickname"] == VALID and user["nickname_code"] and "ok" not in user
    assert client.get("/api/auth/me").get_json()["user"]["nickname"] == VALID


def test_changing_the_nickname_keeps_the_code(app):
    client = client_for(app)
    first = put_nickname(client, VALID).get_json()["user"]
    second = put_nickname(client, "另一個名字").get_json()["user"]
    assert second["nickname"] == "另一個名字" and second["nickname_code"] == first["nickname_code"]
    third = put_nickname(client, "另一個名字").get_json()["user"]                      # 相同的值再送一次也成功
    assert third["nickname_code"] == first["nickname_code"]


def test_two_players_may_use_the_same_nickname_and_get_their_own_codes(app):
    one, two = client_for(app, 1), client_for(app, 2)
    a = put_nickname(one, VALID)
    b = put_nickname(two, VALID)
    assert a.status_code == b.status_code == 200
    assert a.get_json()["user"]["nickname"] == b.get_json()["user"]["nickname"]


def test_an_invalid_nickname_is_refused_with_a_field_error_and_the_old_value_stays(app):
    client = client_for(app)
    put_nickname(client, VALID)
    for bad in ("", "x", "x" * 21, "0912345678", "官方客服", None, 42):
        response = put_nickname(client, bad)
        assert response.status_code == 400 and set(response.get_json()["error"]["fields"]) == {"nickname"}, bad
    assert user_row(app).nickname == VALID


def test_an_admin_without_the_age_confirmation_cannot_set_a_nickname_yet(app):
    admin = client_for(app, role="admin", age=None)
    response = put_nickname(admin, VALID)
    assert response.status_code == 403 and response.get_json()["error"]["code"] == "age_confirmation_required"
    assert user_row(app).nickname is None


def test_a_locked_player_cannot_use_it(app):
    client = client_for(app, locked_at=FakeClock()(), lock_reason="rate_limited")
    response = put_nickname(client, VALID)
    assert response.status_code == 403 and response.get_json()["error"]["code"] == "account_locked"


def test_a_player_waiting_for_deletion_cannot_use_it(app):
    client = client_for(app, status="deletion_pending", deletion_requested_at=FakeClock()() - timedelta(days=1))
    assert put_nickname(client, VALID).status_code == 403


def test_it_needs_a_login_and_a_csrf_token(app):
    assert app.test_client().put("/api/account/nickname", json={"nickname": VALID}).status_code == 401
    client_for(app)
    raw = raw_client(app)
    sign_in_as(raw, user_row(app).id, 1)
    refused = raw.put("/api/account/nickname", json={"nickname": VALID})                 # 沒有來源與 CSRF 權杖
    assert refused.status_code == 403
    assert user_row(app).nickname is None


def test_it_only_changes_the_callers_own_row(app):
    one = client_for(app, 1)
    client_for(app, 2)
    response = one.put("/api/account/nickname", json={"nickname": VALID, "public_id": user_row(app, 2).public_id, "id": 2})
    assert response.status_code == 200
    assert user_row(app, 1).nickname == VALID and user_row(app, 2).nickname is None


# ---- 只回給本人、不進日誌、管理員看不到 -----------------------------------------------------


def test_only_the_owner_sees_the_nickname_in_the_profile(app):
    one, two = client_for(app, 1), client_for(app, 2)
    put_nickname(one, VALID)
    assert one.get("/api/auth/me").get_json()["user"]["nickname"] == VALID
    other = two.get("/api/auth/me").get_json()["user"]
    assert other["nickname"] is None and VALID not in str(other)


def test_setting_a_nickname_logs_no_nickname_or_code(app, log_lines):
    client = client_for(app)
    put_nickname(client, "雲遊者-獨特暱稱")
    code = user_row(app).nickname_code
    raw = log_lines.raw()
    assert "獨特暱稱" not in raw and code not in raw
    events = [line for line in log_lines() if line.get("event") == "account.nickname_set"]
    assert len(events) == 1 and events[0]["user_id"] == user_row(app).id


def test_the_age_step_with_a_nickname_logs_no_nickname_either(app, log_lines):
    client = client_for(app, age=None)
    client.post("/api/auth/verify-age", json=age_fields(30, nickname="雲遊者-獨特暱稱"))
    assert "獨特暱稱" not in log_lines.raw() and TEST_NICKNAME not in log_lines.raw()


def test_the_admin_user_view_has_no_nickname(app):
    admin = client_for(app, 1, role="admin")
    player = client_for(app, 2)
    put_nickname(player, VALID)
    public_id = user_row(app, 2).public_id
    response = admin.get(f"/api/admin/users/{public_id}")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert VALID not in text and "nickname" not in text


# ---- 個資：匯出與抹除 -----------------------------------------------------------------------


def test_the_json_export_has_the_nickname_and_the_code_in_the_account_block(app):
    client = client_for(app)
    code = put_nickname(client, VALID).get_json()["user"]["nickname_code"]
    response = client.post("/api/account/export", json={"format": "json"})
    account = json.loads(response.get_data(as_text=True))["account"]
    assert account["nickname"] == VALID and account["nickname_code"] == code


def test_the_csv_export_has_the_nickname_columns_with_formula_characters_neutralised(app):
    client = client_for(app)
    put_nickname(client, "=雲遊者")
    response = client.post("/api/account/export", json={"format": "csv"})
    with zipfile.ZipFile(io.BytesIO(response.get_data())) as archive:
        raw = archive.read("account.csv")
    assert raw.startswith(codecs.BOM_UTF8)
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    assert rows[0]["nickname"] == "'=雲遊者" and rows[0]["nickname_code"] == user_row(app).nickname_code


def test_an_export_without_a_nickname_has_empty_values_not_missing_columns(app):
    client = client_for(app)
    account = json.loads(client.post("/api/account/export", json={"format": "json"}).get_data(as_text=True))["account"]
    assert account["nickname"] is None and account["nickname_code"] is None


def test_erasing_the_account_removes_the_nickname_and_the_code_everywhere(member_session):
    from datetime import datetime, timezone

    user = add_user(member_session, 1, last_login_at=datetime(2025, 1, 1, tzinfo=timezone.utc), nickname="雲遊者", nickname_code="7K2M")
    service = AccountErasureService(
        users=UserRepository(member_session), tombstones=DeletionTombstoneRepository(member_session),
        commit=member_session.commit, rollback=member_session.rollback,
    )
    assert service.erase_inactive(user.id, cutoff=datetime(2026, 1, 1, tzinfo=timezone.utc), now=datetime(2026, 11, 4, tzinfo=timezone.utc))
    member_session.expire_all()
    assert member_session.scalar(select(User).where(User.email == "user1@example.com")) is None
    assert member_session.query(User).filter((User.nickname == "雲遊者") | (User.nickname_code == "7K2M")).count() == 0
