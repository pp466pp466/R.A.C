"""cs-usage-limits、global-usage-budget、cs-safety-suspension 規格：用量檢查的每個邊界。

用假時鐘與真的資料庫（記憶體 SQLite，標記 sqlserver 的也在 SQL Server 上跑）。計數都從帳本與訊息表導出，
所以測試直接寫入那些資料列，而不是呼叫模型。
"""
import itertools
from datetime import datetime, timedelta, timezone

import pytest

from app.config import UsageSettings
from app.models import ChatMessage, LlmCallLog
from app.observability import usage_events
from app.repositories.chat_repository import ChatRepository
from app.repositories.llm_call_log_repository import LlmCallLogRepository
from app.services.current_user import CurrentUser
from app.services.errors import (
    ChatSuspended, ConversationLimitReached, DailyQuotaExceeded, RateLimited, SystemBusy,
)
from app.services.usage_service import UsageService
from tests.support.auth import FakeClock, add_user

NOW = datetime(2026, 10, 29, 4, 0, 0, tzinfo=timezone.utc)                    # 台灣 12:00
DAY_START = datetime(2026, 10, 28, 16, 0, tzinfo=timezone.utc)
DAY_END = datetime(2026, 10, 29, 16, 0, tzinfo=timezone.utc)
STATE = {"member_tier": "free", "last_intent": None, "pending_entities": {}, "open_issue": None}
TTL = timedelta(seconds=60)


class Env:
    def __init__(self, session, *, settings=UsageSettings()):
        self.session = session
        self.clock = FakeClock(NOW)
        self.chats = ChatRepository(session)
        self.ledger = LlmCallLogRepository(session)
        self.service = UsageService(ledger=self.ledger, chats=self.chats, settings=settings, clock=self.clock, processing_ttl=TTL)
        self._ids = itertools.count(1)

    def user(self, n=1, tier="free") -> CurrentUser:
        return CurrentUser.from_model(add_user(self.session, n, age=30, member_tier=tier))

    def conversation(self, user):
        return self.chats.create_conversation(user.id, client_id=f"{next(self._ids):036d}", state=STATE, now=NOW)

    def say(self, user, conversation, seconds_ago, *, role="user"):
        n = next(self._ids)
        self.chats.append_message(
            user.id, conversation, client_msg_id=f"{n:036d}", role=role, content="你好", now=NOW - timedelta(seconds=seconds_ago)
        )
        self.session.commit()

    def call(self, user, *, at=NOW, kind="chat", status="ok", counted=False, **tokens):
        self.ledger.add(
            user_id=None if user is None else user.id, conversation_id=None, kind=kind, provider="fake", model="m", attempt=1,
            status=status, counted=counted, latency_ms=10, created_at=at, **tokens,
        )
        self.session.commit()

    def turns(self, user, count, *, at=NOW):
        for _ in range(count):
            self.call(user, counted=True, at=at)

    def blocks(self, user, *ats, kind="chat"):
        for at in ats:
            self.call(user, status="blocked", kind=kind, at=at)


@pytest.fixture()
def env(member_session):
    return Env(member_session)


def send(env, user, conversation, **kwargs):
    env.service.check_send(user, conversation_id=conversation.id, **kwargs)


# ---- 每分鐘訊息數 ---------------------------------------------------------------------------

def test_a_free_user_may_send_up_to_the_per_minute_limit(env):
    user = env.user()
    conversation = env.conversation(user)
    for seconds_ago in (50, 40, 30, 20, 10):
        env.say(user, conversation, seconds_ago)
    send(env, user, conversation)                                       # 第 6 則：前面只有 5 則


def test_the_seventh_message_in_a_minute_is_rate_limited_with_the_time_to_wait(env):
    user = env.user()
    conversation = env.conversation(user)
    for seconds_ago in (55, 40, 30, 20, 10, 5):
        env.say(user, conversation, seconds_ago)
    with pytest.raises(RateLimited) as exc:
        send(env, user, conversation)
    assert exc.value.code == "rate_limited" and exc.value.message == "您傳送訊息的速度太快了，請稍候幾秒再試。"
    assert exc.value.retry_after == 5                                   # 最早那則（55 秒前）再過 5 秒離開視窗


