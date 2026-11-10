"""rpg-notebook 規格：`NotebookService`（玩家檢視與儲存）與存檔快照的記事簿用量摘要。

用真的 Repository、真的存取政策與（SQLite 與真實 SQL Server 各一次的）資料庫；沒有任何模型，所以「儲存不呼叫模型」以
帳本沒有新紀錄、已處理到的輪次不變來證明。AI 在編輯期間追加內容的情境以 Repository 的鎖加追加方法重現。
"""
import logging

import pytest
from sqlalchemy import func, select

from app.models import LlmCallLog
from app.models.rpg_session import SESSION_ENDED
from app.observability import rpg_events
from app.repositories.errors import SessionEndedError, StaleNotebookVersionError
from app.services import notebook_policy
from app.services.current_user import CurrentUser
from app.services.errors import (
    AgeConfirmationRequired, NotebookBlocked, NotebookConflict, NotebookTooLong, NotFound, SessionEnded, ValidationError,
)
from app.services.notebook_service import NotebookService
from tests.support.game import BAD_SENTENCE, GameKit

GHOST = "00000000-0000-4000-8000-000000000000"
SETTING = "師妹的父親是宗門長老，主角答應三年內回來"


@pytest.fixture()
def kit(member_session):
    return GameKit(member_session)


@pytest.fixture()
def service(kit):
    return NotebookService(sessions=kit.sessions, policy=kit.policy)


@pytest.fixture()
def world(kit):
    user = kit.player()
    return user, kit.open_save(user)


def put(kit, save, text, *, version=None):
    """直接寫入記事簿（模擬已存在的內容）；不經過 Service。"""
    save.notebook_text = text
    if version is not None:
        save.notebook_version = version
    kit.session.commit()


def state_of(kit, save):
    kit.session.expire_all()
    return save.notebook_text, save.notebook_version, save.notebook_upto_turn


def events(caplog):
    return {r.event: r for r in caplog.records if r.name == "app.rpg" and getattr(r, "event", "").startswith("rpg.notebook_")}


# ---- view ---------------------------------------------------------------------------------------

def test_a_new_save_has_an_empty_notebook_at_version_one(service, world):
    user, save = world
    assert service.view(user, save.public_id) == {"text": "", "version": 1, "length": 0, "capacity": 2000, "reserve": 500, "full": False}


@pytest.mark.parametrize("tier, capacity, reserve", [("free", 2000, 500), ("plus", 5000, 1250), ("vip", 10000, 2500)])
def test_the_capacity_and_reserve_follow_the_member_tier(kit, service, tier, capacity, reserve):
    user = kit.player(member_tier=tier)
    save = kit.open_save(user)
    view = service.view(user, save.public_id)
    assert (view["capacity"], view["reserve"]) == (capacity, reserve)


def test_the_view_returns_the_whole_text_and_says_when_it_is_full(kit, service, world):
    user, save = world
    put(kit, save, "甲" * 2240, version=4)
    view = service.view(user, save.public_id)
    assert view["text"] == "甲" * 2240 and view["length"] == 2240 and view["version"] == 4 and view["full"] is True


def test_a_notebook_exactly_at_the_capacity_is_not_full(kit, service, world):
    user, save = world
    put(kit, save, "甲" * 2000)
    assert service.view(user, save.public_id)["full"] is False
    put(kit, save, "甲" * 2001)
    assert service.view(user, save.public_id)["full"] is True


def test_the_view_follows_the_current_tier_so_a_downgrade_is_full_and_an_upgrade_is_not(kit, service):
    plus = kit.player(member_tier="plus")
    save = kit.open_save(plus)
    put(kit, save, "甲" * 3000)
    assert service.view(plus, save.public_id)["full"] is False
    free = CurrentUser(**{**plus.__dict__, "member_tier": "free"})
    downgraded = service.view(free, save.public_id)
    assert downgraded["full"] is True and downgraded["text"] == "甲" * 3000        # 不自動刪除，視同已滿
    vip = CurrentUser(**{**plus.__dict__, "member_tier": "vip"})
    assert service.view(vip, save.public_id)["capacity"] == 10000


