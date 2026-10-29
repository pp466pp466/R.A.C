"""各等級的限制與台灣日界（cs-usage-limits 規格：各會員等級的限制）。"""
from datetime import datetime, timedelta, timezone

import pytest

from app.models.user import MEMBER_TIERS
from app.services.chat_service import MAX_MESSAGE_CHARS
from app.services.quota_policy import (
    RATE_WINDOW, TIER_LIMITS, TierLimits, limits_for, taipei_day_bounds, tier_facts,
)

UTC = timezone.utc


@pytest.mark.parametrize(
    ("tier", "daily", "per_minute", "conversations"),
    [("free", 30, 6, 20), ("plus", 100, 10, 100), ("vip", 300, 20, 500)],
)
def test_each_tier_has_the_limits_from_the_specification(tier, daily, per_minute, conversations):
    assert limits_for(tier) == TierLimits(daily_turns=daily, per_minute=per_minute, conversations=conversations)


def test_every_member_tier_has_limits_and_nothing_else_does():
    assert set(TIER_LIMITS) == set(MEMBER_TIERS)


@pytest.mark.parametrize("tier", ["gold", "", None, 7, "FREE"])
def test_an_unknown_tier_gets_the_most_conservative_limits(tier):
    assert limits_for(tier) == TIER_LIMITS["free"]


def test_higher_tiers_never_get_less_than_lower_ones():
    free, plus, vip = (limits_for(t) for t in ("free", "plus", "vip"))
    for field in ("daily_turns", "per_minute", "conversations"):
        assert getattr(free, field) < getattr(plus, field) < getattr(vip, field)


def test_the_rate_window_is_one_minute():
    assert RATE_WINDOW == timedelta(seconds=60)


def test_tier_facts_state_every_number_of_the_table():
    text = tier_facts()
    for tier, limits in TIER_LIMITS.items():
        for number in (limits.daily_turns, limits.per_minute, limits.conversations):
            assert f"{tier} {number}" in text
    assert f"{MAX_MESSAGE_CHARS} 字" in text                    # 單則訊息上限與聊天輸入驗證用的是同一個數字


# ---- 台灣日界 ------------------------------------------------------------------------------

def test_the_taipei_day_starts_at_1600_utc_of_the_previous_day():
    start, end = taipei_day_bounds(datetime(2026, 10, 29, 3, 0, tzinfo=UTC))        # 台灣 11:00
    assert start == datetime(2026, 10, 28, 16, 0, tzinfo=UTC)
    assert end == datetime(2026, 10, 29, 16, 0, tzinfo=UTC)


def test_one_second_before_taipei_midnight_is_still_the_same_day():
    start, end = taipei_day_bounds(datetime(2026, 10, 29, 15, 59, 59, tzinfo=UTC))
    assert (start, end) == (datetime(2026, 10, 28, 16, 0, tzinfo=UTC), datetime(2026, 10, 29, 16, 0, tzinfo=UTC))


def test_taipei_midnight_starts_a_new_day():
    start, end = taipei_day_bounds(datetime(2026, 10, 29, 16, 0, tzinfo=UTC))
    assert (start, end) == (datetime(2026, 10, 29, 16, 0, tzinfo=UTC), datetime(2026, 10, 30, 16, 0, tzinfo=UTC))


def test_a_naive_time_is_treated_as_utc():
    assert taipei_day_bounds(datetime(2026, 10, 29, 3, 0)) == taipei_day_bounds(datetime(2026, 10, 29, 3, 0, tzinfo=UTC))


def test_bounds_are_always_exactly_one_day_apart_and_contain_now():
    now = datetime(2027, 3, 14, 9, 26, 53, tzinfo=UTC)
    start, end = taipei_day_bounds(now)
    assert end - start == timedelta(days=1)
    assert start <= now < end
