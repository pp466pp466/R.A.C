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
from app.models.llm_call_log import KIND_CHAT, KIND_SUMMARY
from app.models.lock_strike import STRIKE_KINDS
from app.models.user import MEMBER_TIERS, USER_STATUSES

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
        ("kind", (KIND_CHAT, KIND_SUMMARY)),
        ("messageStatus", (MSG_STATUS_OK, MSG_STATUS_FALLBACK)),
        ("role", ("user", "admin")),
        ("messageRole", ("user", "assistant")),
    ],
)
def test_the_frontend_labels_cover_exactly_the_backend_values(labels, group, expected):
    assert sorted(labels[group]) == sorted(expected)
    assert all(text.strip() for text in labels[group].values())
