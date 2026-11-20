"""創作頁（`/scripts`）的中文標籤必須涵蓋後端所有的列舉值：後端新增狀態或類別卻忘了加標籤，這裡就會失敗（否則畫面會直接顯示英文代碼）。

標籤表在 `app/static/js/scripts/script-draft.js`（狀態、類別）與 `script-status.js`（下架者）；有安裝 Node 才執行（與 test_admin_format_labels.py 相同）。
對應 rpg-script-publishing 的 script-takedown 規格「作者看得到審核結果與下架狀態」。
"""
import json
import shutil
import subprocess

import pytest

from app.config import PROJECT_ROOT
from app.models.script import DECISION_CATEGORIES, REMOVERS, VERSION_STATUSES

NODE = shutil.which("node")
SCRIPT = (
    "const d = require(process.argv[1]); const s = require(process.argv[2]);"
    "process.stdout.write(JSON.stringify({status: d.STATUS_LABELS, category: d.CATEGORY_LABELS, remover: s.REMOVER_LABELS}))"
)


@pytest.fixture(scope="module")
def labels() -> dict[str, dict[str, str]]:
    result = subprocess.run(
        [NODE, "-e", SCRIPT, str(PROJECT_ROOT / "app/static/js/scripts/script-draft.js"), str(PROJECT_ROOT / "app/static/js/scripts/script-status.js")],
        cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr[-1000:]
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="未安裝 Node.js，略過前端標籤檢查")
@pytest.mark.parametrize(
    "group, expected",
    [("status", VERSION_STATUSES), ("category", DECISION_CATEGORIES), ("remover", REMOVERS)],
)
def test_the_workshop_labels_cover_exactly_the_backend_values(labels, group, expected):
    assert sorted(labels[group]) == sorted(expected)
    assert all(text.strip() for text in labels[group].values())


@pytest.mark.skipif(NODE is None, reason="未安裝 Node.js，略過前端標籤檢查")
def test_the_category_labels_agree_with_the_admin_console_so_an_author_and_an_administrator_read_the_same_words(labels):
    admin = subprocess.run(
        [NODE, "-e", "process.stdout.write(JSON.stringify(require(process.argv[1]).LABELS.category))", str(PROJECT_ROOT / "app/static/js/admin/admin-format.js")],
        cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    assert admin.returncode == 0, admin.stderr[-1000:]
    assert json.loads(admin.stdout) == labels["category"]
