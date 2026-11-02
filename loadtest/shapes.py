"""階梯式加壓（load-testing 規格「階梯式加壓與留存每一輪的設定與結果」）：逐步增加虛擬使用者數、每階段持續固定時間，
用來找出拐點——哪一階段開始 p95 或錯誤率惡化。

這個模組只有純邏輯，**不匯入 Locust**（匯入 Locust 會對標準函式庫做 gevent 猴子補丁，不能發生在 pytest 行程裡）。
Locust 端的薄包裝在 `loadtest/locustfile_stairs.py`：

    set LOADTEST_STAIRS=start=20,step=20,steps=5,seconds=120
    locust -f loadtest/locustfile_stairs.py --headless --host http://127.0.0.1:5000 --csv loadtest/result

`LOADTEST_STAIRS` 的四個欄位：`start`（第一階段的使用者數）、`step`（每階段增加的使用者數）、`steps`（階段數，至少 2）、
`seconds`（每階段的秒數，至少 10）。沒有設定時用預設值（20、20、5、120，共 10 分鐘、最多 100 位）。
每個階段開頭的前幾秒用來把新增的使用者逐步加進來（不一次湧入），其餘時間維持該階段的使用者數。
結束後（`tick` 回傳 `None`）Locust 自行停止，所以不需要另外設定 `-t`。
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_SPEC = "start=20,step=20,steps=5,seconds=120"
FIELDS = ("start", "step", "steps", "seconds")
MIN_STEPS = 2
MIN_SECONDS = 10
MAX_PEAK_USERS = 5000
MAX_RAMP_SECONDS = 10.0


class StairConfigError(ValueError):
    """階梯設定不合法。訊息只含欄位名稱與規則。"""


@dataclass(frozen=True)
class Stage:
    index: int              # 從 1 起算
    users: int              # 這個階段維持的虛擬使用者數
    seconds: float          # 這個階段的持續時間
    spawn_rate: float       # 階段開頭把新增的使用者加進來的速率（人／秒）


class StairShape:
    def __init__(self, *, start_users: int, step_users: int, steps: int, step_seconds: float) -> None:
        if start_users < 1:
            raise StairConfigError("start 必須是正整數")
        if step_users < 1:
            raise StairConfigError("step 必須是正整數")
        if steps < MIN_STEPS:
            raise StairConfigError(f"steps 至少要有 {MIN_STEPS} 個階段，否則不算階梯")
        if step_seconds < MIN_SECONDS:
            raise StairConfigError(f"seconds 每階段至少 {MIN_SECONDS} 秒")
        if start_users + step_users * (steps - 1) > MAX_PEAK_USERS:
            raise StairConfigError(f"最高的階段不得超過 {MAX_PEAK_USERS} 位虛擬使用者")
        self.start_users, self.step_users, self.steps, self.step_seconds = start_users, step_users, steps, float(step_seconds)

    @property
    def stages(self) -> tuple[Stage, ...]:
        ramp = min(MAX_RAMP_SECONDS, self.step_seconds / 2)
        result = []
        previous = 0
        for i in range(self.steps):
            users = self.start_users + self.step_users * i
            result.append(Stage(i + 1, users, self.step_seconds, max(1.0, (users - previous) / ramp)))
            previous = users
        return tuple(result)

    @property
    def total_seconds(self) -> float:
        return self.step_seconds * self.steps

    @property
    def peak_users(self) -> int:
        return self.start_users + self.step_users * (self.steps - 1)

    def tick(self, elapsed: float) -> tuple[int, float] | None:
        """Locust 的 `LoadTestShape.tick` 需要的值：這個時間點的（使用者數，加入速率）；結束後回傳 `None`。"""
        if elapsed < 0 or not math.isfinite(elapsed):
            raise ValueError("elapsed 必須是不小於 0 的有限數字")
        index = int(elapsed // self.step_seconds)
        if index >= self.steps:
            return None
        stage = self.stages[index]
        return stage.users, stage.spawn_rate

    def describe(self) -> str:
        """一行摘要（給每一輪的記錄用）：只有數字。"""
        users = "、".join(str(stage.users) for stage in self.stages)
        return f"{self.steps} 個階段（虛擬使用者 {users}），每階段 {self.step_seconds:g} 秒，共 {self.total_seconds:g} 秒"


def parse_stairs(spec: str | None) -> StairShape:
    """`start=20,step=20,steps=5,seconds=120` → `StairShape`；沒有提供的欄位用預設值。不合法就拋出 `StairConfigError`（不回顯值）。"""
    values: dict[str, str] = dict(item.split("=", 1) for item in DEFAULT_SPEC.split(","))
    for item in (spec or "").split(","):
        item = item.strip()
        if not item:
            continue
        name, separator, raw = item.partition("=")
        name = name.strip()
        if not separator or name not in FIELDS:
            raise StairConfigError(f"LOADTEST_STAIRS 的欄位必須是 {'、'.join(FIELDS)}，格式為 名稱=數字")
        values[name] = raw.strip()

    def number(name: str, *, integer: bool) -> int | float:
        try:
            value = int(values[name]) if integer else float(values[name])
        except ValueError:
            raise StairConfigError(f"{name} 必須是{'整數' if integer else '數字'}") from None
        if not math.isfinite(value):
            raise StairConfigError(f"{name} 必須是有限的數字")
        return value

    return StairShape(
        start_users=int(number("start", integer=True)), step_users=int(number("step", integer=True)),
        steps=int(number("steps", integer=True)), step_seconds=float(number("seconds", integer=False)),
    )


def stairs_from_environment(environ: Mapping[str, str]) -> StairShape:
    return parse_stairs(environ.get("LOADTEST_STAIRS"))
