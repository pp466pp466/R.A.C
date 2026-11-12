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
    assert len(actual) >= 15, "沒有掃到預期數量的路由，比對可能是空洞的"
    assert documented == actual, f"README 多寫：{sorted(documented - actual)}；漏寫：{sorted(actual - documented)}"


def test_readme_script_authoring_tables_list_exactly_the_registered_routes_and_both_limit_commands():
    """劇本創作的端點與頁面表格必須與實際註冊的路由完全一致；兩個營運限制指令與全部的鍵（分散在創作劇本與玩家主角兩節）都要記載。"""
    from app import create_app
    from app.config import load_config
    from app.services.runtime_limits import LIMIT_KEYS

    section = readme_section("玩家創作劇本（rpg-script-authoring）")
    documented: set[tuple[str, str]] = set()
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and re.fullmatch(r"`/[^`]*`", cells[0]):
            methods = set(cells[1].split("、")) if re.fullmatch(r"[A-Z]+(、[A-Z]+)*", cells[1]) else {"GET"}
            documented.update((cells[0].strip("`"), method) for method in methods)

    app = create_app(load_config(environ={"APP_ENV": "testing"}))
    actual: set[tuple[str, str]] = set()
    for rule in app.url_map.iter_rules():
        if rule.rule.startswith("/api/scripts") or rule.endpoint.startswith("script_pages."):
            actual.update((rule.rule, method) for method in rule.methods - {"HEAD", "OPTIONS"})
    assert len(actual) == 12, "沒有掃到預期數量的路由，比對可能是空洞的"
    assert documented == actual, f"README 多寫：{sorted(documented - actual)}；漏寫：{sorted(actual - documented)}"
    for command in ("set-script-limit", "show-script-limits"):
        assert command in section, command
    whole = README.read_text(encoding="utf-8-sig")                                    # 十個鍵分散在創作劇本與玩家主角兩節
    for key in LIMIT_KEYS:
        assert key.split(".")[0] in whole, key


def test_readme_protagonist_table_lists_exactly_the_registered_session_routes_and_the_timeout_advice():
    """玩家主角、開存檔與 RPG 入口清單（`catalog`、`GET /api/sessions`）的端點表必須與實際註冊的 `/api/sessions` 路由完全一致（共 5 條）；逾時提醒與新限制鍵、錯誤代碼都要記載。"""
    from app import create_app
    from app.config import load_config

    section = readme_section("玩家主角與開存檔（rpg-character-creation）")
    documented: set[tuple[str, str]] = set()
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and re.fullmatch(r"`/[^`]*`", cells[0]):
            methods = set(cells[1].split("、")) if re.fullmatch(r"[A-Z]+(、[A-Z]+)*", cells[1]) else {"GET"}
            documented.update((cells[0].strip("`"), method) for method in methods)

    app = create_app(load_config(environ={"APP_ENV": "testing"}))
    actual: set[tuple[str, str]] = set()
    for rule in app.url_map.iter_rules():
        if rule.rule.startswith("/api/sessions"):
            actual.update((rule.rule, method) for method in rule.methods - {"HEAD", "OPTIONS"})
    assert len(actual) == 5, "沒有掃到預期數量的路由，比對可能是空洞的"
    assert documented == actual, f"README 多寫：{sorted(documented - actual)}；漏寫：{sorted(actual - documented)}"
    for needed in ("protagonist_suggest_daily", "65 秒", "suggestion_unavailable", "suggestion_limit", "protagonist_rejected", "review_unavailable", "十個鍵"):
        assert needed in section, needed


def test_readme_wallet_table_lists_exactly_the_registered_routes_and_both_wallet_commands():
    """遊戲幣的端點表必須與實際註冊的 `/api/wallet` 路由完全一致；兩個指令、五個設定鍵與錯誤代碼都要記載。"""
    from app import create_app
    from app.config import load_config
    from app.services.runtime_limits import WALLET_LIMIT_KEYS

    section = readme_section("遊戲幣錢包（game-wallet）")
    documented: set[tuple[str, str]] = set()
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and re.fullmatch(r"`/api/wallet[^`]*`", cells[0]):
            documented.add((cells[0].strip("`"), cells[1]))

    app = create_app(load_config(environ={"APP_ENV": "testing"}))
    actual = {
        (rule.rule, method) for rule in app.url_map.iter_rules() if rule.rule.startswith("/api/wallet")
        for method in rule.methods - {"HEAD", "OPTIONS"}
    }
    assert len(actual) == 3, "沒有掃到預期數量的路由，比對可能是空洞的"
    assert documented == actual, f"README 多寫：{sorted(documented - actual)}；漏寫：{sorted(actual - documented)}"
    for command in ("set-wallet-setting", "show-wallet-settings"):
        assert command in section, command
    for key in WALLET_LIMIT_KEYS:
        assert f"`{key}`" in section, key
    for code in ("insufficient_coins", "checkin_overflow", "already_checked_in", "wallet_busy"):
        assert code in section, code


