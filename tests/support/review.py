"""劇本審查的測試替身。

`SessionService` 開存檔前會問審查把關（`ReviewGate`）；與審查無關的測試用 `PassingReviews`：永遠回報指定的結果、
記下被問過幾次。真正的模型層（`ScriptReviewService`）另以假供應商測試（`test_script_review.py`）。
"""
from __future__ import annotations

from typing import Any

from app.models.script import REVIEW_PASS, ScriptVersion
from app.services.script_review_service import ModelReview


class PassingReviews:
    def __init__(self, verdict: str | None = REVIEW_PASS, categories: tuple[str, ...] = (), unavailable: str | None = None) -> None:
        self.verdict = verdict
        self.categories = tuple(categories)
        self.unavailable = unavailable
        self.calls: list[tuple[int, str]] = []

    def ensure_reviewed(self, user_id: int, version: ScriptVersion, content: dict[str, Any]) -> ModelReview:
        self.calls.append((user_id, version.public_id))
        if self.verdict is None:
            return ModelReview(None, unavailable=self.unavailable or "degraded")
        return ModelReview(self.verdict, self.categories)


PROTAGONIST_MARKER = "test-protagonist-marker-000000"


class PassingProtagonistReviews:
    """開存檔時主角的模型層審查的替身（`ProtagonistReviewGate`）：永遠回報指定的結果，記下被問過幾次與問了什麼。"""

    def __init__(self, verdict: str | None = REVIEW_PASS, categories: tuple[str, ...] = (), unavailable: str | None = None) -> None:
        self.verdict = verdict
        self.categories = tuple(categories)
        self.unavailable = unavailable
        self.calls: list[tuple[int, dict[str, Any], str]] = []

    def review(self, user_id: int, protagonist: dict[str, Any], script_content: dict[str, Any], tier: str) -> ModelReview:
        self.calls.append((user_id, protagonist, tier))
        if self.verdict is None:
            return ModelReview(None, unavailable=self.unavailable or "degraded")
        return ModelReview(self.verdict, self.categories)
