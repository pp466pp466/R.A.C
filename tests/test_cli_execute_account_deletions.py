"""account-deletion 規格：排程指令 `execute-account-deletions`（寬限期滿才刪、預覽、重複執行、輸出與日誌不含個資、剛撤銷的不刪）。"""
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import (
    AuthToken, Base, ChatConversation, ChatMessage, DeletionTombstone, LlmCallLog, User, UserIdentity,
)
from app.models.user import PROVIDER_GOOGLE, STATUS_ACTIVE, STATUS_DELETION_PENDING, TOKEN_VERIFY_EMAIL
from app.repositories.deletion_tombstone_repository import DeletionTombstoneRepository
from app.repositories.user_repository import UserRepository
from app.services.account_deletion_executor import AccountDeletionExecutor
from app.services.account_erasure_service import AccountErasureService
from tests.support.auth import START, FakeClock, add_user

SECRET_EMAIL = "gone.person@example.com"
SECRET_NAME = "即將刪除的姓名"
GRACE = timedelta(days=7)


@pytest.fixture()
def app():
    application = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


@pytest.fixture()
def runner(app):
    return app.test_cli_runner()


def pending(session, n: int, requested_at, **overrides) -> User:
    return add_user(
        session, n, status=STATUS_DELETION_PENDING, deletion_requested_at=requested_at, status_before_deletion=STATUS_ACTIVE, **overrides
    )


def seed(app) -> dict[str, int]:
    """五個帳號：兩個寬限期已滿（剛好 7 天與 30 天）、一個差一小時、一個已撤銷（active）、一個資料齊全的待刪帳號。"""
    with Session(app.extensions["db_engine"]) as s:
        exactly = pending(s, 1, START - GRACE)
        long_ago = pending(s, 2, START - timedelta(days=30), age=30)
        soon = pending(s, 3, START - GRACE + timedelta(hours=1))
        cancelled = add_user(s, 4)
        rich = pending(
            s, 5, START - timedelta(days=8), email=SECRET_EMAIL, email_canonical=SECRET_EMAIL, display_name=SECRET_NAME, age=40,
        )
        s.add(UserIdentity(user_id=rich.id, provider=PROVIDER_GOOGLE, provider_subject="sub-secret-2"))
        s.add(AuthToken(user_id=rich.id, purpose=TOKEN_VERIFY_EMAIL, token_hash="b" * 64, expires_at=START))
        conv = ChatConversation(user_id=rich.id, client_id="00000000-0000-4000-8000-0000000000aa", state_json={})
        s.add(conv)
        s.flush()
        s.add(ChatMessage(conversation_id=conv.id, client_msg_id="m1", seq=1, role="user", content="我的訊息全文"))
        s.add(LlmCallLog(
            user_id=rich.id, conversation_id=conv.id, kind="chat", provider="gemini", model="m", attempt=1, status="ok",
            counted=True, prompt_tokens=9, completion_tokens=4, latency_ms=120, created_at=START,
        ))
        s.commit()
        return {"exactly": exactly.id, "long_ago": long_ago.id, "soon": soon.id, "cancelled": cancelled.id, "rich": rich.id}


def remaining(app) -> set[int]:
    with Session(app.extensions["db_engine"]) as s:
        return set(s.scalars(select(User.id)))


def tombstones(app) -> list[DeletionTombstone]:
    with Session(app.extensions["db_engine"]) as s:
        return list(s.scalars(select(DeletionTombstone).order_by(DeletionTombstone.id)))


def events(log_lines) -> list[dict]:
    return [line for line in log_lines() if line.get("event") == "account.deleted_by_request"]


