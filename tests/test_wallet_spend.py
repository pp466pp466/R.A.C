"""game-wallet 規格「預扣先扣免費幣再扣付費幣」「預扣可被結算或退回且兩者互斥」「逾時未結算的預扣自動退回」。
SQLite 與真實 SQL Server 各跑一次；並行情境同時在檔案型 SQLite 與 SQL Server 上以多執行緒執行。
"""
import threading

import pytest
from sqlalchemy.orm import Session

from app.services.errors import HoldAlreadyRefunded, HoldAlreadySettled, InsufficientCoins, NotFound, ValidationError


def fund(kit, player, free, paid):
    """把兩個餘額調到指定的值（先建立錢包與贈幣，再用管理員調整對齊）。"""
    kit.service.view(player)
    current_free, current_paid = kit.balances(player)
    if free != current_free:
        kit.service.adjust(player.id, bucket="free", delta=free - current_free)
    if paid != current_paid:
        kit.service.adjust(player.id, bucket="paid", delta=paid - current_paid)
    assert kit.balances(player) == (free, paid)


# ---- 預扣的拆分 --------------------------------------------------------------------------------------


def test_the_free_balance_is_spent_first(wallet_kit):
    player = wallet_kit.player(1)
    fund(wallet_kit, player, 100, 50)
    hold = wallet_kit.service.hold(player, key="turn:a")
    assert (hold.free_taken, hold.paid_taken, hold.cost) == (30, 0, 30)
    assert wallet_kit.balances(player) == (70, 50)
    entry = wallet_kit.entry(player, "turn:a")
    assert (entry.free_delta, entry.paid_delta, entry.kind) == (-30, 0, "turn_hold")


def test_a_hold_that_spans_both_balances_takes_the_rest_from_paid(wallet_kit):
    player = wallet_kit.player(1)
    fund(wallet_kit, player, 20, 50)
    hold = wallet_kit.service.hold(player, key="turn:a")
    assert (hold.free_taken, hold.paid_taken) == (20, 10)
    assert wallet_kit.balances(player) == (0, 40)
    entry = wallet_kit.entry(player, "turn:a")
    assert (entry.free_delta, entry.paid_delta) == (-20, -10)


def test_exactly_the_free_balance_is_enough_and_leaves_paid_untouched(wallet_kit):
    player = wallet_kit.player(1)
    fund(wallet_kit, player, 30, 7)
    hold = wallet_kit.service.hold(player, key="turn:a")
    assert (hold.free_taken, hold.paid_taken) == (30, 0) and wallet_kit.balances(player) == (0, 7)


def test_an_insufficient_total_is_refused_with_the_balance_and_the_price_and_changes_nothing(wallet_kit):
    player = wallet_kit.player(1)
    fund(wallet_kit, player, 10, 10)
    before = wallet_kit.all_entries(player)
    with pytest.raises(InsufficientCoins) as caught:
        wallet_kit.service.hold(player, key="turn:a")
    assert (caught.value.balance, caught.value.needed) == (20, 30)
    assert wallet_kit.balances(player) == (10, 10) and wallet_kit.all_entries(player) == before


def test_the_price_comes_from_the_setting_table_for_the_model_key(wallet_kit):
    player = wallet_kit.player(1)
    wallet_kit.set_limit("coin.turn_cost.standard", 45)
    assert wallet_kit.service.hold(player, key="turn:a", model_key="standard").cost == 45
    assert wallet_kit.balances(player) == (155, 0)


def test_an_unknown_model_key_is_refused_before_anything_is_charged(wallet_kit):
    player = wallet_kit.player(1)
    wallet_kit.service.view(player)
    for bad in ("premium", "", None):
        with pytest.raises(ValidationError):
            wallet_kit.service.hold(player, key="turn:a", model_key=bad)
    assert wallet_kit.balances(player) == (200, 0) and wallet_kit.entry(player, "turn:a") is None


@pytest.mark.parametrize("bad", ["", None, 5, "x" * 181])
def test_an_invalid_key_is_refused(wallet_kit, bad):
    player = wallet_kit.player(1)
    with pytest.raises(ValidationError):
        wallet_kit.service.hold(player, key=bad)


def test_the_longest_allowed_key_still_fits_its_refund_key(wallet_kit):
    player = wallet_kit.player(1)
    key = "k" * 180
    wallet_kit.service.hold(player, key=key)
    wallet_kit.service.refund(player, key)
    assert wallet_kit.entry(player, f"refund:{key}") is not None


def test_a_key_already_used_for_something_else_cannot_be_a_hold(wallet_kit):
    player = wallet_kit.player(1)
    wallet_kit.service.view(player)
    with pytest.raises(ValidationError):
        wallet_kit.service.hold(player, key=f"signup:{player.id}")


# ---- 並行 -------------------------------------------------------------------------------------------


