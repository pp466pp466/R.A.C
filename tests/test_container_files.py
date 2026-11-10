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


RPG_CONFIG = PROJECT_ROOT / "gunicorn.rpg.conf.py"


def test_both_gunicorn_configurations_exist_and_the_image_contains_them():
    """container-runtime 規格「RPG 串流使用獨立的 gunicorn 設定」：兩份設定都在映像裡，預設啟動客服池。"""
    assert (PROJECT_ROOT / "gunicorn.conf.py").is_file() and RPG_CONFIG.is_file()
    copies = " ".join(i for i in instructions() if i.upper().startswith("COPY "))
    assert "gunicorn.conf.py" in copies and "gunicorn.rpg.conf.py" in copies
    cmd = next(i for i in instructions() if i.upper().startswith("CMD "))
    assert "gunicorn.conf.py" in cmd and "gunicorn.rpg" not in cmd, "預設仍啟動客服池，RPG 池由部署指定設定檔"
    assert "gunicorn.rpg.conf.py" not in DOCKERIGNORE


def test_the_rpg_gunicorn_configuration_outlasts_the_generation_limit_and_is_a_real_server_config():
    config = runpy.run_path(str(RPG_CONFIG))
    assert config["worker_class"] == "gthread" and config["workers"] >= 1
    assert config["threads"] > runpy.run_path(str(PROJECT_ROOT / "gunicorn.conf.py"))["threads"], "每個串流多佔一條讀取執行緒，執行緒數要比客服池多"
    assert config["timeout"] > 60 + 15, "worker 逾時必須大於單次生成上限 60 秒與心跳間隔 15 秒"
    assert config["timeout"] >= 150, "一個回合最壞約 150 秒（被擋兩次加狀態呼叫）"
    assert config["graceful_timeout"] >= 30, "進行中的回合要有時間寫回（完成、結算或退款）"
    assert config["preload_app"] is True and config["accesslog"] is None and config["control_socket_disable"] is True
    assert config["bind"].startswith("0.0.0.0:")


def test_the_rpg_gunicorn_configuration_only_sets_options_the_customer_service_pool_already_uses():
    """gunicorn 在 Windows 上無法匯入（`grp`），所以本機以名稱檢查：RPG 設定的每個選項都已在客服池的設定出現過（那份在 staging 實跑驗證過），
    拼錯的選項名稱 gunicorn 會悄悄忽略。"""
    base = {key for key in runpy.run_path(str(PROJECT_ROOT / "gunicorn.conf.py")) if not key.startswith("_")}
    rpg = {key for key in runpy.run_path(str(RPG_CONFIG)) if not key.startswith("_")}
    assert rpg - {"os"} <= base | {"os"}, sorted(rpg - base)


def test_the_rpg_gunicorn_configuration_is_accepted_by_gunicorn_itself():
    """用 gunicorn 自己的設定載入器讀取（Linux 與容器內；Windows 沒有 `grp` 時略過）：值有誤時在這裡失敗，而不是到部署才發現。"""
    pytest.importorskip("grp")
    from gunicorn.config import Config

    cfg = Config()
    for key, value in runpy.run_path(str(RPG_CONFIG)).items():
        if key in cfg.settings:
            cfg.set(key, value)
    assert cfg.worker_class_str == "gthread" and cfg.timeout == 180


def test_the_rpg_configuration_never_uses_the_development_server():
    text = RPG_CONFIG.read_text(encoding="utf-8-sig").lower()
    assert "app.run" not in text and "flask run" not in text and "debug" not in text.replace("loglevel", "")


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

def test_the_container_acceptance_covers_the_script_authoring_migration_limits_and_retention_preview():
    """rpg-script-authoring：遷移升級到 0013、flask 的限制指令（七個鍵、修改後讀回）與期限清除預覽含送審紀錄。"""
    script = (PROJECT_ROOT / "scripts" / "verify_container.py").read_text(encoding="utf-8-sig")
    for needed in ("0013_rpg_script_authoring", "show-script-limits", "set-script-limit", "script_limit.free", "purge-expired-data", "劇本送審紀錄"):
        assert needed in script, needed
    namespace = runpy.run_path(str(PROJECT_ROOT / "scripts" / "verify_container.py"))
    from app.services.runtime_limits import SCRIPT_LIMIT_KEYS

    assert tuple(namespace["SCRIPT_LIMIT_KEYS"]) == tuple(SCRIPT_LIMIT_KEYS)                  # 驗收腳本的鍵與程式的規格一致


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


def test_the_container_acceptance_covers_the_protagonist_migration_schema_lexicon_group_and_login_gates():
    """rpg-character-creation：遷移升級到 0014、sessions.protagonist_json 欄位、三支 /api/sessions 端點未登入為 401。"""
    script = (PROJECT_ROOT / "scripts" / "verify_container.py").read_text(encoding="utf-8-sig")
    for needed in ("0014_rpg_character_creation", "protagonist_json", "/api/sessions/setup/", "/api/sessions/protagonist/suggest", "protagonist_suggest_daily.vip"):
        assert needed in script, needed
    namespace = runpy.run_path(str(PROJECT_ROOT / "scripts" / "verify_container.py"))
    flow = namespace["render_auth_flow"]("minted-session-value")
    compile(flow, "flow", "exec")
    assert flow.isascii() and flow.count('/api/sessions') >= 3
    assert len(namespace["SCRIPT_LIMIT_KEYS"]) == 10


def test_the_container_acceptance_covers_the_wallet_migration_tables_settings_and_login_gates():
    """game-wallet：遷移升級到 0015、兩張表、五個設定鍵（與程式的規格一致）與修改讀回、兩組指令互相拒絕對方的鍵、三支 /api/wallet 端點未登入為 401。"""
    script = (PROJECT_ROOT / "scripts" / "verify_container.py").read_text(encoding="utf-8-sig")
    for needed in ("0015_game_wallet", "wallets", "wallet_entries", "show-wallet-settings", "set-wallet-setting", "/api/wallet", "/api/wallet/checkin", "/api/wallet/entries"):
        assert needed in script, needed
    namespace = runpy.run_path(str(PROJECT_ROOT / "scripts" / "verify_container.py"))
    from app.services.runtime_limits import WALLET_LIMIT_KEYS

    assert tuple(namespace["WALLET_SETTING_KEYS"]) == tuple(WALLET_LIMIT_KEYS)                  # 驗收腳本的鍵與程式的規格一致