def test_readme_rpg_turn_engine_section_lists_exactly_the_registered_game_routes_and_documents_the_protocol():
    """RPG 回合引擎的端點表必須與實際註冊的 `/api/game` 路由完全一致；七種事件、結束標記、開始前的錯誤代碼、前端逾時建議與兩份 gunicorn 設定都要記載。"""
    from app import create_app
    from app.config import load_config
    from app.controllers.game_api_controller import SSE_CACHE_CONTROL, SSE_CONTENT_TYPE
    from app.services.rpg_runner_base import ERROR_MESSAGES

    section = readme_section("RPG 回合引擎（rpg-turn-engine）")
    documented: set[tuple[str, str]] = set()
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and re.fullmatch(r"`/api/game[^`]*`", cells[0]):
            documented.add((cells[0].strip("`").replace("<session_id>", "<id>"), cells[1]))

    app = create_app(load_config(environ={"APP_ENV": "testing"}))
    actual = {
        (rule.rule.replace("<session_id>", "<id>"), method) for rule in app.url_map.iter_rules() if rule.rule.startswith("/api/game")
        for method in rule.methods - {"HEAD", "OPTIONS"}
    }
    assert len(actual) == 6, "沒有掃到預期數量的路由，比對可能是空洞的"       # 4 個回合與唯讀端點，加上記事簿的 GET 與 PUT（rpg-notebook）
    assert documented == actual, f"README 多寫：{sorted(documented - actual)}；漏寫：{sorted(actual - documented)}"
    for event in ("meta", "token", "ping", "transition", "state", "done", "error"):
        assert f"| `{event}` |" in section, event
    for code in ERROR_MESSAGES:
        assert f"`{code}`" in section, code
    for code in ("insufficient_coins", "session_ended", "opening_pending", "opening_in_progress", "input_blocked", "system_busy", "service_unavailable"):
        assert code in section, code
    assert SSE_CONTENT_TYPE in section and SSE_CACHE_CONTROL in section and "X-Accel-Buffering: no" in section
    assert "180 秒" in section and "gunicorn.rpg.conf.py" in section and "0016_rpg_turn_engine" in section


def test_readme_rpg_notebook_section_documents_the_numbers_errors_and_every_log_event():
    """記事簿一節的數值（三個等級的容量與預備空間、5 條 50 字、60／30／20 秒）、玩家 API 的錯誤代碼與六種日誌事件都要記載，且與程式的常數一致。"""
    from app.observability import rpg_events
    from app.services import notebook_policy
    from app.services.rpg_notebook_capture import CALL_BUDGET_SECONDS, CONDENSE_MIN_REMAINING_SECONDS, TOTAL_BUDGET_SECONDS
    from app.services.rpg_notebook_prompt import MAX_ENTRIES_PER_CAPTURE, MAX_ENTRY_CHARS

    section = readme_section("RPG 記事簿（rpg-notebook）")
    for tier in ("free", "plus", "vip"):
        assert f"{tier} {notebook_policy.capacity_for(tier)}／{notebook_policy.reserve_for(tier)}" in section, tier
    assert f"每次最多 {MAX_ENTRIES_PER_CAPTURE} 條、每條 {MAX_ENTRY_CHARS} 字" in section
    assert f"總預算 {int(TOTAL_BUDGET_SECONDS)} 秒" in section and f"min({int(CALL_BUDGET_SECONDS)}, 剩餘)" in section
    assert f"不足 {int(CONDENSE_MIN_REMAINING_SECONDS)} 秒" in section
    for code in ("notebook_too_long", "notebook_blocked", "notebook_conflict", "session_ended"):
        assert f"`{code}`" in section, code
    for event in (
        rpg_events.EVENT_NOTEBOOK_CAPTURED, rpg_events.EVENT_NOTEBOOK_SKIPPED, rpg_events.EVENT_NOTEBOOK_ABANDONED,
        rpg_events.EVENT_NOTEBOOK_FAILED, rpg_events.EVENT_NOTEBOOK_SAVED, rpg_events.EVENT_NOTEBOOK_SAVE_REJECTED,
    ):
        assert f"| `{event}` |" in section, event
    assert "/api/game/sessions/<id>/notebook" in section and "rpg_notebook" in section and "不需要資料庫遷移" in section


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
    assert len(actual) == 19, "沒有掃到預期數量的路由，比對可能是空洞的"
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


