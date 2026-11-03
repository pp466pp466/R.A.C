"""container-runtime 規格的靜態檢查：不需要 Docker 就能守住 Dockerfile、.dockerignore 與 gunicorn 設定。

實際建置與執行行為由 scripts/verify_container.py 驗收（需要 WSL 內的 Docker）。
"""
import re
import runpy

import pytest

from app.config import PROJECT_ROOT

DOCKERFILE = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
DOCKERIGNORE = [
    line.strip() for line in (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.startswith("#")
]


def instructions() -> list[str]:
    """把 Dockerfile 的續行合併後，回傳每個指令（略過註解）。"""
    merged = re.sub(r"\\\n", " ", DOCKERFILE)
    return [ln.strip() for ln in merged.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def test_base_image_matches_the_python_version_used_in_development():
    wanted = (PROJECT_ROOT / ".python-version").read_text(encoding="utf-8").strip()
    from_line = next(i for i in instructions() if i.upper().startswith("FROM "))
    assert f"python:{wanted}" in from_line, f"{from_line} 與 .python-version（{wanted}）不一致"
    assert "bookworm" in from_line, "必須明確指定 Debian 版本，Microsoft ODBC Driver 才有保證"


def test_image_runs_as_a_non_root_user():
    users = [i.split(None, 1)[1].strip() for i in instructions() if i.upper().startswith("USER ")]
    assert users, "Dockerfile 沒有 USER 指令"
    assert users[-1] not in ("root", "0")


def test_image_starts_with_gunicorn_and_never_the_development_server():
    cmd = next(i for i in instructions() if i.upper().startswith("CMD "))
    assert "gunicorn" in cmd
    lowered = DOCKERFILE.lower()
    assert "flask run" not in lowered and "app.run" not in lowered and "FLASK_DEBUG" not in DOCKERFILE


def test_dockerfile_copies_only_explicit_paths_never_the_whole_context():
    copies = [i for i in instructions() if i.upper().startswith(("COPY ", "ADD "))]
    assert copies
    for line in copies:
        assert not re.match(r"(COPY|ADD)\s+(--\S+\s+)*\.\s", line, re.I), f"不可整包複製：{line}"
    assert not any(".env" in line for line in copies)


def test_dockerfile_contains_no_secrets_or_secret_arguments():
    assert not re.search(r"(?im)^\s*(ENV|ARG)\s+\S*(PASSWORD|SECRET|API_KEY|TOKEN)\S*\s*=?\s*\S+", DOCKERFILE)
    assert "GEMINI_API_KEY" not in DOCKERFILE and "SECRET_KEY" not in DOCKERFILE


def test_odbc_driver_is_installed_for_sql_server_connections():
    assert "msodbcsql18" in DOCKERFILE and "unixodbc" in DOCKERFILE
    assert "ACCEPT_EULA=Y" in DOCKERFILE


def test_frontend_assets_and_migrations_are_copied_into_the_image():
    copies = " ".join(i for i in instructions() if i.upper().startswith("COPY "))
    assert "app ./app" in copies          # 含 static/vendor 與 static/fonts
    assert "migrations" in copies and "alembic.ini" in copies


@pytest.mark.parametrize("pattern", [".env", ".env.*", ".git", "tests", "docs", "openspec", ".venv", ".claude", "scripts"])
def test_dockerignore_excludes_secrets_vcs_tests_and_docs(pattern):
    assert pattern in DOCKERIGNORE, f".dockerignore 缺少 {pattern}"


def test_dockerignore_does_not_exclude_what_the_image_needs():
    for needed in ("app", "migrations", "requirements.txt", "gunicorn.conf.py", "wsgi.py", "alembic.ini"):
        assert needed not in DOCKERIGNORE


def test_gunicorn_configuration():
    config = runpy.run_path(str(PROJECT_ROOT / "gunicorn.conf.py"))
    assert config["worker_class"] == "gthread"
    assert config["workers"] >= 1 and config["threads"] >= 2
    assert config["preload_app"] is True, "需要 preload，設定錯誤才會讓主程序以非零碼結束"
    assert config["timeout"] > 30, "worker 逾時必須大於後端單次請求總預算 30 秒"
    assert config["graceful_timeout"] >= 10
    assert config["accesslog"] is None, "請求日誌由應用程式輸出（不含查詢字串），不要開 gunicorn 存取日誌"
    assert config["bind"].startswith("0.0.0.0:")


def test_gunicorn_does_not_open_its_control_socket_which_cannot_be_created_for_the_homeless_non_root_user():
    """staging-deployment 的本機容器預演發現：gunicorn 26 的控制通道預設路徑在家目錄，非 root 使用者的家目錄是 /nonexistent，
    啟動時會寫出 `ERROR Control server error: Permission denied`。服務不受影響，但不該有看起來像異常的錯誤行。"""
    assert runpy.run_path(str(PROJECT_ROOT / "gunicorn.conf.py"))["control_socket_disable"] is True


def test_gunicorn_port_follows_the_platform_port_variable(monkeypatch):
    monkeypatch.setenv("PORT", "9191")
    assert runpy.run_path(str(PROJECT_ROOT / "gunicorn.conf.py"))["bind"] == "0.0.0.0:9191"


def test_verification_scripts_exist_and_the_wrapper_is_ascii_only():
    assert (PROJECT_ROOT / "scripts" / "verify_container.py").is_file()
    wrapper = (PROJECT_ROOT / "scripts" / "verify-container.ps1").read_bytes()
    assert wrapper.isascii(), "Windows PowerShell 5.1 以系統編碼讀無 BOM 的腳本，非 ASCII 字元會亂碼"

def test_the_container_acceptance_covers_the_model_key_and_the_chat_flow():
    """container-runtime 規格：staging 與 production 沒有金鑰就無法啟動，所以驗收用假金鑰啟動、另驗缺少金鑰、並走完客服的 HTTP 流程。"""
    script = (PROJECT_ROOT / "scripts" / "verify_container.py").read_text(encoding="utf-8-sig")
    assert script.count("GEMINI_API_KEY={FAKE_GEMINI_KEY}") >= 2                  # production 與預備環境的流程都帶假金鑰
    assert "staging 缺少 GEMINI_API_KEY" in script and "FAKE_GEMINI_KEY" in script
    for needed in ("age_confirmation_required", "version_conflict", "/cs", "request_timeout_ms", "chat:"):
        assert needed in script, needed
    namespace = runpy.run_path(str(PROJECT_ROOT / "scripts" / "verify_container.py"))
    flow = namespace["render_auth_flow"]("minted-session-value")
    compile(flow, "flow", "exec")
    assert flow.isascii() and flow.count("chat: ") >= 14                          # 容器內的腳本以 stdin 傳入：必須是 ASCII
    assert namespace["FAKE_GEMINI_KEY"] not in (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8-sig")
