"""personal-data-export 規格「匯出納入錢包與帳本」：JSON 的 `wallet` 區塊與 CSV 的 `wallet_entries.csv` 含兩個餘額與全部帳本
（含已退回的預扣與退回列），不含冪等鍵與內部編號，公式字元被中和，沒有錢包的使用者匯出後仍沒有錢包。
SQLite 與真實 SQL Server 各跑一次。
"""
import csv
import io
import json
import zipfile

import pytest
from sqlalchemy import func, select

from app.models import User, Wallet
from app.repositories.chat_repository import ChatRepository
from app.repositories.lock_appeal_repository import LockAppealRepository
from app.repositories.script_repository import ScriptRepository
from app.repositories.script_submission_repository import ScriptSubmissionRepository
from app.repositories.session_repository import SessionRepository
from app.repositories.user_identity_repository import UserIdentityRepository
from app.repositories.user_repository import UserRepository
from app.repositories.wallet_repository import WalletRepository
from app.services.current_user import CurrentUser
from app.services.personal_export_service import WALLET_ENTRY_COLUMNS, PersonalExportService
from tests.support.auth import FakeClock
from tests.support.wallet import WalletKit

SESSION_ID = "1b4e28ba-2fa1-11d2-883f-0016d3cca427"
KEY_SECRET = "turn:KEY-SECRET-DO-NOT-EXPORT"


def service_for(session, *, with_wallets: bool = True) -> PersonalExportService:
    return PersonalExportService(
        users=UserRepository(session), identities=UserIdentityRepository(session), chats=ChatRepository(session),
        appeals=LockAppealRepository(session), scripts=ScriptRepository(session), sessions=SessionRepository(session),
        submissions=ScriptSubmissionRepository(session), clock=FakeClock(), wallets=WalletRepository(session) if with_wallets else None,
    )


def export_json(session, user_id: int, **kwargs) -> tuple[dict, str]:
    session.expire_all()
    result = service_for(session, **kwargs).build(CurrentUser.from_model(session.get(User, user_id)), "json")
    raw = result.handle.read()
    result.handle.close()
    return json.loads(raw.decode("utf-8")), raw.decode("utf-8")


def export_csv(session, user_id: int) -> dict[str, bytes]:
    session.expire_all()
    result = service_for(session).build(CurrentUser.from_model(session.get(User, user_id)), "csv")
    with zipfile.ZipFile(result.handle) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    result.handle.close()
    return files


def rows_of(raw: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))


def history(kit, player):
    """註冊贈送 200、簽到 200、一輪已結算（-30）、一輪失敗已退回、一輪處理中（-30）、管理員調整付費 +500。"""
    service = kit.service
    service.checkin(player)
    service.hold(player, key="turn:done", session_public_id=SESSION_ID)
    service.settle(player, "turn:done")
    service.hold(player, key=KEY_SECRET, session_public_id=SESSION_ID)
    service.refund(player, KEY_SECRET)
    service.hold(player, key="turn:open", session_public_id=SESSION_ID)
    service.adjust(player.id, bucket="paid", delta=500)


@pytest.fixture()
def kit(member_session):
    return WalletKit(member_session)


# ---- JSON ----------------------------------------------------------------------------------------


def test_the_json_export_has_the_balances_and_every_ledger_row_including_the_refunded_hold_and_its_refund(kit, member_session):
    player = kit.player(1)
    history(kit, player)
    member_session.commit()
    data, _raw = export_json(member_session, player.id)
    wallet = data["wallet"]
    assert (wallet["free_balance"], wallet["paid_balance"]) == (340, 500)
    assert [e["kind"] for e in wallet["entries"]] == [
        "signup_grant", "daily_checkin", "turn_hold", "turn_hold", "turn_refund", "turn_hold", "admin_adjust",
    ]
    refunded = wallet["entries"][3]
    assert refunded["resolution"] == "refunded" and wallet["entries"][4]["free_delta"] == 30      # 已退回的預扣與它的退回列都在
    assert [e["resolution"] for e in wallet["entries"]][:3] == [None, None, "settled"]
    assert wallet["entries"][-1]["paid_after"] == 500 and wallet["entries"][2]["session_id"] == SESSION_ID


