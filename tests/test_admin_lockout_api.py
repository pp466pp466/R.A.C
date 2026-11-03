"""account-lockout 與 lockout-appeals 規格：管理員檢視被鎖定的帳號與申訴、重新輸入密碼後解除鎖定。

非管理員一律 404（與不存在逐字相同）由 `test_admin_api.py` 的身分矩陣涵蓋（本檔的三個端點都在其中）；
這裡驗證各端點的行為：可見範圍、稽核、重新輸入密碼、衝突與並行。
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AdminAuditLog, LockAppeal, LockStrike, User
from app.repositories.admin_repository import AdminRepository
from app.repositories.chat_repository import ChatRepository
from app.repositories.lock_appeal_repository import LockAppealRepository
from tests.support.admin import STATE
from tests.support.auth import GOOD_PASSWORD, START, add_user
from tests.support.idor import logged_in_client
from tests.test_admin_api import World

LOCKED_APPEAL = "請幫我解除鎖定"


@pytest.fixture()
def world():
    return World()


def admin_client(world):
    return world.client("admin")


def audit_rows(world, action=None) -> list[AdminAuditLog]:
    with Session(world.engine) as s:
        query = select(AdminAuditLog).order_by(AdminAuditLog.id)
        if action:
            query = query.where(AdminAuditLog.action == action)
        return list(s.scalars(query))


def unlock(world, public_id=None, password=GOOD_PASSWORD):
    return admin_client(world).post(f"/api/admin/users/{public_id or world.locked_public}/unlock", json={"password": password})


# ---- 被鎖定的帳號清單 ---------------------------------------------------------------------------------

def test_the_locked_list_has_only_locked_players_and_only_public_fields(world):
    response = admin_client(world).get("/api/admin/locked")
    assert response.status_code == 200
    (account,) = response.get_json()["accounts"]
    assert account == {
        "public_id": world.locked_public, "display_name": "使用者5", "locked_at": "2026-10-27T12:00:00+00:00", "reason": "rate_limited",
    }
    text = response.get_data(as_text=True)
    assert "user5@example.com" not in text and "password" not in text and "token_version" not in text


def test_the_locked_list_is_paged_and_follows_new_locks_and_unlocks(world):
    with Session(world.engine) as s:
        for n in range(6, 9):
            add_user(s, n, age=30, locked_at=START + timedelta(minutes=n), lock_reason="identity_rejected")
    client = admin_client(world)
    everyone = client.get("/api/admin/locked").get_json()["accounts"]
    assert len(everyone) == 4
    assert [a["public_id"] for a in client.get("/api/admin/locked?limit=2").get_json()["accounts"]] == [a["public_id"] for a in everyone[:2]]
    assert [a["public_id"] for a in client.get("/api/admin/locked?limit=2&offset=2").get_json()["accounts"]] == [a["public_id"] for a in everyone[2:]]
    assert unlock(world).status_code == 200
    assert world.locked_public not in {a["public_id"] for a in client.get("/api/admin/locked").get_json()["accounts"]}


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1"])
def test_bad_paging_is_a_field_error(world, query):
    response = admin_client(world).get(f"/api/admin/locked?{query}")
    assert response.status_code == 400 and response.get_json()["error"]["code"] == "validation_error"


def test_viewing_the_locked_list_is_not_an_audited_read_of_personal_content(world):
    admin_client(world).get("/api/admin/locked")
    assert audit_rows(world) == []


# ---- 申訴的檢視 ---------------------------------------------------------------------------------------

def test_the_admin_sees_the_appeal_of_a_locked_account_and_leaves_an_appeal_read_row_with_only_a_count(world):
    response = admin_client(world).get("/api/admin/appeals")
    assert response.status_code == 200
    (appeal,) = response.get_json()["appeals"]
    assert appeal["content"] == LOCKED_APPEAL and appeal["user_public_id"] == world.locked_public and appeal["status"] == "pending"
    assert set(appeal) == {"id", "user_public_id", "display_name", "content", "created_at", "status"}
    (row,) = audit_rows(world, "appeal.read")
    assert row.admin_user_id == world.admin_id and row.detail_json == {"count": 1}
    assert LOCKED_APPEAL not in repr((row.detail_json, row.reason, row.target_user_public_id))


def test_only_appeals_of_accounts_that_are_locked_right_now_are_visible(world):
    with Session(world.engine) as s:
        LockAppealRepository(s).create(world.target_id, "我沒有被鎖定，這則不該被看到", START)        # 一般帳號
        s.commit()
    appeals = admin_client(world).get("/api/admin/appeals").get_json()["appeals"]
    assert [a["content"] for a in appeals] == [LOCKED_APPEAL]


def test_after_the_unlock_the_appeal_disappears_from_the_admins_view_but_stays_in_the_database(world):
    assert unlock(world).status_code == 200
    assert admin_client(world).get("/api/admin/appeals").get_json() == {"appeals": []}
    with Session(world.engine) as s:
        (appeal,) = s.scalars(select(LockAppeal))
        assert appeal.content == LOCKED_APPEAL and appeal.handled_at is not None


def test_an_empty_view_still_leaves_a_read_row_with_a_zero_count(world):
    unlock(world)
    admin_client(world).get("/api/admin/appeals")
    assert [row.detail_json for row in audit_rows(world, "appeal.read")] == [{"count": 0}]


def test_the_appeal_view_gives_no_way_to_read_that_accounts_conversations(world):
    with Session(world.engine) as s:
        chats = ChatRepository(s)
        conversation = chats.create_conversation(world.locked_id, client_id="2" * 36, state=STATE, now=START)
        chats.append_message(world.locked_id, conversation, client_msg_id="c" * 36, role="user", content="私人的客服對話內容", now=START)
        chats.append_message(world.locked_id, conversation, client_msg_id="d" * 36, role="assistant", content="回覆", intent="order_status", now=START)
        s.commit()
        public_id = conversation.public_id
    client = admin_client(world)
    assert client.get(f"/api/admin/conversations/{public_id}").status_code == 404          # 不是意見回饋或被標記的對話：與一般帳號相同
    everything = "".join(client.get(path).get_data(as_text=True) for path in ("/api/admin/appeals", "/api/admin/locked", "/api/admin/feedback", "/api/admin/flagged"))
    assert "私人的客服對話內容" not in everything and public_id not in everything


# ---- 解除鎖定 -----------------------------------------------------------------------------------------

def test_the_right_password_unlocks_clears_the_strikes_and_marks_the_appeal_handled(world):
    with Session(world.engine) as s:
        for _ in range(3):
            s.add(LockStrike(user_id=world.locked_id, kind="rate_limited", created_at=START))
        s.add(LockStrike(user_id=world.target_id, kind="rate_limited", created_at=START))          # 別人的計次不受影響
        s.commit()
    response = unlock(world)
    assert response.status_code == 200
    assert response.get_json() == {"user": {"public_id": world.locked_public, "display_name": "使用者5", "locked": False}}
    with Session(world.engine) as s:
        user = s.get(User, world.locked_id)
        assert user.locked_at is None and user.lock_reason is None
        assert [(strike.user_id) for strike in s.scalars(select(LockStrike))] == [world.target_id]
        assert s.scalar(select(LockAppeal.handled_at)) is not None


def test_the_unlock_leaves_one_audit_row_naming_the_admin_the_target_and_the_handled_count(world):
    unlock(world)
    (row,) = audit_rows(world, "account.unlock")
    assert row.admin_user_id == world.admin_id and row.target_user_public_id == world.locked_public
    assert row.detail_json == {"handled_appeals": 1} and row.reason is None
    assert audit_rows(world, "reauth.failed") == []


def test_the_players_existing_session_is_whole_again_without_signing_in_once_more(world):
    player = logged_in_client(world.app, _row(world, world.locked_id))
    assert player.get("/api/cs/conversations").status_code == 403
    assert unlock(world).status_code == 200
    assert player.get("/api/cs/conversations").status_code == 200


def test_a_wrong_password_does_not_unlock_records_the_failure_and_counts_toward_the_admins_lockout(world):
    response = unlock(world, password="definitely-wrong-password")
    assert response.status_code == 400 and "password" in response.get_json()["error"]["fields"]
    with Session(world.engine) as s:
        assert s.get(User, world.locked_id).locked_at is not None
        assert s.get(User, world.admin_id).failed_login_count == 1
    (row,) = audit_rows(world, "reauth.failed")
    assert row.detail_json == {"attempted": "account.unlock"} and row.target_user_public_id == world.locked_public
    assert audit_rows(world, "account.unlock") == []


def test_five_wrong_passwords_lock_the_admin_so_even_the_right_one_is_refused(world):
    for _ in range(5):
        unlock(world, password="definitely-wrong-password")
    assert unlock(world).status_code == 400
    with Session(world.engine) as s:
        assert s.get(User, world.locked_id).locked_at is not None


def test_a_missing_password_is_a_field_error(world):
    response = admin_client(world).post(f"/api/admin/users/{world.locked_public}/unlock", json={})
    assert response.status_code == 400 and "password" in response.get_json()["error"]["fields"]


def test_an_account_that_is_not_locked_is_a_409_and_costs_no_password_attempt(world):
    response = unlock(world, world.target_public)
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "not_locked"
    assert audit_rows(world, "account.unlock") == []


def test_unlocking_twice_the_second_time_is_a_409(world):
    assert unlock(world).status_code == 200
    again = unlock(world)
    assert again.status_code == 409 and again.get_json()["error"]["code"] == "not_locked"
    assert len(audit_rows(world, "account.unlock")) == 1


def test_an_unknown_account_is_404_and_an_admin_target_is_403(world):
    assert unlock(world, "00000000-0000-4000-8000-000000000000").status_code == 404
    assert unlock(world, "not-a-uuid").status_code == 404
    refused = admin_client(world).post(f"/api/admin/users/{_public(world, world.admin_id)}/unlock", json={"password": GOOD_PASSWORD})
    assert refused.status_code == 403 and refused.get_json()["error"]["code"] == "admin_target_forbidden"


def test_after_the_unlock_five_new_rejections_are_needed_to_lock_the_account_again(world):
    from app.models.lock_strike import STRIKE_IDENTITY_REJECTED
    from app.services.registry import build_account_lock_service
    from tests.support.auth import FakeClock

    with Session(world.engine) as s:
        for _ in range(4):
            s.add(LockStrike(user_id=world.locked_id, kind=STRIKE_IDENTITY_REJECTED, created_at=START))
        s.commit()
    unlock(world)
    with Session(world.engine) as s:
        service = build_account_lock_service(s, clock=FakeClock())
        results = [service.register_strike(world.locked_id, STRIKE_IDENTITY_REJECTED) for _ in range(5)]
        s.commit()
    assert results == [False, False, False, False, True]


# ---- 日誌 ---------------------------------------------------------------------------------------------

def test_the_logs_never_hold_the_appeal_text(world, log_lines):
    admin_client(world).get("/api/admin/appeals")
    unlock(world)
    assert LOCKED_APPEAL not in log_lines.raw()
    events = [line["event"] for line in log_lines() if "event" in line]
    assert "account.player_unlocked" in events and "admin.action" in events


# ---- 並行 ---------------------------------------------------------------------------------------------

def test_eight_admins_unlocking_at_once_only_one_succeeds(concurrent_engine):
    with Session(concurrent_engine) as setup:
        user = add_user(setup, 1, locked_at=START, lock_reason="rate_limited")
        LockAppealRepository(setup).create(user.id, "申訴", START)
        for _ in range(3):
            setup.add(LockStrike(user_id=user.id, kind="rate_limited", created_at=START))
        setup.commit()
        user_id = user.id
    barrier = Barrier(8)

    def task(_index: int):
        with Session(concurrent_engine) as session:
            repo = AdminRepository(session)
            row = session.get(User, user_id)
            barrier.wait(timeout=30)
            result = repo.unlock_account(row, now=START)
            session.commit()
            return result

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [f.result(timeout=60) for f in [pool.submit(task, i) for i in range(8)]]
    assert sorted(results, key=lambda r: (r is not None, r or 0)) == [None] * 7 + [1]
    with Session(concurrent_engine) as check:
        assert check.get(User, user_id).locked_at is None and check.query(LockStrike).count() == 0


# ---- 小工具 -------------------------------------------------------------------------------------------

def _row(world, user_id: int) -> User:
    with Session(world.engine) as s:
        return s.get(User, user_id)


def _public(world, user_id: int) -> str:
    with Session(world.engine) as s:
        return s.get(User, user_id).public_id
