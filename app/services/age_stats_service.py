"""年齡區間的彙總統計（見 age-confirmation 規格「生日不對外且只提供彙總統計」）。

用於「定期察看用戶群」：只有各年齡區間的人數與尚未完成年齡確認的人數，**沒有任何個別使用者的資料**
（沒有生日、email 或編號）。區間的順序與標籤來自 `AgePolicy`。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.repositories.user_repository import UserRepository
from app.services.age_policy import DEFAULT_AGE_POLICY, AgePolicy


@dataclass(frozen=True)
class AgeStats:
    by_range: list[tuple[str, int]]     # （區間標籤, 人數），依政策的順序，沒有人的區間也列出（人數 0）
    not_completed: int                  # 尚未完成年齡確認的人數
    total: int


class AgeStatsService:
    def __init__(self, *, users: UserRepository, age_policy: AgePolicy = DEFAULT_AGE_POLICY) -> None:
        self._users = users
        self._policy = age_policy

    def summary(self) -> AgeStats:
        counts = dict(self._users.count_by_age_range())
        not_completed = counts.pop(None, 0)
        rows = [(r.label, counts.pop(r.code, 0)) for r in self._policy.ranges]
        rows += [(f"（舊的區間代碼 {code}）", number) for code, number in sorted(counts.items())]     # 政策改版後留下的代碼
        return AgeStats(by_range=rows, not_completed=not_completed, total=sum(n for _, n in rows) + not_completed)
