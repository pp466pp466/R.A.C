"""access-control 規格：以一次性指令建立首位管理員。"""
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import Base, User
from app.models.user import ROLE_ADMIN, STATUS_ACTIVE
from tests.support.auth import FAST_HASHER, GOOD_PASSWORD

SECRET_PASSWORD = "an-extremely-secret-admin-pw"


@pytest.fixture()
def app():
    application = create_app(load_config(environ={"APP_ENV": "testing"}))
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


@pytest.fixture()
def runner(app):
    return app.test_cli_runner()


def users(app) -> list[User]:
    with Session(app.extensions["db_engine"]) as session:
        return list(session.scalars(select(User)))


def test_interactive_creation_makes_an_active_verified_admin_with_a_hashed_password(app, runner, log_lines):
    result = runner.invoke(
        args=["create-admin"],
        input=f"Boss@Example.com\n系統管理員\n{SECRET_PASSWORD}\n{SECRET_PASSWORD}\n",
    )
    assert result.exit_code == 0, result.output
    (admin,) = users(app)
    assert admin.role == ROLE_ADMIN and admin.status == STATUS_ACTIVE
    assert admin.email == "boss@example.com" and admin.display_name == "系統管理員"
    assert admin.email_verified_at is not None                       # 信箱視為已驗證
    assert admin.terms_version is None and admin.terms_accepted_at is None   # 管理員沒有「同意」這個動作
    assert admin.password_hash.startswith("$argon2id$") and SECRET_PASSWORD not in admin.password_hash
    assert admin.public_id in result.output


def test_the_password_and_the_address_never_appear_in_the_output_or_the_logs(app, runner, log_lines):
    result = runner.invoke(
        args=["create-admin"],
        input=f"boss@example.com\n管理員\n{SECRET_PASSWORD}\n{SECRET_PASSWORD}\n",
    )
    assert result.exit_code == 0
    assert SECRET_PASSWORD not in result.output
    assert "boss@example.com" not in result.output.split("電子郵件", 1)[-1].split("\n", 1)[-1]
    assert SECRET_PASSWORD not in log_lines.raw() and "boss@example.com" not in log_lines.raw()
    assert [line["event"] for line in log_lines() if "event" in line] == ["auth.admin_created"]


def test_non_interactive_creation_reads_the_password_from_stdin(app, runner):
    result = runner.invoke(
        args=["create-admin", "--email", "ops@example.com", "--display-name", "維運", "--password-stdin"],
        input=f"{SECRET_PASSWORD}\n",
    )
    assert result.exit_code == 0, result.output
    (admin,) = users(app)
    assert FAST_HASHER.verify(admin.password_hash, SECRET_PASSWORD)


@pytest.mark.parametrize("line_ending", ["\n", "\r\n"])
def test_a_utf8_bom_from_a_windows_powershell_pipe_is_not_made_part_of_the_password(app, runner, line_ending):
    """Windows PowerShell 5.1 以管線傳字串時會在最前面加 BOM；密碼必須與使用者實際輸入的相同。"""
    result = runner.invoke(
        args=["create-admin", "--email", "ps@example.com", "--display-name", "維運", "--password-stdin"],
        input=f"﻿{SECRET_PASSWORD}{line_ending}",
    )
    assert result.exit_code == 0, result.output
    (admin,) = users(app)
    assert FAST_HASHER.verify(admin.password_hash, SECRET_PASSWORD)
    assert not FAST_HASHER.verify(admin.password_hash, "﻿" + SECRET_PASSWORD)


def test_stdin_mode_requires_the_address_and_name_as_options(app, runner):
    result = runner.invoke(args=["create-admin", "--password-stdin"], input=f"{SECRET_PASSWORD}\n")
    assert result.exit_code == 2
    assert "--email" in result.output and users(app) == []


def test_the_password_cannot_be_given_as_a_command_line_argument(app, runner):
    result = runner.invoke(args=["create-admin", "--email", "a@example.com", "--display-name", "x", "--password", SECRET_PASSWORD])
    assert result.exit_code == 2
    assert users(app) == []


def test_an_existing_address_is_refused_and_the_existing_account_is_untouched(app, runner):
    first = runner.invoke(args=["create-admin", "--email", "boss@example.com", "--display-name", "原本", "--password-stdin"], input=f"{GOOD_PASSWORD}\n")
    assert first.exit_code == 0
    (before,) = users(app)

    second = runner.invoke(
        args=["create-admin", "--email", "BOSS@example.com", "--display-name", "冒名", "--password-stdin"],
        input="another-password-123\n",
    )
    assert second.exit_code != 0
    assert "已經有帳號" in second.output
    (after,) = users(app)
    assert (after.password_hash, after.display_name, after.role) == (before.password_hash, "原本", ROLE_ADMIN)


def test_a_gmail_alias_of_an_existing_account_is_also_refused(app, runner):
    runner.invoke(args=["create-admin", "--email", "boss@gmail.com", "--display-name", "甲", "--password-stdin"], input=f"{GOOD_PASSWORD}\n")
    result = runner.invoke(args=["create-admin", "--email", "b.o.s.s+x@googlemail.com", "--display-name", "乙", "--password-stdin"], input=f"{GOOD_PASSWORD}\n")
    assert result.exit_code != 0
    assert len(users(app)) == 1


@pytest.mark.parametrize(
    ("email", "password", "fragment"),
    [("not-an-address", GOOD_PASSWORD, "電子郵件"), ("a@example.com", "short", "密碼長度")],
)
def test_invalid_input_is_refused_with_a_chinese_message_and_creates_nothing(app, runner, email, password, fragment):
    result = runner.invoke(args=["create-admin", "--email", email, "--display-name", "x", "--password-stdin"], input=f"{password}\n")
    assert result.exit_code != 0
    assert fragment in result.output
    with Session(app.extensions["db_engine"]) as session:
        assert session.scalar(select(func.count(User.id))) == 0


def test_a_mismatched_confirmation_does_not_create_an_account(app, runner):
    result = runner.invoke(args=["create-admin", "--email", "a@example.com", "--display-name", "x"], input=f"{GOOD_PASSWORD}\ndifferent-confirmation\n")
    assert result.exit_code != 0
    assert users(app) == []
