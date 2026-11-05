"""可以拿來開存檔的劇本版本（見 rpg-sessions、rpg-protagonist 規格）：開存檔、創角資料與 AI 生成預覽共用同一套可見性與內容模式的判定。

- 可見性：上架中的任何版本，或呼叫者自己的任何版本；別人的非上架版本與不存在的一樣是 404（`NotFound`）。
- 內容模式：劇本宣告的內容模式不得高於玩家目前**生效**的模式（玩家偏好與年齡上限中較保守者），否則 `ContentTierRestricted`（403）。
- `tier` 是存檔將使用的內容模式：玩家生效的模式與劇本宣告兩者中較保守者。

這裡**不做**年齡與帳號閘門（呼叫端先 `require_chat_access`）、也不做審查（審查有成本，由各自的流程決定何時做）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models.script import Script, ScriptVersion
from app.repositories.script_repository import ScriptRepository
from app.services.access_policy import AccessPolicy
from app.services.content_tier import most_conservative, tier_rank
from app.services.current_user import CurrentUser
from app.services.errors import ContentTierRestricted, NotFound


@dataclass(frozen=True)
class Startable:
    version: ScriptVersion
    script: Script
    content: dict[str, Any]          # 標準化後的劇本內容（開存檔時整份複製進存檔）
    tier: str                        # 存檔將使用的內容模式


class StartableVersions:
    def __init__(self, *, scripts: ScriptRepository, policy: AccessPolicy) -> None:
        self._scripts = scripts
        self._policy = policy

    def resolve(self, current: CurrentUser, version_public_id: object) -> Startable:
        version = self._scripts.get_startable(current.id, version_public_id) if isinstance(version_public_id, str) else None
        if version is None:
            raise NotFound()
        effective = self._policy.effective_tier(current)
        if tier_rank(version.content_tier) > tier_rank(effective):
            raise ContentTierRestricted()
        return Startable(
            version=version, script=self._scripts.get_script_of(version), content=self._scripts.content_of(version),
            tier=most_conservative(effective, version.content_tier),
        )
