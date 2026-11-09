"""game-wallet 的資料模型約束（SQLite 與 SQL Server 兩軌各跑一次）：

餘額不得為負（`CHECK`）、錢包每位使用者一個、帳本的冪等鍵與 `(user_id, seq)` 唯一、刪除使用者串聯刪除錢包與帳本，
以及欄位型別（Unicode、UTC 時間）與固定列舉。
"""
import pytest
from sqlalchemy import Unicode, delete, func, select
from sqlalchemy.exc import IntegrityError

from app.models import Base, User, Wallet, WalletEntry
from app.models.base import UTCDateTime
from app.models.wallet import BUCKETS, ENTRY_KINDS, RESOLUTIONS


def table(name: str):
    return Base.metadata.tables[name]


def make_user(session, number: int) -> User:
    user = User(email=f"w{number}@example.com", email_canonical=f"w{number}@example.com", display_name=f"使用者{number}", status="active")
    session.add(user)
    session.flush()
    return user


def make_wallet(session, user: User, free: int = 0, paid: int = 0) -> Wallet:
    wallet = Wallet(user_id=user.id, free_balance=free, paid_balance=paid)
    session.add(wallet)
    session.flush()
    return wallet


def make_entry(session, user: User, seq: int, key: str, **extra) -> WalletEntry:
    values = dict(kind="signup_grant", free_delta=200, paid_delta=0, free_after=200, paid_after=0)
    values.update(extra)
    entry = WalletEntry(user_id=user.id, seq=seq, idempotency_key=key, **values)
    session.add(entry)
    session.flush()
    return entry


def expect_integrity_error(session, action) -> None:
    with pytest.raises(IntegrityError):
        action()
        session.flush()
    session.rollback()


# ---- 欄位與外鍵（不需要資料庫）---------------------------------------------------------------

def test_the_documented_columns_exist_and_the_optional_ones_are_nullable():
    wallets, entries = table("wallets"), table("wallet_entries")
    assert {c.name for c in wallets.columns} == {"id", "user_id", "free_balance", "paid_balance", "version", "created_at", "updated_at"}
    assert {c.name for c in entries.columns} == {
        "id", "user_id", "seq", "kind", "idempotency_key", "free_delta", "paid_delta", "free_after", "paid_after",
        "session_public_id", "ref_key", "resolution", "created_at",
    }
    for name in ("session_public_id", "ref_key", "resolution"):
        assert entries.c[name].nullable, name
    for name in ("seq", "kind", "idempotency_key", "free_delta", "paid_delta", "free_after", "paid_after"):
        assert not entries.c[name].nullable, name


def test_text_columns_are_unicode_and_times_are_utc():
    entries = table("wallet_entries")
    for name in ("kind", "idempotency_key", "session_public_id", "ref_key", "resolution"):
        assert isinstance(entries.c[name].type, Unicode), name
    assert isinstance(entries.c.created_at.type, UTCDateTime)
    assert isinstance(table("wallets").c.created_at.type, UTCDateTime) and isinstance(table("wallets").c.updated_at.type, UTCDateTime)


def test_both_tables_cascade_from_users_and_the_ledger_never_points_at_an_internal_session_id():
    for name in ("wallets", "wallet_entries"):
        foreign_key = next(iter(table(name).c.user_id.foreign_keys))
        assert foreign_key.column.table.name == "users" and foreign_key.ondelete == "CASCADE", name
    assert {fk.parent.name for fk in table("wallet_entries").foreign_keys} == {"user_id"}      # 沒有指向存檔的外鍵或內部編號欄位
    assert "session_id" not in table("wallet_entries").c


def test_the_enumerations_are_fixed():
    assert ENTRY_KINDS == ("signup_grant", "daily_checkin", "turn_hold", "turn_refund", "admin_adjust")
    assert RESOLUTIONS == ("settled", "refunded") and BUCKETS == ("free", "paid")


def test_the_longest_realistic_idempotency_key_fits_the_column():
    key = "turn:" + "a" * 36 + ":" + "b" * 64 + ":" + "9" * 3 + ":" + "9" * 3
    assert len(key) <= table("wallet_entries").c.idempotency_key.type.length


# ---- 資料庫約束（雙軌）-------------------------------------------------------------------------

def test_defaults_and_one_wallet_per_user(member_session):
    user = make_user(member_session, 1)
    wallet = make_wallet(member_session, user)
    assert (wallet.free_balance, wallet.paid_balance, wallet.version) == (0, 0, 0)
    assert wallet.created_at is not None and wallet.updated_at is not None
    expect_integrity_error(member_session, lambda: member_session.add(Wallet(user_id=user.id)))


@pytest.mark.parametrize("free, paid", [(-1, 0), (0, -1), (-5, -5)])
def test_a_negative_balance_is_refused_by_the_database(member_session, free, paid):
    user = make_user(member_session, 1)
    expect_integrity_error(member_session, lambda: member_session.add(Wallet(user_id=user.id, free_balance=free, paid_balance=paid)))


def test_zero_and_large_balances_are_legal(member_session):
    wallet = make_wallet(member_session, make_user(member_session, 1), free=0, paid=2_000_000_000)
    assert wallet.paid_balance == 2_000_000_000


def test_the_idempotency_key_is_unique_across_the_whole_site(member_session):
    first, second = make_user(member_session, 1), make_user(member_session, 2)
    make_entry(member_session, first, 1, "signup:1")
    expect_integrity_error(member_session, lambda: member_session.add(
        WalletEntry(user_id=second.id, seq=1, kind="signup_grant", idempotency_key="signup:1", free_after=0, paid_after=0)))


def test_seq_is_unique_per_user_but_not_across_users(member_session):
    first, second = make_user(member_session, 1), make_user(member_session, 2)
    make_entry(member_session, first, 1, "k1")
    make_entry(member_session, second, 1, "k2")                    # 另一位使用者可以有同樣的序號
    make_entry(member_session, first, 2, "k3")
    expect_integrity_error(member_session, lambda: member_session.add(
        WalletEntry(user_id=first.id, seq=2, kind="daily_checkin", idempotency_key="k4", free_after=0, paid_after=0)))


def test_unicode_survives_in_the_optional_text_columns(member_session):
    entry = make_entry(member_session, make_user(member_session, 1), 1, "k", session_public_id="a" * 36, ref_key="turn:中文:鍵")
    member_session.refresh(entry)
    assert entry.ref_key == "turn:中文:鍵" and entry.resolution is None


def test_deleting_a_user_deletes_the_wallet_and_its_ledger_but_not_other_users(member_session):
    keep, drop = make_user(member_session, 1), make_user(member_session, 2)
    for user, key in ((keep, "keep"), (drop, "drop")):
        make_wallet(member_session, user, free=200)
        make_entry(member_session, user, 1, key)
    member_session.commit()
    member_session.execute(delete(User).where(User.id == drop.id))
    member_session.commit()
    assert member_session.scalar(select(func.count()).select_from(Wallet)) == 1
    assert member_session.scalar(select(func.count()).select_from(WalletEntry)) == 1
    assert member_session.scalar(select(WalletEntry.idempotency_key)) == "keep"
