"""營運告警的評估、狀態轉換與電子郵件通知（見 operational-alerts 規格與 ops-alerting 的 design D1、D3、D7、D8）。

流程（每項告警各自獨立，一項失敗不影響其餘）：
1. **評估**：查資料庫（唯讀）、套用 `alert_rules` 的純函式，得到「正常／警告／嚴重」或「樣本不足，維持原狀」。
2. **決定動作**：依上一次保存的狀態（`alert_states`）決定進入、升級、提醒、恢復、靜默降級或什麼都不做。
3. **搶佔再寄信**：以條件式 UPDATE 寫入新狀態並提交，**只有搶到的行程負責寄信**（兩個檢查行程重疊時不會各寄一封）。
4. **寄信**：每位收件人各寄一封。至少一位成功就維持已通知（避免已收到的人重複收到）；全部失敗則還原成搶佔之前的狀態，
   下次檢查再試。任何一位失敗都記一筆只含告警代碼的事件，並使指令以非零代碼結束。
已知的取捨：行程在「搶佔之後、寄信之前」被殺掉，這一封會遺失（至多一次）；6 小時的提醒是安全網。

預覽模式（`dry_run`）與沒有設定收件人時都是**唯讀**：只評估並回報「如果現在正式執行會做什麼」，不寄信、不建立或改變狀態、
不記錄告警事件（沒有收件人時，之後設定了收件人，已成立的告警才會被正常通知，而不是因為狀態早已更新而沉默）。
信件與事件都不含任何個資：函式的參數裡根本沒有（見 `alert_messages`、`alert_events`）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Sequence

from app.config import UsageSettings
from app.models.alert_state import ALERT_KEYS, STATE_OK, AlertState
from app.models.llm_call_log import STATUS_CONFIG_ERROR
from app.observability import alert_events
from app.repositories.alert_state_repository import AlertStateRepository
from app.repositories.llm_call_log_repository import LlmCallLogRepository
from app.repositories.user_repository import UserRepository
from app.services import alert_rules as rules
from app.services.alert_messages import alert_mail, event_value
from app.services.alert_rules import Evaluation, PreviousState
from app.services.mail_sender import MailSender
from app.services.quota_policy import taipei_day_bounds

# 結果（供指令顯示與測試斷言）
OUTCOME_PREVIEW = "preview"                  # 唯讀：只評估
OUTCOME_UNCHANGED = "unchanged"              # 沒有狀態變化（只更新最近數值）
OUTCOME_QUIET = "quiet"                      # 狀態更新但不寄信（靜默降級）
OUTCOME_NOTIFIED = "notified"                # 已通知
OUTCOME_DELIVERY_FAILED = "delivery_failed"  # 通知沒有寄出（或部分收件人失敗）
OUTCOME_CHECK_FAILED = "check_failed"        # 評估失敗
OUTCOME_LOST_RACE = "lost_race"              # 另一個行程搶先處理了這項告警


@dataclass(frozen=True)
class AlertResult:
    key: str
    outcome: str
    evaluation: Evaluation | None = None
    action: str = rules.ACTION_NONE          # 實際執行（或預覽時「會」執行）的動作
    sent: int = 0                            # 成功寄出的信件數
    failed_recipients: int = 0


@dataclass(frozen=True)
class AlertReport:
    results: tuple[AlertResult, ...]
    dry_run: bool
    recipients_configured: bool

    @property
    def read_only(self) -> bool:
        return self.dry_run or not self.recipients_configured

    @property
    def failed(self) -> list[str]:
        """評估失敗或通知（部分）失敗的告警代碼。"""
        return [r.key for r in self.results if r.outcome in (OUTCOME_CHECK_FAILED, OUTCOME_DELIVERY_FAILED)]


class AlertService:
    def __init__(
        self,
        *,
        states: AlertStateRepository,
        ledger: LlmCallLogRepository,
        users: UserRepository,
        usage_settings: UsageSettings,
        recipients: Sequence[str],
        mail_sender: MailSender | None,
        clock: Callable[[], datetime],
        commit: Callable[[], None],
        rollback: Callable[[], None],
    ) -> None:
        if recipients and mail_sender is None:
            raise ValueError("設定了告警收件人，卻沒有寄信器。")
        self._states = states
        self._ledger = ledger
        self._users = users
        self._usage = usage_settings
        self._recipients = tuple(recipients)
        self._sender = mail_sender
        self._clock = clock
        self._commit = commit
        self._rollback = rollback
        # 順序即評估與通知的順序
        self._evaluators = (
            (ALERT_KEYS[0], self._usage_budget),
            (ALERT_KEYS[1], self._error_rate),
            (ALERT_KEYS[2], self._config_errors),
            (ALERT_KEYS[3], self._latency),
            (ALERT_KEYS[4], self._lockouts),
        )

    # ---- 入口 --------------------------------------------------------------------------

    def run(self, *, dry_run: bool = False) -> AlertReport:
        now = self._clock()
        read_only = dry_run or not self._recipients
        previous_by_key = self._states.get_all() if read_only else {}
        results: list[AlertResult] = []
        for key, evaluate in self._evaluators:
            try:
                evaluation = evaluate(now)
            except Exception:  # noqa: BLE001 - 一項評估失敗不影響其餘；只記錄告警代碼，不記錄例外內容
                self._rollback()
                if not dry_run:
                    alert_events.log_check_failed(alert=key)
                results.append(AlertResult(key, OUTCOME_CHECK_FAILED))
                continue
            if read_only:
                action = rules.decide(_previous_of(previous_by_key.get(key)), evaluation, now)
                results.append(AlertResult(key, OUTCOME_PREVIEW, evaluation, action))
                continue
            try:
                results.append(self._apply(key, evaluation, now))
            except Exception:  # noqa: BLE001 - 狀態的讀寫失敗：回復並照樣隔離
                self._rollback()
                alert_events.log_check_failed(alert=key)
                results.append(AlertResult(key, OUTCOME_CHECK_FAILED, evaluation))
        if not read_only and all(r.outcome != OUTCOME_CHECK_FAILED for r in results):
            alert_events.log_check_completed()  # 心跳：平台以它偵測「告警檢查停擺」（預覽、唯讀與有評估失敗的執行都不記）
        return AlertReport(tuple(results), dry_run, bool(self._recipients))

    # ---- 狀態轉換與通知 ----------------------------------------------------------------------

    def _apply(self, key: str, evaluation: Evaluation, now: datetime) -> AlertResult:
        row = self._states.ensure(key, now)
        previous = _previous_of(row)
        assert previous is not None
        action = rules.decide(previous, evaluation, now)
        values = rules.next_state(action, evaluation, now)
        if values is None:
            self._states.touch(key, value=evaluation.value, now=now)
            self._commit()
            return AlertResult(key, OUTCOME_UNCHANGED, evaluation, action)

        claimed = self._states.claim(
            key,
            expected_state=previous.state,
            expected_severity=previous.severity,
            expected_notified_at=previous.last_notified_at,
            state=values.state,
            severity=values.severity,
            changed_at=values.changed_at,
            notified_at=values.last_notified_at,
            value=evaluation.value,
            now=now,
        )
        self._commit()
        if not claimed:
            return AlertResult(key, OUTCOME_LOST_RACE, evaluation, action)
        if action not in rules.NOTIFYING_ACTIONS:
            return AlertResult(key, OUTCOME_QUIET, evaluation, action)

        sent, failed = self._deliver(key, action, evaluation, previous, now)
        if sent == 0:
            # 全部失敗：還原成搶佔之前的狀態，下次檢查會再嘗試
            self._states.restore(
                key, claimed_at=now, state=previous.state, severity=previous.severity,
                changed_at=previous.changed_at, notified_at=previous.last_notified_at, now=now,
            )
            self._commit()
        if failed:
            alert_events.log_delivery_failed(alert=key)
        if sent:
            number = event_value(key, evaluation.value)
            if action == rules.ACTION_RESOLVE:
                alert_events.log_resolved(alert=key, value=number)
            else:
                assert evaluation.severity is not None
                alert_events.log_firing(alert=key, severity=evaluation.severity, value=number)
        outcome = OUTCOME_DELIVERY_FAILED if failed else OUTCOME_NOTIFIED
        return AlertResult(key, outcome, evaluation, action, sent=sent, failed_recipients=failed)

    def _deliver(self, key: str, action: str, evaluation: Evaluation, previous: PreviousState, now: datetime) -> tuple[int, int]:
        """每位收件人各寄一封；回傳（成功數，失敗數）。例外訊息與收件人地址都不記錄。"""
        assert self._sender is not None
        content = alert_mail(
            alert=key, kind=action, severity=evaluation.severity, value=evaluation.value, threshold=evaluation.threshold,
            since=now if action == rules.ACTION_FIRING else previous.changed_at, now=now,
        )
        sent = failed = 0
        for address in self._recipients:
            try:
                self._sender.send(to=address, subject=content.subject, body=content.body)
                sent += 1
            except Exception:  # noqa: BLE001
                failed += 1
        return sent, failed

    # ---- 五項評估（唯讀查詢）-----------------------------------------------------------------

    def _usage_budget(self, now: datetime) -> Evaluation:
        start, end = taipei_day_bounds(now)
        tokens, calls = self._ledger.usage_totals(start=start, end=end)
        return rules.evaluate_usage_budget(
            tokens_used=tokens, token_limit=self._usage.daily_token_limit, calls_used=calls, call_limit=self._usage.daily_call_limit
        )

    def _error_rate(self, now: datetime) -> Evaluation:
        return rules.evaluate_error_rate(self._ledger.chat_status_counts(start=now - rules.ERROR_WINDOW, end=now))

    def _config_errors(self, now: datetime) -> Evaluation:
        return rules.evaluate_config_errors(
            self._ledger.status_count(STATUS_CONFIG_ERROR, start=now - rules.CONFIG_ERROR_WINDOW, end=now)
        )

    def _latency(self, now: datetime) -> Evaluation:
        start = now - rules.LATENCY_WINDOW
        count = self._ledger.completed_chat_call_count(start=start, end=now)
        if count < rules.LATENCY_MIN_SAMPLE:
            return rules.evaluate_latency(sample_count=count, p95_ms=None)
        p95 = self._ledger.completed_chat_latency_at(rules.percentile_offset(count), start=start, end=now)
        return rules.evaluate_latency(sample_count=count, p95_ms=p95)

    def _lockouts(self, now: datetime) -> Evaluation:
        return rules.evaluate_lockouts(self._users.count_locked(now))


def _previous_of(row: AlertState | None) -> PreviousState | None:
    if row is None:
        return None
    return PreviousState(row.state, row.severity if row.state != STATE_OK else None, row.last_notified_at, row.changed_at)