def test_an_ended_save_can_still_be_viewed(kit, service, world):
    user, save = world
    put(kit, save, "已經確立的設定")
    kit.sessions.end_session(user.id, save.public_id, now=kit.clock())
    assert service.view(user, save.public_id)["text"] == "已經確立的設定"


def test_another_player_and_a_missing_save_get_the_same_not_found(kit, service, world):
    _, save = world
    bob = kit.player(2)
    messages = set()
    for public_id in (save.public_id, GHOST):
        with pytest.raises(NotFound) as caught:
            service.view(bob, public_id)
        messages.add((caught.value.code, caught.value.message))
    assert len(messages) == 1                                                          # 回應完全相同
    for public_id in (save.public_id, GHOST):
        with pytest.raises(NotFound):
            service.save(bob, public_id, text="別人的", expected_version=1)
    assert state_of(kit, save) == ("", 1, 0)


def test_the_account_and_age_gate_comes_first(kit, service, world):
    _, save = world
    unconfirmed = kit.player(2, age=None)
    with pytest.raises(AgeConfirmationRequired):
        service.view(unconfirmed, save.public_id)
    with pytest.raises(AgeConfirmationRequired):
        service.save(unconfirmed, save.public_id, text="x", expected_version=1)


# ---- save：成功 --------------------------------------------------------------------------------

def test_a_save_with_the_current_version_writes_the_text_and_adds_one_to_the_version(kit, service, world, caplog):
    user, save = world
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        result = service.save(user, save.public_id, text=SETTING, expected_version=1)
    assert result == {"text": SETTING, "version": 2, "length": len(SETTING), "capacity": 2000, "reserve": 500, "full": False}
    assert state_of(kit, save) == (SETTING, 2, 0)
    record = events(caplog)[rpg_events.EVENT_NOTEBOOK_SAVED]
    assert (record.version, record.total) == (2, len(SETTING))


def test_a_save_never_calls_a_model_and_never_moves_the_marker(kit, service, world):
    user, save = world
    kit.sessions.lock_notebook_for_capture(user.id, save.public_id, 10)
    kit.sessions.append_notebook(user.id, save.public_id, text="AI 抓的重點", boundary=10)
    kit.session.commit()
    service.save(user, save.public_id, text="玩家改過的重點", expected_version=2)
    assert state_of(kit, save) == ("玩家改過的重點", 3, 10)                             # 標記停在 AI 處理到的輪次
    assert kit.session.scalar(select(func.count()).select_from(LlmCallLog)) == 0       # 沒有任何模型呼叫（帳本沒有紀錄）


def test_the_text_is_cleaned_before_it_is_stored(kit, service, world):
    user, save = world
    result = service.save(user, save.public_id, text="  第一行\x00\r\n第二行\x07\t結尾  ", expected_version=1)
    assert result["text"] == "第一行\n第二行 結尾" == state_of(kit, save)[0]          # 控制字元去除、CRLF 統一、Tab 換空格、頭尾空白去除


def test_the_length_is_counted_after_nfc_normalisation(kit, service, world):
    user, save = world
    decomposed = "é" * 2000                                                      # 4000 個字碼點，NFC 後 2000 字
    assert len(decomposed) == 4000
    assert service.save(user, save.public_id, text=decomposed, expected_version=1)["length"] == 2000


def test_the_player_can_clear_the_notebook(kit, service, world):
    user, save = world
    put(kit, save, "一些設定", version=3)
    result = service.save(user, save.public_id, text="", expected_version=3)
    assert (result["text"], result["version"], result["length"]) == ("", 4, 0)
    assert state_of(kit, save) == ("", 4, 0)


