"""launch-gates 規格：營運手冊引用的指令都真實存在、上線前關卡齊全且有負責人與狀態、未滿足的規範項目明列、還原與輪替的順序正確。

也對照 report.md 4.2 的上線前關卡與路線圖，確認沒有關卡漏列。
"""
import re

import pytest

from app import create_app
from app.config import PROJECT_ROOT, load_config
from tests.support.scripts import load_script

plan = load_script("launch_plan")

OPS = (PROJECT_ROOT / "docs" / "營運與部署-operations.md").read_text(encoding="utf-8-sig")
REPORT = (PROJECT_ROOT / "docs" / "report.md").read_text(encoding="utf-8-sig")
ROADMAP = (PROJECT_ROOT / "docs" / "路線圖-roadmap.md").read_text(encoding="utf-8-sig")

STATUSES = {"未開始", "進行中", "已通過"}
GATE_KEYWORDS = ("寄信設定", "模型供應商方案", "網域", "法務", "資料庫的成本", "負載測試", "IP 限流", "成年驗證")


def section(text: str, heading: str) -> str:
    """從 `heading` 那一行到下一個同級或更高級標題之前。"""
    lines = text.split("\n")
    start = next(i for i, line in enumerate(lines) if line.strip() == heading)
    level = len(heading) - len(heading.lstrip("#"))
    end = len(lines)
    for i in range(start + 1, len(lines)):
        match = re.match(r"^(#{1,6}) ", lines[i])
        if match and len(match.group(1)) <= level:
            end = i
            break
    return "\n".join(lines[start:end])


