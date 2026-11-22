"""後台前端的中文標籤必須涵蓋後端所有的列舉值：後端新增動作或狀態卻忘了加標籤，這裡就會失敗（否則畫面會直接顯示英文代碼）。

標籤表在 `app/static/js/admin/admin-format.js`；有安裝 Node 才執行（與 test_js_unit_tests.py 相同）。
"""
import json
import shutil
import subprocess

import pytest

from app.config import PROJECT_ROOT
from app.models.admin_audit_log import AUDIT_ACTIONS
from app.models.chat import MSG_STATUS_FALLBACK, MSG_STATUS_OK
from app.models.llm_call_log import CALL_KINDS
from app.models.lock_strike import STRIKE_KINDS
from app.models.script import DECISION_CATEGORIES, ENDING_MODES, REMOVERS, REVIEW_VERDICTS, VERSION_STATUSES
from app.models.script_report import CLOSED_RESULTS, REPORT_REASONS
from app.models.user import AUDIENCES, MEMBER_TIERS, USER_STATUSES
from app.models.wallet import BUCKETS
from app.services.script_spec import TRIGGER_KINDS

NODE = shutil.which("node")
FORMAT_JS = "app/static/js/admin/admin-format.js"


@pytest.fixture(scope="module")
def labels() -> dict[str, dict[str, str]]:
    result = subprocess.run(
        [NODE, "-e", "process.stdout.write(JSON.stringify(require(process.argv[1]).LABELS))", str(PROJECT_ROOT / FORMAT_JS)],
        cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr[-1000:]
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="未安裝 Node.js，略過前端標籤檢查")
@pytest.mark.parametrize(
    "group, expected",
    [
        ("status", USER_STATUSES),
        ("tier", MEMBER_TIERS),
        ("action", AUDIT_ACTIONS),
        ("lockReason", STRIKE_KINDS),
        ("kind", CALL_KINDS),
        ("messageStatus", (MSG_STATUS_OK, MSG_STATUS_FALLBACK)),
        ("role", ("user", "admin")),
        ("messageRole", ("user", "assistant")),
        ("bucket", BUCKETS),                      # game-wallet：遊戲幣的兩個餘額
        # rpg-script-publishing：劇本審核分頁（版本狀態、下架者、人工決定的類別、模型層結果、讀者頻道、結局模式、里程碑觸發種類）
        ("scriptStatus", VERSION_STATUSES),
        ("remover", REMOVERS),
        ("category", DECISION_CATEGORIES),
        ("verdict", REVIEW_VERDICTS),
        ("audience", AUDIENCES),
        ("endingMode", ENDING_MODES),
        ("triggerKind", TRIGGER_KINDS),
        # rpg-script-reports：被檢舉檢視的理由（玩家勾選的七個）與結案結果
        ("reportReason", REPORT_REASONS),
        ("reportResult", CLOSED_RESULTS),
    ],
)
def test_the_frontend_labels_cover_exactly_the_backend_values(labels, group, expected):
    assert sorted(labels[group]) == sorted(expected)
    assert all(text.strip() for text in labels[group].values())


@pytest.mark.skipif(NODE is None, reason="未安裝 Node.js，略過前端標籤檢查")
def test_the_category_dropdown_follows_the_backend_order_with_the_manual_only_category_last(labels):
    """退回與下架的類別下拉依後端列舉的順序（先四項紅線、再四項疑慮、最後是人工專用的 other_policy）。"""
    assert list(labels["category"]) == list(DECISION_CATEGORIES)
    assert list(labels["category"])[-1] == "other_policy"