def test_the_dry_run_reports_how_many_would_go_and_deletes_nothing(app, runner, log_lines):
    ids = seed(app)
    result = runner.invoke(args=["execute-account-deletions", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "預覽" in result.output and "3 個" in result.output and "未刪除" in result.output
    assert remaining(app) == set(ids.values()) and tombstones(app) == [] and events(log_lines) == []


def test_only_accounts_whose_grace_period_is_over_are_deleted(app, runner):
    ids = seed(app)
    result = runner.invoke(args=["execute-account-deletions"])
    assert result.exit_code == 0, result.output
    assert "已刪除 3 個" in result.output
    assert remaining(app) == {ids["soon"], ids["cancelled"]}               # 差一小時的與已撤銷的都保留
    with Session(app.extensions["db_engine"]) as s:
        assert s.query(UserIdentity).count() == 0 and s.query(AuthToken).count() == 0
        assert s.query(ChatConversation).count() == 0 and s.query(ChatMessage).count() == 0       # 串聯刪除
        (ledger,) = s.scalars(select(LlmCallLog))
        assert (ledger.user_id, ledger.conversation_id, ledger.prompt_tokens) == (None, None, 9)   # 帳本匿名化、統計保留


def test_the_tombstones_hold_only_the_internal_id_the_time_and_the_reason_user_requested(app, runner):
    ids = seed(app)
    runner.invoke(args=["execute-account-deletions"])
    rows = tombstones(app)
    assert sorted(row.user_id for row in rows) == sorted([ids["exactly"], ids["long_ago"], ids["rich"]])
    assert {row.reason for row in rows} == {"user_requested"} and {row.deleted_at for row in rows} == {START}


def test_running_it_twice_deletes_nothing_the_second_time(app, runner):
    seed(app)
    assert "已刪除 3 個" in runner.invoke(args=["execute-account-deletions"]).output
    assert "已刪除 0 個" in runner.invoke(args=["execute-account-deletions"]).output
    assert len(tombstones(app)) == 3


def test_the_output_and_the_logs_carry_no_personal_data(app, runner, log_lines):
    ids = seed(app)
    dry = runner.invoke(args=["execute-account-deletions", "--dry-run"])
    real = runner.invoke(args=["execute-account-deletions"])
    logged = events(log_lines)
    assert sorted(line["user_id"] for line in logged) == sorted([ids["exactly"], ids["long_ago"], ids["rich"]])
    everything = dry.output + real.output + log_lines.raw()
    for secret in (SECRET_EMAIL, SECRET_NAME, "sub-secret-2", "我的訊息全文"):
        assert secret not in everything, secret


def test_an_empty_database_is_fine(app, runner):
    assert "已刪除 0 個" in runner.invoke(args=["execute-account-deletions"]).output


# ---- 服務的保護機制（使用真的資料庫，替換掉其中一個 Repository 方法）---------------------

def make_executor(session, users=None, **kwargs) -> AccountDeletionExecutor:
    users = users or UserRepository(session)
    erasure = AccountErasureService(
        users=users, tombstones=DeletionTombstoneRepository(session), commit=session.commit, rollback=session.rollback
    )
    return AccountDeletionExecutor(users=users, erasure=erasure, clock=FakeClock(), **kwargs)


def test_an_account_cancelled_during_the_run_is_kept_and_leaves_no_trace(member_session, log_lines):
    class CancelsDuringRun(UserRepository):
        def find_pending_due_ids(self, cutoff, *, limit=200):
            found = super().find_pending_due_ids(cutoff, limit=limit)
            for user_id in found:                              # 判斷之後、刪除之前，使用者撤銷並提交
                self.cancel_deletion(self._session.get(User, user_id), now=START - timedelta(days=8))
            self._session.commit()
            return found

    user = pending(member_session, 1, START - GRACE - timedelta(days=1))
    user_id = user.id
    member_session.add(LlmCallLog(
        user_id=user_id, conversation_id=7, kind="chat", provider="gemini", model="m", attempt=1, status="ok", counted=True,
        latency_ms=1, created_at=START,
    ))
    member_session.commit()

    result = make_executor(member_session, CancelsDuringRun(member_session)).execute_due()

    assert (result.matched, result.deleted) == (1, 0)
    member_session.expire_all()
    kept = member_session.get(User, user_id)
    assert kept is not None and kept.status == STATUS_ACTIVE
    assert member_session.query(DeletionTombstone).count() == 0 and events(log_lines) == []
    assert member_session.scalars(select(LlmCallLog.conversation_id)).one() == 7        # 交易回復：帳本的對話連結沒有被清掉


def test_many_accounts_are_handled_across_several_batches(member_session):
    for n in range(1, 8):
        pending(member_session, n, START - timedelta(days=9))
    result = make_executor(member_session, batch_size=3).execute_due()
    assert (result.matched, result.deleted) == (7, 7)
    assert member_session.query(User).count() == 0 and member_session.query(DeletionTombstone).count() == 7


def test_a_batch_where_nothing_could_be_deleted_ends_the_run_instead_of_looping(member_session):
    class NeverDeletes(UserRepository):
        def delete_if_pending_due(self, user_id, cutoff):
            return False

    pending(member_session, 1, START - timedelta(days=9))
    result = make_executor(member_session, NeverDeletes(member_session)).execute_due()
    assert (result.matched, result.deleted) == (1, 0)