def test_the_window_slides(env):
    user = env.user()
    conversation = env.conversation(user)
    for seconds_ago in (70, 65, 61, 20, 10, 5):                         # 前三則已超過 60 秒
        env.say(user, conversation, seconds_ago)
    send(env, user, conversation)


def test_messages_in_different_conversations_all_count(env):
    user = env.user()
    first, second = env.conversation(user), env.conversation(user)
    for index, seconds_ago in enumerate((50, 40, 30, 20, 10, 5)):
        env.say(user, first if index % 2 else second, seconds_ago)
    with pytest.raises(RateLimited):
        send(env, user, first)


def test_assistant_messages_and_other_users_do_not_count(env):
    user, other = env.user(1), env.user(2)
    mine, theirs = env.conversation(user), env.conversation(other)
    for seconds_ago in (50, 40, 30, 20, 10, 5):
        env.say(user, mine, seconds_ago, role="assistant")
        env.say(other, theirs, seconds_ago)
    send(env, user, mine)
    with pytest.raises(RateLimited):
        send(env, other, theirs)


@pytest.mark.parametrize(("tier", "limit"), [("free", 6), ("plus", 10), ("vip", 20)])
def test_the_per_minute_limit_follows_the_tier(env, tier, limit):
    user = env.user(tier=tier)
    conversation = env.conversation(user)
    for index in range(limit - 1):
        env.say(user, conversation, 30 - index * 0.5)
    send(env, user, conversation)
    env.say(user, conversation, 1)
    with pytest.raises(RateLimited):
        send(env, user, conversation)


def test_a_message_that_was_already_accepted_skips_the_per_minute_limit(env):
    user = env.user()
    conversation = env.conversation(user)
    for seconds_ago in (50, 40, 30, 20, 10, 5):
        env.say(user, conversation, seconds_ago)
    send(env, user, conversation, already_accepted=True)


def test_the_retry_after_is_never_below_one_second(env):
    user = env.user()
    conversation = env.conversation(user)
    for seconds_ago in (59.9, 40, 30, 20, 10, 5):
        env.say(user, conversation, seconds_ago)
    with pytest.raises(RateLimited) as exc:
        send(env, user, conversation)
    assert exc.value.retry_after == 1


# ---- 每日對話輪數 ---------------------------------------------------------------------------

def test_a_free_user_has_exactly_thirty_turns_a_day(env):
    user = env.user()
    conversation = env.conversation(user)
    env.turns(user, 29)
    send(env, user, conversation)
    env.turns(user, 1)
    with pytest.raises(DailyQuotaExceeded) as exc:
        send(env, user, conversation)
    assert exc.value.code == "daily_quota_exceeded"
    assert exc.value.message == "您今日的對話額度已用完，將於明日 00:00（台灣時間）重置。"
    assert exc.value.retry_after == 12 * 3600                           # 現在是台灣 12:00，距離明日 00:00 十二小時


@pytest.mark.parametrize(("tier", "limit"), [("free", 30), ("plus", 100), ("vip", 300)])
def test_the_daily_limit_follows_the_tier(env, tier, limit):
    user = env.user(tier=tier)
    conversation = env.conversation(user)
    env.turns(user, limit - 1)
    send(env, user, conversation)
    env.turns(user, 1)
    with pytest.raises(DailyQuotaExceeded):
        send(env, user, conversation)


def test_degraded_replies_and_summaries_do_not_use_up_the_quota(env):
    user = env.user()
    conversation = env.conversation(user)
    env.turns(user, 29)
    for _ in range(5):
        env.call(user, counted=False, status="timeout")                 # 失敗的嘗試與降級回覆
    env.call(user, kind="summary", counted=True)                         # 摘要
    send(env, user, conversation)


