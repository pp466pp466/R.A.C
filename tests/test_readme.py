"""README 與程式／設定檔一致性的測試：文件列出的內容必須與實際行為相符。"""
import re

from app.config import PROJECT_ROOT

README = PROJECT_ROOT / "README.md"


def readme_section(title: str) -> str:
    """取出 `## <title>` 到下一個 `## ` 之間的內容。"""
    text = README.read_text(encoding="utf-8-sig")
    match = re.search(rf"^## {re.escape(title)}\s*$(.*?)(?=^## |\Z)", text, re.S | re.M)
    assert match, f"README 缺少章節：{title}"
    return match.group(1)


def table_first_column_names(section: str) -> set[str]:
    names: set[str] = set()
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and re.fullmatch(r"`[A-Z][A-Z0-9_]*`", cells[0]):
            names.add(cells[0].strip("`"))
    return names


def env_example_names() -> set[str]:
    names: set[str] = set()
    for line in (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8-sig").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            names.add(line.partition("=")[0].strip())
    return names


def test_readme_environment_variables_match_env_example():
    documented = table_first_column_names(readme_section("環境變數與機密"))
    assert documented == env_example_names()


def test_readme_database_section_documents_real_commands_and_prerequisites():
    section = readme_section("資料庫與 migration")
    assert "alembic upgrade head" in section
    assert "pytest -m sqlserver" in section
    assert "TEST_SQLSERVER_URL" in section and "project_test" in section
    assert "純 ASCII" in section
    # 文件中的標記必須真的存在於 pytest 設定
    assert "sqlserver:" in (PROJECT_ROOT / "pytest.ini").read_text(encoding="utf-8")
    # 文件提到的輔助函式必須真的存在
    from app.models import base as models_base
    from app.services import normalization

    assert hasattr(models_base, "unique_when_not_null")
    assert hasattr(normalization, "normalize_unique_text")
    assert "unique_when_not_null()" in section and "normalize_unique_text()" in section


def test_readme_logging_section_matches_the_formatter_allowlist():
    from app.observability.json_logging import ALLOWED_FIELDS

    section = readme_section("日誌欄位與健康檢查")
    documented: set[str] = set()
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and re.fullmatch(r"`[a-z_]+`", cells[0]):
            documented.add(cells[0].strip("`"))
    # 健康檢查表格的第一欄是路徑（含 /），不會被上面的規則抓到
    assert documented == {"time", "level", "logger", "message", *ALLOWED_FIELDS}


def test_readme_logging_section_lists_every_forbidden_kind_of_data_and_the_health_endpoints():
    section = readme_section("日誌欄位與健康檢查")
    for phrase in ("密碼", "Cookie", "權杖", "授權標頭", "API 金鑰", "email", "使用者訊息全文", "模型回覆"):
        assert phrase in section, f"README 應明確列出禁止記錄：{phrase}"
    assert "/healthz" in section and "/readyz" in section
    assert "不含查詢字串" in section or "不含查詢字串" in section.replace("**", "")


def test_readme_asset_table_matches_the_manifest_and_the_real_font_size():
    import json

    manifest = json.loads((PROJECT_ROOT / "app" / "static" / "MANIFEST.json").read_text(encoding="utf-8-sig"))
    section = readme_section("離線資產與更新方式")
    for asset in manifest["assets"]:
        assert asset["version"] in section, f"README 沒有記載 {asset['name']} 的版本 {asset['version']}"
        assert asset["license"] in section
    font_bytes = sum(p.stat().st_size for p in (PROJECT_ROOT / "app" / "static" / "fonts").glob("*.woff2"))
    assert f"{font_bytes / 1024 / 1024:.1f} MB" in section, "README 記載的字型總量與實際不符"
    assert "update_asset_manifest.py" in section and "dist.integrity" in section


def test_readme_components_section_documents_the_real_api_and_acceptance_record():
    section = readme_section("共用元件")
    for needle in ("UI.install", "UI.toast", "TypewriterBuffer", "/dev/components", "node --test", "54 項全數通過", "仍需你目視確認"):
        assert needle in section, f"README 共用元件章節缺少：{needle}"
    # 文件列出的全域 API 必須真的存在
    code = (PROJECT_ROOT / "app" / "static" / "js" / "ui" / "ui-components.js").read_text(encoding="utf-8")
    for api in ("root.UI.install", "root.UI.toast", "root.UI.useToast"):
        assert api in code
    typewriter = (PROJECT_ROOT / "app" / "static" / "js" / "ui" / "typewriter.js").read_text(encoding="utf-8")
    for method in ("prototype.play", "prototype.push", "prototype.finish", "prototype.flush", "prototype.cancel"):
        assert method in typewriter


def test_readme_typewriter_numbers_match_the_defaults_in_code():
    typewriter = (PROJECT_ROOT / "app" / "static" / "js" / "ui" / "typewriter.js").read_text(encoding="utf-8")
    assert "baseCharsPerSecond: 40" in typewriter and "accelerationPerChar: 0.2" in typewriter and "finishSeconds: 3" in typewriter
    section = readme_section("共用元件")
    assert "基本速度 40 字／秒" in section and "加速係數 0.2" in section and "補完期限 3 秒" in section


def test_readme_container_section_matches_the_actual_files_and_states_the_sql_server_limitation():
    import runpy

    section = readme_section("容器建置與驗收")
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"FROM (\S+)", dockerfile).group(1) in section, "README 的基底映像與 Dockerfile 不一致"
    config = runpy.run_path(str(PROJECT_ROOT / "gunicorn.conf.py"))
    assert f"{config['workers']} 個 worker" in section and f"每個 {config['threads']} 條執行緒" in section
    assert f"worker 逾時 {config['timeout']} 秒" in section and f"最多等 {config['graceful_timeout']} 秒" in section
    for command in ("verify-container.ps1", "--no-build", "alembic upgrade head", "WEB_CONCURRENCY", "GUNICORN_THREADS"):
        assert command in section
    # 限制必須被明確記載
    assert "127.0.0.1" in section and "SQLite" in section and "pytest -m sqlserver" in section
    assert "沒有" in section and "慢請求" in section


