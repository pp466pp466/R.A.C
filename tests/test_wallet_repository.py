"""WalletRepository：擁有者隔離、CAS（版本不符不改資料）、冪等鍵衝突、結算結果只能設定一次、記錄頁的排除規則、
匯出不建立錢包、並行建立錢包只有一列。SQLite 與真實 SQL Server 各跑一次。
"""
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import User, Wallet, WalletEntry
from app.repositories.wallet_repository import APPLIED, DUPLICATE, STALE, EntryDraft, WalletRepository
from tests.support.auth import FakeClock

NOW = FakeClock()()


def make_user(session, number: int) -> User:
    user = User(email=f"r{number}@example.com", email_canonical=f"r{number}@example.com", display_name=f"使用者{number}", status="active")
    session.add(user)
    session.flush()
    return user


def draft(key: str, kind: str = "signup_grant", free: int = 200, paid: int = 0, **extra) -> EntryDraft:
    return EntryDraft(kind=kind, idempotency_key=key, free_delta=free, paid_delta=paid, **extra)


def credit(repo, user_id, key, free, paid=0, *, kind="signup_grant", at=NOW, **extra):
    """增加餘額（讀、算、套用），回傳套用結果。"""
    state = repo.state(user_id) or repo.create_if_missing(user_id, at)
    return repo.apply(
        user_id, expected_version=state.version, new_free=state.free_balance + free, new_paid=state.paid_balance + paid,
        draft=draft(key, kind, free, paid, **extra), now=at,
    )


@pytest.fixture()
def repo(member_session):
    return WalletRepository(member_session)


# ---- 錢包 -----------------------------------------------------------------------------------


def test_a_wallet_is_created_once_with_zero_balances(repo, member_session):
    user = make_user(member_session, 1)
    assert repo.state(user.id) is None
    first = repo.create_if_missing(user.id, NOW)
    again = repo.create_if_missing(user.id, NOW)
    assert (first.free_balance, first.paid_balance, first.version) == (0, 0, 0) and again == first
    assert member_session.scalar(select(func.count()).select_from(Wallet)) == 1


def test_a_losing_concurrent_creation_returns_the_existing_wallet(repo, member_session):
    """檢查之後才被搶先建立（唯一衝突）時，只回復這一筆，不留下半套資料，也不拋出錯誤。"""
    user = make_user(member_session, 1)
    member_session.add(Wallet(user_id=user.id, free_balance=0, paid_balance=0, version=0, created_at=NOW, updated_at=NOW))
    member_session.flush()
    real_state, calls = repo.state, []

    def blind_first_time(user_id):
        calls.append(user_id)
        return None if len(calls) == 1 else real_state(user_id)           # 第一次檢查時還沒有，建立時才發現已被搶先

    repo.state = blind_first_time
    state = repo.create_if_missing(user.id, NOW)
    assert len(calls) == 2 and state.version == 0
    assert member_session.scalar(select(func.count()).select_from(Wallet)) == 1


def test_state_is_a_fresh_read_every_time_not_a_cached_entity(repo, member_session):
    user = make_user(member_session, 1)
    repo.create_if_missing(user.id, NOW)
    assert repo.state(user.id).free_balance == 0
    credit(repo, user.id, "k1", 200)
    assert repo.state(user.id).free_balance == 200 and repo.state(user.id).version == 1


# ---- apply：CAS 與冪等鍵 ---------------------------------------------------------------------


def test_a_successful_apply_changes_the_balance_bumps_the_version_and_writes_one_entry(repo, member_session):
    user = make_user(member_session, 1)
    result = credit(repo, user.id, "signup:1", 200, session_public_id="a" * 36)
    assert result.status == APPLIED
    entry = result.entry
    assert (entry.seq, entry.kind, entry.free_delta, entry.free_after, entry.paid_after) == (1, "signup_grant", 200, 200, 0)
    assert entry.session_public_id == "a" * 36 and entry.resolution is None
    assert repo.state(user.id).version == 1
    second = credit(repo, user.id, "k2", 50, 10, kind="admin_adjust")
    assert second.entry.seq == 2 and (second.entry.free_after, second.entry.paid_after) == (250, 10)