def test_yesterdays_turns_do_not_count_and_the_taipei_boundary_is_exact(env):
    user = env.user()
    conversation = env.conversation(user)
    env.turns(user, 30, at=DAY_START - timedelta(seconds=1))            # 昨天最後一秒
    send(env, user, conversation)
    env.turns(user, 30, at=DAY_START)                                    # 台灣 00:00 整點屬於今天
    with pytest.raises(DailyQuotaExceeded):
        send(env, user, conversation)


def test_other_users_turns_do_not_count(env):
    user, other = env.user(1), env.user(2)
    conversation = env.conversation(user)
    env.turns(other, 30)
    send(env, user, conversation)


def test_a_message_being_processed_in_another_conversation_reserves_a_slot(env):
    user = env.user()
    mine, other = env.conversation(user), env.conversation(user)
    env.turns(user, 29)
    assert env.chats.acquire_processing(user.id, other, now=NOW - timedelta(seconds=10), stale_before=NOW - timedelta(seconds=60))
    env.session.commit()
    with pytest.raises(DailyQuotaExceeded):
        send(env, user, mine)


def test_the_conversation_being_sent_to_is_not_counted_as_in_flight(env):
    user = env.user()
    conversation = env.conversation(user)
    env.turns(user, 29)
    assert env.chats.acquire_processing(user.id, conversation, now=NOW - timedelta(seconds=10), stale_before=NOW - timedelta(seconds=60))
    env.session.commit()
    send(env, user, conversation)                                        # 自己的標記不該讓自己被擋（忙碌另有檢查）


def test_a_stale_processing_marker_reserves_nothing(env):
    user = env.user()
    mine, other = env.conversation(user), env.conversation(user)
    env.turns(user, 29)
    assert env.chats.acquire_processing(user.id, other, now=NOW - timedelta(seconds=61), stale_before=NOW - timedelta(seconds=120))
    env.session.commit()
    send(env, user, mine)


def test_the_daily_limit_still_applies_to_a_message_that_was_already_accepted(env):
    user = env.user()
    conversation = env.conversation(user)
    env.turns(user, 30)
    with pytest.raises(DailyQuotaExceeded):
        send(env, user, conversation, already_accepted=True)


# ---- 全站用量 -------------------------------------------------------------------------------

def budget_env(session, *, tokens=1000, calls=100):
    return Env(session, settings=UsageSettings(daily_token_limit=tokens, daily_call_limit=calls))


def test_the_site_is_busy_once_the_token_limit_is_reached(member_session, log_lines):
    env = budget_env(member_session)
    user = env.user()
    conversation = env.conversation(user)
    env.call(None, prompt_tokens=600, completion_tokens=399)
    send(env, user, conversation)                                        # 999 < 1000
    env.call(None, prompt_tokens=1)
    with pytest.raises(SystemBusy) as exc:
        send(env, user, conversation)
    assert exc.value.code == "system_busy" and exc.value.message == "目前使用人數較多，請稍後再試。"
    denied = [line for line in log_lines() if line.get("event") == usage_events.EVENT_DENIED]
    assert [(line["user_id"], line["reason"]) for line in denied] == [(user.id, "global_budget")]


def test_the_site_is_busy_once_the_call_limit_is_reached_even_with_few_tokens(member_session):
    env = budget_env(member_session, calls=5)
    user = env.user()
    conversation = env.conversation(user)
    for _ in range(4):
        env.call(None)
    send(env, user, conversation)
    env.call(None)
    with pytest.raises(SystemBusy):
        send(env, user, conversation)


def test_summaries_failures_and_anonymised_rows_count_toward_the_site_total(member_session):
    env = budget_env(member_session, tokens=1000)
    user = env.user()
    conversation = env.conversation(user)
    env.call(None, kind="summary", prompt_tokens=400)
    env.call(user, status="timeout", prompt_tokens=300)
    env.call(None, completion_tokens=200, thinking_tokens=100)
    with pytest.raises(SystemBusy):
        send(env, user, conversation)


