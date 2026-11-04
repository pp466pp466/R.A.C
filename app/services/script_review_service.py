"""劇本審查的模型層（見 script-review 規格與 rpg-script-authoring 的 design D6、D7）。

- `review_model(user_id, content, tier)`：用 RPG 用途的模型（`OPENROUTER_MODEL`，備援 `OPENROUTER_FALLBACK_MODEL`，**不使用 Gemini**）
  以非串流、結構化輸出審查一份劇本；呼叫走 `LlmService.run_structured`（重試、備援、斷路、總時間預算與用量帳本都一樣，
  呼叫類別 `script_review`）。**失敗一律不放行**：RPG 供應商沒有設定、全站用量已達上限、降級（兩次驗證失敗、備援也失敗）、
  被供應商的安全過濾阻擋，都回報「不可用」，由呼叫端拒絕送審或開存檔，也不寫入任何通過標記。
- `ensure_reviewed(user_id, version, content)`：結果對應內容雜湊，記在版本上（`record_review`，條件式比對雜湊）。雜湊相同就直接用
  先前的結果、不再呼叫模型（包含 `reject`：被拒絕的內容在改變之前不會重複花 token）；內容有變動就重新審查。寫入時內容已被修改
  （審查期間作者改了草稿），結果會被丟棄並回報 `stale`，不會蓋在新內容上。
- 日誌與用量帳本只有類別、結果與數字；劇本文字與模型原文不進日誌（驗證失敗的原文依既有規則截斷存在帳本 `error_detail`，14 天）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from app.llm.types import LLMContent
from app.models.llm_call_log import KIND_SCRIPT_REVIEW
from app.models.script import REVIEW_FLAG, REVIEW_PASS, REVIEW_REJECT, ScriptVersion
from app.observability import script_events
from app.repositories.script_repository import ScriptRepository
from app.services.llm_service import DEGRADED_BLOCKED, LlmService
from app.services.script_review_prompt import (
    REVIEW_SCHEMA, build_review_contents, build_review_instruction, review_rules,
)

UNAVAILABLE_NOT_CONFIGURED = "not_configured"
UNAVAILABLE_BUDGET = "budget"
UNAVAILABLE_DEGRADED = "degraded"
UNAVAILABLE_BLOCKED = "blocked"


@dataclass(frozen=True)
class ModelReview:
    """模型層的結果。`unavailable` 非 None 時沒有結果（`verdict` 為 None）；`stale` 表示內容在審查期間被修改，結果沒有被記錄。"""

    verdict: str | None
    categories: tuple[str, ...] = ()
    unavailable: str | None = None
    stale: bool = False
    from_record: bool = False          # 直接使用先前記錄的結果，沒有呼叫模型


class ScriptReviewService:
    def __init__(
        self,
        *,
        scripts: ScriptRepository,
        llm: LlmService | None,
        marker: str,
        budget_exhausted: Callable[[], bool],
        clock: Callable[[], datetime],
    ) -> None:
        self._scripts = scripts
        self._llm = llm
        self._marker = marker
        self._budget_exhausted = budget_exhausted
        self._clock = clock

    def review_model(self, user_id: int, content: dict[str, Any], tier: str) -> ModelReview:
        """呼叫模型審查一份標準化內容。不碰資料庫（不記錄結果）。"""
        if self._llm is None:
            return self._unavailable(user_id, UNAVAILABLE_NOT_CONFIGURED)
        if self._budget_exhausted():
            return self._unavailable(user_id, UNAVAILABLE_BUDGET)
        outcome = self._llm.run_structured(
            kind=KIND_SCRIPT_REVIEW, user_id=user_id, conversation_id=None,
            system_instruction=build_review_instruction(self._marker),
            contents=list[LLMContent](build_review_contents(content, tier)),
            schema=REVIEW_SCHEMA, rules=review_rules(self._marker),
        )
        if not outcome.ok:
            reason = UNAVAILABLE_BLOCKED if outcome.degraded == DEGRADED_BLOCKED else UNAVAILABLE_DEGRADED
            return self._unavailable(user_id, reason)
        verdict = outcome.value["verdict"]
        categories = tuple(outcome.value["categories"])
        script_events.log_review(user_id=user_id, model=outcome.model, verdict=verdict, attempt=outcome.attempts)
        return ModelReview(verdict, categories)

    def ensure_reviewed(self, user_id: int, version: ScriptVersion, content: dict[str, Any]) -> ModelReview:
        """取得這個版本「目前內容」的模型層結果：雜湊相同用記錄的，否則審查並記錄。

        `content` 必須是呼叫端剛由這個版本還原的標準化內容（與 `version.content_hash` 對應）；用它審查，所以即使審查期間版本
        被修改，被審查的仍是呼叫端手上的那一份。
        """
        content_hash = version.content_hash
        if version.review_hash == content_hash and version.review_verdict in (REVIEW_PASS, REVIEW_FLAG, REVIEW_REJECT):
            return ModelReview(version.review_verdict, tuple(version.review_categories_json or ()), from_record=True)
        review = self.review_model(user_id, content, version.content_tier)
        if review.verdict is None:
            return review
        recorded = self._scripts.record_review(version.id, content_hash, review.verdict, review.categories, self._clock())
        if not recorded:
            return ModelReview(review.verdict, review.categories, stale=True)
        return review

    @staticmethod
    def _unavailable(user_id: int, reason: str) -> ModelReview:
        script_events.log_review_unavailable(user_id=user_id, reason=reason)
        return ModelReview(None, unavailable=reason)
