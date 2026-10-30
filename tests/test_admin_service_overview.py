"""admin-console 規格：只有管理員、總覽只有彙總資料、今日警告；admin-audit-log 規格：管理員可查看稽核。"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.config import UsageSettings
from app.models.user import MEMBER_TIERS, STATUS_SUSPENDED, USER_STATUSES
from app.services.errors import NotFound, ValidationError
from tests.support.admin import AdminKit

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)            # FakeClock 的起點，台灣 20:00
DAY_START = datetime(2026, 10, 26, 16, 0, tzinfo=timezone.utc)      # 台灣 10/27 00:00


@pytest.fixture()
def kit(member_session):
    return AdminKit(member_session)


# ---- 只有管理員 -----------------------------------------------------------------------------

@pytest.mark.parametrize("who", ["member", "vip", "suspended_admin"])
@pytest.mark.parametrize("method", ["overview", "audit_log", "feedback_conversations", "flagged_conversations", "failures"])
def test_non_admins_get_a_404_from_every_read(kit, who, method):
    if who == "member":
        user = kit.current(kit.member(2))
    elif who == "vip":
        user = kit.current(kit.member(2, member_tier="vip"))
    else:
        user = kit.current(kit.member(2, role="admin", status=STATUS_SUSPENDED))
    with pytest.raises(NotFound):
        getattr(kit.service, method)(user)
    assert kit.audit_rows() == []


# ---- 總覽 -----------------------------------------------------------------------------------

def test_the_overview_has_the_documented_sections(kit):
    overview = kit.service.overview(kit.admin())
    assert set(overview) == {"users", "usage", "warnings", "ages"}
    assert set(overview["users"]) == {"total", "by_status", "by_tier"}
    assert set(overview["usage"]) == {"today", "days", "budget"}
    assert set(overview["usage"]["budget"]) == {"tokens", "calls"}
    assert set(overview["warnings"]) == {"blocked_calls", "blocked_users", "suspended_users"}
    assert set(overview["ages"]) == {"by_range", "not_completed", "total"}


def test_users_are_counted_by_every_status_and_tier_including_zeros(kit):
    admin = kit.admin()
    kit.member(2)
    kit.member(3, member_tier="plus")
    kit.member(4, member_tier="plus", status=STATUS_SUSPENDED)
    users = kit.service.overview(admin)["users"]
    assert users["total"] == 4
    assert users["by_status"] == {status: {"active": 3, "suspended": 1}.get(status, 0) for status in USER_STATUSES}
    assert users["by_tier"] == {"free": 2, "plus": 2, "vip": 0} and list(users["by_tier"]) == list(MEMBER_TIERS)


def test_the_budget_ratios_follow_the_configured_limits(member_session):
    kit = AdminKit(member_session, settings=UsageSettings(daily_token_limit=1000, daily_call_limit=100))
    admin = kit.admin()
    for _ in range(10):
        kit.call(prompt_tokens=30, completion_tokens=10)              # 10 次呼叫、400 token
    budget = kit.service.overview(admin)["usage"]["budget"]
    assert budget["tokens"] == {"used": 400, "limit": 1000, "ratio": 0.4}
    assert budget["calls"] == {"used": 10, "limit": 100, "ratio": 0.1}


def test_the_ratio_may_exceed_one_and_never_divides_by_zero(member_session):
    kit = AdminKit(member_session, settings=UsageSettings(daily_token_limit=100, daily_call_limit=2))
    admin = kit.admin()
    for _ in range(3):
        kit.call(prompt_tokens=100)
    budget = kit.service.overview(admin)["usage"]["budget"]
    assert budget["tokens"]["ratio"] == 3.0 and budget["calls"]["ratio"] == 1.5


def test_the_last_seven_taipei_days_come_oldest_first_and_the_last_one_is_today(kit):
    admin = kit.admin()
    usage = kit.service.overview(admin)["usage"]
    assert [d["date"] for d in usage["days"]] == [f"2026-10-{n}" for n in range(21, 28)]
    assert usage["days"][-1] == usage["today"]


def test_each_call_lands_on_its_taipei_day_and_empty_days_are_zero(kit):
    admin = kit.admin()
    kit.call(at=DAY_START - timedelta(seconds=1), prompt_tokens=7, counted=True)             # 台灣 10/26 23:59:59
    kit.call(at=DAY_START, prompt_tokens=100, counted=True)                                    # 台灣 10/27 00:00:00
    kit.call(at=DAY_START - timedelta(days=3, seconds=1), prompt_tokens=5)
    days = {d["date"]: d for d in kit.service.overview(admin)["usage"]["days"]}
    assert (days["2026-10-26"]["tokens"], days["2026-10-26"]["turns"]) == (7, 1)
    assert (days["2026-10-27"]["tokens"], days["2026-10-27"]["turns"]) == (100, 1)
    assert days["2026-10-23"]["tokens"] == 5 and days["2026-10-24"] == {"date": "2026-10-24", "tokens": 0, "calls": 0, "turns": 0, "blocked_calls": 0}


def test_the_warnings_count_blocked_calls_blocked_users_and_users_currently_suspended(kit):
    admin = kit.admin()
    noisy, mild, old = kit.member(2), kit.member(3), kit.member(4)
    for minutes in (30, 20, 10):
        kit.block(noisy, None, at=NOW - timedelta(minutes=minutes))                          # 三次、最近一次 10 分鐘前：暫停中
    kit.block(mild, None, at=NOW - timedelta(minutes=5))                                     # 一次：沒有暫停
    for minutes in (200, 190, 180):
        kit.block(old, None, at=NOW - timedelta(minutes=minutes))                            # 三次但最近一次已超過一小時：暫停已結束
    kit.block(noisy, None, kind="summary", at=NOW)                                           # 摘要被擋不算
    kit.block(old, None, at=DAY_START - timedelta(hours=1))                                  # 昨天的不算
    assert kit.service.overview(admin)["warnings"] == {"blocked_calls": 7, "blocked_users": 3, "suspended_users": 1}


def test_the_warnings_are_zero_on_a_quiet_day(kit):
    assert kit.service.overview(kit.admin())["warnings"] == {"blocked_calls": 0, "blocked_users": 0, "suspended_users": 0}


def test_ages_are_only_range_counts(kit):
    admin = kit.admin()
    kit.member(2)
    kit.member(3)
    ages = kit.service.overview(admin)["ages"]
    assert ages["total"] == 3 and all(set(row) == {"label", "count"} for row in ages["by_range"])
    assert sum(row["count"] for row in ages["by_range"]) + ages["not_completed"] == 3


def test_the_overview_carries_nothing_about_any_individual(kit):
    admin = kit.admin()
    person = kit.member(2)
    kit.member(3, status=STATUS_SUSPENDED)
    kit.block(person, None)
    text = json.dumps(kit.service.overview(admin), ensure_ascii=False)
    for secret in (person.email, person.display_name, person.public_id, str(person.birth_date), person.age_range or "§"):
        assert secret not in text
    assert "birth" not in text and "@" not in text


def test_reading_the_overview_writes_no_audit(kit):
    kit.service.overview(kit.admin())
    assert kit.audit_rows() == []


# ---- 稽核檢視 -------------------------------------------------------------------------------

def test_the_audit_log_lists_newest_first_with_the_admins_display_name_and_no_email(kit):
    admin_user = kit.admin()
    target = kit.member(2)
    kit.audit.add(admin_user_id=admin_user.id, action="user.suspend", now=NOW, target_user_public_id=target.public_id, reason="違規", detail={"from_status": "active"})
    kit.audit.add(admin_user_id=admin_user.id, action="user.lookup", now=NOW + timedelta(minutes=1), detail={"via": "id"})
    entries = kit.service.audit_log(admin_user)
    assert [e["action"] for e in entries] == ["user.lookup", "user.suspend"]
    assert entries[1] == {
        "id": entries[1]["id"], "created_at": "2026-10-27T12:00:00+00:00", "action": "user.suspend", "admin": admin_user.display_name,
        "target_user": target.public_id, "target_conversation": None, "reason": "違規", "detail": {"from_status": "active"},
    }
    assert admin_user.email not in json.dumps(entries, ensure_ascii=False)


def test_viewing_the_audit_log_is_not_itself_audited(kit):
    admin_user = kit.admin()
    kit.service.audit_log(admin_user)
    kit.service.audit_log(admin_user)
    assert kit.audit_rows() == []


@pytest.mark.parametrize(("limit", "offset", "field"), [(0, 0, "limit"), (101, 0, "limit"), (-1, 0, "limit"), (50, -1, "offset")])
def test_the_audit_page_parameters_are_checked(kit, limit, offset, field):
    with pytest.raises(ValidationError) as exc:
        kit.service.audit_log(kit.admin(), limit=limit, offset=offset)
    assert set(exc.value.fields) == {field}


def test_the_audit_log_pages(kit):
    admin_user = kit.admin()
    for minute in range(5):
        kit.audit.add(admin_user_id=admin_user.id, action="failures.read", now=NOW + timedelta(minutes=minute), detail={"count": minute})
    assert [e["detail"]["count"] for e in kit.service.audit_log(admin_user, limit=2, offset=1)] == [3, 2]
