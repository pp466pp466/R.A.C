"""load-testing 與 staging-verification 規格：在目標環境內建立測試玩家並簽出 session cookie（scripts/mint_test_sessions.py）。

映像不新增任何能簽發登入狀態的指令或端點：被執行的只是這個腳本產生的一段文字，腳本本身在 `scripts/`（不進映像）。
"""
import json
import os
import re
import subprocess
import sys

import pytest
from sqlalchemy.orm import Session

from app import create_app
from app.config import PROJECT_ROOT, load_config
from app.models import Base, User
from tests.support.scripts import load_script

mint = load_script("mint_test_sessions")


def environment(database_path, **overrides):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "APP_ENV": "testing", "DATABASE_URL": f"sqlite+pysqlite:///{database_path}"}
    env.pop("SECRET_KEY", None)
    env.update(overrides)
    return env


def run_snippet(snippet, env):
    return subprocess.run([sys.executable, "-c", snippet], cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, encoding="utf-8")


# ---- 參數驗證：不可能夾帶其他程式 ------------------------------------------------------------------


@pytest.mark.parametrize("count", [0, -1, mint.MAX_COUNT + 1, True, "5", 2.5])
def test_the_count_must_be_a_plain_integer_in_range(count):
    with pytest.raises(mint.MintError) as error:
        mint.render_snippet(count, "loadtest-ab12")
    assert error.value.refused


@pytest.mark.parametrize("prefix", ["", "Upper", "a b", "x';import os;'", "-leading", "a" * 40, "中文", "a\nb"])
def test_the_prefix_is_restricted_so_nothing_else_can_be_injected(prefix):
    with pytest.raises(mint.MintError):
        mint.render_snippet(3, prefix)


def test_the_snippet_is_valid_python_names_the_reserved_domain_and_contains_no_password():
    snippet = mint.render_snippet(3, "loadtest-ab12")
    compile(snippet, "<snippet>", "exec")
    assert snippet.isascii()                                       # 容器內由標準輸入傳入，不依賴容器的區域編碼
    assert "loadtest.invalid" in snippet and "password_hash=None" in snippet and "production" in snippet


def test_a_generated_prefix_always_passes_the_prefix_rule():
    for _ in range(20):
        assert mint.PREFIX_PATTERN.fullmatch(mint.new_prefix())


# ---- 真的在環境內執行 -----------------------------------------------------------------------------


def test_the_minted_cookies_are_accepted_by_the_environment_that_signed_them_and_the_players_look_like_real_adults(tmp_path):
    database = tmp_path / "target.sqlite3"
    config = load_config(environ={"APP_ENV": "testing", "DATABASE_URL": f"sqlite+pysqlite:///{database}"})
    app = create_app(config)
    Base.metadata.create_all(app.extensions["db_engine"])

    result = run_snippet(mint.render_snippet(3, "loadtest-ab12"), environment(database))
    assert result.returncode == 0, result.stderr[-400:]
    data = mint.parse_output(result.stdout)
    assert data["cookie_name"] == app.config["SESSION_COOKIE_NAME"] and len(data["sessions"]) == 3
    assert [s["email"] for s in data["sessions"]] == [f"loadtest-ab12-{n}@loadtest.invalid" for n in (1, 2, 3)]

    for session in data["sessions"]:
        client = app.test_client()
        client.set_cookie(data["cookie_name"], session["value"])
        me = client.get("/api/auth/me")
        assert me.status_code == 200
        user = me.get_json()["user"]
        assert user["email"] == session["email"] and user["role"] == "user" and user["status"] == "active"
        assert user["age_confirmed"] is True and user["age_group"] == "adult" and user["restriction"] is None
        assert user["has_password"] is False and user["email_verified"] is True
    with Session(app.extensions["db_engine"]) as db:
        rows = db.query(User).all()
        assert len(rows) == 3 and all(u.password_hash is None and u.terms_version and u.birth_date for u in rows)


def test_every_player_gets_a_different_cookie(tmp_path):
    database = tmp_path / "target.sqlite3"
    app = create_app(load_config(environ={"APP_ENV": "testing", "DATABASE_URL": f"sqlite+pysqlite:///{database}"}))
    Base.metadata.create_all(app.extensions["db_engine"])
    data = mint.parse_output(run_snippet(mint.render_snippet(5, "loadtest-cd34"), environment(database)).stdout)
    assert len({s["value"] for s in data["sessions"]}) == 5


@pytest.mark.parametrize("env", ["production"])
def test_production_is_refused_before_anything_is_created(tmp_path, env):
    database = tmp_path / "must-not-be-created.sqlite3"
    result = run_snippet(mint.render_snippet(2, "loadtest-ef56"), environment(database, APP_ENV=env))
    assert result.returncode != 0 and mint.REFUSED_TOKEN in result.stderr
    assert not database.exists() and mint.MARKER not in result.stdout