def test_every_json_entry_has_exactly_the_documented_fields_and_no_idempotency_key_or_internal_number(kit, member_session):
    player = kit.player(1)
    history(kit, player)
    member_session.commit()
    data, raw = export_json(member_session, player.id)
    for entry in data["wallet"]["entries"]:
        assert set(entry) == {"kind", "free_delta", "paid_delta", "free_after", "paid_after", "resolution", "session_id", "created_at"}
    for secret in (KEY_SECRET, "signup:", "checkin:", "refund:", "admin:", "idempotency", "ref_key", "\"seq\"", "\"user_id\""):
        assert secret not in raw, secret


def test_the_other_players_wallet_never_leaks_into_the_export(kit, member_session):
    first, second = kit.player(1), kit.player(2)
    history(kit, first)
    kit.service.hold(second, key="turn:second-only")
    member_session.commit()
    data, raw = export_json(member_session, second.id)
    assert (data["wallet"]["free_balance"], len(data["wallet"]["entries"])) == (170, 2) and "turn:done" not in raw


def test_a_player_who_never_touched_the_wallet_gets_zeros_and_an_empty_ledger_and_no_wallet_is_created(kit, member_session):
    player = kit.player(1)
    member_session.commit()
    data, _raw = export_json(member_session, player.id)
    assert data["wallet"] == {"free_balance": 0, "paid_balance": 0, "entries": []}
    assert member_session.scalar(select(func.count()).select_from(Wallet)) == 0                  # 匯出沒有建立錢包，也沒有贈幣


def test_an_export_without_the_wallet_component_still_works_and_shows_zeros(kit, member_session):
    player = kit.player(1)
    member_session.commit()
    data, _raw = export_json(member_session, player.id, with_wallets=False)
    assert data["wallet"] == {"free_balance": 0, "paid_balance": 0, "entries": []}


def test_the_json_is_still_valid_utf8_without_a_bom_with_the_wallet_block_last_of_the_known_blocks(kit, member_session):
    player = kit.player(1)
    kit.service.view(player)
    member_session.commit()
    data, raw = export_json(member_session, player.id)
    assert list(data)[-1] == "wallet" and not raw.startswith("﻿")


# ---- CSV -----------------------------------------------------------------------------------------


def test_the_csv_export_has_one_row_per_ledger_entry_with_the_documented_columns(kit, member_session):
    player = kit.player(1)
    history(kit, player)
    member_session.commit()
    files = export_csv(member_session, player.id)
    assert "wallet_entries.csv" in files and files["wallet_entries.csv"].startswith(b"\xef\xbb\xbf")
    rows = rows_of(files["wallet_entries.csv"])
    assert tuple(rows[0]) == WALLET_ENTRY_COLUMNS and len(rows) == 1 + 7
    assert rows[1][0] == "signup_grant" and rows[-1][0] == "admin_adjust"
    text = files["wallet_entries.csv"].decode("utf-8-sig")
    for secret in (KEY_SECRET, "signup:", "idempotency", "ref_key"):
        assert secret not in text


def test_the_csv_neutralises_formula_characters_in_text_cells(kit, member_session):
    player = kit.player(1)
    kit.service.hold(player, key="turn:a", session_public_id="=SUM(1+1)")
    member_session.commit()
    rows = rows_of(export_csv(member_session, player.id)["wallet_entries.csv"])
    assert rows[-1][WALLET_ENTRY_COLUMNS.index("session_id")] == "'=SUM(1+1)"


def test_the_csv_of_a_player_without_a_wallet_has_only_the_header_and_creates_nothing(kit, member_session):
    player = kit.player(1)
    member_session.commit()
    rows = rows_of(export_csv(member_session, player.id)["wallet_entries.csv"])
    assert len(rows) == 1 and tuple(rows[0]) == WALLET_ENTRY_COLUMNS
    assert member_session.scalar(select(func.count()).select_from(Wallet)) == 0


def test_a_long_ledger_is_exported_completely_across_pages(kit, member_session):
    player = kit.player(1)
    kit.service.view(player)
    for _ in range(620):                                                         # 超過每頁 500 筆
        kit.service.adjust(player.id, bucket="free", delta=1)
    member_session.commit()
    data, _raw = export_json(member_session, player.id)
    assert len(data["wallet"]["entries"]) == 621 and data["wallet"]["free_balance"] == 820
