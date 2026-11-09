"""service-observability 規格「遊戲幣事件只含白名單欄位」：事件函式只收得下整數、內部編號與固定列舉。

不是「記得不要放」，而是「放不進來」：函式簽名沒有任何文字參數可以接冪等鍵、存檔識別碼或理由，
餘額種類與模型鍵傳入列舉以外的值、幣量不是整數都會立刻出錯。
"""
import inspect
import logging

import pytest

from app.observability import wallet_events
from app.observability.json_logging import ALLOWED_FIELDS
from app.services.runtime_limits import WALLET_MODEL_KEYS

LOG_FUNCTIONS = {
    name: function for name, function in inspect.getmembers(wallet_events, inspect.isfunction)
    if name.startswith("log_") and function.__module__ == wallet_events.__name__
}
ALLOWED_PARAMETERS = {"user_id", "coins", "lost_coins", "balance", "model_key", "bucket"}
FORBIDDEN_PARAMETERS = (
    "key", "idempotency_key", "session", "session_public_id", "public_id", "reason", "note", "text", "content", "message", "email",
    "nickname", "detail", "raw", "body",
)


@pytest.fixture(autouse=True)
def _info_level(caplog):
    caplog.set_level(logging.INFO, logger="app.wallet")


def capture(caplog):
    return [record for record in caplog.records if record.name == "app.wallet"]


def emit_everything():
    wallet_events.log_signup_granted(user_id=1, coins=200)
    wallet_events.log_checkin(user_id=1, coins=100, lost_coins=100)
    wallet_events.log_checkin_overflow(user_id=1, coins=100, lost_coins=100)
    wallet_events.log_hold(user_id=1, coins=30, model_key="standard")
    wallet_events.log_settle(user_id=1, coins=30)
    wallet_events.log_refund(user_id=1, coins=30)
    wallet_events.log_stale_released(user_id=1, coins=30)
    wallet_events.log_insufficient(user_id=1, balance=20, coins=30)
    wallet_events.log_adjust(user_id=1, bucket="paid", coins=-500, balance=0)
    wallet_events.log_grant_deferred(user_id=1)


def test_every_logging_function_takes_only_ids_integers_or_enumerations():
    assert len(LOG_FUNCTIONS) == 10, "找不到全部事件函式"
    for name, function in LOG_FUNCTIONS.items():
        parameters = set(inspect.signature(function).parameters)
        assert parameters <= ALLOWED_PARAMETERS, (name, parameters - ALLOWED_PARAMETERS)
        for forbidden in FORBIDDEN_PARAMETERS:
            assert forbidden not in parameters, (name, forbidden)


def test_the_signature_check_would_catch_a_text_parameter():
    def bad(*, user_id: int, idempotency_key: str) -> None: ...

    assert set(inspect.signature(bad).parameters) - ALLOWED_PARAMETERS == {"idempotency_key"}


def test_every_event_name_is_unique_and_namespaced():
    names = [value for key, value in vars(wallet_events).items() if key.startswith("EVENT_") and isinstance(value, str)]
    assert len(names) == len(set(names)) == len(wallet_events.EVENT_NAMES) == 10
    assert all(name.startswith("wallet.") for name in names)


def test_the_extra_fields_are_all_in_the_log_whitelist(caplog):
    emit_everything()
    records = capture(caplog)
    assert {record.event for record in records} == set(wallet_events.EVENT_NAMES)
    standard = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
    for record in records:
        extra = set(record.__dict__) - standard
        assert extra <= set(ALLOWED_FIELDS) | {"event"}, (record.event, extra - set(ALLOWED_FIELDS))


def test_the_new_whitelist_fields_are_integers_only(caplog):
    emit_everything()
    for record in capture(caplog):
        for name in ("coins", "lost_coins", "balance"):
            if hasattr(record, name):
                assert isinstance(getattr(record, name), int) and not isinstance(getattr(record, name), bool), (record.event, name)


def test_the_model_keys_match_the_wallets_price_keys():
    assert wallet_events.MODEL_KEYS == WALLET_MODEL_KEYS


@pytest.mark.parametrize("bad", ["premium", "", None, "standard "])
def test_an_unknown_model_key_is_a_programming_error(bad):
    with pytest.raises(ValueError):
        wallet_events.log_hold(user_id=1, coins=30, model_key=bad)


@pytest.mark.parametrize("bad", ["gold", "", None, "FREE"])
def test_an_unknown_bucket_is_a_programming_error(bad):
    with pytest.raises(ValueError):
        wallet_events.log_adjust(user_id=1, bucket=bad, coins=5, balance=5)


@pytest.mark.parametrize("bad", ["30", 3.5, None, True])
def test_a_coin_amount_that_is_not_an_integer_is_refused_so_text_cannot_reach_the_log(bad):
    with pytest.raises(ValueError):
        wallet_events.log_settle(user_id=1, coins=bad)
    with pytest.raises(ValueError):
        wallet_events.log_insufficient(user_id=1, balance=bad, coins=30)
    with pytest.raises(ValueError):
        wallet_events.log_checkin(user_id=1, coins=100, lost_coins=bad)


def test_the_adjustment_event_has_no_reason_and_no_target_identifier(caplog):
    wallet_events.log_adjust(user_id=7, bucket="free", coins=100, balance=300)
    (record,) = capture(caplog)
    assert (record.user_id, record.kind, record.coins, record.balance) == (7, "free", 100, 300)
    assert not hasattr(record, "reason") and not hasattr(record, "public_id")
