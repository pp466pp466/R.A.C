"""把前端的 Node 單元測試納入 pytest：有安裝 Node 才執行，沒有就略過（Node 只是可選的測試工具）。"""
import shutil
import subprocess

import pytest

from app.config import PROJECT_ROOT

NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="未安裝 Node.js，略過前端單元測試")
def test_frontend_unit_tests_pass():
    result = subprocess.run(
        [NODE, "--test", "tests/js/**/*.test.js"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-1000:]
    assert "fail 0" in result.stdout
