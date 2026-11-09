"""遊戲幣錢包的結構化日誌事件（見 service-observability 規格「遊戲幣事件只含白名單欄位」與 game-wallet 的 design D10）。

欄位限於「事件名稱、內部使用者編號、整數的幣量與餘額、固定列舉的餘額種類與模型鍵」。**絕不含**冪等鍵（它帶有存檔識別碼與
前端產生的訊息編號）、存檔識別碼、管理員的理由、email、暱稱或任何玩家文字——這裡的函式根本沒有接收這些的參數，所以不是
「記得不要放」，而是「放不進來」。傳入列舉以外的值或不是整數的幣量會立刻出錯，日誌裡因此不會出現自由文字。
"""
from __future__ import annotations

import logging

from app.models.wallet import BUCKETS

logger = logging.getLogger("app.wallet")

EVENT_SIGNUP_GRANTED = "wallet.signup_granted"
EVENT_CHECKIN = "wallet.checkin"
EVENT_CHECKIN_OVERFLOW = "wallet.checkin_overflow"
EVENT_HOLD = "wallet.hold"
EVENT_SETTLE = "wallet.settle"
EVENT_REFUND = "wallet.refund"
EVENT_STALE_RELEASED = "wallet.stale_released"
EVENT_INSUFFICIENT = "wallet.insufficient"
EVENT_ADJUST = "wallet.adjust"
EVENT_GRANT_DEFERRED = "wallet.grant_deferred"

EVENT_NAMES = frozenset(
    value for name, value in dict(globals()).items() if name.startswith("EVENT_") and isinstance(value, str)
)

# 可以出現在日誌裡的模型鍵（固定列舉；與 `runtime_limits.WALLET_MODEL_KEYS` 一致，由測試比對）
MODEL_KEYS = ("standard",)


def _integer(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必須是整數：{value!r}")
    return value


def _info(event: str, message: str, **fields: object) -> None:
    logger.info(message, extra={"event": event, **fields})


def log_signup_granted(*, user_id: int, coins: int) -> None:
    _info(EVENT_SIGNUP_GRANTED, "首次註冊贈幣已發放", user_id=user_id, coins=_integer("coins", coins))


def log_checkin(*, user_id: int, coins: int, lost_coins: int) -> None:
    _info(EVENT_CHECKIN, "每日簽到", user_id=user_id, coins=_integer("coins", coins), lost_coins=_integer("lost_coins", lost_coins))


def log_checkin_overflow(*, user_id: int, coins: int, lost_coins: int) -> None:
    """簽到會使一部分幣作廢，玩家尚未確認，沒有執行。"""
    _info(
        EVENT_CHECKIN_OVERFLOW, "簽到需要確認（會有幣作廢）", user_id=user_id, coins=_integer("coins", coins),
        lost_coins=_integer("lost_coins", lost_coins),
    )


def log_hold(*, user_id: int, coins: int, model_key: str) -> None:
    if model_key not in MODEL_KEYS:
        raise ValueError(f"未知的模型鍵：{model_key!r}")
    _info(EVENT_HOLD, "遊戲幣已預扣", user_id=user_id, coins=_integer("coins", coins), model=model_key)


def log_settle(*, user_id: int, coins: int) -> None:
    _info(EVENT_SETTLE, "預扣已結算", user_id=user_id, coins=_integer("coins", coins))


def log_refund(*, user_id: int, coins: int) -> None:
    _info(EVENT_REFUND, "預扣已退回", user_id=user_id, coins=_integer("coins", coins))


def log_stale_released(*, user_id: int, coins: int) -> None:
    """逾時未結算的預扣，在玩家下一次存取錢包時被退回。"""
    logger.warning("逾時的預扣已退回", extra={"event": EVENT_STALE_RELEASED, "user_id": user_id, "coins": _integer("coins", coins)})


def log_insufficient(*, user_id: int, balance: int, coins: int) -> None:
    """餘額不足被拒絕：`balance` 是合計餘額，`coins` 是所需的數量。"""
    _info(EVENT_INSUFFICIENT, "遊戲幣不足", user_id=user_id, balance=_integer("balance", balance), coins=_integer("coins", coins))


def log_adjust(*, user_id: int, bucket: str, coins: int, balance: int) -> None:
    """管理員調整：`bucket` 是 `free` 或 `paid`，`coins` 是增減量（可為負），`balance` 是該餘額調整後的數量。"""
    if bucket not in BUCKETS:
        raise ValueError(f"未知的餘額種類：{bucket!r}")
    _info(EVENT_ADJUST, "遊戲幣餘額已調整", user_id=user_id, kind=bucket, coins=_integer("coins", coins), balance=_integer("balance", balance))


def log_grant_deferred(*, user_id: int) -> None:
    """年齡確認後發放贈幣失敗：不影響年齡確認，贈幣會在玩家第一次存取錢包時補發。"""
    logger.warning("首次註冊贈幣暫時無法發放，將於下次存取錢包時補發", extra={"event": EVENT_GRANT_DEFERRED, "user_id": user_id})