def test_readme_member_auth_endpoint_tables_list_exactly_the_registered_routes():
    """README 的端點與頁面表格必須與程式實際註冊的路由完全一致（多寫或漏寫都會失敗）。"""
    from app import create_app
    from app.config import load_config

    section = readme_section("會員驗證（C-2）")
    documented: set[tuple[str, str]] = set()
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and re.fullmatch(r"`/[^`]*`(、`/[^`]*`)*", cells[0]):
            methods = {m.strip() for m in cells[1].split("、")} if cells[1] in ("GET", "POST") else {"GET"}
            for path in re.findall(r"`(/[^`]*)`", cells[0]):
                documented.add((path, "POST" if "POST" in methods else "GET"))

    app = create_app(load_config(environ={"APP_ENV": "testing"}))
    actual: set[tuple[str, str]] = set()
    for rule in app.url_map.iter_rules():
        if rule.rule.startswith("/api/auth/") or rule.endpoint.startswith("auth_pages."):
            for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
                actual.add((rule.rule, method))
    assert len(actual) >= 20, "沒有掃到預期數量的路由，比對可能是空洞的"
    assert documented == actual, f"README 多寫：{sorted(documented - actual)}；漏寫：{sorted(actual - documented)}"


def test_readme_cs_endpoint_tables_list_exactly_the_registered_routes():
    """客服的端點與頁面表格必須與實際註冊的路由完全一致；同一路徑的不同方法各佔一列。"""
    from app import create_app
    from app.config import load_config

    section = readme_section("客服對話（C-3）")
    documented: set[tuple[str, str]] = set()
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and re.fullmatch(r"`/[^`]*`(、`/[^`]*`)*", cells[0]):
            methods = {m.strip() for m in cells[1].split("、")} if re.fullmatch(r"[A-Z]+(、[A-Z]+)*", cells[1]) else {"GET"}
            for path in re.findall(r"`(/[^`]*)`", cells[0]):
                documented.update((path, method) for method in methods)

    app = create_app(load_config(environ={"APP_ENV": "testing"}))
    actual: set[tuple[str, str]] = set()
    for rule in app.url_map.iter_rules():
        if rule.rule.startswith("/api/cs/") or rule.endpoint.startswith("cs_pages."):
            actual.update((rule.rule, method) for method in rule.methods - {"HEAD", "OPTIONS"})
    assert len(actual) == 8, "沒有掃到預期數量的路由，比對可能是空洞的"
    assert documented == actual, f"README 多寫：{sorted(documented - actual)}；漏寫：{sorted(actual - documented)}"


