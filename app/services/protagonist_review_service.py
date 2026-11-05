"""玩家主角的模型層審查（見 rpg-protagonist 規格「主角文字須通過規則層與模型層審查」與 rpg-character-creation 的 design D4）。

開存檔時對**最終**的主角角色卡與錨定事實審查一次（預覽的候選會被玩家修改，所以不沿用預覽時的任何結果）。用 RPG 用途的模型
（`OPENROUTER_MODEL`，備援 `OPENROUTER_FALLBACK_MODEL`，**不使用 Gemini**）以非串流、結構化輸出審查，呼叫走 `LlmService.run_structured`
（重試、備援、斷路、總時間預算與用量帳本都一樣，呼叫類別 `protagonist_review`）。**失敗一律不放行**：RPG 供應商沒有設定、全站用量已達
上限、降級（兩次驗證失敗、備援也失敗）、被供應商的安全過濾阻擋，都回報「不可用」，由呼叫端以 503 擋下，也不寫入任何通過標記。

結果沒有地方快取：主角沒有版本列可以附著，每次開存檔審查一次。日誌與用量帳本只有類別、結果與數字；主角文字與模型原文不進日誌。
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from app.llm.types import LLMContent
from app.models.llm_call_log import KIND_PROTAGONIST_REVIEW
from app.observability import protagonist_events
from app.services.llm_service import DEGRADED_BLOCKED, LlmService
from app.services.protagonist_prompt import REVIEW_SCHEMA, build_review_contents, build_review_instruction, review_rules
from app.services.script_review_service import (
    UNAVAILABLE_BLOCKED, UNAVAILABLE_BUDGET, UNAVAILABLE_DEGRADED, UNAVAILABLE_NOT_CONFIGURED, ModelReview,
)


class ProtagonistReviewService:
    def __init__(self, *, llm: LlmService | None, marker: str, budget_exhausted: Callable[[], bool]) -> None:
        self._llm = llm
        self._marker = marker
        self._budget_exhausted = budget_exhausted

    def review(self, user_id: int, protagonist: Mapping[str, Any], script_content: Mapping[str, Any], tier: str) -> ModelReview:
        """呼叫模型審查一個主角（`{"character": …, "anchor": … 或 None}`）。不碰資料庫。"""
        if self._llm is None:
            return self._unavailable(user_id, UNAVAILABLE_NOT_CONFIGURED)
        if self._budget_exhausted():
            return self._unavailable(user_id, UNAVAILABLE_BUDGET)
        outcome = self._llm.run_structured(
            kind=KIND_PROTAGONIST_REVIEW, user_id=user_id, conversation_id=None,
            system_instruction=build_review_instruction(self._marker),
            contents=list[LLMContent](build_review_contents(protagonist, script_content, tier)),
            schema=REVIEW_SCHEMA, rules=review_rules(self._marker),
        )
        if not outcome.ok:
            reason = UNAVAILABLE_BLOCKED if outcome.degraded == DEGRADED_BLOCKED else UNAVAILABLE_DEGRADED
            return self._unavailable(user_id, reason)
        verdict = outcome.value["verdict"]
        categories = tuple(outcome.value["categories"])
        protagonist_events.log_review(user_id=user_id, model=outcome.model, verdict=verdict, attempt=outcome.attempts)
        return ModelReview(verdict, categories)

    @staticmethod
    def _unavailable(user_id: int, reason: str) -> ModelReview:
        protagonist_events.log_review_unavailable(user_id=user_id, reason=reason)
        return ModelReview(None, unavailable=reason)