def test_yesterdays_site_usage_does_not_count(member_session):
    env = budget_env(member_session, tokens=1000, calls=1)
    user = env.user()
    conversation = env.conversation(user)
    env.call(None, prompt_tokens=5000, at=DAY_START - timedelta(seconds=1))
    send(env, user, conversation)


def test_crossing_eighty_percent_logs_a_warning_but_does_not_block(member_session, log_lines):
    env = budget_env(member_session, tokens=1000, calls=100)
    user = env.user()
    conversation = env.conversation(user)
    env.call(None, prompt_tokens=850)
    send(env, user, conversation)
    [warning] = [line for line in log_lines() if line.get("event") == usage_events.EVENT_BUDGET_WARNING]
    assert (warning["reason"], warning["used"], warning["limit"], warning["level"]) == ("tokens", 850, 1000, "WARNING")


def test_the_call_count_can_raise_its_own_warning(member_session, log_lines):
    env = budget_env(member_session, tokens=10_000_000, calls=10)
    user = env.user()
    conversation = env.conversation(user)
    for _ in range(8):
        env.call(None)
    send(env, user, conversation)
    [warning] = [line for line in log_lines() if line.get("event") == usage_events.EVENT_BUDGET_WARNING]
    assert (warning["reason"], warning["used"], warning["limit"]) == ("calls", 8, 10)


def test_below_eighty_percent_logs_nothing(member_session, log_lines):
    env = budget_env(member_session, tokens=1000, calls=100)
    user = env.user()
    conversation = env.conversation(user)
    env.call(None, prompt_tokens=799)
    send(env, user, conversation)
    assert not [line for line in log_lines() if line.get("event") in (usage_events.EVENT_BUDGET_WARNING, usage_events.EVENT_DENIED)]


def test_exactly_eighty_percent_warns(member_session, log_lines):
    env = budget_env(member_session, tokens=1000)
    user = env.user()
    conversation = env.conversation(user)
    env.call(None, prompt_tokens=800)
    send(env, user, conversation)
    assert [line for line in log_lines() if line.get("event") == usage_events.EVENT_BUDGET_WARNING]


def test_a_blocked_request_does_not_also_log_the_warning(member_session, log_lines):
    env = budget_env(member_session, tokens=1000)
    user = env.user()
    conversation = env.conversation(user)
    env.call(None, prompt_tokens=1000)
    with pytest.raises(SystemBusy):
        send(env, user, conversation)
    assert not [line for line in log_lines() if line.get("event") == usage_events.EVENT_BUDGET_WARNING]


def test_global_budget_exhausted_is_a_plain_yes_or_no_without_logging(member_session, log_lines):
    env = budget_env(member_session, tokens=1000, calls=100)
    assert env.service.global_budget_exhausted() is False
    env.call(None, prompt_tokens=1000)
    assert env.service.global_budget_exhausted() is True
    assert log_lines() == []


def test_the_default_limits_leave_normal_use_alone(env):
    user = env.user()
    conversation = env.conversation(user)
    env.call(None, prompt_tokens=100_000, completion_tokens=20_000)
    send(env, user, conversation)


# ---- 安全過濾累計暫停 -----------------------------------------------------------------------

def test_two_blocks_do_not_suspend(env):
    user = env.user()
    conversation = env.conversation(user)
    env.blocks(user, NOW - timedelta(minutes=30), NOW - timedelta(minutes=5))
    assert env.service.suspended_until(user) is None
    send(env, user, conversation)