def test_readme_privacy_lifecycle_endpoint_tables_list_exactly_the_registered_routes():
    """個資生命週期的端點表格必須與實際註冊的 `/api/account/*` 路由完全一致。"""
    from app import create_app
    from app.config import load_config

    section = readme_section("個資生命週期（C-4c）")
    documented: set[tuple[str, str]] = set()
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and re.fullmatch(r"`/[^`]*`", cells[0]) and re.fullmatch(r"[A-Z]+(、[A-Z]+)*", cells[1]):
            documented.update((cells[0].strip("`"), method) for method in cells[1].split("、"))

    app = create_app(load_config(environ={"APP_ENV": "testing"}))
    actual: set[tuple[str, str]] = set()
    for rule in app.url_map.iter_rules():
        if rule.rule.startswith("/api/account/"):
            actual.update((rule.rule, method) for method in rule.methods - {"HEAD", "OPTIONS"})
    assert len(actual) == 8, "沒有掃到預期數量的路由，比對可能是空洞的"       # story-preference 新增 GET 與 PUT /api/account/story-preference（6 → 8）
    assert documented == actual, f"README 多寫：{sorted(documented - actual)}；漏寫：{sorted(actual - documented)}"


def test_readme_privacy_lifecycle_section_documents_the_commands_the_periods_and_the_limits():
    from app.cli import RETENTION_LABELS
    from app.services import data_retention_service as retention

    section = readme_section("個資生命週期（C-4c）")
    for command in ("execute-account-deletions", "purge-expired-data", "--dry-run"):
        assert command in section, command
    for phrase in ("365 天", "14 天", "7 天", "730 天", "account_deletion_pending", "grace_expired", "admin_delete_forbidden",
                   "受限登入狀態預設拒絕", "單一抹除出口", "已知限制", "C-4d", "recreate=\"never\"", "`0008`"):
        assert phrase in section, phrase
    assert (retention.CONVERSATION_RETENTION.days, retention.LEDGER_RETENTION.days, retention.ERROR_DETAIL_RETENTION.days,
            retention.AUTH_TOKEN_RETENTION.days, retention.ADMIN_AUDIT_RETENTION.days, retention.LOCK_STRIKE_RETENTION.days) == (365, 365, 14, 7, 730, 7)
    assert set(RETENTION_LABELS) == {
        "conversations", "error_detail", "ledger", "auth_tokens", "admin_audit", "lock_strikes", "turn_content", "sessions", "script_submissions",
    }
    for path in ("app/static/js/auth/account-lifecycle.js", "tests/js/account-lifecycle.test.js", "tests/test_restricted_session.py",
                 "tests/test_privacy_lifecycle_flow.py", "tests/architecture/test_account_erasure_coverage.py"):
        assert (PROJECT_ROOT / path).is_file(), path


def test_readme_alerts_section_matches_the_real_rules_constants_codes_and_events():
    """營運告警章節的告警代碼、門檻與事件必須與程式一致（改了常數卻忘了改文件會失敗）。"""
    from app.models.alert_state import ALERT_KEYS
    from app.observability import alert_events
    from app.services import alert_rules as rules

    section = readme_section("營運告警（C-4d）")
    for key in ALERT_KEYS:
        assert f"`{key}`" in section, f"README 沒有記載告警代碼：{key}"
    for event in (alert_events.EVENT_FIRING, alert_events.EVENT_RESOLVED, alert_events.EVENT_DELIVERY_FAILED, alert_events.EVENT_CHECK_FAILED):
        assert f"`{event}`" in section, event
    for phrase in ("flask --app wsgi check-alerts", "--dry-run", "ALERT_EMAIL_TO", "每 5 分鐘", "非零代碼", "6 小時", "搶佔", "alert_states",
                   "`0009`", "已知限制", "超過 10%", "5 個", "樣本不足就維持原狀", "5xx", "不再是通知來源", "沒有 webhook"):
        assert phrase in section, phrase
    # 門檻：文件寫的數字就是程式裡的常數
    assert int(rules.ERROR_RATE_THRESHOLD * 100) == 10 and rules.LATENCY_P95_THRESHOLD_MS == 10_000 and rules.LOCKOUT_THRESHOLD == 5
    assert rules.REMINDER_INTERVAL.total_seconds() == 6 * 3600 and rules.ERROR_WINDOW.total_seconds() == 5 * 60 and rules.LATENCY_WINDOW.total_seconds() == 15 * 60
    assert (rules.ERROR_MIN_SAMPLE, rules.LATENCY_MIN_SAMPLE) == (10, 20) and rules.USAGE_WARNING_RATIO * 100 == 80
    for sample in ("| 10 次 |", "| 20 次 |", "近 5 分鐘", "近 15 分鐘", "80%"):
        assert sample in section, sample
    for path in ("app/services/alert_rules.py", "app/services/alert_service.py", "tests/test_alert_service.py", "tests/test_alerting_flow.py"):
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


