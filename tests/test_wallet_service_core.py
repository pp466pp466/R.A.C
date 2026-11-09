"""game-wallet 規格的核心情境：錢包有兩個餘額且不得為負、帳本只增不改、相同冪等鍵只生效一次、首次註冊贈幣。
SQLite 與真實 SQL Server 各跑一次。
"""
import pytest
from sqlalchemy import func, select

from app.models import Wallet, WalletEntry


# ---- 錢包有免費與付費兩個餘額 ------------------------------------------------------------------------


def test_the_first_access_creates_the_wallet_and_the_signup_grant_arrives_with_it(wallet_kit):
    player = wallet_kit.player(1)
    assert wallet_kit.wallets.state(player.id) is None                          # 還沒有錢包
    view = wallet_kit.service.view(player)
    assert (view.free_balance, view.paid_balance, view.total) == (200, 0, 200)
    assert wallet_kit.session.scalar(select(func.count()).select_from(Wallet)) == 1


def test_an_administrator_gets_an_empty_wallet_and_no_grant(wallet_kit):
    admin = wallet_kit.admin()
    view = wallet_kit.service.view(admin)
    assert (view.free_balance, view.paid_balance) == (0, 0)
    assert wallet_kit.all_entries(admin) == []
    assert wallet_kit.service.ensure_signup_grant(admin) == 0


def test_a_player_who_has_not_confirmed_age_gets_no_grant(wallet_kit):
    player = wallet_kit.unconfirmed()
    assert wallet_kit.service.ensure_signup_grant(player) == 0
    assert wallet_kit.wallets.state(player.id) is None and wallet_kit.all_entries(player) == []


def test_a_balance_can_never_go_negative(wallet_kit):
    player = wallet_kit.player(1)
    wallet_kit.service.view(player)
    from app.services.errors import InsufficientCoins

    with pytest.raises(InsufficientCoins):
        wallet_kit.service.adjust(player.id, bucket="free", delta=-201)
    assert wallet_kit.balances(player) == (200, 0)
    assert wallet_kit.ledger_matches_balances(player)


# ---- 帳本只增不改 -----------------------------------------------------------------------------------


def test_a_balance_change_and_its_ledger_entry_exist_together(wallet_kit):
    player = wallet_kit.player(1)
    wallet_kit.service.checkin(player)
    entries = wallet_kit.all_entries(player)
    assert [(e.kind, e.free_delta) for e in entries] == [("signup_grant", 200), ("daily_checkin", 200)]
    assert entries[-1].free_after == wallet_kit.balances(player)[0] == 400
    assert wallet_kit.ledger_matches_balances(player)


def test_a_refused_change_leaves_no_ledger_entry_and_does_not_bump_the_sequence(wallet_kit):
    from app.services.errors import InsufficientCoins

    player = wallet_kit.player(1)
    wallet_kit.service.view(player)
    before = wallet_kit.all_entries(player)
    wallet_kit.set_limit("coin.turn_cost.standard", 5000)
    with pytest.raises(InsufficientCoins):
        wallet_kit.service.hold(player, key="turn:a")
    assert wallet_kit.all_entries(player) == before
    assert wallet_kit.wallets.state(player.id).version == 1


def test_every_entry_carries_the_balances_after_it_and_the_sequence_is_gapless(wallet_kit):
    player = wallet_kit.player(1)
    service = wallet_kit.service
    service.checkin(player)
    hold = service.hold(player, key="turn:1")
    service.settle(player, "turn:1")
    service.adjust(player.id, bucket="paid", delta=50)
    running = (0, 0)
    for entry in wallet_kit.all_entries(player):
        running = (running[0] + entry.free_delta, running[1] + entry.paid_delta)
        assert (entry.free_after, entry.paid_after) == running
    assert hold.cost == 30 and wallet_kit.ledger_matches_balances(player)


def test_the_ledger_stores_the_session_public_id_not_an_internal_number(wallet_kit):
    player = wallet_kit.player(1)
    wallet_kit.service.hold(player, key="turn:s", session_public_id="1b4e28ba-2fa1-11d2-883f-0016d3cca427")
    assert wallet_kit.entry(player, "turn:s").session_public_id == "1b4e28ba-2fa1-11d2-883f-0016d3cca427"


# ---- 冪等 -------------------------------------------------------------------------------------------


def test_the_signup_grant_is_issued_once_however_many_times_it_is_requested(wallet_kit):
    player = wallet_kit.player(1)
    service = wallet_kit.service
    assert [service.ensure_signup_grant(player) for _ in range(3)] == [200, 0, 0]
    service.view(player)
    service.view(player)
    assert wallet_kit.balances(player) == (200, 0)
    assert wallet_kit.session.scalar(select(func.count()).select_from(WalletEntry).where(WalletEntry.kind == "signup_grant")) == 1


