"""創角所需的資料與 AI 生成主角的預覽（見 rpg-protagonist、protagonist-suggestion 規格與 rpg-character-creation 的 design D5、D7）。

**生成預覽是無狀態的**：不建立或修改存檔、劇本或使用者資料；唯一的資料庫寫入是用量帳本（`LlmService` 自己寫並提交）。玩家可以反覆生成、
修改、放棄，資料庫不會留下半成品；自我介紹只存在於這次請求與送給模型的資料區塊，不寫入資料庫、日誌或回應。

`suggest` 的順序（每一步失敗就停下）：
1. 帳號與年齡閘門（`require_chat_access`）；2. 取得可見的版本與內容模式（404、403）；3. 自我介紹的標準化與長度；
4. 自我介紹的規則層（`reject` -> 422，**不呼叫模型、不計次**）；5. 今日次數（用盡 -> 429，不呼叫模型）；
6. RPG 模型沒有設定或全站用量已到上限 -> 503（**不計次**）；7. 呼叫模型（結構化輸出，驗證失敗同模型重試一次，暫時性錯誤切備援）；
8. 對輸出再做一次標準化。呼叫了模型的請求才計次（以帳本中第一次嘗試的筆數計）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from app.llm.types import LLMContent
from app.models.llm_call_log import KIND_PROTAGONIST_GEN
from app.observability import protagonist_events
from app.repositories.llm_call_log_repository import LlmCallLogRepository
from app.services import protagonist as pro
from app.services.access_policy import AccessPolicy
from app.services.current_user import CurrentUser
from app.services.errors import ProtagonistRejected, SuggestionLimit, SuggestionUnavailable, ValidationError
from app.services.llm_service import DEGRADED_BLOCKED, LlmService
from app.services.protagonist_prompt import SUGGEST_SCHEMA, build_suggest_contents, build_suggest_instruction, suggest_rules
from app.services.quota_policy import taipei_day_bounds
from app.services.runtime_limits import RuntimeLimits
from app.services.script_rules import check_text_rules
from app.services.script_spec import (
    CHARACTER_AGE_MAX, CHARACTER_APPEARANCE_MAX, CHARACTER_HIDDEN_TRAIT_MAX, CHARACTER_NAME_MAX, CHARACTER_OCCUPATION_MAX,
    CHARACTER_PERSONALITY_MAX, CHARACTER_SPEECH_STYLE_MAX,
)
from app.services.startable import StartableVersions

# 沒有結果的原因（固定列舉，進日誌）
REASON_NOT_CONFIGURED = "not_configured"
REASON_BUDGET = "budget"
REASON_DEGRADED = "degraded"
REASON_BLOCKED = "blocked"


class ProtagonistService:
    def __init__(
        self,
        *,
        startable: StartableVersions,
        policy: AccessPolicy,
        limits: RuntimeLimits,
        ledger: LlmCallLogRepository,
        llm: LlmService | None,
        marker: str,
        budget_exhausted: Callable[[], bool],
        clock: Callable[[], datetime],
    ) -> None:
        self._startable = startable
        self._policy = policy
        self._limits = limits
        self._ledger = ledger
        self._llm = llm
        self._marker = marker
        self._budget_exhausted = budget_exhausted
        self._clock = clock

    # ---- 創角所需的資料 ---------------------------------------------------------------

    def setup(self, current: CurrentUser, version_public_id: object) -> dict[str, Any]:
        """序章與創角畫面需要的資料。人物只給姓名與職業（不給年齡與隱藏特質）；不含審查結果、內容雜湊與內部編號。"""
        self._policy.require_chat_access(current)
        found = self._startable.resolve(current, version_public_id)
        options = pro.anchor_options(found.content)
        limit, used = self._limit_and_used(current)
        return {
            "title": found.content["title"], "background": found.content["background"],
            "characters": options["characters"], "locations": options["locations"], "milestones": options["milestones"],
            "limits": {
                "name": CHARACTER_NAME_MAX, "personality": CHARACTER_PERSONALITY_MAX, "age_max": CHARACTER_AGE_MAX,
                "appearance": CHARACTER_APPEARANCE_MAX, "occupation": CHARACTER_OCCUPATION_MAX,
                "speech_style": CHARACTER_SPEECH_STYLE_MAX, "hidden_trait": CHARACTER_HIDDEN_TRAIT_MAX,
                "intro": pro.INTRO_MAX, "anchor_text": pro.ANCHOR_TEXT_MAX,
            },
            "suggestions_remaining": max(0, limit - used),
            "suggestions_available": self._llm is not None,
        }

    # ---- AI 生成預覽 ------------------------------------------------------------------

    def suggest(self, current: CurrentUser, version_public_id: object, intro: object) -> dict[str, Any]:
        self._policy.require_chat_access(current)
        found = self._startable.resolve(current, version_public_id)
        text, errors = pro.clean_intro(intro, forbidden_markers=[self._marker])
        if errors:
            raise ValidationError(fields=errors)
        adult_age = self._policy.adult_age
        if text is not None:
            verdict = check_text_rules([text], tier=found.tier, adult_age=adult_age)
            if verdict.verdict == "reject":
                protagonist_events.log_rejected(user_id=current.id, stage="suggest", layer="rules", categories=verdict.categories)
                raise ProtagonistRejected(verdict.categories)

        limit, used = self._limit_and_used(current)
        if used >= limit:
            protagonist_events.log_suggest_limited(user_id=current.id, used=used, limit=limit)
            start, end = taipei_day_bounds(self._clock())
            raise SuggestionLimit(retry_after=max(1, int((end - self._clock()).total_seconds())))
        if self._llm is None:
            raise self._unavailable(current.id, REASON_NOT_CONFIGURED)
        if self._budget_exhausted():
            raise self._unavailable(current.id, REASON_BUDGET)

        outcome = self._llm.run_structured(
            kind=KIND_PROTAGONIST_GEN, user_id=current.id, conversation_id=None,
            system_instruction=build_suggest_instruction(self._marker),
            contents=list[LLMContent](build_suggest_contents(found.content, text, found.tier)),
            schema=SUGGEST_SCHEMA,
            rules=suggest_rules(self._marker, script_content=found.content, tier=found.tier, adult_age=adult_age),
        )
        if not outcome.ok:
            raise self._unavailable(current.id, REASON_BLOCKED if outcome.degraded == DEGRADED_BLOCKED else REASON_DEGRADED)

        character, card_errors = pro.validate_protagonist(outcome.value["character"], forbidden_markers=[self._marker])
        anchor, anchor_errors = pro.validate_anchor(
            outcome.value["anchor"], script_content=found.content, protagonist_age=character["age"] if character else None,
            adult_age=adult_age, forbidden_markers=[self._marker],
        )
        if character is None or anchor is None or card_errors or anchor_errors:         # 語意規則已驗證過；防禦：不給玩家看不合格的候選
            raise self._unavailable(current.id, REASON_DEGRADED)
        remaining = max(0, limit - used - 1)
        protagonist_events.log_suggested(user_id=current.id, model=outcome.model, attempt=outcome.attempts, remaining=remaining)
        return {"protagonist": character, "anchor": anchor, "remaining": remaining}

    # ---- 內部 ----------------------------------------------------------------------

    def _limit_and_used(self, current: CurrentUser) -> tuple[int, int]:
        start, end = taipei_day_bounds(self._clock())
        used = self._ledger.count_first_attempts(current.id, KIND_PROTAGONIST_GEN, start=start, end=end)
        return self._limits.suggest_daily(current.member_tier), used

    @staticmethod
    def _unavailable(user_id: int, reason: str) -> SuggestionUnavailable:
        protagonist_events.log_suggest_unavailable(user_id=user_id, reason=reason)
        return SuggestionUnavailable()
