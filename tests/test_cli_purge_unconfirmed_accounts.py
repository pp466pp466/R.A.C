"""account-retention 與 post-login-age-confirmation 規格：玩家首次以 Google 登入後 24 小時內沒有完成年齡確認，帳號被每小時的排程直接刪除。"""
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base, ChatConversation, DeletionTombstone, LockStrike, User, UserIdentity
from app.models.user import PROVIDER_GOOGLE
from tests.support.auth import START, FakeClock, add_user

SECRET_EMAIL = "never.finished@example.com"
SECRET_NAME = "沒填完的人"


@pytest.fixture()
def app():
    application = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


@pytest.fixture()
def runner(app):
    return app.test_cli_runner()


def seed(app) -> dict[str, int]:
    """START 這一刻的帳號：25 小時前建立（期限已過）、23 小時前建立（還沒到）、已完成但期限殘留、管理員、沒有期限的舊帳號。"""
    with Session(app.extensions["db_engine"]) as s:
        late = add_user(
            s, 1, age=None, password=None, email=SECRET_EMAIL, email_canonical=SECRET_EMAIL, display_name=SECRET_NAME,
            age_deadline_at=START - timedelta(hours=1),
        )
        s.add(UserIdentity(user_id=late.id, provider=PROVIDER_GOOGLE, provider_subject="sub-never-finished"))
        s.add(LockStrike(user_id=late.id, kind="identity_rejected", created_at=START - timedelta(minutes=5)))
        early = add_user(s, 2, age=None, age_deadline_at=START + timedelta(hours=1))
        done = add_user(s, 3, age=30, age_deadline_at=START - timedelta(hours=10))
        admin = add_user(s, 4, age=None, role="admin", age_deadline_at=START - timedelta(days=3))
        legacy = add_user(s, 5, age=None)
        s.commit()
        return {"late": late.id, "early": early.id, "done": done.id, "admin": admin.id, "legacy": legacy.id}


def remaining(app) -> set[int]:
    with Session(app.extensions["db_engine"]) as s:
        return set(s.scalars(select(User.id)))


def tombstones(app) -> list[DeletionTombstone]:
    with Session(app.extensions["db_engine"]) as s:
        return list(s.scalars(select(DeletionTombstone).order_by(DeletionTombstone.id)))


def purge_events(log_lines) -> list[dict]:
    return [line for line in log_lines() if line.get("event") == "account.unconfirmed_purged"]


# ---- 預覽 -----------------------------------------------------------------------

def test_the_dry_run_reports_the_count_and_deletes_nothing_and_writes_no_record(app, runner, log_lines):
    ids = seed(app)
    result = runner.invoke(args=["purge-unconfirmed-accounts", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "預覽" in result.output and "1 個" in result.output and "未刪除" in result.output
    assert remaining(app) == set(ids.values()) and tombstones(app) == []
    assert purge_events(log_lines) == []


# ---- 實際刪除 -------------------------------------------------------------------

def test_only_players_past_the_deadline_without_a_birthday_are_deleted(app, runner):
    ids = seed(app)
    result = runner.invoke(args=["purge-unconfirmed-accounts"])
    assert result.exit_code == 0, result.output
    assert "已刪除 1 個" in result.output
    # 23 小時的保留、已完成的保留、管理員與沒有期限記錄的舊帳號都不刪
    assert remaining(app) == {ids["early"], ids["done"], ids["admin"], ids["legacy"]}
    with Session(app.extensions["db_engine"]) as s:
        assert s.query(UserIdentity).count() == 0 and s.query(LockStrike).count() == 0      # 串聯刪除


def test_the_tombstone_holds_only_the_internal_id_the_time_and_the_reason(app, runner):
    ids = seed(app)
    runner.invoke(args=["purge-unconfirmed-accounts"])
    (row,) = tombstones(app)
    assert (row.user_id, row.reason, row.deleted_at) == (ids["late"], "age_unconfirmed", START)


def test_the_deadline_boundary_is_exact_to_the_second(app, runner):
    with Session(app.extensions["db_engine"]) as s:
        just_before = add_user(s, 1, age=None, age_deadline_at=START + timedelta(seconds=1)).id
        exactly = add_user(s, 2, age=None, age_deadline_at=START).id
        s.commit()
    runner.invoke(args=["purge-unconfirmed-accounts"])
    assert remaining(app) == {just_before} and exactly not in remaining(app)


def test_running_it_twice_deletes_nothing_the_second_time(app, runner):
    seed(app)
    assert "已刪除 1 個" in runner.invoke(args=["purge-unconfirmed-accounts"]).output
    assert "已刪除 0 個" in runner.invoke(args=["purge-unconfirmed-accounts"]).output
    assert len(tombstones(app)) == 1


def test_the_accounts_conversations_go_with_it(app, runner):
    ids = seed(app)
    with Session(app.extensions["db_engine"]) as s:
        s.add(ChatConversation(user_id=ids["late"], client_id="00000000-0000-4000-8000-000000000001", state_json={}))
        s.commit()
    runner.invoke(args=["purge-unconfirmed-accounts"])
    with Session(app.extensions["db_engine"]) as s:
        assert s.query(ChatConversation).count() == 0


def test_the_output_and_the_logs_carry_no_personal_data(app, runner, log_lines):
    ids = seed(app)
    dry = runner.invoke(args=["purge-unconfirmed-accounts", "--dry-run"])
    real = runner.invoke(args=["purge-unconfirmed-accounts"])
    events = purge_events(log_lines)
    assert [line["user_id"] for line in events] == [ids["late"]] and events[0]["level"] == "INFO"
    everything = dry.output + real.output + log_lines.raw()
    for secret in (SECRET_EMAIL, SECRET_NAME, "sub-never-finished"):
        assert secret not in everything, secret


def test_an_empty_database_is_fine(app, runner):
    assert "已刪除 0 個" in runner.invoke(args=["purge-unconfirmed-accounts"]).output


def test_the_command_is_hourly_in_the_schedule():
    from app import cli

    assert cli.SCHEDULED_COMMANDS["purge-unconfirmed-accounts"] == "0 * * * *"