def test_the_third_block_suspends_for_one_hour_after_the_latest_one(env, log_lines):
    user = env.user()
    conversation = env.conversation(user)
    latest = NOW - timedelta(minutes=10)
    env.blocks(user, NOW - timedelta(hours=3), NOW - timedelta(hours=2), latest)
    assert env.service.suspended_until(user) == latest + timedelta(hours=1)
    with pytest.raises(ChatSuspended) as exc:
        send(env, user, conversation)
    assert exc.value.code == "chat_suspended" and exc.value.until == latest + timedelta(hours=1)
    assert "12:50（台灣時間）" in exc.value.message                          # 03:50 UTC = 台灣 11:50 被擋，+1 小時 = 台灣 12:50
    denied = [line for line in log_lines() if line.get("event") == usage_events.EVENT_DENIED]
    assert [line["reason"] for line in denied] == ["suspended"]


def test_the_suspension_ends_one_hour_after_the_latest_block(env):
    user = env.user()
    conversation = env.conversation(user)
    env.blocks(user, NOW - timedelta(hours=3), NOW - timedelta(hours=2), NOW - timedelta(minutes=59))
    with pytest.raises(ChatSuspended):
        send(env, user, conversation)
    env.clock.advance(minutes=1, seconds=1)
    send(env, user, conversation)


def test_a_new_block_after_the_suspension_suspends_again(env):
    user = env.user()
    conversation = env.conversation(user)
    env.blocks(user, NOW - timedelta(hours=4), NOW - timedelta(hours=3), NOW - timedelta(hours=2))
    send(env, user, conversation)                                        # 已經過了一小時：恢復
    env.blocks(user, NOW - timedelta(minutes=1))
    assert env.service.suspended_until(user) == NOW + timedelta(minutes=59)
    with pytest.raises(ChatSuspended):
        send(env, user, conversation)


def test_summary_blocks_and_other_users_blocks_are_not_counted(env):
    user, other = env.user(1), env.user(2)
    conversation = env.conversation(user)
    env.blocks(user, NOW - timedelta(minutes=3), NOW - timedelta(minutes=2))
    env.blocks(user, NOW - timedelta(minutes=1), kind="summary")
    env.blocks(other, NOW - timedelta(minutes=1), NOW - timedelta(minutes=2), NOW - timedelta(minutes=3))
    assert env.service.suspended_until(user) is None
    send(env, user, conversation)


def test_blocks_of_the_previous_taipei_day_do_not_count(env):
    user = env.user()
    conversation = env.conversation(user)
    env.blocks(user, DAY_START - timedelta(seconds=1), DAY_START - timedelta(minutes=5), DAY_START - timedelta(minutes=10))
    send(env, user, conversation)


def test_the_suspension_outranks_the_other_limits(env):
    user = env.user()
    conversation = env.conversation(user)
    env.blocks(user, *(NOW - timedelta(minutes=m) for m in (5, 4, 3)))
    env.turns(user, 30)
    for seconds_ago in (50, 40, 30, 20, 10, 5):
        env.say(user, conversation, seconds_ago)
    with pytest.raises(ChatSuspended):
        send(env, user, conversation)


def test_recording_a_block_logs_the_suspension_only_when_the_threshold_is_reached(env, log_lines):
    user = env.user()
    env.blocks(user, NOW - timedelta(minutes=2), NOW - timedelta(minutes=1))
    env.service.record_block_outcome(user)
    assert not [line for line in log_lines() if line.get("event") == usage_events.EVENT_SUSPENDED]
    env.blocks(user, NOW)
    env.service.record_block_outcome(user)
    [line] = [line for line in log_lines() if line.get("event") == usage_events.EVENT_SUSPENDED]
    assert line["user_id"] == user.id and line["reason"] == "safety_blocks"


# ---- 檢查順序與唯讀 -------------------------------------------------------------------------

def test_the_order_is_rate_then_daily_then_site(member_session):
    env = budget_env(member_session, calls=1)
    user = env.user()
    conversation = env.conversation(user)
    env.turns(user, 30)
    env.call(None)
    for seconds_ago in (50, 40, 30, 20, 10, 5):
        env.say(user, conversation, seconds_ago)
    with pytest.raises(RateLimited):
        send(env, user, conversation)
    env.clock.advance(minutes=2)
    with pytest.raises(DailyQuotaExceeded):
        send(env, user, conversation)
    other = env.user(2)
    with pytest.raises(SystemBusy):
        send(env, other, env.conversation(other))


