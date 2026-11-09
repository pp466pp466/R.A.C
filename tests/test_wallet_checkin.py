"""game-wallet 規格「每日簽到」「簽到超過上限時須先確認」「免費幣上限只限制新發放」。SQLite 與真實 SQL Server 各跑一次。

假時鐘預設是 2026-10-27 12:00 UTC（台北 20:00）；台北日界用明確的 UTC 時間表示（台北 00:00 = 前一日 16:00 UTC）。
"""
from datetime import datetime, timezone

import pytest

from app.services.errors import AlreadyCheckedIn, CheckinOverflow
from app.services.wallet_service import checkin_amounts


def utc(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def give_free(kit, player, amount):
    """把免費餘額調到指定的值（管理員調整不受上限限制）。"""
    kit.service.view(player)
    free, _paid = kit.balances(player)
    if amount != free:
        kit.service.adjust(player.id, bucket="free", delta=amount - free)


# ---- 純函式 ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "free, grant, cap, expected",
    [
        (0, 200, 1000, (200, 0)), (800, 200, 1000, (200, 0)), (801, 200, 1000, (199, 1)), (900, 200, 1000, (100, 100)),
        (1000, 200, 1000, (0, 200)), (1030, 200, 1000, (0, 200)), (0, 200, 0, (0, 200)), (500, 0, 1000, (0, 0)),
    ],
)
def test_checkin_amounts_is_the_smaller_of_the_grant_and_the_room_under_the_cap(free, grant, cap, expected):
    assert checkin_amounts(free, grant, cap) == expected


# ---- 每日簽到 ---------------------------------------------------------------------------------------


def test_an_ordinary_checkin_adds_the_grant_and_records_one_entry(wallet_kit):
    player = wallet_kit.player(1)
    wallet_kit.service.view(player)
    give_free(wallet_kit, player, 300)
    view = wallet_kit.service.checkin(player)
    assert view.free_balance == 500
    entries = [e for e in wallet_kit.all_entries(player) if e.kind == "daily_checkin"]
    assert len(entries) == 1 and entries[0].free_delta == 200
    assert wallet_kit.ledger_matches_balances(player)


def test_a_second_checkin_on_the_same_taipei_day_is_refused_and_changes_nothing(wallet_kit):
    player = wallet_kit.player(1)
    wallet_kit.service.checkin(player)
    before = wallet_kit.balances(player)
    with pytest.raises(AlreadyCheckedIn):
        wallet_kit.service.checkin(player)
    assert wallet_kit.balances(player) == before
    assert wallet_kit.service.view(player).checkin.claimable is False


def test_the_taipei_midnight_is_the_day_boundary(wallet_kit):
    player = wallet_kit.player(1)
    wallet_kit.clock.now = utc(2026, 10, 27, 15, 59)                       # 台北 23:59
    wallet_kit.service.checkin(player)
    with pytest.raises(AlreadyCheckedIn):
        wallet_kit.service.checkin(player)
    wallet_kit.clock.now = utc(2026, 10, 27, 16, 1)                        # 台北 00:01（隔日）
    assert wallet_kit.service.view(player).checkin.claimable is True
    wallet_kit.service.checkin(player)
    assert [e.kind for e in wallet_kit.all_entries(player)].count("daily_checkin") == 2


def test_the_checkin_that_was_just_before_utc_midnight_but_after_taipei_midnight_belongs_to_the_taipei_day(wallet_kit):
    """UTC 還是 10/27 的 20:00，台北已是 10/28 04:00：日界以台北計，不是 UTC。"""
    player = wallet_kit.player(1)
    wallet_kit.clock.now = utc(2026, 10, 27, 9)                            # 台北 17:00，10/27
    wallet_kit.service.checkin(player)
    wallet_kit.clock.now = utc(2026, 10, 27, 20)                           # 台北 04:00，10/28
    wallet_kit.service.checkin(player)                                     # 是新的一天，成功
    assert [e.kind for e in wallet_kit.all_entries(player)].count("daily_checkin") == 2


def test_the_preview_reports_when_the_next_checkin_opens(wallet_kit):
    player = wallet_kit.player(1)
    wallet_kit.clock.now = utc(2026, 10, 27, 12)
    wallet_kit.service.checkin(player)
    view = wallet_kit.service.view(player).to_dict()
    assert view["checkin"]["claimable"] is False and view["checkin"]["next_reset_at"] == "2026-10-27T16:00:00Z"
    assert (view["checkin"]["would_gain"], view["checkin"]["would_lose"]) == (0, 0)


def test_the_grant_amount_is_read_from_the_setting_table(wallet_kit):
    wallet_kit.set_limit("coin.checkin_grant", 100)
    player = wallet_kit.player(1)
    wallet_kit.service.view(player)
    assert wallet_kit.service.checkin(player).free_balance == 300


# ---- 超過上限 ---------------------------------------------------------------------------------------


def test_a_checkin_that_would_lose_coins_needs_confirmation_and_writes_nothing_without_it(wallet_kit):
    player = wallet_kit.player(1)
    give_free(wallet_kit, player, 900)
    before = wallet_kit.all_entries(player)
    with pytest.raises(CheckinOverflow) as caught:
        wallet_kit.service.checkin(player)
    assert (caught.value.would_gain, caught.value.would_lose) == (100, 100)
    assert wallet_kit.all_entries(player) == before and wallet_kit.balances(player) == (900, 0)
    assert wallet_kit.service.view(player).checkin.claimable is True       # 沒有消耗當日簽到


