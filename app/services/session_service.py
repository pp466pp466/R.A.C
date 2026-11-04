"""RPG 存檔的建立與回合的開始（見 rpg-sessions 規格、AGENTS.md 第 7、10 節、rpg-data-and-script 的 design D3、D4、D11）。

開存檔的順序（每一步失敗就停下，不留下半個存檔）：
1. 先過 `AccessPolicy` 的年齡閘門（帳號啟用、已完成年齡確認、年齡不低於最低使用年齡；管理員與各會員等級不豁免）；
2. 驗證主角姓名與角色年齡（劇中角色的年齡，與使用者的年齡無關）；
3. 找來源版本：上架中的任何版本，或呼叫者自己的任何版本；別人的非上架版本與不存在的一樣是 404；
4. 劇本宣告的內容模式不得高於玩家目前**生效**的模式（玩家偏好與年齡上限中較保守者），否則 `ContentTierRestricted`（403）；
5. 把版本標準化後的完整內容複製進存檔，存檔的內容模式取玩家生效模式與劇本宣告兩者中較保守者。之後存檔不再讀取劇本。

存檔與回合的內容是個資：這裡不記錄任何日誌。
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from app.models.rpg_session import RpgSession
from app.repositories.errors import SessionEndedError, TurnInProgressError
from app.repositories.script_repository import ScriptRepository
from app.repositories.session_repository import SessionRepository, TurnStart
from app.services.access_policy import AccessPolicy
from app.services.content_tier import most_conservative, tier_rank
from app.services.current_user import CurrentUser
from app.services.errors import Conflict, ContentTierRestricted, NotFound, SessionEnded, ValidationError
from app.services.script_spec import normalize_single_line

PLAYER_NAME_MAX = 20
CHARACTER_AGE_MAX = 9999
CLIENT_MSG_ID_MAX = 64            # 與 memory_logs.client_msg_id 的欄位長度一致

TURN_IN_PROGRESS_MESSAGE = "這個回合仍在處理中，請稍候。"


class SessionService:
    def __init__(
        self, *, scripts: ScriptRepository, sessions: SessionRepository, policy: AccessPolicy, clock: Callable[[], datetime]
    ) -> None:
        self._scripts = scripts
        self._sessions = sessions
        self._policy = policy
        self._clock = clock

    def start_session(
        self, current: CurrentUser, version_public_id: str, *, player_name: object, character_age: object
    ) -> RpgSession:
        """用一個劇本版本開新存檔，回傳存檔。"""
        self._policy.require_chat_access(current)
        name, age = _validate_protagonist(player_name, character_age)
        version = self._scripts.get_startable(current.id, version_public_id) if isinstance(version_public_id, str) else None
        if version is None:
            raise NotFound()
        effective = self._policy.effective_tier(current)
        if tier_rank(version.content_tier) > tier_rank(effective):
            raise ContentTierRestricted()
        return self._sessions.create(
            current.id, source_version_id=version.id, snapshot=self._scripts.content_of(version),
            snapshot_spec_version=version.spec_version, player_name=name, character_age=age,
            content_tier=most_conservative(effective, version.content_tier), now=self._clock(),
        )

    def begin_turn(self, current: CurrentUser, session_public_id: str, *, client_msg_id: object, user_action: object) -> TurnStart:
        """開始一個回合（冪等；細節見 `SessionRepository.start_turn`），把資料層的錯誤轉成業務錯誤：
        存檔已完結 → `SessionEnded`（409，`session_ended`）；同一編號仍在生成中 → 衝突（409，`turn_in_progress`）。"""
        self._policy.require_chat_access(current)
        if not isinstance(client_msg_id, str) or not 1 <= len(client_msg_id) <= CLIENT_MSG_ID_MAX:
            raise ValidationError(fields={"client_msg_id": f"必須是 1 至 {CLIENT_MSG_ID_MAX} 字的文字。"})
        if not isinstance(user_action, str):
            raise ValidationError(fields={"user_action": "必須是文字。"})
        try:
            started = self._sessions.start_turn(
                current.id, session_public_id, client_msg_id=client_msg_id, user_action=user_action, now=self._clock()
            )
        except SessionEndedError as exc:
            raise SessionEnded() from exc
        except TurnInProgressError as exc:
            raise Conflict(TURN_IN_PROGRESS_MESSAGE, code="turn_in_progress") from exc
        if started is None:
            raise NotFound()
        return started


def _validate_protagonist(player_name: object, character_age: object) -> tuple[str, int]:
    errors: dict[str, str] = {}
    name = ""
    if not isinstance(player_name, str) or not (name := normalize_single_line(player_name)):
        errors["player_name"] = "請填寫主角的姓名。"
    elif len(name) > PLAYER_NAME_MAX:
        errors["player_name"] = f"最多 {PLAYER_NAME_MAX} 字。"
    age = 0
    if isinstance(character_age, bool) or not isinstance(character_age, int):
        errors["character_age"] = "主角的年齡必須是整數。"
    elif not 0 <= character_age <= CHARACTER_AGE_MAX:
        errors["character_age"] = f"必須介於 0 與 {CHARACTER_AGE_MAX} 之間。"
    else:
        age = character_age
    if errors:
        raise ValidationError(fields=errors)
    return name, age