def test_a_check_never_writes_anything(member_session):
    env = budget_env(member_session)
    user = env.user()
    conversation = env.conversation(user)
    env.session.commit()
    before = (member_session.query(ChatMessage).count(), member_session.query(LlmCallLog).count(), conversation.processing_since)
    send(env, user, conversation)
    env.service.snapshot(user)
    env.service.check_create_conversation(user)
    assert not member_session.new and not member_session.dirty
    assert (member_session.query(ChatMessage).count(), member_session.query(LlmCallLog).count(), conversation.processing_since) == before


# ---- 對話數上限與用量快照 ---------------------------------------------------------------------

@pytest.mark.parametrize(("tier", "limit"), [("free", 20), ("plus", 100), ("vip", 500)])
def test_the_conversation_limit_follows_the_tier(env, tier, limit):
    user = env.user(tier=tier)
    for _ in range(limit - 1):
        env.chats.create_conversation(user.id, client_id=f"{next(env._ids):036d}", state=STATE, now=NOW)
    env.service.check_create_conversation(user)
    env.chats.create_conversation(user.id, client_id=f"{next(env._ids):036d}", state=STATE, now=NOW)
    with pytest.raises(ConversationLimitReached) as exc:
        env.service.check_create_conversation(user)
    assert exc.value.code == "conversation_limit_reached" and exc.value.message == "對話數已達上限，請先刪除不需要的對話。"


def test_deleting_a_conversation_frees_a_slot_and_other_users_are_unaffected(env):
    user, other = env.user(1), env.user(2)
    made = [env.conversation(user) for _ in range(20)]
    with pytest.raises(ConversationLimitReached):
        env.service.check_create_conversation(user)
    env.service.check_create_conversation(other)
    assert env.chats.delete_conversation(user.id, made[0])
    env.service.check_create_conversation(user)


def test_denials_log_the_reason_for_the_conversation_limit(env, log_lines):
    user = env.user()
    for _ in range(20):
        env.conversation(user)
    with pytest.raises(ConversationLimitReached):
        env.service.check_create_conversation(user)
    [line] = [line for line in log_lines() if line.get("event") == usage_events.EVENT_DENIED]
    assert (line["user_id"], line["reason"]) == (user.id, "conversation_limit")


def test_the_snapshot_describes_only_the_users_own_usage(member_session):
    env = budget_env(member_session)
    user, other = env.user(1, "plus"), env.user(2)
    env.conversation(user), env.conversation(user), env.conversation(other)
    env.turns(user, 3)
    env.turns(other, 9)
    env.call(None, prompt_tokens=99_999)
    snapshot = env.service.snapshot(user)
    assert snapshot.to_dict() == {
        "tier": "plus",
        "daily": {"used": 3, "limit": 100, "resets_at": "2026-10-29T16:00:00+00:00"},
        "per_minute_limit": 10,
        "conversations": {"count": 2, "limit": 100},
        "suspended_until": None,
    }
    assert "99999" not in str(snapshot.to_dict()) and "token" not in str(snapshot.to_dict())


def test_the_snapshot_reports_the_suspension(env):
    user = env.user()
    env.blocks(user, *(NOW - timedelta(minutes=m) for m in (30, 20, 10)))
    assert env.service.snapshot(user).to_dict()["suspended_until"] == "2026-10-29T04:50:00+00:00"


def test_an_unknown_tier_is_treated_as_free(env):
    user = env.user()
    stranger = CurrentUser(**{**user.__dict__, "member_tier": "gold"})
    conversation = env.conversation(user)
    env.turns(user, 30)
    with pytest.raises(DailyQuotaExceeded):
        send(env, stranger, conversation)
