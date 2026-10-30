"""deletion-replay 規格：`flask replay-deletions`——輸出只有筆數、檔案先整體驗證、預覽不改資料、重複執行為 0、日誌與輸出沒有個資。"""
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base, DeletionTombstone, User
from app.repositories.deletion_tombstone_repository import DeletionTombstoneRepository
from tests.support.auth import START, FakeClock, add_user

LONG_AGO = START - timedelta(days=90)
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


def user_ids(app) -> list[int]:
    with Session(app.extensions["db_engine"]) as s:
        return sorted(s.scalars(select(User.id)))


def seed(app, *, tombstoned: int = 1) -> list[int]:
    """`tombstoned` 位復活的帳號（有刪除紀錄）加一位沒有紀錄的一般帳號。"""
    with Session(app.extensions["db_engine"]) as s:
        zombies = [
            add_user(s, n, email=SECRET_EMAIL if n == 1 else f"user{n}@example.com", email_canonical=SECRET_EMAIL if n == 1 else f"user{n}@example.com",
                     display_name=SECRET_NAME if n == 1 else f"使用者{n}", created_at=LONG_AGO)
            for n in range(1, tombstoned + 1)
        ]
        add_user(s, 50, created_at=LONG_AGO)
        for zombie in zombies:
            DeletionTombstoneRepository(s).record(user_id=zombie.id, reason="user_requested", deleted_at=START)
        s.commit()
        return [zombie.id for zombie in zombies]


def write(tmp_path, text: str, name="deleted.txt", encoding="utf-8"):
    path = tmp_path / name
    path.write_text(text, encoding=encoding)
    return str(path)


def test_the_command_is_registered_documented_and_classified_as_manual(app):
    from app import cli

    assert "replay-deletions" in app.cli.commands and "replay-deletions" in (cli.__doc__ or "")
    assert "replay-deletions" in cli.MANUAL_COMMANDS and "replay-deletions" not in cli.SCHEDULED_COMMANDS


def test_it_erases_the_restored_account_and_prints_only_counts(app, runner):
    (zombie,) = seed(app)
    result = runner.invoke(args=["replay-deletions"])
    assert result.exit_code == 0, result.output
    assert "已抹除：1 個帳號" in result.output and "略過（編號已被還原後的新使用者重新使用）：0 個" in result.output
    assert zombie not in user_ids(app) and len(user_ids(app)) == 1
    for secret in (SECRET_NAME, SECRET_EMAIL, str(zombie) + ","):
        assert secret not in result.output


def test_running_it_twice_changes_nothing_the_second_time(app, runner):
    seed(app)
    runner.invoke(args=["replay-deletions"])
    second = runner.invoke(args=["replay-deletions"])
    assert second.exit_code == 0 and "已抹除：0 個帳號" in second.output and "已經不存在：1 個" in second.output


def test_the_preview_reports_the_counts_and_changes_nothing(app, runner, log_lines):
    seed(app, tombstoned=2)
    before = user_ids(app)
    result = runner.invoke(args=["replay-deletions", "--dry-run"])
    assert result.exit_code == 0 and "預覽" in result.output and "將抹除：2 個帳號" in result.output
    assert user_ids(app) == before
    assert [line for line in log_lines() if line.get("event") == "account.deletion_replayed"] == []


def test_the_ids_file_covers_deletions_that_happened_after_the_backup(app, runner, tmp_path):
    with Session(app.extensions["db_engine"]) as s:
        later = add_user(s, 12, created_at=LONG_AGO)
        later_id = later.id
    path = write(tmp_path, f"{later_id},{START.isoformat()}\n")
    result = runner.invoke(args=["replay-deletions", "--ids-file", path])
    assert result.exit_code == 0 and "已抹除：1 個帳號" in result.output
    assert later_id not in user_ids(app)
    with Session(app.extensions["db_engine"]) as s:
        (row,) = s.scalars(select(DeletionTombstone)).all()
        assert (row.user_id, row.reason) == (later_id, "restore_replay")


def test_the_ids_file_is_read_as_utf8_with_or_without_a_bom(app, runner, tmp_path):
    with Session(app.extensions["db_engine"]) as s:
        user_id = add_user(s, 1, created_at=LONG_AGO).id
    path = write(tmp_path, f"{user_id},{START.isoformat()}\n", encoding="utf-8-sig")
    assert runner.invoke(args=["replay-deletions", "--ids-file", path]).exit_code == 0
    assert user_id not in user_ids(app)


def test_a_malformed_file_fails_before_anything_is_erased_and_names_only_the_line(app, runner, tmp_path):
    zombies = seed(app, tombstoned=2)
    marker = "do-not-echo-this-content"
    path = write(tmp_path, f"{zombies[0]},{START.isoformat()}\n{marker}\n")
    result = runner.invoke(args=["replay-deletions", "--ids-file", path])
    assert result.exit_code != 0 and "第 2 行" in result.output
    assert marker not in result.output and path not in result.output
    assert set(zombies) <= set(user_ids(app)), "格式錯誤時不得抹除任何帳號（連資料庫裡有紀錄的也不行）"


def test_a_missing_file_fails_without_echoing_the_path(app, runner, tmp_path):
    seed(app)
    missing = str(tmp_path / "no-such-secret-folder" / "nope.txt")
    result = runner.invoke(args=["replay-deletions", "--ids-file", missing])
    assert result.exit_code != 0 and "no-such-secret-folder" not in result.output
    assert len(user_ids(app)) == 2


def test_a_reissued_id_is_counted_and_not_erased(app, runner):
    with Session(app.extensions["db_engine"]) as s:
        newcomer = add_user(s, 1, created_at=START + timedelta(days=5))
        DeletionTombstoneRepository(s).record(user_id=newcomer.id, reason="user_requested", deleted_at=START)
        s.commit()
        newcomer_id = newcomer.id
    result = runner.invoke(args=["replay-deletions"])
    assert "略過（編號已被還原後的新使用者重新使用）：1 個" in result.output and user_ids(app) == [newcomer_id]


def test_the_whole_flow_leaves_no_personal_data_in_the_logs_or_the_output(app, runner, log_lines, tmp_path):
    zombies = seed(app)
    path = write(tmp_path, f"{zombies[0]},{START.isoformat()}\n")
    outputs = [
        runner.invoke(args=["replay-deletions", "--dry-run"]).output,
        runner.invoke(args=["replay-deletions", "--ids-file", path]).output,
        runner.invoke(args=["replay-deletions"]).output,
    ]
    raw = log_lines.raw() + "".join(outputs)
    for secret in (SECRET_NAME, SECRET_EMAIL, "secret.person", path, "deleted.txt"):
        assert secret not in raw, secret
    events = [line for line in log_lines() if line.get("event") == "account.deletion_replayed"]
    assert [e["user_id"] for e in events] == zombies