def test_a_text_of_only_whitespace_is_an_empty_notebook(kit, service, world):
    user, save = world
    assert service.save(user, save.public_id, text="   \n\t  ", expected_version=1)["text"] == ""


# ---- save：容量 --------------------------------------------------------------------------------

@pytest.mark.parametrize("tier, capacity", [("free", 2000), ("plus", 5000), ("vip", 10000)])
def test_the_capacity_is_accepted_and_one_more_character_is_not(kit, service, tier, capacity):
    user = kit.player(member_tier=tier)
    save = kit.open_save(user)
    assert service.save(user, save.public_id, text="甲" * capacity, expected_version=1)["length"] == capacity
    with pytest.raises(NotebookTooLong) as caught:
        service.save(user, save.public_id, text="甲" * (capacity + 1), expected_version=2)
    assert (caught.value.length, caught.value.capacity, caught.value.code) == (capacity + 1, capacity, "notebook_too_long")
    assert state_of(kit, save) == ("甲" * capacity, 2, 0)                                # 被拒時資料與版本都不變


def test_the_player_cannot_use_the_ai_reserve(kit, service, world):
    user, save = world
    assert notebook_policy.reserve_for("free") == 500
    with pytest.raises(NotebookTooLong):
        service.save(user, save.public_id, text="甲" * 2240, expected_version=1)
    assert state_of(kit, save) == ("", 1, 0)


def test_a_full_notebook_must_be_cut_to_the_capacity_in_one_save(kit, service, world):
    user, save = world
    put(kit, save, "甲" * 2240, version=5)
    with pytest.raises(NotebookTooLong) as caught:
        service.save(user, save.public_id, text="甲" * 2100, expected_version=5)       # 分次刪減不行
    assert (caught.value.length, caught.value.capacity) == (2100, 2000)
    assert state_of(kit, save) == ("甲" * 2240, 5, 0)
    result = service.save(user, save.public_id, text="甲" * 2000, expected_version=5)    # 一次刪到容量以內才行
    assert result["full"] is False and result["version"] == 6


def test_saving_a_full_notebook_unchanged_is_rejected(kit, service, world):
    user, save = world
    put(kit, save, "甲" * 2240, version=2)
    with pytest.raises(NotebookTooLong):
        service.save(user, save.public_id, text="甲" * 2240, expected_version=2)
    assert state_of(kit, save) == ("甲" * 2240, 2, 0)


def test_after_the_player_cuts_it_down_the_ai_may_append_again(kit, service, world):
    user, save = world
    put(kit, save, "甲" * 2240, version=2)
    service.save(user, save.public_id, text="甲" * 1990, expected_version=2)
    assert notebook_policy.is_full(notebook_policy.length_of(state_of(kit, save)[0]), "free") is False
    assert notebook_policy.room_for(state_of(kit, save)[0], "free") > 0


# ---- save：規則層 ------------------------------------------------------------------------------

def test_text_that_hits_a_must_reject_category_is_refused_with_the_category_only(kit, service, world, caplog):
    user, save = world
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        with pytest.raises(NotebookBlocked) as caught:
            service.save(user, save.public_id, text=SETTING + "\n" + BAD_SENTENCE, expected_version=1)
    assert caught.value.categories == ("minor_sexual",) and caught.value.code == "notebook_blocked"
    assert BAD_SENTENCE not in caught.value.message and SETTING not in caught.value.message
    assert state_of(kit, save) == ("", 1, 0)
    record = events(caplog)[rpg_events.EVENT_NOTEBOOK_SAVE_REJECTED]
    assert (record.reason, record.categories) == ("blocked", "minor_sexual")
    assert BAD_SENTENCE not in caplog.text and SETTING not in caplog.text


def test_a_non_consensual_scene_is_refused_too(kit, service, world):
    user, save = world
    with pytest.raises(NotebookBlocked) as caught:
        service.save(user, save.public_id, text="她被強暴並被迫與他做愛。", expected_version=1)
    assert "non_consensual" in caught.value.categories
    assert state_of(kit, save) == ("", 1, 0)