def table_rows(text: str) -> list[list[str]]:
    rows = []
    for line in text.split("\n"):
        if line.startswith("|"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if not all(set(cell) <= {"-", ":", " "} for cell in cells) and cells[0] not in ("關卡", "項目", "事故", "機密"):
                rows.append(cells)
    return rows


# ---- 引用的指令與腳本真實存在 -----------------------------------------------------------


@pytest.fixture(scope="module")
def registered_commands() -> set[str]:
    return set(create_app(load_config(environ={"APP_ENV": "testing"})).cli.commands)


def test_every_flask_command_the_manual_mentions_exists(registered_commands):
    mentioned = set(re.findall(r"flask --app wsgi ([a-z][a-z-]+)", OPS))
    assert len(mentioned) >= 8, "沒有讀到預期數量的指令，檢查可能是空洞的"
    assert mentioned <= registered_commands, f"手冊提到不存在的指令：{sorted(mentioned - registered_commands)}"


def test_every_script_the_manual_mentions_exists_and_its_subcommands_are_real():
    scripts = set(re.findall(r"python ((?:scripts|loadtest)/[\w]+\.py)", OPS))
    assert {"scripts/gcp_launch.py", "scripts/restore_drill.py", "loadtest/evaluate.py"} <= scripts
    for script in scripts:
        assert (PROJECT_ROOT / script).is_file(), f"手冊提到不存在的檔案：{script}"
    for sub in re.findall(r"python scripts/gcp_launch\.py ([a-z][\w-]*)", OPS):
        assert sub in ("plan", *plan.GROUPS), sub
    restore = load_script("restore_drill")
    help_text = restore.build_parser().format_help()
    for sub in set(re.findall(r"python scripts/restore_drill\.py ([a-z][\w-]*)", OPS)):
        assert sub in help_text, sub
    assert (PROJECT_ROOT / "loadtest" / "locustfile.py").is_file() and "locust -f loadtest/locustfile.py" in OPS


def test_every_gcp_launch_option_the_manual_uses_exists():
    parser_help = load_script("gcp_launch").build_parser().format_help()
    used = set(re.findall(r"python scripts/gcp_launch\.py[^`\n]*?(--[a-z][\w-]*)", OPS))
    used |= set(re.findall(r"(--rotate|--with-secret|--execute|--notify-email|--vars-file)", OPS))
    for option in used:
        assert option in parser_help, option


# ---- 上線前關卡 -------------------------------------------------------------------------


def validate_gates(rows: list[list[str]]) -> list[str]:
    """回傳違規描述；空清單代表關卡表合格。獨立成函式，下面的測試也用它證明它真的會抓到問題。"""
    problems = []
    for keyword in GATE_KEYWORDS:
        if not any(keyword in row[0] for row in rows):
            problems.append(f"缺少關卡：{keyword}")
    for row in rows:
        gate, owner, status, evidence = (row + ["", "", "", ""])[:4]
        if not owner:
            problems.append(f"沒有負責人：{gate[:20]}")
        if status not in STATUSES:
            problems.append(f"狀態不合法（{status}）：{gate[:20]}")
        if status == "已通過" and not re.search(r"\d{4}-\d{2}-\d{2}", evidence):
            problems.append(f"標為已通過卻沒有依據（日期與驗收紀錄）：{gate[:20]}")
    return problems


def gate_rows() -> list[list[str]]:
    return table_rows(section(OPS, "### 上線前關卡"))


def test_the_gate_table_has_all_eight_gates_each_with_an_owner_and_a_valid_status():
    rows = gate_rows()
    assert len(rows) == 8
    assert validate_gates(rows) == []


def test_no_gate_is_marked_passed_without_evidence_today():
    assert not [row for row in gate_rows() if row[2] == "已通過" and not re.search(r"\d{4}-\d{2}-\d{2}", row[3])]


def test_the_validator_would_catch_a_missing_gate_a_missing_owner_a_bad_status_and_unsupported_pass():
    good = gate_rows()
    assert validate_gates(good[:-1]) != []
    assert validate_gates([[good[0][0], "", good[0][2], good[0][3]], *good[1:]]) != []
    assert validate_gates([[good[0][0], good[0][1], "完成", good[0][3]], *good[1:]]) != []
    assert validate_gates([[good[0][0], good[0][1], "已通過", "口頭確認"], *good[1:]]) != []
    assert validate_gates([[good[0][0], good[0][1], "已通過", "2026-11-01 驗收紀錄見 README"], *good[1:]]) == []


def test_the_ip_rate_limit_gate_records_the_users_decision_to_postpone_it():
    row = next(r for r in gate_rows() if "IP 限流" in r[0])
    assert "先不做" in row[3] and "重新決定" in row[3]


def test_the_gates_cover_every_gate_in_the_report_and_the_roadmap_lists_what_the_user_must_provide():
    gates = REPORT.split("**上線前關卡**", 1)[1].split("### 4.3", 1)[0]
    report_items = re.findall(r"^\d+\. (.+)$", gates, re.M)
    assert len(report_items) == 6, "report.md 的上線前關卡數量變了：請同步營運文件的關卡表與路線圖"
    table_text = " ".join(" ".join(row) for row in gate_rows())
    for keyword in ("寄信設定", "方案", "網域", "法務", "成本", "負載測試"):
        assert any(keyword in item for item in report_items), keyword
        assert keyword in table_text, f"營運文件的關卡表漏了：{keyword}"
    rows = {
        line.split("|")[1].strip(): line for line in ROADMAP.split("\n")
        if line.startswith(("| staging-deployment", "| cloud-staging-run", "| production-launch"))
    }
    local, cloud, launch = rows["staging-deployment（C-5b）"], rows["cloud-staging-run（C-5c）"], rows["production-launch（C-5d）"]
    for keyword in ("Brevo", "ngrok", "不花錢", "尚未做"):
        assert keyword in local, f"路線圖的 C-5b 沒有提到：{keyword}"
    for keyword in ("可計費的 Google Cloud 專案", "Cloud SQL", "壓測", "還原演練"):
        assert keyword in cloud, f"路線圖的 C-5c 沒有提到：{keyword}"
    for keyword in ("網域", "法務", "供應商方案", "IP 限流"):
        assert keyword in launch, f"路線圖的 C-5d 沒有提到：{keyword}"


# ---- 未滿足的規範項目 -------------------------------------------------------------------


def test_unmet_items_name_the_reason_the_risk_and_when_to_revisit_each():
    rows = table_rows(section(OPS, "### 未滿足的規範項目"))
    assert len(rows) >= 4 and all(len(row) == 5 and all(row) for row in rows)
    by_name = {row[0]: row for row in rows}
    ip = next(row for name, row in by_name.items() if "IP 限流" in name)
    assert "使用者決定先不做" in ip[2] and "第一道防線" in ip[3] and "上線前" in ip[4]
    assert any("內容安全政策" in name for name in by_name) and any("獨立 worker 池" in name for name in by_name)
    assert any("平台告警" in name for name in by_name)


# ---- 還原順序與機密輪替 -----------------------------------------------------------------


def test_the_restore_steps_run_in_the_right_order():
    text = section(OPS, "### 資料庫還原與重新抹除（C-5a）")
    sequence = text.split("```mermaid", 1)[1].split("```", 1)[0]
    phrases = ["先關閉流量", "還原資料庫", "結構檢查", "取出 account.deleted_by_request", "replay-deletions --dry-run", "實際抹除", "驗證", "開放流量"]
    positions = [sequence.index(phrase) for phrase in phrases]
    assert positions == sorted(positions), "還原順序必須是：關閉流量、還原、結構檢查、取出日誌、預覽、實際抹除、驗證、開放流量"
    assert "還原之後、對外開放流量之前" in text


def test_the_rotation_covers_every_secret_both_model_keys_and_the_fixed_step_order():
    text = section(OPS, "### 機密輪替")
    assert "90 天" in text and "立即" in text
    for var in plan.SECRET_VARS:
        assert var in text, f"輪替表沒有涵蓋機密：{var}"
    assert "GEMINI_API_KEY" in text and "OPENROUTER_API_KEY" in text
    order = ["建立新金鑰", "更新機密", "重新部署", "驗證", "停用舊金鑰"]
    chain = " → ".join(order)
    assert chain in text, "輪替步驟的固定順序"
    assert "SECRET_KEY_FALLBACKS" in text and "先" in text.split("SECRET_KEY_FALLBACKS", 1)[0][-40:]


def test_the_incident_section_covers_the_five_incidents_with_actions_that_exist():
    rows = table_rows(section(OPS, "### 事故處理"))
    kinds = " ".join(row[0] for row in rows)
    for kind in ("金鑰外洩", "模型供應商故障", "資料庫不通", "被攻擊", "資料毀損"):
        assert kind in kinds, kind
    assert all(len(row) == 4 and all(row) for row in rows)
    leak = next(row for row in rows if "金鑰外洩" in row[0])
    assert "立即輪替" in leak[2]


# ---- 本機預演與預備環境驗收（staging-deployment）----------------------------------------------------------


def test_the_local_rehearsal_section_lists_the_steps_and_splits_verified_from_cloud_only_items():
    text = section(OPS, "### 本機預演（C-5b）")
    for step in ("alembic upgrade head", "create-admin", "ngrok http", "staging_smoke.py", "check-alerts", "unset SECRET_KEY"):
        assert step in text, f"本機預演缺少步驟：{step}"
    assert "不放寬任何 staging 的檢查" in text and "不需要" in text and "TRUST_PROXY_HEADERS" in text
    assert "本機預演可以驗證" in text and "必須在雲端驗證" in text and "尚未做" in text
    for cloud_only in ("私有連線", "Secret Manager", "Cloud Scheduler", "平台告警", "壓測容量", "還原演練"):
        assert cloud_only in text, f"必須在雲端驗證的項目沒有列出：{cloud_only}"
    assert "不因本機預演而通過" in text and "不以 Flask 開發伺服器的數字代替" in text


def test_the_staging_verification_section_documents_every_check_the_guard_and_the_record_rules():
    text = section(OPS, "### 預備環境驗收（C-5b）")
    for fragment in ("/healthz", "/readyz", "Strict-Transport-Security", "Content-Security-Policy", "__Host-", "--allow-model-call", "404", "401"):
        assert fragment in text, f"預備環境驗收缺少：{fragment}"
    assert "預設略過" in text and "沒有任何確認參數可以繞過" in text and "https" in text
    assert ".invalid" in text and "不放真實個資" in text and "人工驗收" in text and "未驗證" in text
    assert "本機預演已驗證" in text and "必須在雲端驗證（尚未做）" in text
    rows = [row for row in table_rows(text) if row[0].isdigit()]
    assert len(rows) == len(load_script("staging_smoke").CHECKS), "文件的檢查清單與腳本的檢查項目數量不一致"
