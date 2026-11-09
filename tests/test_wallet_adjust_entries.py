"""game-wallet 規格「管理員調整餘額不受上限限制且不得為負」與 wallet-api 規格「遊戲幣記錄」的 Service 層行為
（端點在 test_wallet_api.py）。SQLite 與真實 SQL Server 各跑一次。
"""
import pytest

from app.models.wallet import ENTRY_KINDS
from app.services import wallet_messages
from app.services.errors import InsufficientCoins, ValidationError


# ---- 管理員調整 -------------------------------------------------------------------------------------


def test_adding_paid_coins_writes_an_admin_adjust_entry(wallet_kit):
    player = wallet_kit.player(1)
    wallet_kit.service.view(player)
    assert wallet_kit.service.adjust(player.id, bucket="paid", delta=500) == (200, 500)
    entry = wallet_kit.all_entries(player)[-1]
    assert (entry.kind, entry.free_delta, entry.paid_delta, entry.paid_after) == ("admin_adjust", 0, 500, 500)
    assert wallet_kit.ledger_matches_balances(player)


def test_subtracting_free_coins_works_down_to_exactly_zero(wallet_kit):
    player = wallet_kit.player(1)
    wallet_kit.service.view(player)
    assert wallet_kit.service.adjust(player.id, bucket="free", delta=-200) == (0, 0)


def test_taking_more_than_the_balance_is_refused_and_leaves_no_entry(wallet_kit):
    player = wallet_kit.player(1)
    wallet_kit.service.view(player)
    before = wallet_kit.all_entries(player)
    with pytest.raises(InsufficientCoins):
        wallet_kit.service.adjust(player.id, bucket="free", delta=-300)
    with pytest.raises(InsufficientCoins):
        wallet_kit.service.adjust(player.id, bucket="paid", delta=-1)
    assert wallet_kit.all_entries(player) == before and wallet_kit.balances(player) == (200, 0)


def test_an_adjustment_may_exceed_the_free_cap(wallet_kit):
    player = wallet_kit.player(1)
    wallet_kit.service.view(player)
    assert wallet_kit.service.adjust(player.id, bucket="free", delta=1300) == (1500, 0)


@pytest.mark.parametrize("bucket, delta", [("gold", 5), ("", 5), (None, 5), ("FREE", 5), ("free", 0), ("free", 1.5), ("free", "5"), ("free", True)])
def test_invalid_adjustments_are_refused(wallet_kit, bucket, delta):
    player = wallet_kit.player(1)
    wallet_kit.service.view(player)
    with pytest.raises(ValidationError):
        wallet_kit.service.adjust(player.id, bucket=bucket, delta=delta)
    assert wallet_kit.balances(player) == (200, 0)


def test_each_adjustment_is_its_own_entry_even_when_the_same_amount_is_repeated(wallet_kit):
    player = wallet_kit.player(1)
    wallet_kit.service.view(player)
    wallet_kit.service.adjust(player.id, bucket="free", delta=10)
    wallet_kit.service.adjust(player.id, bucket="free", delta=10)
    assert wallet_kit.balances(player) == (220, 0)
    assert [e.kind for e in wallet_kit.all_entries(player)].count("admin_adjust") == 2


def test_adjusting_a_player_without_a_wallet_creates_an_empty_one_without_granting_anything(wallet_kit):
    """管理員調整不是玩家的存取：不補發贈幣；之後玩家第一次存取時才補發。"""
    player = wallet_kit.player(1)
    assert wallet_kit.service.adjust(player.id, bucket="paid", delta=100) == (0, 100)
    assert [e.kind for e in wallet_kit.all_entries(player)] == ["admin_adjust"]
    assert wallet_kit.service.view(player).free_balance == 200


# ---- 記錄 -------------------------------------------------------------------------------------------


def make_history(kit, player):
    """註冊贈送 → 簽到 → 一輪冒險（已結算）→ 一輪失敗（已退回）→ 一輪處理中。"""
    service = kit.service
    service.checkin(player)
    service.hold(player, key="turn:done")
    service.settle(player, "turn:done")
    service.hold(player, key="turn:failed")
    service.refund(player, "turn:failed")
    kit.clock.advance(seconds=1)
    service.hold(player, key="turn:open")


