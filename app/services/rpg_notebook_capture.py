"""RPG 記事簿「每完成 10 輪抓取重點」的執行者：`NotebookCapturer`（見 rpg-notebook-capture 規格與 rpg-notebook 的 design D1、D4、D5）。

抓取在回合串流的回應關閉之後執行（Controller 以 `response.call_on_close` 呼叫 `run`），所以和 `TurnRunner` 一樣**自己管理短交易**：
模型呼叫期間沒有任何資料庫交易，每個資料庫階段各開一個新的工作階段、提交、關閉；依賴（工作階段工廠、模型服務工廠、時鐘）都在請求內取好
（`RunnerDeps`）。**整個 `run` 不拋出**：任何失敗只記一筆列舉事件並回傳結果，絕不影響已送達的回合。抓取不碰錢包、不計輪數。

    run(target)                                                     總預算 60 秒，從開始起以單調時鐘計
     1 短交易 A  讀存檔 → 邊界 B = window_for(完成輪數) → 取視窗的 10 輪
                  · 存檔已完結、B 不大於已處理到的輪次  → 沒有待抓取（不記事件）
                  · 記事簿已滿                          → 推進標記到 B，不呼叫模型（略過：full）
                  · 全站 token 預算用盡                 → 不推進（略過：budget）
     2 抓取呼叫  run_structured(kind=rpg_notebook, budget=min(30, 剩餘))   暫時性失敗／預算逾時／驗證失敗 → 不推進（失敗）
     3 驗證      merge_entries（清理、去除空的與重複）→ 規則層（逐條累積，前一條為相鄰句）與洩漏檢查
                  · 沒有新條目 → 推進（略過：empty）；任何一條未通過 → 整次放棄並推進（放棄：blocked）
     4 長度      追加後 ≤ 容量 + 預備空間？ 否且剩餘預算 ≥ 20 秒 → 精簡呼叫一次（回到 3）；否則放棄並推進（放棄：too_long）
     5 短交易 B  鎖定存檔列（條件式 UPDATE）→ 對**鎖內讀到的最新文字**重新計算 → 追加、版本加 1、標記 = B
                  · 已被編輯成已滿 → 推進（略過：full）；追加後會超過上限 → 推進（放棄：stale）；標記已被別人推進 → 不動作

「推進」指把 `notebook_upto_turn` 推到 B（不改文字、不加版本）：成功、已滿、沒有新條目與放棄都推進；暫時性失敗、預算逾時、全站預算用盡與
驗證失敗不推進，由下一個完成的回合以同一個視窗補做（若期間又跨過下一個邊界，邊界 B 於下次執行時重算，所以只會抓最新的視窗）。
條數與字數是對模型的要求（提示詞），這裡不因此丟棄或截斷任何條目，只把超出的整數記進日誌；總長以「容量 + 預備空間」把關。

日誌只經 `rpg_events`（內部編號、整數與列舉）：條目、記事簿、行動與劇情都不進日誌；帳本（`rpg_notebook`）只有模型與用量。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from sqlalchemy.orm import Session

from app.models.llm_call_log import KIND_RPG_NOTEBOOK
from app.models.rpg_session import SESSION_ACTIVE
from app.observability import rpg_events
from app.repositories.session_repository import SessionRepository
from app.services import notebook_policy as policy
from app.services import rpg_notebook_prompt as prompts
from app.services import rpg_prompt, rpg_world
from app.services.llm_service import (
    DEGRADED_BLOCKED, DEGRADED_BUDGET, DEGRADED_CONFIG, DEGRADED_TRANSIENT, DEGRADED_VALIDATION, StructuredOutcome,
)
from app.services.rpg_context import TURN_COMPLETE, HistoryTurn
from app.services.rpg_output_rules import evaluate, leak_categories
from app.services.rpg_runner_base import RunnerDeps

TOTAL_BUDGET_SECONDS = 60.0              # 一次抓取（含 JSON 重試、備援切換與精簡重試）的總預算；最壞 4 次模型呼叫
CALL_BUDGET_SECONDS = 30.0               # 單次 `run_structured` 的預算上限（之後取 `min(30, 剩餘)`）
CONDENSE_MIN_REMAINING_SECONDS = 20.0    # 剩餘預算不少於這個秒數才啟動精簡重試

# `CaptureResult.outcome`
OUTCOME_NOTHING = "nothing"              # 沒有待抓取的視窗（存檔已完結、邊界不大於標記，或不是自己的存檔）
OUTCOME_CAPTURED = "captured"            # 追加了新條目
OUTCOME_SKIPPED = "skipped"              # 略過：已滿、沒有新條目、全站預算用盡
OUTCOME_ABANDONED = "abandoned"          # 放棄：規則層／洩漏未通過、超過上限、鎖內發現超過上限
OUTCOME_FAILED = "failed"                # 失敗（不推進標記）
OUTCOME_SUPERSEDED = "superseded"        # 鎖內發現標記已被另一次抓取推進（重複觸發），不動作

_FAILURE_REASONS = {
    DEGRADED_BUDGET: "timeout", DEGRADED_TRANSIENT: "transient", DEGRADED_BLOCKED: "blocked", DEGRADED_CONFIG: "config",
    DEGRADED_VALIDATION: "validation",
}


@dataclass(frozen=True)
class CaptureTarget:
    """一次抓取的對象：回合完成時凍結的純資料（沒有 ORM 物件、工作階段或請求物件）。"""

    user_id: int
    session_id: int                      # 內部整數編號（只給日誌）
    session_public_id: str
    member_tier: str                     # 會員等級：決定記事簿容量
    content_tier: str                    # 生效的內容模式（與該回合輸出審查相同）：規則層與提示詞的分級界線
    adult_age: int


@dataclass(frozen=True)
class CaptureResult:
    """`run` 的結果（列舉）：給測試與呼叫端觀察，不含任何文字。`reason` 是日誌事件的原因列舉。"""

    outcome: str
    reason: str | None = None


@dataclass(frozen=True)
class _Window:
    """步驟 1 讀到的視窗：邊界 B、當時的記事簿文字、視窗內的 10 輪與審查用的在場角色。"""

    boundary: int
    notebook: str
    turns: tuple[HistoryTurn, ...]
    characters: tuple[rpg_world.CharacterAge, ...]


@dataclass
class _Progress:
    """這次執行的累計（日誌用）：模型呼叫次數、最後使用的模型、開始時間。"""

    began: float
    calls: int = 0
    model: str | None = None
    boundary: int | None = None


class NotebookCapturer:
    def __init__(
        self, *, deps: RunnerDeps, marker: str, budget_exhausted: Callable[[Session], bool], language: str = rpg_prompt.DEFAULT_LANGUAGE,
    ) -> None:
        """`budget_exhausted(session)`：全站每日 token／呼叫上限是否已用盡（以傳入的工作階段查帳本，與其他用途一體適用）。"""
        self._deps = deps
        self._marker = marker
        self._budget_exhausted = budget_exhausted
        self._language = language

    # ---- 對外 -----------------------------------------------------------------------------------

    def run(self, target: CaptureTarget) -> CaptureResult:
        """依模組說明的五步執行一次抓取。**不拋出**：非預期的錯誤記為 `failed`（`unavailable`），標記不推進，由下個回合補做。"""
        progress = _Progress(began=self._deps.monotonic())
        try:
            return self._run(target, progress)
        except Exception:                                                    # noqa: BLE001 — 背景工作不得影響已送達的回合
            try:
                return self._failed(target, progress, "unavailable")
            except Exception:                                                # noqa: BLE001
                return CaptureResult(OUTCOME_FAILED, "unavailable")

    # ---- 流程 -----------------------------------------------------------------------------------

    def _run(self, target: CaptureTarget, progress: _Progress) -> CaptureResult:
        read = self._read(target, progress)
        if isinstance(read, CaptureResult):
            return read
        window = read
        progress.boundary = window.boundary
        factory = self._deps.llm_factory
        if factory is None:                                                  # 沒有設定供應商：無法抓取，不推進
            return self._failed(target, progress, "unavailable")

        capacity, limit = policy.capacity_for(target.member_tier), policy.ai_limit_for(target.member_tier)
        contents = prompts.build_capture_contents(notebook=window.notebook, capacity=capacity, turns=window.turns)
        for round_number in (1, 2):
            outcome = self._call(target, progress, factory, contents)
            if not outcome.ok:
                return self._failed(target, progress, _FAILURE_REASONS.get(outcome.degraded or "", "unavailable"))
            merge = policy.merge_entries(window.notebook, outcome.value["entries"])
            if not merge.entries:                                            # 沒有值得記錄的新內容（含全部重複）：不算失敗
                return self._skip_empty(target, progress)
            blocked, noted = self._screen(target, window.characters, merge.entries)
            if blocked:
                self._advance_marker(target, window.boundary)
                rpg_events.log_notebook_abandoned(
                    user_id=target.user_id, session_id=target.session_id, boundary=window.boundary, reason="blocked", entries=len(merge.entries),
                    total=merge.length, calls=progress.calls, latency_ms=self._elapsed_ms(progress), model=progress.model, categories=blocked,
                )
                return CaptureResult(OUTCOME_ABANDONED, "blocked")
            if merge.length <= limit:
                return self._append(target, progress, window, merge.entries, noted)
            if round_number == 2 or self._remaining(progress) < CONDENSE_MIN_REMAINING_SECONDS:
                self._advance_marker(target, window.boundary)
                rpg_events.log_notebook_abandoned(
                    user_id=target.user_id, session_id=target.session_id, boundary=window.boundary, reason="too_long", entries=len(merge.entries),
                    total=merge.length, calls=progress.calls, latency_ms=self._elapsed_ms(progress), model=progress.model,
                )
                return CaptureResult(OUTCOME_ABANDONED, "too_long")
            contents = prompts.build_condense_contents(
                notebook=window.notebook, capacity=capacity, turns=window.turns, previous_entries=merge.entries,
                room=policy.room_for(window.notebook, target.member_tier),
            )
        raise AssertionError("不可達")                                        # pragma: no cover — 迴圈內每個分支都已回傳

    # ---- 步驟 1：短交易 A ------------------------------------------------------------------------

    def _read(self, target: CaptureTarget, progress: _Progress) -> _Window | CaptureResult:
        with self._deps.session_factory() as session:
            repo = SessionRepository(session)
            save = repo.get_owned(target.user_id, target.session_public_id)
            if save is None or save.status != SESSION_ACTIVE:
                return CaptureResult(OUTCOME_NOTHING)
            boundary = policy.window_for(repo.count_complete_turns(target.user_id, target.session_public_id) or 0)
            if boundary is None or boundary <= save.notebook_upto_turn:
                return CaptureResult(OUTCOME_NOTHING)
            progress.boundary = boundary
            notebook = save.notebook_text or ""
            if policy.is_full(policy.length_of(notebook), target.member_tier):          # 已滿：不呼叫模型、不補抓被跳過的輪次
                repo.advance_notebook_marker(target.user_id, target.session_public_id, boundary)
                session.commit()
                rpg_events.log_notebook_skipped(
                    user_id=target.user_id, session_id=target.session_id, boundary=boundary, reason="full", total=policy.length_of(notebook),
                )
                return CaptureResult(OUTCOME_SKIPPED, "full")
            if self._budget_exhausted(session):                                          # 全站預算用盡：不推進，之後的回合再試
                rpg_events.log_notebook_skipped(user_id=target.user_id, session_id=target.session_id, boundary=boundary, reason="budget")
                return CaptureResult(OUTCOME_SKIPPED, "budget")
            rows = repo.turns_in_range(
                target.user_id, target.session_public_id, boundary - policy.WINDOW_TURNS + 1, boundary,
            ) or []
            turns = tuple(
                HistoryTurn(turn_number=row.turn_number, status=row.turn_status, action=row.user_action or "", narration=row.llm_raw_response or "")
                for row in rows if row.turn_status == TURN_COMPLETE
            )
            if not turns:                                                                # 視窗內沒有完成的回合（資料被清掉了）：沒有東西可抓
                repo.advance_notebook_marker(target.user_id, target.session_public_id, boundary)
                session.commit()
                rpg_events.log_notebook_skipped(user_id=target.user_id, session_id=target.session_id, boundary=boundary, reason="empty")
                return CaptureResult(OUTCOME_SKIPPED, "empty")
            script = save.script_snapshot_json if isinstance(save.script_snapshot_json, dict) else {}
            protagonist = rpg_world.protagonist_from_session(save.protagonist_json, save.player_name, save.character_age)
            world = rpg_world.WorldState.from_fields(
                entities=save.entities_json, goals=save.goals_json, milestone_progress=save.milestone_progress_json,
                elapsed_months=save.elapsed_months, current_location=save.current_location, status_summary=save.status_summary,
            )
            characters = tuple(rpg_world.known_characters(world, script, protagonist))
        return _Window(boundary=boundary, notebook=notebook, turns=turns, characters=characters)

    # ---- 步驟 2：抓取呼叫 --------------------------------------------------------------------------

    def _remaining(self, progress: _Progress) -> float:
        return TOTAL_BUDGET_SECONDS - (self._deps.monotonic() - progress.began)

    def _elapsed_ms(self, progress: _Progress) -> int:
        return max(0, round((self._deps.monotonic() - progress.began) * 1000))

    def _call(self, target: CaptureTarget, progress: _Progress, factory, contents) -> StructuredOutcome:
        """一次結構化呼叫（含 `run_structured` 自己的 JSON 重試與備援切換）。呼叫期間沒有資料庫交易；帳本（只有模型與用量）隨後提交。"""
        system = prompts.build_capture_system(target.content_tier, self._language, self._marker)
        with self._deps.session_factory() as session:
            llm = factory(session)
            outcome = llm.run_structured(
                kind=KIND_RPG_NOTEBOOK, user_id=target.user_id, conversation_id=None, system_instruction=system, contents=contents,
                schema=prompts.CAPTURE_SCHEMA, rules=prompts.capture_rules(self._marker),
                budget_seconds=min(CALL_BUDGET_SECONDS, self._remaining(progress)),
            )
            session.commit()
        progress.calls += outcome.attempts
        progress.model = outcome.model or progress.model
        return outcome

    # ---- 步驟 3：驗證與規則層 ------------------------------------------------------------------------

    def _screen(
        self, target: CaptureTarget, characters: Sequence[rpg_world.CharacterAge], entries: Sequence[str],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """條目依序餵給輸出審查的規則層（逐條累積為同一段，前一條為相鄰句；內容模式與在場角色的年齡同回合），再檢查標記與內部設定的洩漏。
        回傳（擋下的類別, 放行但記錄的類別）。任何一條被擋下，整次放棄。"""
        blocked: set[str] = set()
        noted: set[str] = set()
        paragraph: list[str] = []
        previous: str | None = None
        for entry in entries:
            paragraph.append(entry)
            result = evaluate(paragraph, previous, tier=target.content_tier, characters=characters, adult_age=target.adult_age)
            blocked.update(result.blocked)
            noted.update(result.noted)
            blocked.update(leak_categories(entry, self._marker))
            previous = entry
        return tuple(sorted(blocked)), tuple(sorted(noted))

    # ---- 步驟 5：短交易 B（鎖定後對最新文字追加）---------------------------------------------------------

    def _append(
        self, target: CaptureTarget, progress: _Progress, window: _Window, entries: Sequence[str], noted: tuple[str, ...],
    ) -> CaptureResult:
        boundary = window.boundary
        limit = policy.ai_limit_for(target.member_tier)
        with self._deps.session_factory() as session:
            repo = SessionRepository(session)
            lock = repo.lock_notebook_for_capture(target.user_id, target.session_public_id, boundary)
            if lock is None:                                                             # 標記已被推進、存檔已完結：什麼都沒有改動
                session.rollback()
                return CaptureResult(OUTCOME_SUPERSEDED)
            latest = policy.merge_entries(lock.text, entries)                            # 對鎖內讀到的最新文字重新計算（含玩家剛做的編輯）
            if policy.is_full(policy.length_of(lock.text), target.member_tier):          # 抓取期間被編輯成已滿：視同已滿跳過
                repo.advance_notebook_marker(target.user_id, target.session_public_id, boundary)
                session.commit()
                rpg_events.log_notebook_skipped(
                    user_id=target.user_id, session_id=target.session_id, boundary=boundary, reason="full", total=policy.length_of(lock.text),
                    calls=progress.calls, latency_ms=self._elapsed_ms(progress), model=progress.model,
                )
                return CaptureResult(OUTCOME_SKIPPED, "full")
            if not latest.entries:                                                       # 玩家剛好自己寫了同樣的內容
                repo.advance_notebook_marker(target.user_id, target.session_public_id, boundary)
                session.commit()
                rpg_events.log_notebook_skipped(
                    user_id=target.user_id, session_id=target.session_id, boundary=boundary, reason="empty", total=policy.length_of(lock.text),
                    calls=progress.calls, latency_ms=self._elapsed_ms(progress), model=progress.model,
                )
                return CaptureResult(OUTCOME_SKIPPED, "empty")
            if latest.length > limit:                                                    # 玩家在期間貼了很多字：不再精簡，直接放棄
                repo.advance_notebook_marker(target.user_id, target.session_public_id, boundary)
                session.commit()
                rpg_events.log_notebook_abandoned(
                    user_id=target.user_id, session_id=target.session_id, boundary=boundary, reason="stale", entries=len(latest.entries),
                    total=latest.length, calls=progress.calls, latency_ms=self._elapsed_ms(progress), model=progress.model,
                )
                return CaptureResult(OUTCOME_ABANDONED, "stale")
            appended = repo.append_notebook(target.user_id, target.session_public_id, text=latest.text, boundary=boundary)
            if not appended:                                                             # 鎖已持有，理論上不會發生
                session.rollback()
                return CaptureResult(OUTCOME_SUPERSEDED)
            session.commit()
            version = lock.version + 1
        over_entries, over_chars = prompts.overage(latest.entries)
        rpg_events.log_notebook_captured(
            user_id=target.user_id, session_id=target.session_id, boundary=boundary, entries=len(latest.entries), longest=latest.longest_entry,
            added=latest.added_length, total=latest.length, version=version, over_entries=over_entries, over_chars=over_chars,
            calls=progress.calls, latency_ms=self._elapsed_ms(progress), model=progress.model, categories=noted,
        )
        return CaptureResult(OUTCOME_CAPTURED)

    # ---- 推進標記與失敗 ----------------------------------------------------------------------------

    def _advance_marker(self, target: CaptureTarget, boundary: int) -> None:
        """獨立的短交易：只把標記推到 `boundary`（條件式、只往前、只改動進行中的存檔）。"""
        with self._deps.session_factory() as session:
            SessionRepository(session).advance_notebook_marker(target.user_id, target.session_public_id, boundary)
            session.commit()

    def _skip_empty(self, target: CaptureTarget, progress: _Progress) -> CaptureResult:
        """模型沒有給出新條目（空陣列，或全部與既有內容重複）：推進標記並記一筆略過事件（`empty`），不算失敗。"""
        assert progress.boundary is not None
        self._advance_marker(target, progress.boundary)
        rpg_events.log_notebook_skipped(
            user_id=target.user_id, session_id=target.session_id, boundary=progress.boundary, reason="empty", calls=progress.calls,
            latency_ms=self._elapsed_ms(progress), model=progress.model,
        )
        return CaptureResult(OUTCOME_SKIPPED, "empty")

    def _failed(self, target: CaptureTarget, progress: _Progress, reason: str) -> CaptureResult:
        """失敗：不推進標記，由後續回合補做。"""
        rpg_events.log_notebook_failed(
            user_id=target.user_id, session_id=target.session_id, boundary=progress.boundary, reason=reason, calls=progress.calls,
            latency_ms=self._elapsed_ms(progress), model=progress.model,
        )
        return CaptureResult(OUTCOME_FAILED, reason)
