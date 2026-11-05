"""RPG 存檔的建立與回合的開始（見 rpg-sessions、rpg-protagonist 規格、AGENTS.md 第 7、10 節、rpg-data-and-script 的 design D3、D4、D11、
rpg-character-creation 的 design D6）。

開存檔的順序（每一步失敗就停下，不留下半個存檔；便宜的檢查在前，不會為了不合格的主角浪費審查的模型呼叫）：
1. 先過 `AccessPolicy` 的年齡閘門（帳號啟用、已完成年齡確認、年齡不低於最低使用年齡；管理員與各會員等級不豁免）；
2. 找來源版本：上架中的任何版本，或呼叫者自己的任何版本；別人的非上架版本與不存在的一樣是 404；
3. 劇本宣告的內容模式不得高於玩家目前**生效**的模式（玩家偏好與年齡上限中較保守者），否則 `ContentTierRestricted`（403）；
4. **主角驗證**：角色卡沿用劇本角色規格；錨定事實指向劇本中存在的項目，未成年的主角或對象不得有戀愛或婚姻的聯繫；
   帶錨定事實或明確略過二選一。所有欄位錯誤一次回報（400）；
5. **主角的規則層**：明確違規 -> `ProtagonistRejected`（422，只帶類別）；
6. **劇本審查的把關**（script-review 規格）：非內建劇本的版本，對應「目前內容」的模型層結果必須是通過或有疑慮（沒有結果時先審查）；
   拒絕 -> `ScriptRejected`（422），模型層不可用 -> `ReviewUnavailable`（503，一律擋下、不放行）。內建的種子劇本視為已審查；
7. **主角的模型層審查**：結果為拒絕 -> `ProtagonistRejected`；不可用 -> `ReviewUnavailable`；有疑慮放行（主角只屬於玩家自己的存檔，
   沒有人工審核佇列；絕對禁止項靠拒絕與回合層的審查）；
8. 把版本標準化後的完整內容與主角複製進存檔，存檔的內容模式取玩家生效模式與劇本宣告兩者中較保守者。之後存檔不再讀取劇本。

存檔與回合的內容（含主角）是個資：這裡不記錄任何日誌（事件只有內部編號與列舉）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Protocol

from app.models.rpg_session import RpgSession
from app.models.script import REVIEW_REJECT, ScriptVersion
from app.repositories.errors import SessionEndedError, TurnInProgressError
from app.repositories.script_repository import ScriptRepository
from app.repositories.session_repository import SessionRepository, TurnStart
from app.observability import protagonist_events
from app.services import protagonist as pro
from app.services.access_policy import AccessPolicy
from app.services.current_user import CurrentUser
from app.services.errors import (
    Conflict, NotFound, ProtagonistRejected, ReviewUnavailable, ScriptRejected, SessionEnded, ValidationError,
)
from app.services.script_review_service import ModelReview
from app.services.script_rules import check_protagonist_rules
from app.services.startable import StartableVersions

CLIENT_MSG_ID_MAX = 64            # 與 memory_logs.client_msg_id 的欄位長度一致

TURN_IN_PROGRESS_MESSAGE = "這個回合仍在處理中，請稍候。"


class ReviewGate(Protocol):
    """開存檔前的審查把關（實作見 `ScriptReviewService`；測試可注入替身）。"""

    def ensure_reviewed(self, user_id: int, version: ScriptVersion, content: dict[str, Any]) -> ModelReview: ...


def session_view(save: RpgSession) -> dict[str, Any]:
    """開存檔後回給呼叫者的存檔摘要：對外識別碼、劇本標題、內容模式與主角。沒有內部編號、來源版本與劇本複本的其他內容。"""
    return {
        "id": save.public_id, "title": save.script_snapshot_json.get("title"), "content_tier": save.content_tier,
        "protagonist": save.protagonist_json,
    }


class ProtagonistReviewGate(Protocol):
    """開存檔時主角的模型層審查（實作見 `ProtagonistReviewService`；測試可注入替身）。"""

    def review(self, user_id: int, protagonist: dict[str, Any], script_content: dict[str, Any], tier: str) -> ModelReview: ...


class SessionService:
    def __init__(
        self,
        *,
        scripts: ScriptRepository,
        sessions: SessionRepository,
        policy: AccessPolicy,
        reviews: ReviewGate,
        protagonist_reviews: ProtagonistReviewGate,
        protagonist_marker: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._scripts = scripts
        self._sessions = sessions
        self._policy = policy
        self._reviews = reviews
        self._protagonist_reviews = protagonist_reviews
        self._marker = protagonist_marker
        self._startable = StartableVersions(scripts=scripts, policy=policy)
        self._clock = clock

    def start_session(
        self, current: CurrentUser, version_public_id: object, *, protagonist: object, anchor: object = None, skip_anchor: object = None
    ) -> RpgSession:
        """用一個劇本版本與一張主角角色卡開新存檔，回傳存檔。`anchor` 與 `skip_anchor`（恰為 `True`）二選一。"""
        self._policy.require_chat_access(current)
        found = self._startable.resolve(current, version_public_id)
        markers = [self._marker]
        adult_age = self._policy.adult_age

        character, errors = pro.validate_protagonist(protagonist, forbidden_markers=markers)
        anchor_value, anchor_errors = pro.validate_anchor_choice(
            anchor, skip_anchor, script_content=found.content, protagonist_age=character["age"] if character else None,
            adult_age=adult_age, forbidden_markers=markers,
        )
        errors.update(anchor_errors)
        if errors or character is None:
            raise ValidationError(fields=errors or {"protagonist": "主角資料不合格。"})

        rules = check_protagonist_rules(character, anchor_value["text"] if anchor_value else None, None, tier=found.tier, adult_age=adult_age)
        if rules.verdict == REVIEW_REJECT:
            protagonist_events.log_rejected(user_id=current.id, stage="start", layer="rules", categories=rules.categories)
            raise ProtagonistRejected(rules.categories)

        if found.script.seed_key is None:                                    # 內建的種子劇本視為已審查
            review = self._reviews.ensure_reviewed(current.id, found.version, found.content)
            if review.verdict is None:
                raise ReviewUnavailable()
            if review.verdict == REVIEW_REJECT:
                raise ScriptRejected(review.categories)

        stored = pro.build_protagonist(character, anchor_value)
        model = self._protagonist_reviews.review(current.id, stored, found.content, found.tier)
        if model.verdict is None:
            raise ReviewUnavailable("主角審查目前無法使用，請稍後再試。")
        if model.verdict == REVIEW_REJECT:
            protagonist_events.log_rejected(user_id=current.id, stage="start", layer="model", categories=model.categories)
            raise ProtagonistRejected(model.categories)

        save = self._sessions.create(
            current.id, source_version_id=found.version.id, snapshot=found.content,
            snapshot_spec_version=found.version.spec_version, protagonist=stored, content_tier=found.tier, now=self._clock(),
        )
        protagonist_events.log_session_started(user_id=current.id, tier=found.tier, anchor="anchored" if anchor_value else "skipped")
        return save

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

