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


def test_readme_states_that_env_must_never_be_committed():
    section = readme_section("環境變數與機密")
    assert "絕不可提交" in section
    assert "沒有預設值" in section