def test_an_ordinary_setting_and_a_tier_mismatch_do_not_block_the_save(kit, service, world):
    user, save = world
    assert service.save(user, save.public_id, text=SETTING, expected_version=1)["version"] == 2
    mismatch = "兩人在房中做愛，主角與師妹成了道侶。"                                     # 安全模式的存檔裡有性用語：只是分級不符，不阻擋儲存
    assert service.save(user, save.public_id, text=mismatch, expected_version=2)["text"] == mismatch


def test_the_length_rule_comes_before_the_rules_layer_so_nothing_about_the_rules_leaks(kit, service, world):
    user, save = world
    with pytest.raises(NotebookTooLong):
        service.save(user, save.public_id, text="甲" * 2100 + BAD_SENTENCE, expected_version=1)


# ---- save：版本 --------------------------------------------------------------------------------

def test_an_old_version_is_refused_with_the_latest_notebook_and_the_ai_text_survives(kit, service, world, caplog):
    user, save = world
    old = service.view(user, save.public_id)                                           # 玩家開始編輯
    assert kit.sessions.lock_notebook_for_capture(user.id, save.public_id, 10) is not None
    assert kit.sessions.append_notebook(user.id, save.public_id, text="AI 追加的重點", boundary=10) is True
    kit.session.commit()                                                               # AI 追加（版本加 1）
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        with pytest.raises(NotebookConflict) as caught:
            service.save(user, save.public_id, text="玩家以舊版本寫的", expected_version=old["version"])
    assert caught.value.code == "notebook_conflict"
    latest = caught.value.notebook
    assert latest["text"] == "AI 追加的重點" and latest["version"] == 2 and latest["length"] == len("AI 追加的重點")
    assert set(latest) == {"text", "version", "length", "capacity", "reserve", "full"}   # 與檢視端點同形
    assert state_of(kit, save) == ("AI 追加的重點", 2, 10)
    assert events(caplog)[rpg_events.EVENT_NOTEBOOK_SAVE_REJECTED].reason == "conflict"
    assert service.save(user, save.public_id, text="確認後再寫的", expected_version=latest["version"])["version"] == 3


def test_a_version_ahead_of_the_backend_is_also_a_conflict(kit, service, world):
    user, save = world
    with pytest.raises(NotebookConflict):
        service.save(user, save.public_id, text="x", expected_version=9)
    assert state_of(kit, save) == ("", 1, 0)


def test_each_successful_save_adds_exactly_one_to_the_version(kit, service, world):
    user, save = world
    for expected in (1, 2, 3):
        assert service.save(user, save.public_id, text=f"第 {expected} 次", expected_version=expected)["version"] == expected + 1


# ---- save：已完結 ------------------------------------------------------------------------------

def test_an_ended_save_refuses_the_save_and_keeps_the_notebook(kit, service, world, caplog):
    user, save = world
    put(kit, save, "完結前的設定", version=3)
    kit.sessions.end_session(user.id, save.public_id, now=kit.clock())
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        with pytest.raises(SessionEnded) as caught:
            service.save(user, save.public_id, text="完結後想改", expected_version=3)
    assert caught.value.code == "session_ended"
    kit.session.expire_all()
    assert (save.notebook_text, save.notebook_version, save.status) == ("完結前的設定", 3, SESSION_ENDED)
    assert events(caplog)[rpg_events.EVENT_NOTEBOOK_SAVE_REJECTED].reason == "ended"


def test_ended_comes_before_the_version_and_the_length_checks(kit, service, world):
    user, save = world
    kit.sessions.end_session(user.id, save.public_id, now=kit.clock())
    for text, version in (("x", 99), ("甲" * 2100, 1)):
        with pytest.raises(SessionEnded):
            service.save(user, save.public_id, text=text, expected_version=version)