def test_the_mint_error_for_a_production_refusal_has_exit_code_two():
    def runner(command, snippet):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr=mint.REFUSED_TOKEN + " | garbled")

    messages: list[str] = []
    assert mint.main(["--count", "2", "--local"], out=messages.append, runner=runner) == mint.EXIT_REFUSED
    assert messages == [mint.REFUSED_IN_PRODUCTION]


# ---- 命令列 -------------------------------------------------------------------------------------


def fake_runner(calls):
    payload = {"cookie_name": "__Host-session", "sessions": [{"email": f"p-{n}@loadtest.invalid", "value": f"cookie-secret-{n}"} for n in (1, 2)]}

    def runner(command, snippet):
        calls.append((command, snippet))
        return subprocess.CompletedProcess(command, 0, stdout='{"log":"noise"}\n' + mint.MARKER + json.dumps(payload) + "\n", stderr="")

    return runner


def test_main_writes_the_utf8_file_and_prints_no_cookie_value(tmp_path):
    calls: list = []
    messages: list[str] = []
    out_file = tmp_path / "sessions.json"
    code = mint.main(["--count", "2", "--prefix", "loadtest-zz99", "--local", "--out", str(out_file)], out=messages.append, runner=fake_runner(calls))
    assert code == 0 and calls[0][0] == [sys.executable]
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["cookie_name"] == "__Host-session" and len(data["sessions"]) == 2
    text = "\n".join(messages)
    assert "cookie-secret" not in text and "loadtest.invalid" in text and "loadtest-zz99" in text


def test_the_smoke_cookie_option_prints_the_name_and_value_pair_only_when_asked(tmp_path):
    messages: list[str] = []
    mint.main(["--count", "1", "--local", "--smoke-cookie", "--out", str(tmp_path / "s.json")], out=messages.append, runner=fake_runner([]))
    assert "__Host-session=cookie-secret-1" in messages


def test_docker_exec_builds_the_command_with_the_prefix_and_the_images_python(tmp_path):
    calls: list = []
    mint.main(
        ["--count", "1", "--docker-exec", "cs-staging-1", "--docker-prefix", "wsl docker", "--out", str(tmp_path / "s.json")],
        out=lambda line: None, runner=fake_runner(calls),
    )
    assert calls[0][0] == ["wsl", "docker", "exec", "cs-staging-1", "python"]


def test_print_snippet_runs_nothing_and_writes_no_file(tmp_path):
    messages: list[str] = []
    out_file = tmp_path / "never.json"

    def never(command, snippet):
        raise AssertionError("只印出程式時不得執行")

    assert mint.main(["--count", "2", "--print-snippet", "--out", str(out_file)], out=messages.append, runner=never) == 0
    assert "create_app" in messages[0] and not out_file.exists()


def test_a_target_failure_is_reported_without_details_and_exits_with_one(tmp_path):
    def runner(command, snippet):
        return subprocess.CompletedProcess(command, 3, stdout="", stderr="Traceback ... secret-detail")

    messages: list[str] = []
    assert mint.main(["--count", "1", "--local", "--out", str(tmp_path / "s.json")], out=messages.append, runner=runner) == mint.EXIT_FAILED
    assert "secret-detail" not in "\n".join(messages) and not (tmp_path / "s.json").exists()


@pytest.mark.parametrize("stdout", ["", "no marker here", mint.MARKER + "not json", mint.MARKER + '{"cookie_name": "x", "sessions": []}'])
def test_unusable_output_is_an_error(stdout):
    with pytest.raises(mint.MintError):
        mint.parse_output(stdout)


# ---- 不進映像、不進版本庫 ----------------------------------------------------------------------------


def test_the_script_and_its_output_stay_out_of_the_image_and_the_repository():
    dockerignore = [line.strip() for line in (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8-sig").splitlines()]
    assert "scripts" in dockerignore and "loadtest" in dockerignore
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8-sig")
    assert "mint_test_sessions" not in dockerfile and not re.search(r"^\s*COPY\s+.*scripts", dockerfile, re.M)
    gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8-sig")
    assert "test-sessions.json" in gitignore


def test_no_route_or_command_of_the_application_can_issue_a_login_state_without_google_or_the_admin_password():
    app = create_app(load_config(environ={"APP_ENV": "testing"}))
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert not {"/api/auth/login", "/api/auth/register", "/__test__/session", "/api/_test/session"} & rules
    commands = set(app.cli.commands)
    assert not {name for name in commands if "session" in name and "purge" not in name and "cookie" in name}
    assert not {"mint-session", "mint-test-sessions", "sign-in", "login-as"} & commands
