"""階梯式加壓的 Locust 入口：沿用 `locustfile.py` 的情境與位址守門，再掛上階梯形狀（見 `loadtest/shapes.py`）。

    set LOADTEST_STAIRS=start=20,step=20,steps=5,seconds=120
    locust -f loadtest/locustfile_stairs.py --headless --host http://127.0.0.1:5000 --csv loadtest/result --csv-full-history

階梯設定不合法時以非零碼結束並說明欄位規則（不回顯值）。階梯結束後 Locust 自行停止，不需要 `-t`。
Locust 只在 requirements-dev.txt，不進映像。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from locust import LoadTestShape  # noqa: E402

from loadtest.locustfile import MemberUser  # noqa: E402,F401  (Locust 從這個檔案的命名空間找出使用者類別)
from loadtest.shapes import StairConfigError, stairs_from_environment  # noqa: E402

try:
    STAIRS = stairs_from_environment(os.environ)
except StairConfigError as error:
    raise SystemExit(f"階梯設定不合法：{error}") from None


class StairStepShape(LoadTestShape):
    def tick(self):
        return STAIRS.tick(self.get_run_time())