def run_concurrently(engine, player, jobs):
    """每個工作在自己的 Session（自己的 worker）裡執行；回傳（結果, 錯誤）。"""
    from tests.support.wallet import WalletKit

    results, errors = [], []
    barrier = threading.Barrier(len(jobs))

    def worker(job):
        try:
            with Session(engine) as session:
                kit = WalletKit(session)
                barrier.wait(timeout=10)
                results.append(job(kit))
                session.commit()
        except Exception as error:                                       # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(job,)) for job in jobs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results, errors


def test_two_concurrent_holds_against_a_balance_of_forty_let_exactly_one_succeed(concurrent_engine):
    from tests.support.wallet import WalletKit

    with Session(concurrent_engine) as setup:
        kit = WalletKit(setup)
        player = kit.player(1)
        fund(kit, player, 40, 0)
        setup.commit()
    results, errors = run_concurrently(
        concurrent_engine, player,
        [lambda kit: kit.service.hold(player, key="turn:a"), lambda kit: kit.service.hold(player, key="turn:b")],
    )
    assert len(results) == 1 and len(errors) == 1 and isinstance(errors[0], InsufficientCoins)
    with Session(concurrent_engine) as check:
        kit = WalletKit(check)
        assert kit.balances(player) == (10, 0) and kit.ledger_matches_balances(player)


def test_many_concurrent_holds_never_overspend_and_the_ledger_explains_the_balance(concurrent_engine):
    from tests.support.wallet import WalletKit

    with Session(concurrent_engine) as setup:
        kit = WalletKit(setup)
        player = kit.player(1)
        fund(kit, player, 95, 0)                                          # 最多 3 個 30 幣的預扣成功
        setup.commit()
    jobs = [(lambda kit, index=index: kit.service.hold(player, key=f"turn:{index}")) for index in range(6)]
    results, errors = run_concurrently(concurrent_engine, player, jobs)
    assert len(results) == 3 and all(isinstance(e, (InsufficientCoins,)) for e in errors) and len(errors) == 3
    with Session(concurrent_engine) as check:
        kit = WalletKit(check)
        assert kit.balances(player) == (5, 0) and kit.ledger_matches_balances(player)


def test_a_concurrent_settle_and_refund_of_the_same_hold_let_exactly_one_win(concurrent_engine):
    from tests.support.wallet import WalletKit

    with Session(concurrent_engine) as setup:
        kit = WalletKit(setup)
        player = kit.player(1)
        kit.service.hold(player, key="turn:a")
        setup.commit()
    results, errors = run_concurrently(
        concurrent_engine, player,
        [lambda kit: kit.service.settle(player, "turn:a"), lambda kit: kit.service.refund(player, "turn:a")],
    )
    assert len(results) + len(errors) == 2
    with Session(concurrent_engine) as check:
        kit = WalletKit(check)
        hold = kit.entry(player, "turn:a")
        refunded = kit.entry(player, "refund:turn:a") is not None
        assert hold.resolution in ("settled", "refunded") and (hold.resolution == "refunded") == refunded
        assert kit.balances(player) == ((200, 0) if refunded else (170, 0)) and kit.ledger_matches_balances(player)


# ---- 結算 -------------------------------------------------------------------------------------------


def test_settling_keeps_the_balance_and_marks_the_hold(wallet_kit):
    player = wallet_kit.player(1)
    wallet_kit.service.hold(player, key="turn:a")
    before = wallet_kit.wallets.state(player.id)
    wallet_kit.service.settle(player, "turn:a")
    assert wallet_kit.entry(player, "turn:a").resolution == "settled"
    after = wallet_kit.wallets.state(player.id)
    assert (after.free_balance, after.paid_balance, after.version) == (before.free_balance, before.paid_balance, before.version)


def test_settling_twice_is_fine(wallet_kit):
    player = wallet_kit.player(1)
    wallet_kit.service.hold(player, key="turn:a")
    wallet_kit.service.settle(player, "turn:a")
    wallet_kit.service.settle(player, "turn:a")
    assert wallet_kit.balances(player) == (170, 0)


def test_a_settled_hold_cannot_be_refunded(wallet_kit):
    player = wallet_kit.player(1)
    wallet_kit.service.hold(player, key="turn:a")
    wallet_kit.service.settle(player, "turn:a")
    with pytest.raises(HoldAlreadySettled):
        wallet_kit.service.refund(player, "turn:a")
    assert wallet_kit.balances(player) == (170, 0) and wallet_kit.entry(player, "refund:turn:a") is None


# ---- 退回 -------------------------------------------------------------------------------------------