def test_confirming_the_overflow_claims_up_to_the_cap_and_forfeits_the_rest(wallet_kit):
    player = wallet_kit.player(1)
    give_free(wallet_kit, player, 900)
    view = wallet_kit.service.checkin(player, confirm_overflow=True)
    assert view.free_balance == 1000
    entry = [e for e in wallet_kit.all_entries(player) if e.kind == "daily_checkin"][0]
    assert entry.free_delta == 100 and entry.free_after == 1000


def test_claiming_at_the_cap_gains_nothing_but_uses_up_the_day(wallet_kit):
    player = wallet_kit.player(1)
    give_free(wallet_kit, player, 1000)
    with pytest.raises(CheckinOverflow) as caught:
        wallet_kit.service.checkin(player)
    assert (caught.value.would_gain, caught.value.would_lose) == (0, 200)
    wallet_kit.service.checkin(player, confirm_overflow=True)
    assert wallet_kit.balances(player) == (1000, 0)
    assert [e.free_delta for e in wallet_kit.all_entries(player) if e.kind == "daily_checkin"] == [0]
    with pytest.raises(AlreadyCheckedIn):
        wallet_kit.service.checkin(player, confirm_overflow=True)


def test_cancelling_leaves_the_day_open_and_a_later_checkin_uses_the_balance_at_that_time(wallet_kit):
    player = wallet_kit.player(1)
    give_free(wallet_kit, player, 900)
    with pytest.raises(CheckinOverflow):
        wallet_kit.service.checkin(player)                                  # 玩家看到提醒後取消
    for index in range(4):                                                  # 花掉 120 幣（4 輪）
        wallet_kit.service.hold(player, key=f"turn:{index}")
        wallet_kit.service.settle(player, f"turn:{index}")
    assert wallet_kit.balances(player) == (780, 0)
    view = wallet_kit.service.checkin(player)                               # 現在領得下完整的 200，不需要確認
    assert view.free_balance == 980


def test_the_confirmation_recomputes_from_the_balance_at_that_moment_not_from_the_preview(wallet_kit):
    player = wallet_kit.player(1)
    give_free(wallet_kit, player, 900)
    preview = wallet_kit.service.view(player).checkin
    assert (preview.would_gain, preview.would_lose) == (100, 100)
    wallet_kit.service.adjust(player.id, bucket="free", delta=-500)         # 預覽之後餘額變成 400
    view = wallet_kit.service.checkin(player, confirm_overflow=True)
    assert view.free_balance == 600 and [e.free_delta for e in wallet_kit.all_entries(player) if e.kind == "daily_checkin"] == [200]


def test_no_confirmation_is_needed_when_nothing_is_lost(wallet_kit):
    player = wallet_kit.player(1)
    give_free(wallet_kit, player, 800)
    assert wallet_kit.service.checkin(player).free_balance == 1000          # 剛好補滿，沒有作廢


def test_an_unconfirmed_checkin_never_reaches_the_ledger_even_when_the_balance_changes_during_the_attempt(wallet_kit):
    """沒有確認時，無論重試幾次都不會悄悄作廢：平常用的是同一個 plan，作廢判斷在 CAS 迴圈內。"""
    player = wallet_kit.player(1)
    give_free(wallet_kit, player, 950)
    with pytest.raises(CheckinOverflow):
        wallet_kit.service.checkin(player, confirm_overflow=False)
    assert [e.kind for e in wallet_kit.all_entries(player)].count("daily_checkin") == 0


# ---- 免費幣上限只限制新發放 -------------------------------------------------------------------------------


def test_a_refund_is_not_truncated_by_the_cap(wallet_kit):
    """免費餘額 1000 的玩家有一筆 30 幣的預扣（970），期間簽到補回 30（1000），之後預扣被退回 30：餘額 1030。"""
    player = wallet_kit.player(1)
    give_free(wallet_kit, player, 1000)
    wallet_kit.service.hold(player, key="turn:a")                            # 970
    wallet_kit.set_limit("coin.checkin_grant", 30)
    wallet_kit.service.checkin(player)                                       # 回到 1000
    assert wallet_kit.balances(player) == (1000, 0)
    wallet_kit.service.refund(player, "turn:a")
    assert wallet_kit.balances(player) == (1030, 0)


def test_an_administrator_adjustment_is_not_limited_by_the_cap_and_checkin_then_adds_nothing(wallet_kit):
    player = wallet_kit.player(1)
    give_free(wallet_kit, player, 1500)
    assert wallet_kit.balances(player) == (1500, 0)
    view = wallet_kit.service.view(player).checkin
    assert (view.would_gain, view.would_lose) == (0, 200)


def test_the_cap_does_not_apply_to_the_paid_balance(wallet_kit):
    player = wallet_kit.player(1)
    wallet_kit.service.view(player)
    wallet_kit.service.adjust(player.id, bucket="paid", delta=5000)
    assert wallet_kit.balances(player)[1] == 5000
    assert wallet_kit.service.view(player).checkin.would_lose == 0