def test_a_save_that_ends_between_the_check_and_the_write_is_also_session_ended(kit, service, world, monkeypatch):
    user, save = world

    def ended(*_args, **_kwargs):
        raise SessionEndedError

    monkeypatch.setattr(kit.sessions, "save_notebook", ended)
    with pytest.raises(SessionEnded):
        service.save(user, save.public_id, text="x", expected_version=1)


def test_a_save_that_disappears_between_the_check_and_the_write_is_not_found(kit, service, world, monkeypatch):
    user, save = world
    monkeypatch.setattr(kit.sessions, "save_notebook", lambda *_args, **_kwargs: None)
    with pytest.raises(NotFound):
        service.save(user, save.public_id, text="x", expected_version=1)


def test_a_conflict_whose_save_vanished_meanwhile_is_not_found(kit, service, world, monkeypatch):
    user, save = world
    calls = {"count": 0}
    real_get = kit.sessions.get_owned

    def get_owned(*args, **kwargs):
        calls["count"] += 1
        return real_get(*args, **kwargs) if calls["count"] == 1 else None             # 第二次（衝突後重新讀取）已經不存在

    def stale(*_args, **_kwargs):
        raise StaleNotebookVersionError

    monkeypatch.setattr(kit.sessions, "get_owned", get_owned)
    monkeypatch.setattr(kit.sessions, "save_notebook", stale)
    with pytest.raises(NotFound):
        service.save(user, save.public_id, text="x", expected_version=1)


# ---- save：輸入 --------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [None, 123, ["a"], {"text": "a"}, "壞\ud800字"])
def test_a_text_that_cannot_be_stored_is_a_validation_error(kit, service, world, text):
    user, save = world
    with pytest.raises(ValidationError) as caught:
        service.save(user, save.public_id, text=text, expected_version=1)
    assert set(caught.value.fields) == {"text"}
    assert state_of(kit, save) == ("", 1, 0)


@pytest.mark.parametrize("version", [None, "1", 1.0, True, -1, [1]])
def test_a_bad_version_is_a_validation_error(kit, service, world, version):
    user, save = world
    with pytest.raises(ValidationError) as caught:
        service.save(user, save.public_id, text="x", expected_version=version)
    assert set(caught.value.fields) == {"expected_version"}
    assert state_of(kit, save) == ("", 1, 0)


def test_nothing_in_the_logs_contains_the_notebook_text(kit, service, world, caplog):
    user, save = world
    secret = "只有玩家知道的祕密設定"
    with caplog.at_level(logging.DEBUG):
        service.save(user, save.public_id, text=secret, expected_version=1)
        with pytest.raises(NotebookTooLong):
            service.save(user, save.public_id, text=secret + "甲" * 2100, expected_version=2)
        with pytest.raises(NotebookConflict):
            service.save(user, save.public_id, text=secret, expected_version=1)
    assert secret not in caplog.text


# ---- 存檔快照的摘要 -----------------------------------------------------------------------------

def test_the_snapshot_carries_a_summary_and_never_the_text(kit, world):
    user, save = world
    text = "祕" * 1800
    put(kit, save, text, version=3)
    snapshot = kit.game.session_snapshot(user, save.public_id)
    assert snapshot["notebook"] == {"version": 3, "length": 1800, "capacity": 2000, "full": False}
    assert text not in repr(snapshot)


def test_the_snapshot_summary_says_when_the_notebook_is_full(kit, world):
    user, save = world
    put(kit, save, "甲" * 2240)
    assert kit.game.session_snapshot(user, save.public_id)["notebook"]["full"] is True
    put(kit, save, "甲" * 2000)
    assert kit.game.session_snapshot(user, save.public_id)["notebook"]["full"] is False


def test_the_snapshot_summary_of_an_ended_save_is_still_readable(kit, world):
    user, save = world
    kit.sessions.end_session(user.id, save.public_id, now=kit.clock())
    assert kit.game.session_snapshot(user, save.public_id)["notebook"]["version"] == 1