def test_a_wrong_version_changes_nothing_and_leaves_no_entry(repo, member_session):
    user = make_user(member_session, 1)
    repo.create_if_missing(user.id, NOW)
    credit(repo, user.id, "k1", 100)
    result = repo.apply(user.id, expected_version=0, new_free=999, new_paid=0, draft=draft("k2", free=899), now=NOW)   # 過期的版本
    assert result.status == STALE and result.entry is None
    assert repo.state(user.id).free_balance == 100 and repo.state(user.id).version == 1
    assert repo.entry(user.id, "k2") is None


def test_a_duplicate_key_returns_the_existing_entry_and_rolls_the_balance_update_back(repo, member_session):
    user = make_user(member_session, 1)
    first = credit(repo, user.id, "signup:1", 200)
    state = repo.state(user.id)
    result = repo.apply(user.id, expected_version=state.version, new_free=400, new_paid=0, draft=draft("signup:1"), now=NOW)
    assert result.status == DUPLICATE and result.entry == first.entry
    after = repo.state(user.id)
    assert (after.free_balance, after.version) == (200, 1)                    # 餘額更新隨 SAVEPOINT 一併復原


def test_a_balance_that_would_go_negative_is_refused_by_the_database_not_swallowed(repo, member_session):
    from sqlalchemy.exc import IntegrityError

    user = make_user(member_session, 1)
    repo.create_if_missing(user.id, NOW)
    with pytest.raises(IntegrityError):
        repo.apply(user.id, expected_version=0, new_free=-1, new_paid=0, draft=draft("k", free=-1), now=NOW)
    member_session.rollback()


# ---- 擁有者隔離 ---------------------------------------------------------------------------------


def test_every_read_is_scoped_to_the_owner(repo, member_session):
    first, second = make_user(member_session, 1), make_user(member_session, 2)
    credit(repo, first.id, "k1", 200)
    credit(repo, second.id, "k2", 300)
    assert repo.entry(second.id, "k1") is None and repo.entry(first.id, "k2") is None            # 別人的鍵看不到
    assert [e.idempotency_key for e in repo.list_entries(first.id, before_seq=None, limit=10)] == ["k1"]
    assert repo.stale_open_holds(first.id, cutoff=NOW + timedelta(days=1), limit=10) == []
    assert repo.export_wallet(first.id)[0].free_balance == 200 and repo.state(second.id).free_balance == 300


def test_resolving_someone_elses_hold_does_nothing(repo, member_session):
    first, second = make_user(member_session, 1), make_user(member_session, 2)
    credit(repo, first.id, "turn:a", 200)
    credit(repo, first.id, "hold-1", -30, kind="turn_hold")
    assert repo.resolve_hold(second.id, "hold-1", "settled") is False
    assert repo.entry(first.id, "hold-1").resolution is None


# ---- 結算結果只能設定一次 ---------------------------------------------------------------------


def test_a_hold_can_be_resolved_only_once(repo, member_session):
    user = make_user(member_session, 1)
    credit(repo, user.id, "g", 200)
    credit(repo, user.id, "hold-1", -30, kind="turn_hold")
    assert repo.resolve_hold(user.id, "hold-1", "settled") is True
    assert repo.resolve_hold(user.id, "hold-1", "refunded") is False                 # 已結算，不能再改
    assert repo.resolve_hold(user.id, "hold-1", "settled") is False
    assert repo.entry(user.id, "hold-1").resolution == "settled"


def test_only_a_hold_row_can_be_resolved(repo, member_session):
    user = make_user(member_session, 1)
    credit(repo, user.id, "g", 200)
    assert repo.resolve_hold(user.id, "g", "settled") is False
    assert repo.entry(user.id, "g").resolution is None
    assert repo.resolve_hold(user.id, "nope", "settled") is False


