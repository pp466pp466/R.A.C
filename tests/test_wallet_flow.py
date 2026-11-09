"""game-wallet 的端對端流程與日誌掃描（service-observability 規格「遊戲幣事件只含白名單欄位」的完整流程驗證）。

流程：新玩家登入 -> 補填年齡確認（得 200 贈幣）-> 簽到 -> 連續三個回合各預扣再結算 -> 一個回合失敗被退回 -> 一個回合還在處理中 ->
接近上限的簽到（需要確認、取消、再確認）-> 餘額不足 -> 管理員調整（含密碼錯誤與扣到負數）-> 記錄頁與管理員檢視 -> 個人資料匯出 ->
抹除。結束後掃描完整的日誌輸出：找不到冪等鍵、存檔識別碼、管理員的理由與暱稱，每個事件只有白名單內的欄位。

回合的預扣、結算與退回沒有 HTTP 端點（由日後的回合引擎呼叫），所以這裡直接呼叫 Service，與 HTTP 端點共用同一個資料庫。
"""
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import create_app
from app.config import load_config
from app.models import AdminAuditLog, Base, User, Wallet, WalletEntry
from app.observability.json_logging import ALLOWED_FIELDS
from app.repositories.deletion_tombstone_repository import DeletionTombstoneRepository
from app.repositories.user_repository import UserRepository
from app.services.account_erasure_service import AccountErasureService
from app.services.current_user import CurrentUser
from app.services.errors import InsufficientCoins
from tests.support.age import age_fields
from tests.support.auth import GOOD_PASSWORD, START, FakeClock, add_user, sign_in_as
from tests.support.wallet import WalletKit

BASE_KEYS = {"time", "level", "logger", "message"}
SECRET_KEY = "turn:FLOW-KEY-SECRET-DO-NOT-LOG"
SECRET_SESSION = "00000000-0000-4000-8000-FLOWSESSION1"
SECRET_REASON = "活動補償FLOW-REASON-SECRET"
SECRET_NICKNAME = "流程暱稱FLOW-NICK-SECRET"


@pytest.fixture()
def world():
    app = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
    Base.metadata.create_all(app.extensions["db_engine"])
    return app


def kit_for(app):
    """與應用程式共用資料庫的錢包 Service（自己的 Session；每次操作後提交，讓 HTTP 端看得到）。"""
    session = Session(app.extensions["db_engine"])
    return WalletKit(session), session


def signed_in(app, user_id: int, token_version: int):
    client = app.test_client()
    sign_in_as(client, user_id, token_version)
    return client


def wallet(client) -> dict:
    response = client.get("/api/wallet")
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["wallet"]


