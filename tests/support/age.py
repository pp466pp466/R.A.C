"""年齡相關測試的共用輔助：以固定的「今天」產生 N 歲的生日、對應的年齡區間與資料庫欄位值。

「今天」與 `tests.support.auth.START`（2026-10-27 12:00 UTC，也就是台北 20:00）的台北日期一致；
`test_age_policy.py` 會驗證這個對應，所以時鐘預設值改變時這裡會立刻失敗。
這個模組不匯入 `tests.support.auth`（後者會匯入它）。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from app.services.age_policy import DEFAULT_AGE_POLICY, AgePolicy, range_for_age

TODAY = date(2026, 10, 27)
TODAY_UTC = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)     # = tests.support.auth.START


def birth_date_for_age(age: int, today: date = TODAY) -> date:
    """剛好 `age` 歲的人的生日（今天過生日）。若要「差一天才滿」請自行減一天。"""
    return date(today.year - age, today.month, today.day)


def age_fields(age: int = 30, *, policy: AgePolicy = DEFAULT_AGE_POLICY, today: date = TODAY, **overrides: Any) -> dict[str, Any]:
    """註冊與補填的請求欄位：ISO 日期字串與對應的年齡區間代碼。可用 overrides 覆蓋（例如造成不一致）。"""
    chosen = range_for_age(age, policy)
    assert chosen is not None, f"沒有涵蓋 {age} 歲的年齡區間"
    values: dict[str, Any] = {"birth_date": birth_date_for_age(age, today).isoformat(), "age_range": chosen.code}
    values.update(overrides)
    return values


def age_columns(age: int = 30, *, policy: AgePolicy = DEFAULT_AGE_POLICY, today: date = TODAY, confirmed_at: datetime = TODAY_UTC) -> dict[str, Any]:
    """直接寫入 `users` 的年齡欄位值（不經過 Service），代表「已完成年齡確認」的帳號。"""
    chosen = range_for_age(age, policy)
    assert chosen is not None
    return {
        "birth_date": birth_date_for_age(age, today),
        "age_range": chosen.code,
        "is_adult_at_confirmation": age >= policy.adult_age,
        "age_confirmed_at": confirmed_at,
        "age_policy_version": policy.version,
    }
