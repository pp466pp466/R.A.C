"""RPG 串流生成的期限、備援與帳本（見 llm-streaming 規格與 design D4）。

`StreamRunner` 把一次「串流生成」包成一個可迭代的 `StreamRun`：外層（`TurnRunner`）迭代它，取得 `StreamText`（正文片段）與
`StreamTick`（閒置，供外層送出心跳、檢查客戶端是否斷線）；迭代結束後看 `run.outcome` 得知成功、失敗或被中止。

    嘗試 1：主要模型（近 60 秒主要模型曾逾時或伺服器錯誤則直接用備援）
      第一個正文片段 ≤ 20 秒；兩次嘗試共用 60 秒的總預算
      第一個正文片段之前的暫時性失敗（429、5xx、連線失敗、逾時、空回應）─► 嘗試 2：備援模型（剩餘不足 2 秒不再嘗試）
      已經送出任何正文之後的失敗 ─► 不切換，結束（外層處理為中斷）
      被安全過濾阻擋、設定錯誤（400、401、402、403、404）─► 不切換，結束

- **不做任何資料庫工作**：帳本透過注入的 `record(entry)` 寫入，由外層在自己的短交易內提交；斷路透過注入的 `breaker_open(model)`
  查帳本（多個 worker 看到同一份真相，不放行程內狀態）。期限以單調時鐘檢查（每個事件到達時）。
- 每次嘗試一筆帳本，**只有分類與數字**：模型名稱、嘗試序號、結果狀態、token、延遲、首字時間、結束原因，以及列舉式的
  `error_detail`（例如 `rate_limited 429`、`timeout first_chunk`、`disconnected`、`output_blocked minor`）。**絕不含劇情、
  行動或上下文的任何文字**——這裡的函式根本沒有接收這些的參數（類別只收進 `StreamRequest`，帳本欄位只用長度）。
- 供應商有提供用量時使用；斷線、我方中止（審查命中）或供應商沒提供時，以上下文字數與已收字數估算（中文約 1 至 1.5 token 每字，
  這是保守但偏低的估算），結束原因記為 `other`。這些呼叫仍計入全站每日用量。
- 外層中止（`abort`）有兩種原因：`disconnected`（客戶端斷線；帳本狀態 `ok`，因為上游呼叫本身成功）與 `output_blocked`
  （我方審查命中；帳本狀態 `blocked`，`error_detail` 附被擋類別的列舉）。外層直接丟棄或關閉迭代器時，視同 `disconnected`，
  所以任何出口都不會漏記帳本。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Iterator

from app.config import LlmSettings
from app.llm.types import (
    BLOCKED_INPUT, FINISH_OTHER, FINISH_PROMPT_BLOCKED, FINISH_SAFETY, TRANSIENT_NETWORK, TRANSIENT_RATE_LIMITED,
    TRANSIENT_SERVER_ERROR, TRANSIENT_TIMEOUT, LLMBlockedError, LLMConfigError, LLMContent, LLMRequest,
    LLMTransientError, StreamEnd, StreamingLLMProvider, StreamText, StreamTick,
)
from app.models.llm_call_log import (
    BREAKER_STATUSES, STATUS_BLOCKED, STATUS_CONFIG_ERROR, STATUS_NETWORK, STATUS_OK, STATUS_RATE_LIMITED,
    STATUS_SERVER_ERROR, STATUS_TIMEOUT,
)
from app.observability.llm_events import log_breaker_opened, log_call
from app.repositories.llm_call_log_repository import LlmCallLogRepository

FIRST_CHUNK_SECONDS = 20.0          # 每次嘗試的首字期限
TOTAL_BUDGET_SECONDS = 60.0         # 兩次嘗試共用的總預算
MIN_ATTEMPT_SECONDS = 2.0           # 剩餘不足這麼久就不再嘗試
MAX_ATTEMPTS = 2

# 結束（失敗）的分類，供外層決定怎麼處理回合
FAILURE_TIMEOUT = "timeout"         # 首字或總時間逾時（含供應商回報的逾時）
FAILURE_TRANSIENT = "transient"     # 429、5xx、連線失敗、空回應
FAILURE_BLOCKED = "blocked"         # 被供應商的內容安全過濾擋下
FAILURE_CONFIG = "config"           # 設定錯誤（請求不合法、金鑰無權限、餘額不足、模型下架）

# 外層主動中止的原因
ABORT_DISCONNECTED = "disconnected"
ABORT_OUTPUT_BLOCKED = "output_blocked"

_CATEGORY = re.compile(r"[a-z][a-z0-9_]{0,31}")         # 被擋類別必須是列舉式的小寫識別字；其他一律記為 unknown
_TRANSIENT_STATUS = {
    TRANSIENT_RATE_LIMITED: STATUS_RATE_LIMITED, TRANSIENT_SERVER_ERROR: STATUS_SERVER_ERROR,
    TRANSIENT_TIMEOUT: STATUS_TIMEOUT, TRANSIENT_NETWORK: STATUS_NETWORK,
}


@dataclass(frozen=True)
class StreamRequest:
    """一次串流生成的輸入。`model` 是首選模型（斷路開啟時會改用備援）。"""

    model: str
    system_instruction: str
    contents: tuple[LLMContent, ...]
    max_output_tokens: int


@dataclass(frozen=True)
class StreamLedgerEntry:
    """一次嘗試的帳本內容。只有分類與數字，沒有任何文字。"""

    user_id: int | None
    conversation_id: int | None
    kind: str
    provider: str
    model: str
    attempt: int
    status: str
    latency_ms: int
    first_chunk_ms: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    thinking_tokens: int | None
    finish_reason: str | None
    error_detail: str | None


@dataclass(frozen=True)
class StreamOutcome:
    """一次串流生成的結果。成功時 `ok` 為真；失敗時 `failure` 是分類；被外層中止時 `aborted` 是原因。"""

    ok: bool
    model: str | None                   # 最後一次嘗試使用的模型
    attempts: int
    text_started: bool                  # 是否已經送出任何正文片段（決定外層把失敗當中斷或當失敗）
    chars: int                          # 已送出的正文字數
    failure: str | None = None
    blocked_phase: str | None = None    # 因供應商過濾而失敗時：`input` 或 `output`
    aborted: str | None = None
    abort_category: str | None = None
    finish_reason: str | None = None
    first_chunk_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    thinking_tokens: int | None = None


@dataclass
class _Attempt:
    model: str
    number: int
    began: float
    chars: int = 0
    first_chunk_ms: int | None = None


@dataclass(frozen=True)
class _Failure:
    kind: str
    status: str
    detail: str
    switchable: bool                    # 第一個正文片段之前，且還有備援可用時才會真的切換
    finish_reason: str | None = None
    blocked_phase: str | None = None
    error_type: str | None = None
    provider_status: int | None = None


def _transient_failure(error: LLMTransientError) -> _Failure:
    status = _TRANSIENT_STATUS.get(error.kind, STATUS_SERVER_ERROR)
    detail = error.kind if error.http_status is None else f"{error.kind} {error.http_status}"
    kind = FAILURE_TIMEOUT if error.kind == TRANSIENT_TIMEOUT else FAILURE_TRANSIENT
    return _Failure(kind, status, detail, True, error_type=type(error).__name__, provider_status=error.http_status)


def _deadline_failure(which: str) -> _Failure:
    return _Failure(FAILURE_TIMEOUT, STATUS_TIMEOUT, f"timeout {which}", True)


class StreamRunner:
    """一個用途（`rpg_stream` 或 `rpg_opening`）的串流執行器；可重複使用，每次生成呼叫 `start()` 取得一個 `StreamRun`。"""

    def __init__(
        self,
        *,
        provider: StreamingLLMProvider,
        settings: LlmSettings,
        kind: str,
        record: Callable[[StreamLedgerEntry], None],
        breaker_open: Callable[[str], bool] = lambda model: False,
        user_id: int | None = None,
        conversation_id: int | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        first_chunk_seconds: float = FIRST_CHUNK_SECONDS,
        total_budget_seconds: float = TOTAL_BUDGET_SECONDS,
    ) -> None:
        self._provider = provider
        self._settings = settings
        self._kind = kind
        self._record = record
        self._breaker_open = breaker_open
        self._user_id = user_id
        self._conversation_id = conversation_id
        self._monotonic = monotonic
        self._first_chunk = first_chunk_seconds
        self._total_budget = total_budget_seconds

    def start(self, request: StreamRequest) -> "StreamRun":
        return StreamRun(self, request)


class StreamRun:
    """一次串流生成（含備援）。單次使用：迭代取得事件，結束後讀 `outcome`；隨時可以 `abort()`。"""

    def __init__(self, runner: StreamRunner, request: StreamRequest) -> None:
        self._runner = runner
        self._request = request
        self._context_chars = len(request.system_instruction) + sum(len(part) for c in request.contents for part in c.parts)
        self._iterated = False
        self._handle: object | None = None
        self._attempt: _Attempt | None = None          # 進行中、尚未寫入帳本的嘗試
        self._attempts = 0
        self._text_started = False
        self._chars = 0
        self._model: str | None = None
        self._abort: tuple[str, str | None] | None = None
        self.outcome: StreamOutcome | None = None

    # ---- 對外 -----------------------------------------------------------------------------------

    @property
    def model(self) -> str | None:
        """目前（或最後一次）嘗試使用的模型。"""
        return self._model

    @property
    def text_started(self) -> bool:
        return self._text_started

    def __iter__(self) -> Iterator[StreamText | StreamTick]:
        if self._iterated:
            raise RuntimeError("StreamRun 只能迭代一次")
        self._iterated = True
        return self._generate()

    def abort(self, reason: str, category: str | None = None) -> None:
        """外層主動中止：關閉上游、為進行中的嘗試寫入帳本（估算 token）並設定結果。已有結果時不做任何事（可重複呼叫）。

        `reason` 為 `disconnected` 或 `output_blocked`；`category` 只收列舉式的識別字（被擋類別），其他一律記為 `unknown`。
        """
        if self.outcome is not None:
            return
        self._abort = (reason, category)
        self._close_handle()
        detail = reason
        if category is not None:
            detail = f"{reason} {category if _CATEGORY.fullmatch(category) else 'unknown'}"
        entry = self._record_attempt(
            STATUS_BLOCKED if reason == ABORT_OUTPUT_BLOCKED else STATUS_OK, finish_reason=FINISH_OTHER, error_detail=detail,
        )
        self.outcome = self._build_outcome(entry, aborted=reason, abort_category=category)

    # ---- 產生器 ---------------------------------------------------------------------------------

    def _first_model(self) -> str:
        primary, fallback = self._request.model, self._runner._settings.fallback_model
        if primary != fallback and self._runner._breaker_open(primary):
            return fallback                                  # 近期主要模型逾時或伺服器錯誤：直接用備援
        return primary

    def _generate(self) -> Iterator[StreamText | StreamTick]:
        runner = self._runner
        clock = runner._monotonic
        total_deadline = clock() + runner._total_budget
        fallback = runner._settings.fallback_model
        failure: _Failure | None = None
        try:
            if self._abort is not None:
                return
            model = self._first_model()
            while True:
                if self._attempts and total_deadline - clock() < MIN_ATTEMPT_SECONDS:
                    break                                        # 來不及完成一次有意義的嘗試：以上一次的失敗結束
                self._attempts += 1
                failure = yield from self._run_attempt(model, total_deadline)
                if self.outcome is not None or failure is None:
                    return                                       # 成功，或外層已中止
                if not (failure.switchable and not self._text_started and model != fallback and self._attempts < MAX_ATTEMPTS):
                    break
                model = fallback
            assert failure is not None
            self.outcome = self._build_outcome(
                None, failure=failure.kind, blocked_phase=failure.blocked_phase,
            )
        finally:
            self._close_handle()
            if self.outcome is None:
                self.abort(ABORT_DISCONNECTED)                   # 外層直接丟棄或關閉了迭代器：視同斷線，帳本不漏記

    def _run_attempt(self, model: str, total_deadline: float) -> Iterator[StreamText | StreamTick]:
        """一次嘗試。回傳 None 表示成功完成（或已被外層中止）；否則回傳失敗分類（帳本已寫入）。"""
        runner = self._runner
        clock = runner._monotonic
        began = clock()
        attempt = self._attempt = _Attempt(model=model, number=self._attempts, began=began)
        self._model = model
        first_deadline = min(began + runner._first_chunk, total_deadline)
        request = LLMRequest(
            model=model, system_instruction=self._request.system_instruction, contents=self._request.contents,
            response_schema=None, timeout_seconds=max(total_deadline - began, 1.0), max_output_tokens=self._request.max_output_tokens,
        )
        end: StreamEnd | None = None
        try:
            handle = self._handle = runner._provider.stream(request)
            for event in handle:
                if isinstance(event, StreamEnd):
                    end = event                                  # 串流已完整結束：就算剛好越過期限也接受
                    break
                now = clock()
                if now >= total_deadline:
                    return self._fail(_deadline_failure("total"))
                if not self._text_started and now >= first_deadline:
                    return self._fail(_deadline_failure("first_chunk"))
                if isinstance(event, StreamText) and event.text:
                    if attempt.first_chunk_ms is None:
                        attempt.first_chunk_ms = round((now - began) * 1000)
                    self._text_started = True
                    attempt.chars += len(event.text)
                    self._chars += len(event.text)
                    yield event
                else:
                    yield StreamTick()
                if self._abort is not None:
                    return None                                  # 外層在處理上一個事件時中止了（帳本與結果已由 abort 處理）
        except LLMTransientError as error:
            return self._fail(_transient_failure(error))
        except LLMBlockedError as error:
            return self._fail(_Failure(
                FAILURE_BLOCKED, STATUS_BLOCKED, f"blocked {error.phase}", False,
                finish_reason=FINISH_PROMPT_BLOCKED if error.phase == BLOCKED_INPUT else FINISH_SAFETY,
                blocked_phase=error.phase, error_type=type(error).__name__,
            ))
        except LLMConfigError as error:
            return self._fail(_Failure(
                FAILURE_CONFIG, STATUS_CONFIG_ERROR, f"config_error {error.http_status}" if error.http_status else "config_error",
                False, error_type=type(error).__name__, provider_status=error.http_status,
            ))

        if self._abort is not None:
            return None
        if end is None:                                          # 迭代自行結束卻沒有結束事件：連線中途斷掉
            return self._fail(_transient_failure(LLMTransientError(TRANSIENT_NETWORK)))
        if attempt.chars == 0:                                   # 完整結束卻沒有任何正文：不是成功的空劇情
            return self._fail(_Failure(FAILURE_TRANSIENT, STATUS_SERVER_ERROR, "empty_response", True))
        self._close_handle()
        entry = self._record_attempt(STATUS_OK, finish_reason=end.finish_reason, error_detail=None, end=end)
        self.outcome = self._build_outcome(entry)
        return None

    # ---- 帳本與結果 ------------------------------------------------------------------------------

    def _close_handle(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            handle.close()                                       # type: ignore[attr-defined]

    def _fail(self, failure: _Failure) -> _Failure:
        self._close_handle()
        self._record_attempt(
            failure.status, finish_reason=failure.finish_reason, error_detail=failure.detail, error_type=failure.error_type,
            provider_status=failure.provider_status,
        )
        return failure

    def _record_attempt(
        self,
        status: str,
        *,
        finish_reason: str | None,
        error_detail: str | None,
        end: StreamEnd | None = None,
        error_type: str | None = None,
        provider_status: int | None = None,
    ) -> StreamLedgerEntry | None:
        attempt, self._attempt = self._attempt, None             # 先清掉：即使回呼拋出例外也不會重複記錄
        if attempt is None:
            return None
        runner = self._runner
        prompt, completion = (end.prompt_tokens, end.completion_tokens) if end is not None else (None, None)
        thinking = end.thinking_tokens if end is not None else None
        if prompt is None and completion is None and attempt.chars > 0:
            prompt, completion = self._context_chars, attempt.chars            # 沒有用量：以字數估算
        entry = StreamLedgerEntry(
            user_id=runner._user_id, conversation_id=runner._conversation_id, kind=runner._kind,
            provider=getattr(runner._provider, "name", "unknown"), model=attempt.model, attempt=attempt.number, status=status,
            latency_ms=round((runner._monotonic() - attempt.began) * 1000), first_chunk_ms=attempt.first_chunk_ms,
            prompt_tokens=prompt, completion_tokens=completion, thinking_tokens=thinking, finish_reason=finish_reason,
            error_detail=error_detail,
        )
        log_call(
            user_id=entry.user_id, conversation_id=entry.conversation_id, kind=entry.kind, provider=entry.provider,
            model=entry.model, attempt=entry.attempt, outcome=status, latency_ms=entry.latency_ms,
            prompt_tokens=prompt, completion_tokens=completion, thinking_tokens=thinking, finish_reason=finish_reason,
            error_type=error_type, reason=error_detail, provider_status=provider_status,
        )
        if status in BREAKER_STATUSES and attempt.model != runner._settings.fallback_model:
            log_breaker_opened(model=attempt.model, outcome=status)
        runner._record(entry)
        return entry

    def _build_outcome(
        self,
        entry: StreamLedgerEntry | None,
        *,
        failure: str | None = None,
        blocked_phase: str | None = None,
        aborted: str | None = None,
        abort_category: str | None = None,
    ) -> StreamOutcome:
        return StreamOutcome(
            ok=failure is None and aborted is None, model=self._model, attempts=self._attempts, text_started=self._text_started,
            chars=self._chars, failure=failure, blocked_phase=blocked_phase, aborted=aborted, abort_category=abort_category,
            finish_reason=entry.finish_reason if entry else None, first_chunk_ms=entry.first_chunk_ms if entry else None,
            prompt_tokens=entry.prompt_tokens if entry else None, completion_tokens=entry.completion_tokens if entry else None,
            thinking_tokens=entry.thinking_tokens if entry else None,
        )


# ---- 與帳本 Repository 的接線（外層在自己的短交易內使用）-----------------------------------------------------


def write_ledger_entry(ledger: LlmCallLogRepository, entry: StreamLedgerEntry, created_at: datetime) -> None:
    """把一筆串流帳本內容寫進 `llm_call_logs`（`counted` 一律為假：RPG 以遊戲幣計費，不使用每日對話輪數）。只 flush，由呼叫端提交。"""
    ledger.add(
        user_id=entry.user_id, conversation_id=entry.conversation_id, kind=entry.kind, provider=entry.provider,
        model=entry.model, attempt=entry.attempt, status=entry.status, counted=False, latency_ms=entry.latency_ms,
        created_at=created_at, prompt_tokens=entry.prompt_tokens, completion_tokens=entry.completion_tokens,
        thinking_tokens=entry.thinking_tokens, finish_reason=entry.finish_reason, error_detail=entry.error_detail,
        first_chunk_ms=entry.first_chunk_ms,
    )


def ledger_recorder(ledger: LlmCallLogRepository, clock: Callable[[], datetime]) -> Callable[[StreamLedgerEntry], None]:
    """立刻把串流的帳本內容寫進 `llm_call_logs`（呼叫端提供的 Repository 所屬的工作階段由呼叫端提交）。"""

    def record(entry: StreamLedgerEntry) -> None:
        write_ledger_entry(ledger, entry, clock())

    return record


def ledger_breaker(
    ledger: LlmCallLogRepository, clock: Callable[[], datetime], breaker_seconds: float
) -> Callable[[str], bool]:
    """斷路查詢：沿用結構化呼叫的帳本查詢（這個模型近期是否逾時或伺服器錯誤），不放任何行程內狀態。"""

    def breaker_open(model: str) -> bool:
        return ledger.model_unhealthy_since(model, clock() - timedelta(seconds=breaker_seconds))

    return breaker_open