def test_readme_admin_endpoint_tables_list_exactly_the_registered_routes():
    """後台的端點與頁面表格必須與實際註冊的路由完全一致；同一路徑的不同方法各佔一列。"""
    from app import create_app
    from app.config import load_config

    section = readme_section("管理員後台（C-4b）")
    documented: set[tuple[str, str]] = set()
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and re.fullmatch(r"`/[^`]*`(、`/[^`]*`)*", cells[0]):
            methods = {m.strip() for m in cells[1].split("、")} if re.fullmatch(r"[A-Z]+(、[A-Z]+)*", cells[1]) else {"GET"}
            for path in re.findall(r"`(/[^`]*)`", cells[0]):
                documented.update((path, method) for method in methods)

    app = create_app(load_config(environ={"APP_ENV": "testing"}))
    actual: set[tuple[str, str]] = set()
    for rule in app.url_map.iter_rules():
        if rule.rule.startswith("/api/admin/") or rule.endpoint.startswith("admin_pages."):
            actual.update((rule.rule, method) for method in rule.methods - {"HEAD", "OPTIONS"})
    assert len(actual) == 13, "沒有掃到預期數量的路由，比對可能是空洞的"
    assert documented == actual, f"README 多寫：{sorted(documented - actual)}；漏寫：{sorted(actual - documented)}"


def test_readme_admin_section_documents_the_real_audit_actions_error_codes_and_the_limits():
    from app.models.admin_audit_log import AUDIT_ACTIONS

    section = readme_section("管理員後台（C-4b）")
    for action in AUDIT_ACTIONS:
        assert f"`{action}`" in section, f"README 沒有記載稽核動作：{action}"
    for code in ("admin_target_forbidden", "target_pending_deletion", "already_suspended", "not_suspended", "tier_unchanged", "state_changed", "no_password"):
        assert code in section, code
    for phrase in ("非管理員一律 404", "no-store", "連續 5 次鎖定 15 分鐘", "沒有更新與刪除", "已知限制", "C-4c", "只有 `unknown` 的對話**不算**"):
        assert phrase in section, phrase
    # 文件提到的前端檔案必須真的存在
    for path in ("app/static/js/admin/admin-format.js", "app/static/js/admin/admin.js", "app/static/css/admin.css", "tests/js/admin-format.test.js"):
        assert (PROJECT_ROOT / path).is_file(), path


def test_readme_cs_section_states_the_limits_the_models_and_the_numbers_that_matter():
    section = readme_section("客服對話（C-3）")
    for phrase in ("C-4 之前不得對公開網路開放", "gemini-3.5-flash-lite", "35000", "no-store", "意圖、實體、信心、摘要與狀態都不離開伺服器"):
        assert phrase in section, phrase
    for code in ("conversation_busy", "message_id_reused", "version_conflict", "service_unavailable", "age_confirmation_required"):
        assert code in section, code


def test_readme_documents_the_browser_acceptance_and_the_known_limitations():
    section = readme_section("會員驗證（C-2）")
    for needle in ("84 / 84", "v-cloak", "殘留風險", "帳本", "keep_changes_on_error", "草稿", "token_version"):
        assert needle in section, f"README 會員驗證章節缺少：{needle}"
    assert "keep_changes_on_error" in (PROJECT_ROOT / "app" / "db" / "session.py").read_text(encoding="utf-8")


def test_readme_states_that_env_must_never_be_committed():
    section = readme_section("環境變數與機密")
    assert "絕不可提交" in section
    assert "沒有預設值" in section