# ---- 逾時的預扣 ---------------------------------------------------------------------------------


def test_stale_open_holds_are_only_the_old_unresolved_ones_oldest_first_and_bounded(repo, member_session):
    user = make_user(member_session, 1)
    credit(repo, user.id, "g", 1000)
    old = NOW - timedelta(seconds=500)
    for index in range(3):
        credit(repo, user.id, f"hold-{index}", -30, kind="turn_hold", at=old + timedelta(seconds=index))
    credit(repo, user.id, "hold-new", -30, kind="turn_hold", at=NOW)                          # 還沒逾時
    repo.resolve_hold(user.id, "hold-1", "settled")                                           # 已結算的不算
    cutoff = NOW - timedelta(seconds=180)
    assert [e.idempotency_key for e in repo.stale_open_holds(user.id, cutoff=cutoff, limit=10)] == ["hold-0", "hold-2"]
    assert [e.idempotency_key for e in repo.stale_open_holds(user.id, cutoff=cutoff, limit=1)] == ["hold-0"]


# ---- 記錄頁 -------------------------------------------------------------------------------------


def test_the_listing_is_newest_first_hides_refunded_holds_and_refund_rows_but_shows_open_ones(repo, member_session):
    user = make_user(member_session, 1)
    credit(repo, user.id, "signup", 200)                                                     # seq 1
    credit(repo, user.id, "hold-settled", -30, kind="turn_hold")                             # seq 2
    repo.resolve_hold(user.id, "hold-settled", "settled")
    credit(repo, user.id, "hold-refunded", -30, kind="turn_hold")                            # seq 3
    repo.resolve_hold(user.id, "hold-refunded", "refunded")
    credit(repo, user.id, "refund", 30, kind="turn_refund", ref_key="hold-refunded")         # seq 4
    credit(repo, user.id, "hold-open", -30, kind="turn_hold")                                # seq 5：處理中
    listing = repo.list_entries(user.id, before_seq=None, limit=10)
    assert [e.idempotency_key for e in listing] == ["hold-open", "hold-settled", "signup"]
    assert [e.resolution for e in listing] == [None, "settled", None]


def test_the_listing_pages_by_the_users_own_sequence_number(repo, member_session):
    user = make_user(member_session, 1)
    for index in range(7):
        credit(repo, user.id, f"k{index}", 10)
    first = repo.list_entries(user.id, before_seq=None, limit=3)
    second = repo.list_entries(user.id, before_seq=first[-1].seq, limit=3)
    third = repo.list_entries(user.id, before_seq=second[-1].seq, limit=3)
    assert [e.seq for e in first] == [7, 6, 5] and [e.seq for e in second] == [4, 3, 2] and [e.seq for e in third] == [1]


# ---- 匯出 -------------------------------------------------------------------------------------


def test_export_returns_everything_including_refunded_holds_and_does_not_create_a_wallet(repo, member_session):
    user, other = make_user(member_session, 1), make_user(member_session, 2)
    assert repo.export_wallet(other.id) is None
    assert member_session.scalar(select(func.count()).select_from(Wallet)) == 0                # 匯出沒有建立錢包
    credit(repo, user.id, "g", 200)
    credit(repo, user.id, "hold", -30, kind="turn_hold")
    repo.resolve_hold(user.id, "hold", "refunded")
    credit(repo, user.id, "refund", 30, kind="turn_refund", ref_key="hold")
    state, entries = repo.export_wallet(user.id)
    assert state.free_balance == 200 and [e.idempotency_key for e in entries] == ["g", "hold", "refund"]
    assert [e.idempotency_key for e in repo.iter_entries(user.id, page_size=2)] == ["g", "hold", "refund"]


# ---- 營運彙總 -------------------------------------------------------------------------------------