def test_the_listing_is_newest_first_with_chinese_labels(wallet_kit):
    player = wallet_kit.player(1)
    make_history(wallet_kit, player)
    page = wallet_kit.service.entries(player)
    assert [item["label"] for item in page.items] == ["處理中", "冒險回合", "每日簽到", "註冊贈送"]
    assert [item["free_delta"] for item in page.items] == [-30, -30, 200, 200]
    assert [item["pending"] for item in page.items] == [True, False, False, False]
    assert page.next_before is None


def test_refunded_holds_and_refund_rows_are_not_listed_but_the_export_still_has_them(wallet_kit):
    player = wallet_kit.player(1)
    make_history(wallet_kit, player)
    listed = {item["kind"] for item in wallet_kit.service.entries(player).items}
    assert "turn_refund" not in listed
    assert len(wallet_kit.service.entries(player).items) == 4
    assert len(wallet_kit.all_entries(player)) == 6                           # 另含已退回的預扣與它的退回列


def test_the_listing_pages_with_the_users_own_cursor(wallet_kit):
    player = wallet_kit.player(1)
    wallet_kit.service.view(player)
    for index in range(44):
        wallet_kit.service.adjust(player.id, bucket="free", delta=1)
    first = wallet_kit.service.entries(player)
    assert len(first.items) == 20 and first.next_before is not None
    second = wallet_kit.service.entries(player, before_seq=first.next_before)
    third = wallet_kit.service.entries(player, before_seq=second.next_before)
    assert (len(second.items), len(third.items), third.next_before) == (20, 5, None)
    seqs = [item["seq"] for page in (first, second, third) for item in page.items]
    assert seqs == sorted(seqs, reverse=True) and len(set(seqs)) == 45


def test_the_page_size_is_clamped(wallet_kit):
    player = wallet_kit.player(1)
    wallet_kit.service.view(player)
    for _ in range(5):
        wallet_kit.service.adjust(player.id, bucket="free", delta=1)
    assert len(wallet_kit.service.entries(player, limit=2).items) == 2
    assert len(wallet_kit.service.entries(player, limit=0).items) == 1
    assert len(wallet_kit.service.entries(player, limit=10_000).items) == 6


def test_the_listing_never_shows_idempotency_keys_internal_ids_or_other_players(wallet_kit):
    player, other = wallet_kit.player(1), wallet_kit.player(2)
    wallet_kit.service.hold(player, key="turn:secret-key", session_public_id="a" * 36)
    wallet_kit.service.hold(other, key="turn:other")
    page = wallet_kit.service.entries(player)
    for item in page.items:
        assert set(item) == {"kind", "label", "pending", "free_delta", "paid_delta", "created_at", "seq"}
    assert "secret-key" not in repr(page) and "a" * 36 not in repr(page)
    assert len(page.items) == 2                                                # 自己的註冊贈送與處理中的預扣，沒有別人的


def test_an_open_hold_shows_as_processing_and_becomes_an_adventure_once_settled(wallet_kit):
    player = wallet_kit.player(1)
    wallet_kit.service.hold(player, key="turn:a")
    assert wallet_kit.service.entries(player).items[0]["label"] == "處理中"
    wallet_kit.service.settle(player, "turn:a")
    assert wallet_kit.service.entries(player).items[0]["label"] == "冒險回合"


# ---- 說明文字 ---------------------------------------------------------------------------------------


def test_every_kind_has_a_chinese_label_without_english_codes():
    for kind in ENTRY_KINDS:
        for resolution in (None, "settled", "refunded"):
            text = wallet_messages.label(kind, resolution)
            assert text and not any("a" <= ch.lower() <= "z" for ch in text), (kind, resolution, text)


def test_an_unknown_kind_from_corrupt_data_still_gets_a_label():
    assert wallet_messages.label("something_new", None) == "其他"


def test_the_label_table_covers_exactly_the_ledger_kinds():
    assert tuple(wallet_messages._LABELS) == ENTRY_KINDS