def test_a_refund_returns_exactly_what_was_taken_from_each_balance(wallet_kit):
    player = wallet_kit.player(1)
    fund(wallet_kit, player, 20, 50)
    wallet_kit.service.hold(player, key="turn:a")                           # 免費 -20、付費 -10
    wallet_kit.service.refund(player, "turn:a")
    assert wallet_kit.balances(player) == (20, 50)
    refund = wallet_kit.entry(player, "refund:turn:a")
    assert (refund.kind, refund.free_delta, refund.paid_delta, refund.ref_key) == ("turn_refund", 20, 10, "turn:a")
    assert wallet_kit.entry(player, "turn:a").resolution == "refunded"
    assert wallet_kit.ledger_matches_balances(player)


def test_refunding_twice_refunds_once(wallet_kit):
    player = wallet_kit.player(1)
    wallet_kit.service.hold(player, key="turn:a")
    wallet_kit.service.refund(player, "turn:a")
    wallet_kit.service.refund(player, "turn:a")
    assert wallet_kit.balances(player) == (200, 0)
    assert [e.kind for e in wallet_kit.all_entries(player)].count("turn_refund") == 1


def test_a_refunded_hold_cannot_be_settled(wallet_kit):
    player = wallet_kit.player(1)
    wallet_kit.service.hold(player, key="turn:a")
    wallet_kit.service.refund(player, "turn:a")
    with pytest.raises(HoldAlreadyRefunded):
        wallet_kit.service.settle(player, "turn:a")
    assert wallet_kit.balances(player) == (200, 0)


def test_after_a_failed_turn_is_refunded_a_new_key_charges_exactly_once_in_total(wallet_kit):
    """失敗退回後，呼叫端以新的冪等鍵再預扣：這是新的一筆，淨扣款只有這一筆。"""
    player = wallet_kit.player(1)
    wallet_kit.service.hold(player, key="turn:m1:attempt1")
    wallet_kit.service.refund(player, "turn:m1:attempt1")
    wallet_kit.service.hold(player, key="turn:m1:attempt2")
    wallet_kit.service.settle(player, "turn:m1:attempt2")
    assert wallet_kit.balances(player) == (170, 0) and wallet_kit.ledger_matches_balances(player)


def test_resending_the_key_of_a_refunded_hold_reports_that_it_was_refunded(wallet_kit):
    player = wallet_kit.player(1)
    wallet_kit.service.hold(player, key="turn:a")
    wallet_kit.service.refund(player, "turn:a")
    again = wallet_kit.service.hold(player, key="turn:a")                   # 同一個鍵：不會再扣，並告知它已退回
    assert again.existed and again.resolution == "refunded"
    assert wallet_kit.balances(player) == (200, 0)


def test_a_refund_is_not_limited_by_the_free_cap(wallet_kit):
    player = wallet_kit.player(1)
    fund(wallet_kit, player, 1000, 0)
    wallet_kit.service.hold(player, key="turn:a")
    fund(wallet_kit, player, 1000, 0)
    wallet_kit.service.refund(player, "turn:a")
    assert wallet_kit.balances(player) == (1030, 0)


def test_a_failed_refund_rolls_the_resolution_mark_back_with_it(wallet_kit, monkeypatch):
    """退回的標記與入帳在同一個 SAVEPOINT：入帳失敗時標記一併復原，預扣仍然是開著的，之後可以再退回。"""
    from app.services import wallet_service
    from app.services.errors import WalletBusy

    player = wallet_kit.player(1)
    wallet_kit.service.hold(player, key="turn:a")
    original = wallet_kit.wallets.apply
    monkeypatch.setattr(wallet_kit.wallets, "apply", lambda *a, **k: type("R", (), {"status": "stale", "entry": None})())
    monkeypatch.setattr(wallet_service, "MAX_ATTEMPTS", 2)
    with pytest.raises(WalletBusy):
        wallet_kit.service.refund(player, "turn:a")
    monkeypatch.setattr(wallet_kit.wallets, "apply", original)
    assert wallet_kit.entry(player, "turn:a").resolution is None
    wallet_kit.service.refund(player, "turn:a")
    assert wallet_kit.balances(player) == (200, 0) and wallet_kit.ledger_matches_balances(player)


# ---- 他人的預扣 ---------------------------------------------------------------------------------------


def test_someone_elses_hold_is_treated_as_not_existing(wallet_kit):
    owner, stranger = wallet_kit.player(1), wallet_kit.player(2)
    wallet_kit.service.hold(owner, key="turn:a")
    wallet_kit.service.view(stranger)
    with pytest.raises(NotFound):
        wallet_kit.service.settle(stranger, "turn:a")
    with pytest.raises(NotFound):
        wallet_kit.service.refund(stranger, "turn:a")
    assert wallet_kit.entry(owner, "turn:a").resolution is None
    assert wallet_kit.balances(owner) == (170, 0) and wallet_kit.balances(stranger) == (200, 0)