def test_a_repeated_hold_with_the_same_key_is_charged_once_and_returns_the_same_hold(wallet_kit):
    player = wallet_kit.player(1)
    first = wallet_kit.service.hold(player, key="turn:x")
    second = wallet_kit.service.hold(player, key="turn:x")
    assert (first.existed, second.existed) == (False, True)
    assert (first.cost, first.free_taken, first.paid_taken) == (second.cost, second.free_taken, second.paid_taken) == (30, 30, 0)
    assert wallet_kit.balances(player) == (170, 0)


def test_two_concurrent_requests_with_the_same_key_charge_once(concurrent_engine):
    """兩個 Session（兩個 worker）同時以同一個冪等鍵預扣：餘額只被扣一次，兩者得到同一筆預扣。"""
    import threading

    from sqlalchemy.orm import Session

    from tests.support.wallet import WalletKit

    with Session(concurrent_engine) as setup:
        kit = WalletKit(setup)
        player = kit.player(1)
        kit.service.view(player)
        setup.commit()
    outcomes, errors = [], []

    def worker():
        try:
            with Session(concurrent_engine) as session:
                kit = WalletKit(session)
                hold = kit.service.hold(player, key="turn:same")
                session.commit()
                outcomes.append(hold)
        except Exception as error:                                   # noqa: BLE001 — 收集後在主執行緒斷言
            errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == [] and len(outcomes) == 2
    assert sorted(h.existed for h in outcomes) == [False, True] and all(h.cost == 30 for h in outcomes)
    with Session(concurrent_engine) as check:
        kit = WalletKit(check)
        assert kit.balances(player) == (170, 0)
        assert kit.ledger_matches_balances(player)


# ---- 首次註冊贈幣 ---------------------------------------------------------------------------------


def test_a_player_who_confirmed_age_before_the_wallet_existed_is_given_the_grant_on_first_access(wallet_kit):
    """既有玩家補發：錢包功能上線之前就已完成年齡確認的玩家，第一次存取時收到贈幣，之後不再發。"""
    player = wallet_kit.player(1)
    assert wallet_kit.service.view(player).free_balance == 200
    assert wallet_kit.service.view(player).free_balance == 200


def test_the_signup_grant_amount_comes_from_the_setting_table_each_time(wallet_kit):
    wallet_kit.set_limit("coin.signup_grant", 500)
    assert wallet_kit.service.view(wallet_kit.player(1)).free_balance == 500
    wallet_kit.set_limit("coin.signup_grant", 50)
    assert wallet_kit.service.view(wallet_kit.player(2)).free_balance == 50


def test_the_signup_grant_respects_the_free_cap(wallet_kit):
    wallet_kit.set_limit("coin.signup_grant", 800)
    wallet_kit.set_limit("coin.free_cap", 300)
    player = wallet_kit.player(1)
    assert wallet_kit.service.ensure_signup_grant(player) == 300
    assert wallet_kit.balances(player) == (300, 0)


def test_a_zero_grant_still_records_that_the_grant_was_issued(wallet_kit):
    wallet_kit.set_limit("coin.signup_grant", 0)
    player = wallet_kit.player(1)
    assert wallet_kit.service.ensure_signup_grant(player) == 0
    assert wallet_kit.entry(player, f"signup:{player.id}") is not None
    wallet_kit.set_limit("coin.signup_grant", 200)
    assert wallet_kit.service.ensure_signup_grant(player) == 0                    # 已經發過（雖然是 0），不補發


def test_the_view_reports_the_free_cap_the_price_and_the_checkin_preview(wallet_kit):
    view = wallet_kit.service.view(wallet_kit.player(1))
    payload = view.to_dict()
    assert payload["free_cap"] == 1000 and payload["turn_cost"] == {"standard": 30} and payload["total"] == 200
    assert payload["checkin"]["claimable"] is True and payload["checkin"]["grant"] == 200
    assert (payload["checkin"]["would_gain"], payload["checkin"]["would_lose"]) == (200, 0)
    assert payload["checkin"]["next_reset_at"].endswith("Z") and "id" not in payload


def test_seq_equals_the_wallet_version_and_stays_gapless_across_operation_kinds(wallet_kit):
    player = wallet_kit.player(1)
    wallet_kit.service.view(player)
    wallet_kit.service.checkin(player)
    wallet_kit.service.hold(player, key="turn:1")
    wallet_kit.service.refund(player, "turn:1")
    seqs = [e.seq for e in wallet_kit.all_entries(player)]
    assert seqs == [1, 2, 3, 4] and wallet_kit.wallets.state(player.id).version == 4
