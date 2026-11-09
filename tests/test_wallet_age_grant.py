"""post-login-age-confirmation 規格「年齡確認通過時發放首次註冊贈幣且不影響確認本身」：
補填成功得 200、重複補填不再發、回呼失敗時年齡確認仍完成且第一次存取錢包時補發、不一致與未滿 13 歲沒有贈幣也沒有錢包。
以真的 `AuthService` 與 registry 組裝的回呼走完整條路。SQLite 與真實 SQL Server 各跑一次。
"""
import pytest
from sqlalchemy import func, select

from app.models import User, Wallet, WalletEntry
from app.services.errors import UnderageAccountErased, ValidationError
from app.services.registry import _grant_signup_coins_on_age_verified, build_wallet_service
from tests.support.age import age_fields
from tests.support.auth import FakeClock, add_user, make_auth_service


def count(session, model) -> int:
    session.expire_all()
    return session.scalar(select(func.count()).select_from(model))


def submit(session, user: User, *, hook, clock=None, **age_input):
    service = make_auth_service(session, clock=clock or FakeClock(), on_age_verified=hook)
    return service.verify_age(service.resolve_session(user.id, user.token_version), **age_input)


def real_hook(session, clock=None):
    return _grant_signup_coins_on_age_verified(session, clock or FakeClock())


def free_balance(session, user_id) -> int | None:
    session.expire_all()
    return session.scalar(select(Wallet.free_balance).where(Wallet.user_id == user_id))


# ---- 補填成功 ------------------------------------------------------------------------------------------


def test_a_player_who_completes_the_age_step_receives_the_signup_grant(member_session):
    s = member_session
    user = add_user(s, 1, age=None, password=None)
    submit(s, user, hook=real_hook(s), **age_fields(30))
    assert free_balance(s, user.id) == 200
    (entry,) = s.scalars(select(WalletEntry))
    assert (entry.kind, entry.free_delta, entry.free_after) == ("signup_grant", 200, 200)


def test_a_second_submission_after_completion_grants_nothing_more(member_session):
    s = member_session
    user = add_user(s, 1, age=None, password=None)
    hook = real_hook(s)
    submit(s, user, hook=hook, **age_fields(30))
    submit(s, user, hook=hook, **age_fields(30))
    submit(s, user, hook=hook, **age_fields(30, age_range="25_34"))
    assert free_balance(s, user.id) == 200 and count(s, WalletEntry) == 1


def test_an_administrator_who_fills_in_the_age_receives_nothing(member_session):
    s = member_session
    admin = add_user(s, 1, role="admin", age=None)
    submit(s, admin, hook=real_hook(s), **age_fields(30, nickname=None))
    assert count(s, WalletEntry) == 0 and free_balance(s, admin.id) in (None, 0)


# ---- 失敗不影響確認 --------------------------------------------------------------------------------------


def test_a_failing_grant_does_not_fail_or_undo_the_age_confirmation_and_the_first_wallet_access_makes_it_up(member_session, monkeypatch, log_lines):
    s = member_session
    user = add_user(s, 1, age=None, password=None)
    wallet = build_wallet_service(s, clock=FakeClock())

    def broken(*_args, **_kwargs):
        raise RuntimeError("資料庫暫時錯誤")

    hook = real_hook(s)
    monkeypatch.setattr("app.services.wallet_service.WalletService.ensure_signup_grant", broken)
    current = submit(s, user, hook=hook, **age_fields(30))
    assert current.age_confirmed is True                                          # 年齡確認照常完成
    s.expire_all()
    assert s.get(User, user.id).birth_date is not None
    assert count(s, WalletEntry) == 0
    assert any(line.get("event") == "wallet.grant_deferred" and line["user_id"] == user.id for line in log_lines())

    monkeypatch.undo()
    assert wallet.view(current).free_balance == 200                               # 第一次存取錢包時補發
    assert count(s, WalletEntry) == 1


def test_a_hook_that_raises_past_its_own_guard_still_cannot_fail_the_confirmation(member_session, log_lines):
    s = member_session
    user = add_user(s, 1, age=None, password=None)

    def exploding(_current):
        raise RuntimeError("回呼本身壞了")

    current = submit(s, user, hook=exploding, **age_fields(30))
    assert current.age_confirmed is True
    assert not any("回呼本身壞了" in line.get("message", "") for line in log_lines())      # 日誌只有類型與位置，不含訊息文字


# ---- 沒有通過就沒有贈幣 ----------------------------------------------------------------------------------


def test_a_mismatch_grants_nothing_and_creates_no_wallet(member_session):
    s = member_session
    user = add_user(s, 1, age=None, password=None)
    with pytest.raises(ValidationError):
        submit(s, user, hook=real_hook(s), **age_fields(30, age_range="18_24"))
    with pytest.raises(ValidationError):
        submit(s, user, hook=real_hook(s), **age_fields(30, age_range="65_plus"))
    assert count(s, Wallet) == 0 and count(s, WalletEntry) == 0


def test_a_player_found_to_be_under_13_is_erased_and_leaves_no_coins_behind(member_session):
    s = member_session
    user = add_user(s, 1, age=None, password=None)
    with pytest.raises(UnderageAccountErased):
        submit(s, user, hook=real_hook(s), **age_fields(12, age_range="under_13"))
    assert count(s, User) == 0 and count(s, Wallet) == 0 and count(s, WalletEntry) == 0


def test_a_malformed_submission_grants_nothing(member_session):
    s = member_session
    user = add_user(s, 1, age=None, password=None)
    with pytest.raises(ValidationError):
        submit(s, user, hook=real_hook(s), birth_date="not-a-date", age_range="25_34", nickname="測試玩家")
    assert count(s, Wallet) == 0


# ---- 組裝 ---------------------------------------------------------------------------------------------


def test_the_registry_wires_the_grant_into_the_auth_service_used_by_the_application():
    """正式組裝（`build_auth_service`）一定接上回呼；少接會讓新玩家完成年齡確認卻沒有贈幣，直到第一次存取錢包才補發。"""
    from app.config import load_config
    from app.services import registry

    source = __import__("inspect").getsource(registry.build_auth_service)
    assert "on_age_verified=_grant_signup_coins_on_age_verified(session, clock)" in source
    assert load_config is not None