def test_grant_totals_count_only_grants_inside_the_window(repo, member_session):
    first, second = make_user(member_session, 1), make_user(member_session, 2)
    day = NOW
    credit(repo, first.id, "s1", 200, kind="signup_grant", at=day)
    credit(repo, first.id, "c1", 200, kind="daily_checkin", at=day)
    credit(repo, second.id, "c2", 100, kind="daily_checkin", at=day)
    credit(repo, second.id, "h", -30, kind="turn_hold", at=day)                                 # 不是發放
    credit(repo, second.id, "old", 200, kind="daily_checkin", at=day - timedelta(days=2))        # 視窗外
    totals = repo.grant_totals(start=day - timedelta(hours=1), end=day + timedelta(hours=1))
    assert totals == {"signup_grant": (1, 200, 1), "daily_checkin": (2, 300, 2)}
    assert repo.grant_totals(start=day + timedelta(days=5), end=day + timedelta(days=6)) == {"signup_grant": (0, 0, 0), "daily_checkin": (0, 0, 0)}


# ---- 並行（檔案型 SQLite 與真實 SQL Server）------------------------------------------------------


def test_concurrent_applies_from_separate_sessions_never_lose_an_update(concurrent_engine):
    """兩個 Session（兩個 worker）都以同一個版本號套用：恰有一個成功，另一個得到 STALE，餘額不重複計入。"""
    with Session(concurrent_engine) as setup:
        user = make_user(setup, 1)
        WalletRepository(setup).create_if_missing(user.id, NOW)
        setup.commit()
        user_id = user.id
    with Session(concurrent_engine) as worker_a, Session(concurrent_engine) as worker_b:
        results = []
        for worker, key in ((worker_a, "a"), (worker_b, "b")):
            results.append(WalletRepository(worker).apply(
                user_id, expected_version=0, new_free=100, new_paid=0, draft=draft(key, free=100), now=NOW,
            ))
            worker.commit()
    assert sorted(r.status for r in results) == [APPLIED, STALE]
    with Session(concurrent_engine) as check:
        state = WalletRepository(check).state(user_id)
        assert (state.free_balance, state.version) == (100, 1)
        assert check.scalar(select(func.count()).select_from(WalletEntry)) == 1


# ---- 外層交易的回復（SQLite 的 SAVEPOINT 陷阱）---------------------------------------------------------


def test_everything_the_repository_wrote_is_undone_when_the_outer_transaction_rolls_back(repo, member_session):
    """建立錢包與套用異動都是交易裡的第一批陳述式：外層請求失敗而回復時，兩者都要一併消失（SQLite 的 pysqlite 若讓 SAVEPOINT
    成為第一個陳述式，`RELEASE` 會變成 `COMMIT`，回復就救不回來；`_savepoint()` 先開始交易來避免它）。"""
    user = make_user(member_session, 1)
    member_session.commit()
    result = credit(repo, user.id, "signup:1", 200)
    assert result.status == APPLIED and repo.state(user.id).free_balance == 200
    member_session.rollback()
    assert repo.state(user.id) is None and repo.entry(user.id, "signup:1") is None


def test_a_balance_change_made_after_the_wallet_exists_is_also_undone_by_an_outer_rollback(repo, member_session):
    user = make_user(member_session, 1)
    repo.create_if_missing(user.id, NOW)
    member_session.commit()
    credit(repo, user.id, "k1", 100)
    member_session.rollback()
    assert repo.state(user.id).free_balance == 0 and repo.entry(user.id, "k1") is None


def test_resolving_and_refunding_inside_the_savepoint_are_undone_together_by_an_outer_rollback(repo, member_session):
    user = make_user(member_session, 1)
    credit(repo, user.id, "g", 200)
    credit(repo, user.id, "hold", -30, kind="turn_hold")
    member_session.commit()
    with repo.savepoint():
        assert repo.resolve_hold(user.id, "hold", "refunded") is True
    member_session.rollback()
    assert repo.entry(user.id, "hold").resolution is None
