"""account-retention 規格：連續 12 個月未登入的帳號被直接刪除（排程指令）；age-confirmation 規格的彙總統計指令。"""
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base, ChatConversation, ChatMessage, DeletionTombstone, LlmCallLog, User, UserIdentity
from app.models.user import AuthToken, PROVIDER_GOOGLE, TOKEN_VERIFY_EMAIL
from app.repositories.deletion_tombstone_repository import DeletionTombstoneRepository
from app.repositories.user_repository import UserRepository
from app.services.account_retention_service import INACTIVITY_PERIOD, AccountRetentionService
from tests.support.age import age_fields, birth_date_for_age
from tests.support.auth import START, FakeClock, add_user

OLD = INACTIVITY_PERIOD + timedelta(days=35)          # 400 天前
SECRET_NAME = "機密姓名"
SECRET_EMAIL = "secret.person@example.com"


@pytest.fixture()
def app():
    application = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


@pytest.fixture()
def runner(app):
    return app.test_cli_runner()


def seed(app) -> dict[str, int]:
    """五個帳號：兩個該刪（含從未登入者）、一個差一天才滿 12 個月、一個老管理員、一個資料齊全的待刪帳號。"""
    ids: dict[str, int] = {}
    with Session(app.extensions["db_engine"]) as s:
        stale = add_user(s, 1, last_login_at=START - OLD, created_at=START - OLD - timedelta(days=100), age=30)
        never = add_user(s, 2, last_login_at=None, created_at=START - OLD)
        recent = add_user(s, 3, last_login_at=START - INACTIVITY_PERIOD + timedelta(days=1), created_at=START - OLD, age=15)
        admin = add_user(s, 4, role="admin", last_login_at=START - OLD, created_at=START - OLD)
        rich = add_user(
            s, 5, email=SECRET_EMAIL, email_canonical=SECRET_EMAIL, display_name=SECRET_NAME,
            last_login_at=START - OLD, created_at=START - OLD, age=40,
        )
        s.add(UserIdentity(user_id=rich.id, provider=PROVIDER_GOOGLE, provider_subject="sub-secret-1"))
        s.add(AuthToken(user_id=rich.id, purpose=TOKEN_VERIFY_EMAIL, token_hash="a" * 64, expires_at=START))
        s.commit()
        ids = {"stale": stale.id, "never": never.id, "recent": recent.id, "admin": admin.id, "rich": rich.id}
    return ids


def remaining(app) -> set[int]:
    with Session(app.extensions["db_engine"]) as s:
        return set(s.scalars(select(User.id)))


def tombstones(app) -> list[DeletionTombstone]:
    with Session(app.extensions["db_engine"]) as s:
        return list(s.scalars(select(DeletionTombstone).order_by(DeletionTombstone.id)))


def purge_events(log_lines) -> list[dict]:
    return [line for line in log_lines() if line.get("event") == "account.inactive_purged"]


# ---- 預覽 -----------------------------------------------------------------------