@pytest.mark.parametrize("key", ["nope", "", None, 5])
def test_an_unknown_key_is_not_found_for_settle_and_refund(wallet_kit, key):
    player = wallet_kit.player(1)
    wallet_kit.service.view(player)
    with pytest.raises(NotFound):
        wallet_kit.service.settle(player, key)
    with pytest.raises(NotFound):
        wallet_kit.service.refund(player, key)


def test_a_key_of_something_other_than_a_hold_is_not_found_for_settle_and_refund(wallet_kit):
    player = wallet_kit.player(1)
    wallet_kit.service.view(player)
    with pytest.raises(NotFound):
        wallet_kit.service.settle(player, f"signup:{player.id}")
    with pytest.raises(NotFound):
        wallet_kit.service.refund(player, f"signup:{player.id}")


# ---- 逾時的預扣 ---------------------------------------------------------------------------------------


def test_a_hold_older_than_the_timeout_is_refunded_when_the_player_next_looks(wallet_kit):
    player = wallet_kit.player(1)
    wallet_kit.service.hold(player, key="turn:a")
    wallet_kit.clock.advance(seconds=181)
    view = wallet_kit.service.view(player)
    assert view.free_balance == 200
    assert wallet_kit.entry(player, "turn:a").resolution == "refunded" and wallet_kit.entry(player, "refund:turn:a") is not None


def test_a_young_hold_is_left_alone(wallet_kit):
    player = wallet_kit.player(1)
    wallet_kit.service.hold(player, key="turn:a")
    wallet_kit.clock.advance(seconds=60)
    assert wallet_kit.service.view(player).free_balance == 170
    assert wallet_kit.entry(player, "turn:a").resolution is None


def test_the_timeout_comes_from_the_setting_table(wallet_kit):
    player = wallet_kit.player(1)
    wallet_kit.set_limit("coin.hold_timeout_seconds", 60)
    wallet_kit.service.hold(player, key="turn:a")
    wallet_kit.clock.advance(seconds=90)
    assert wallet_kit.service.view(player).free_balance == 200


def test_a_settled_hold_is_never_treated_as_stale(wallet_kit):
    player = wallet_kit.player(1)
    wallet_kit.service.hold(player, key="turn:a")
    wallet_kit.service.settle(player, "turn:a")
    wallet_kit.clock.advance(days=3)
    assert wallet_kit.service.view(player).free_balance == 170


def test_settling_a_slow_turn_is_not_pre_empted_by_the_stale_release(wallet_kit):
    """結算與退回本身不處理逾時預扣：一個走得比較久的回合在結算之前不會被自己的清理退回。"""
    player = wallet_kit.player(1)
    wallet_kit.service.hold(player, key="turn:a")
    wallet_kit.clock.advance(seconds=500)
    wallet_kit.service.settle(player, "turn:a")
    assert wallet_kit.entry(player, "turn:a").resolution == "settled"
    assert wallet_kit.service.view(player).free_balance == 170


def test_stale_holds_are_released_before_a_new_hold_so_the_refund_is_usable(wallet_kit):
    player = wallet_kit.player(1)
    fund(wallet_kit, player, 30, 0)
    wallet_kit.service.hold(player, key="turn:a")                            # 餘額 0
    wallet_kit.clock.advance(seconds=181)
    wallet_kit.service.hold(player, key="turn:b")                            # 先退回逾時的，才夠付這一筆
    assert wallet_kit.balances(player) == (0, 0)
    assert wallet_kit.entry(player, "turn:a").resolution == "refunded"


def test_the_release_is_bounded_per_access(wallet_kit):
    from app.services.wallet_service import STALE_HOLD_BATCH

    player = wallet_kit.player(1)
    fund(wallet_kit, player, 1000, 0)
    for index in range(STALE_HOLD_BATCH + 5):
        wallet_kit.service.hold(player, key=f"turn:{index}")
    wallet_kit.clock.advance(seconds=181)
    wallet_kit.service.view(player)
    refunded = [e for e in wallet_kit.all_entries(player) if e.kind == "turn_hold" and e.resolution == "refunded"]
    assert len(refunded) == STALE_HOLD_BATCH
    wallet_kit.service.view(player)                                          # 下一次存取處理剩下的
    assert all(e.resolution == "refunded" for e in wallet_kit.all_entries(player) if e.kind == "turn_hold")
    assert wallet_kit.balances(player) == (1000, 0)


def test_the_administrators_read_does_not_release_anything(wallet_kit):
    """管理員檢視餘額走 Repository 的唯讀查詢，不經過 `view`：不會觸發清理，也不會建立錢包。"""
    player = wallet_kit.player(1)
    wallet_kit.service.hold(player, key="turn:a")
    wallet_kit.clock.advance(seconds=500)
    assert wallet_kit.balances(player) == (170, 0)
    assert wallet_kit.entry(player, "turn:a").resolution is None