def test_readme_rpg_entry_section_lists_exactly_the_three_game_pages_and_documents_the_privacy_and_timeout_rules():
    """RPG 入口一節的頁面表必須與實際註冊的 `/game` 一族路由完全一致（共 3 條）；創角內容不落地、逾時與中止、預設頁面與未驗證項目都要記載。"""
    from app import create_app
    from app.config import load_config

    section = readme_section("RPG 入口與開存檔畫面（rpg-entry-ui）")
    documented: set[str] = set()
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and re.fullmatch(r"`/game[^`]*`", cells[0]):
            documented.add(cells[0].strip("`"))

    app = create_app(load_config(environ={"APP_ENV": "testing"}))
    actual = {rule.rule for rule in app.url_map.iter_rules() if rule.rule.startswith("/game")}
    assert len(actual) == 3, "沒有掃到預期數量的路由，比對可能是空洞的"
    assert documented == actual, f"README 多寫：{sorted(documented - actual)}；漏寫：{sorted(actual - documented)}"
    for needed in ("不寫入任何瀏覽器儲存", "pagehide", "AbortController", "DEFAULT_AFTER_LOGIN", "沒有資料庫遷移", "未驗證"):
        assert needed in section, needed


def test_readme_rpg_game_screen_section_names_the_real_files_the_numbers_and_what_is_unverified():
    """rpg-game-ui 一節：記載的前端檔案都存在、串流逾時與行動上限等數值與程式一致、側欄預設收合且不記住，驗收紀錄如實列出沒有驗證的項目。"""
    from app.controllers import game_page_controller
    from app.services import game_service

    section = readme_section("RPG 遊戲主畫面（rpg-game-ui）")
    js_dir = PROJECT_ROOT / "app" / "static" / "js" / "game"
    for name in ("sse-parser", "turn-stream", "game-state", "game-labels", "game-store", "game-sync", "game-api", "game-wallet", "game-notebook", "game-session"):
        assert (js_dir / f"{name}.js").is_file(), name
        assert name in section, name
    assert (js_dir / "game.js").is_file() and "`app/static/js/game/game.js`" in section
    assert (PROJECT_ROOT / "app" / "templates" / "game" / "session.html").is_file() and "session.html" in section
    for test_file in ("tests/js/game-session.test.js", "tests/js/game-wallet.test.js", "tests/js/game-notebook.test.js", "tests/js/game.test.js", "tests/js/game-template.test.js",
                      "tests/js/entry.test.js", "tests/test_game_screen_flow.py"):
        assert (PROJECT_ROOT / test_file).is_file(), test_file
    assert str(game_page_controller.STREAM_IDLE_TIMEOUT_MS) in section and str(game_page_controller.STREAM_TOTAL_TIMEOUT_MS) in section
    assert f"目前 {game_service.ACTION_MAX}" in section
    for needed in ("不改任何後端端點", "沒有資料庫遷移", "預設收合", "不記住", "since_seq", "從不自動重送", "不阻止送出", "`done` 是完整接收的唯一依據", "IndexedDB",
                   "未驗證", "任務 6.1", "任務 6.2", "任務 6.3", "pagehide", "AbortController", "992px", "不寫 `localStorage`",
                   "記事簿面板", "只在玩家開啟面板時才讀", "已滿＝長度大於容量", "從不自動儲存", "保留玩家的草稿", "預設分頁", "我的存檔"):
        assert needed in section, needed
    assert "全部至少一個測試失敗" in section                                           # 快速破壞的結果如實記載
    assert "驗收紀錄（rpg-game-ui 第 4 批" in section and "與任務清單或規格不同之處" in section        # 第 4 批的偏離如實記載
    assert "等價改動" in section and "長度大於容量" in section and "既有案例無須修改即通過" in section
    assert "驗收紀錄（rpg-game-ui 第 5 批" in section and "十項都至少一個測試失敗" in section             # 任務 6.2 的正式破壞驗證如實記載
    assert "9989 項通過" in section and "922 項通過" in section and "驗收紀錄（rpg-game-ui 任務 6.1" in section   # 任務 6.3 的筆數；6.1 的結果另有一節
    # 登入後的入口一節不能再說存檔頁是空殼而沒有指向取代它的這一節
    entry = readme_section("RPG 入口與開存檔畫面（rpg-entry-ui）")
    assert "RPG 遊戲主畫面（rpg-game-ui）" in entry