def test_the_whole_wallet_lifecycle_adds_up_and_leaves_no_secret_in_the_logs(world, log_lines):
    app = world
    with Session(app.extensions["db_engine"]) as s:
        player = add_user(s, 1, age=None, password=None)
        admin = add_user(s, 2, role="admin", age=30)
        pid, ptv, aid, atv = player.id, player.token_version, admin.id, admin.token_version
    client = signed_in(app, pid, ptv)
    admin_client = signed_in(app, aid, atv)

    # 1. 完成年齡確認之前什麼都不能做，也沒有任何幣
    assert client.get("/api/wallet").status_code == 403
    verified = client.post("/api/auth/verify-age", json=age_fields(30, nickname=SECRET_NICKNAME))
    assert verified.status_code == 200, verified.get_data(as_text=True)

    # 2. 年齡確認通過：200 贈幣；簽到：+200
    assert wallet(client)["free_balance"] == 200
    assert client.post("/api/wallet/checkin", json={}).status_code == 200
    assert wallet(client)["free_balance"] == 400

    # 3. 回合：三輪結算、一輪失敗退回、一輪處理中
    kit, s = kit_for(app)
    me = CurrentUser.from_model(s.get(User, pid))
    for index in range(3):
        kit.service.hold(me, key=f"{SECRET_KEY}-{index}", session_public_id=SECRET_SESSION)
        kit.service.settle(me, f"{SECRET_KEY}-{index}")
    kit.service.hold(me, key=f"{SECRET_KEY}-failed", session_public_id=SECRET_SESSION)
    kit.service.refund(me, f"{SECRET_KEY}-failed")
    kit.clock.advance(seconds=1)
    kit.service.hold(me, key=f"{SECRET_KEY}-open", session_public_id=SECRET_SESSION)
    s.commit()
    body = wallet(client)
    assert body["free_balance"] == 400 - 4 * 30 and body["total"] == 280

    # 4. 記錄頁：處理中、三輪冒險、簽到、註冊贈送；已退回的不出現
    entries = client.get("/api/wallet/entries").get_json()["entries"]
    assert [(e["label"], e["free_delta"]) for e in entries] == [
        ("處理中", -30), ("冒險回合", -30), ("冒險回合", -30), ("冒險回合", -30), ("每日簽到", 200), ("註冊贈送", 200),
    ]

    # 5. 管理員調整：密碼錯誤、扣到負數、成功
    url = f"/api/admin/users/{s.get(User, pid).public_id}/wallet/adjust"
    wrong = admin_client.post(url, json={"password": "definitely-not-the-password", "bucket": "free", "delta": 10, "reason": SECRET_REASON})
    assert wrong.status_code == 400
    too_much = admin_client.post(url, json={"password": GOOD_PASSWORD, "bucket": "free", "delta": -99999, "reason": SECRET_REASON})
    assert too_much.status_code == 409 and too_much.get_json()["error"]["code"] == "insufficient_coins"
    adjusted = admin_client.post(url, json={"password": GOOD_PASSWORD, "bucket": "paid", "delta": 500, "reason": SECRET_REASON})
    assert adjusted.status_code == 200 and adjusted.get_json()["user"]["wallet"] == {"free_balance": 280, "paid_balance": 500}
    assert wallet(client)["paid_balance"] == 500

    # 6. 餘額不足：把幣價調高（部署者的設定），預扣被拒絕
    kit.set_limit("coin.turn_cost.standard", 5000)
    s.commit()
    with pytest.raises(InsufficientCoins):
        kit.service.hold(me, key=f"{SECRET_KEY}-too-expensive", session_public_id=SECRET_SESSION)
    s.rollback()
    kit.set_limit("coin.turn_cost.standard", 30)
    s.commit()

    # 7. 接近上限：隔日（錢包 Service 自己的時鐘前進一天）免費餘額在上限，簽到需要確認，確認後入帳 0（超出的作廢）
    kit.service.settle(me, f"{SECRET_KEY}-open")                                  # 處理中的預扣先結算，隔日才不會被當成逾時而退回
    s.commit()
    kit.clock.advance(days=1)
    with Session(app.extensions["db_engine"]) as other:
        other_kit = WalletKit(other)
        other_kit.service.adjust(pid, bucket="free", delta=1000 - 280)           # 免費餘額到 1000（管理員調整不受上限限制）
        other.commit()
    from app.services.errors import CheckinOverflow

    with pytest.raises(CheckinOverflow):
        kit.service.checkin(me)
    kit.service.checkin(me, confirm_overflow=True)
    s.commit()
    assert kit.balances(me) == (1000, 500)

    # 8. 個人資料匯出：含錢包，不含任何冪等鍵
    export = client.post("/api/account/export", json={"format": "json"})
    assert export.status_code == 200
    exported = json.loads(export.get_data(as_text=True))
    assert exported["wallet"]["free_balance"] == 1000 and exported["wallet"]["paid_balance"] == 500
    kinds = [e["kind"] for e in exported["wallet"]["entries"]]
    assert kinds.count("turn_refund") == 1 and kinds.count("turn_hold") == 5          # 含已退回的預扣與退回列

    # 9. 抹除：錢包與帳本一併消失，稽核紀錄保留（只有對外識別碼與固定內容）
    s.expire_all()
    s.get(User, pid).status = "deletion_pending"
    s.get(User, pid).deletion_requested_at = START - timedelta(days=8)
    s.commit()
    erased = AccountErasureService(
        users=UserRepository(s), tombstones=DeletionTombstoneRepository(s), commit=s.commit, rollback=s.rollback,
    ).erase_pending_due(pid, cutoff=START - timedelta(days=7), now=START)
    assert erased is True
    s.expire_all()
    assert s.scalar(select(func.count()).select_from(Wallet).where(Wallet.user_id == pid)) == 0
    assert s.scalar(select(func.count()).select_from(WalletEntry).where(WalletEntry.user_id == pid)) == 0
    assert s.scalar(select(func.count()).select_from(AdminAuditLog).where(AdminAuditLog.action == "wallet.adjust")) == 1
    s.close()

    # 日誌：沒有冪等鍵、存檔識別碼、管理員的理由與暱稱
    raw = log_lines.raw()
    for secret in (SECRET_KEY, "FLOW-KEY-SECRET", SECRET_SESSION, "FLOW-REASON-SECRET", SECRET_REASON, "FLOW-NICK-SECRET", SECRET_NICKNAME,
                   "signup:", "checkin:", "refund:", "admin:"):
        assert secret not in raw, f"日誌洩漏了：{secret}"
    wallet_events = {line["event"] for line in log_lines() if str(line.get("event", "")).startswith("wallet.")}
    assert {"wallet.signup_granted", "wallet.checkin", "wallet.hold", "wallet.settle", "wallet.refund", "wallet.insufficient",
            "wallet.adjust", "wallet.checkin_overflow"} <= wallet_events
    for line in log_lines():
        assert set(line) <= BASE_KEYS | set(ALLOWED_FIELDS), f"出現不允許的欄位：{set(line) - BASE_KEYS - set(ALLOWED_FIELDS)}"