def test_the_dry_run_reports_how_many_would_go_and_deletes_nothing(app, runner, log_lines):
    ids = seed(app)
    result = runner.invoke(args=["purge-inactive-accounts", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "預覽" in result.output and "3 個" in result.output and "未刪除" in result.output
    assert remaining(app) == set(ids.values()) and tombstones(app) == []
    assert purge_events(log_lines) == []                                       # 預覽不產生任何清除事件


# ---- 實際刪除 -------------------------------------------------------------------

def test_the_purge_deletes_exactly_the_inactive_regular_accounts(app, runner):
    ids = seed(app)
    result = runner.invoke(args=["purge-inactive-accounts"])
    assert result.exit_code == 0, result.output
    assert "已刪除 3 個" in result.output
    assert remaining(app) == {ids["recent"], ids["admin"]}                     # 差一天的與管理員都保留
    with Session(app.extensions["db_engine"]) as s:
        assert s.query(UserIdentity).count() == 0 and s.query(AuthToken).count() == 0      # 串聯刪除


def test_the_tombstones_hold_only_the_internal_id_the_time_and_the_reason(app, runner):
    ids = seed(app)
    runner.invoke(args=["purge-inactive-accounts"])
    rows = tombstones(app)
    assert sorted(row.user_id for row in rows) == sorted([ids["stale"], ids["never"], ids["rich"]])
    assert {row.reason for row in rows} == {"inactive_12_months"} and {row.deleted_at for row in rows} == {START}
    assert {column.name for column in DeletionTombstone.__table__.columns} == {"id", "user_id", "deleted_at", "reason"}


def test_running_it_twice_deletes_nothing_the_second_time(app, runner):
    seed(app)
    assert "已刪除 3 個" in runner.invoke(args=["purge-inactive-accounts"]).output
    second = runner.invoke(args=["purge-inactive-accounts"])
    assert "已刪除 0 個" in second.output and len(tombstones(app)) == 3


def test_the_output_and_the_logs_carry_no_personal_data(app, runner, log_lines):
    ids = seed(app)
    dry = runner.invoke(args=["purge-inactive-accounts", "--dry-run"])
    real = runner.invoke(args=["purge-inactive-accounts"])
    events = purge_events(log_lines)
    assert sorted(line["user_id"] for line in events) == sorted([ids["stale"], ids["never"], ids["rich"]])
    assert all(line["level"] == "INFO" for line in events)
    everything = dry.output + real.output + log_lines.raw()
    for secret in (SECRET_EMAIL, SECRET_NAME, "sub-secret-1", birth_date_for_age(40).isoformat(), birth_date_for_age(30).isoformat(), "35_44"):
        assert secret not in everything, secret


def test_inactive_accounts_conversations_are_deleted_and_the_ledger_is_anonymised(app, runner):
    """閒置清除與自助刪除共用抹除出口：對話與訊息一併刪除，帳本保留但使用者與對話編號都清為空值。"""
    ids = seed(app)
    with Session(app.extensions["db_engine"]) as s:
        for key, client in (("stale", "00000000-0000-4000-8000-000000000001"), ("recent", "00000000-0000-4000-8000-000000000002")):
            conv = ChatConversation(user_id=ids[key], client_id=client, state_json={})
            s.add(conv)
            s.flush()
            s.add(ChatMessage(conversation_id=conv.id, client_msg_id=f"m-{key}", seq=1, role="user", content="哈囉"))
            s.add(LlmCallLog(
                user_id=ids[key], conversation_id=conv.id, kind="chat", provider="gemini", model="m", attempt=1,
                status="ok", counted=True, prompt_tokens=5, completion_tokens=3, latency_ms=100, created_at=START,
            ))
        s.commit()

    assert runner.invoke(args=["purge-inactive-accounts"]).exit_code == 0

    with Session(app.extensions["db_engine"]) as s:
        assert [c.user_id for c in s.scalars(select(ChatConversation))] == [ids["recent"]]
        assert s.query(ChatMessage).count() == 1
        by_user = {row.user_id: row.conversation_id for row in s.scalars(select(LlmCallLog))}
        assert set(by_user) == {None, ids["recent"]} and by_user[None] is None and by_user[ids["recent"]] is not None


def test_an_empty_database_is_fine(app, runner):
    assert "已刪除 0 個" in runner.invoke(args=["purge-inactive-accounts"]).output


# ---- 服務的保護機制（使用真的資料庫，替換掉其中一個 Repository 方法）-----------------

def make_service(session, users=None, **kwargs):
    return AccountRetentionService(
        users=users or UserRepository(session), tombstones=DeletionTombstoneRepository(session),
        clock=FakeClock(), commit=session.commit, **kwargs,
    )


def test_an_account_that_logs_in_during_the_purge_is_kept_and_leaves_no_record(member_session, log_lines):
    class LogsInDuringPurge(UserRepository):
        def delete_inactive(self, user_id, cutoff):
            row = self._session.get(User, user_id)
            row.last_login_at = START                      # 判斷之後、刪除之前剛好登入
            self._session.flush()
            return super().delete_inactive(user_id, cutoff)

    user = add_user(member_session, 1, last_login_at=START - OLD)
    result = make_service(member_session, LogsInDuringPurge(member_session)).purge_inactive()
    assert (result.matched, result.deleted) == (1, 0)
    member_session.expire_all()
    assert member_session.get(User, user.id) is not None
    assert member_session.query(DeletionTombstone).count() == 0 and purge_events(log_lines) == []


def test_many_accounts_are_handled_across_several_batches(member_session):
    for n in range(1, 8):
        add_user(member_session, n, last_login_at=START - OLD)
    result = make_service(member_session, batch_size=3).purge_inactive()
    assert (result.matched, result.deleted) == (7, 7)
    assert member_session.query(User).count() == 0 and member_session.query(DeletionTombstone).count() == 7


def test_a_batch_where_nothing_could_be_deleted_ends_the_run_instead_of_looping(member_session):
    class NeverDeletes(UserRepository):
        def delete_inactive(self, user_id, cutoff):
            return False

    add_user(member_session, 1, last_login_at=START - OLD)
    result = make_service(member_session, NeverDeletes(member_session)).purge_inactive()
    assert (result.matched, result.deleted) == (1, 0)


def test_each_deletion_is_committed_with_its_tombstone_so_a_failure_midway_keeps_what_was_done(member_session):
    class FailsOnSecond(UserRepository):
        calls = 0

        def delete_inactive(self, user_id, cutoff):
            FailsOnSecond.calls += 1
            if FailsOnSecond.calls == 2:
                raise RuntimeError("資料庫暫時失敗")
            return super().delete_inactive(user_id, cutoff)

    for n in (1, 2, 3):
        add_user(member_session, n, last_login_at=START - OLD)
    with pytest.raises(RuntimeError):
        make_service(member_session, FailsOnSecond(member_session)).purge_inactive()
    member_session.rollback()
    assert member_session.query(User).count() == 2 and member_session.query(DeletionTombstone).count() == 1


def test_the_period_is_365_days_and_the_boundary_is_strict(member_session):
    exactly = add_user(member_session, 1, last_login_at=START - INACTIVITY_PERIOD).id
    just_over = add_user(member_session, 2, last_login_at=START - INACTIVITY_PERIOD - timedelta(seconds=1)).id
    result = make_service(member_session).purge_inactive()
    assert result.deleted == 1
    member_session.expire_all()
    assert member_session.get(User, exactly) is not None and member_session.get(User, just_over) is None


# ---- age-stats ------------------------------------------------------------------

def test_age_stats_prints_only_counts_per_range_including_empty_ones(app, runner):
    with Session(app.extensions["db_engine"]) as s:
        add_user(s, 1, age=15)
        add_user(s, 2, age=15)
        add_user(s, 3, age=30)
        add_user(s, 4, age=None)                               # 尚未完成
        s.commit()
    result = runner.invoke(args=["age-stats"])
    assert result.exit_code == 0, result.output
    lines = {line.strip() for line in result.output.splitlines()}
    assert "13 至 17 歲：2" in lines and "25 至 34 歲：1" in lines
    assert "未滿 13 歲：0" in lines and "55 歲以上：0" in lines                      # 沒有人的區間也列出
    assert "尚未完成年齡確認：1" in lines and "合計：4" in lines


def test_age_stats_shows_no_individual_data(app, runner):
    with Session(app.extensions["db_engine"]) as s:
        add_user(s, 1, age=30, email=SECRET_EMAIL, email_canonical=SECRET_EMAIL, display_name=SECRET_NAME)
        s.commit()
    result = runner.invoke(args=["age-stats"])
    for secret in (SECRET_EMAIL, SECRET_NAME, birth_date_for_age(30).isoformat(), "1996", "user1"):
        assert secret not in result.output


def test_age_stats_on_an_empty_database_lists_zero_everywhere(app, runner):
    result = runner.invoke(args=["age-stats"])
    assert result.exit_code == 0 and "合計：0" in result.output and "尚未完成年齡確認：0" in result.output


def test_age_stats_keeps_counting_accounts_whose_range_code_is_no_longer_in_the_policy(app, runner):
    with Session(app.extensions["db_engine"]) as s:
        add_user(s, 1, age=30, age_range="legacy_code")
        s.commit()
    output = runner.invoke(args=["age-stats"]).output
    assert "legacy_code" in output and "合計：1" in output
