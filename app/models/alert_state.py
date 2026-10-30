"""營運告警的狀態（見 operational-alerts 規格與 ops-alerting 的 design D2、D3）。

固定的五項告警各一列（`ALERT_KEYS`），保存「現在是正常還是告警中、嚴重度、何時開始、最近一次通知的時間與最近的數值」，
讓每次檢查都能判斷「這是進入、升級、提醒或恢復，還是什麼都不必做」，不會隨檢查頻率重複寄信。

- 不指向使用者或對話，也不含任何個資：只有告警代碼、狀態與數字。所以沒有外鍵、不需要保存期限清除，
  也不在抹除流程的登記清單內（`tests/architecture/test_account_erasure_coverage.py` 只看指向使用者與對話的欄位）。
- `alert_key` 唯一：兩個檢查行程同時建立同一列時，唯一約束讓其中一個失敗並重新讀取。
- 狀態轉換靠條件式 UPDATE 搶佔（`AlertStateRepository.claim`），比對的欄位是 `state`、`severity`、`last_notified_at`。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Float, Integer, Unicode
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime, utcnow

# 告警代碼（固定列舉）。日誌事件與信件也用同一組代碼。
ALERT_USAGE_BUDGET = "usage_budget"
ALERT_LLM_ERROR_RATE = "llm_error_rate"
ALERT_LLM_CONFIG_ERROR = "llm_config_error"
ALERT_LLM_LATENCY_P95 = "llm_latency_p95"
ALERT_LOGIN_LOCKOUTS = "login_lockouts"
ALERT_KEYS = (
    ALERT_USAGE_BUDGET, ALERT_LLM_ERROR_RATE, ALERT_LLM_CONFIG_ERROR, ALERT_LLM_LATENCY_P95, ALERT_LOGIN_LOCKOUTS,
)

STATE_OK = "ok"
STATE_FIRING = "firing"
ALERT_STATES = (STATE_OK, STATE_FIRING)

SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"
ALERT_SEVERITIES = (SEVERITY_WARNING, SEVERITY_CRITICAL)


class AlertState(Base):
    __tablename__ = "alert_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alert_key: Mapped[str] = mapped_column(Unicode(40), unique=True)
    state: Mapped[str] = mapped_column(Unicode(10), default=STATE_OK)
    severity: Mapped[str | None] = mapped_column(Unicode(10), nullable=True)       # 只有告警中才有值
    changed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)      # 目前這個狀態是何時開始的
    last_notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_value: Mapped[float | None] = mapped_column(Float, nullable=True)         # 最近一次評估的數值（比例、毫秒或筆數）
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
