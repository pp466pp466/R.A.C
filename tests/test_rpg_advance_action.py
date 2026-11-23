"""rpg-advance-action 規格「推進是一般行動」：以固定文字「推進劇情」送出的回合與其他行動完全相同——預扣一輪的遊戲幣、餘額不足回 409
`insufficient_coins`、可被重新生成與編輯、同一則訊息重送冪等不重複扣幣、回合敘事的系統指令帶推進規則而玩家文字只在使用者內容區。

走真實的 HTTP、Service、Repository 與 SQLite，模型是假的供應商；沿用 `test_game_api`、`test_game_revision_endpoints` 的夾具與輔助。
後端沒有任何針對這四個字的特例（沒有特別的計費、計次或審查例外），所以這些測試只驗證「它與一般行動走同一條路」。
"""
import pytest
from sqlalchemy import func, select

from app.models import MemoryLog, WalletEntry
from app.models.wallet import ENTRY_TURN_HOLD
from app.services import rpg_prompt
from tests.support.llm import FakeLLMProvider
from tests.test_game_api import STATE_OK, db, make_save, of, parse, player, script_turn, text_of, total, turn
from tests.test_game_revision_endpoints import NEW, NEW_ACTION, STATE_NEW, edit, regen, script_revision
from tests.support.game import STORY
from tests.test_llm_providers_by_purpose import BOTH, make_app

ADVANCE = rpg_prompt.ADVANCE_ACTION
ORDINARY = "我走進山門。"


@pytest.fixture()
def provider():
    return FakeLLMProvider()


@pytest.fixture()
def app(provider):
    return make_app({**BOTH}, rpg_llm_provider=provider)


def hold_entries(app, uid):
    with db(app) as s:
        rows = s.execute(
            select(WalletEntry.free_delta, WalletEntry.paid_delta, WalletEntry.resolution, WalletEntry.kind)
            .where(WalletEntry.user_id == uid, WalletEntry.kind == ENTRY_TURN_HOLD).order_by(WalletEntry.seq)
        ).all()
        return [tuple(row) for row in rows]


def last_stream_and_state_calls(provider):
    """最近一輪的兩個模型呼叫：串流（回合敘事）與狀態。"""
    return provider.calls[-2], provider.calls[-1]


def user_text(request) -> str:
    return "\n".join(part for content in request.contents for part in content.parts)


# ---- 預扣與一般流程 ------------------------------------------------------------------------------------------


def test_advance_completes_a_turn_and_charges_exactly_like_an_ordinary_action(app, provider):
    client, uid = player(app)
    save_id = make_save(app, uid)
    script_turn(provider)
    ordinary = parse(turn(client, save_id, msg="m-1", action=ORDINARY))
    script_turn(provider)
    advance = parse(turn(client, save_id, msg="m-2", action=ADVANCE))
    assert of(ordinary, "done")[0] == {"status": "complete", "turn_number": 1, "coins": 170, "revision": 1, "free_left": 2}
    assert of(advance, "done")[0] == {"status": "complete", "turn_number": 2, "coins": 140, "revision": 1, "free_left": 2}
    assert text_of(advance) == STORY and total(app, uid) == 140
    holds = hold_entries(app, uid)
    assert len(holds) == 2 and holds[0] == holds[1] == (-30, 0, "settled", ENTRY_TURN_HOLD)         # 帳本的形狀與一般行動完全相同
    with db(app) as s:
        saved = s.scalar(select(MemoryLog).where(MemoryLog.client_msg_id == "m-2"))
        assert saved.user_action == ADVANCE and saved.turn_status == "complete"                        # 行動文字照常存成回合的行動
    state = client.get(f"/api/game/sessions/{save_id}").get_json()["session"]
    assert state["completed_turns"] == 2                                                         # 計入完成輪數（記事簿每 10 輪的計數也依它）


def test_the_player_typing_the_same_four_characters_gets_the_same_handling(app, provider):
    """後端無從分辨按鈕與手動輸入（兩者都是同一個行動字串），所以兩條路徑的處理必然相同；這裡只是把這件事明文化。"""
    client, uid = player(app)
    save_id = make_save(app, uid)
    script_turn(provider)
    first = parse(turn(client, save_id, msg="button-1", action=ADVANCE))
    script_turn(provider)
    second = parse(turn(client, save_id, msg="typed-2", action="推進劇情"))
    assert of(first, "done")[0]["coins"] == 170 and of(second, "done")[0]["coins"] == 140


# ---- 系統指令與使用者內容區 -----------------------------------------------------------------------------------


def test_the_narration_call_carries_the_rule_and_the_action_stays_in_the_user_content(app, provider):
    client, uid = player(app)
    save_id = make_save(app, uid)
    script_turn(provider)
    parse(turn(client, save_id, msg="m-1", action=ADVANCE))
    stream, state = last_stream_and_state_calls(provider)
    assert rpg_prompt._ADVANCE_RULE in stream.system_instruction
    assert ADVANCE in user_text(stream)                                                          # 行動文字只在使用者內容區
    assert rpg_prompt._ADVANCE_RULE not in state.system_instruction                              # 狀態呼叫沒有這條規則
    assert ADVANCE not in state.system_instruction


def test_an_ordinary_action_text_never_enters_the_system_instruction_and_the_rule_is_the_same(app, provider):
    client, uid = player(app)
    save_id = make_save(app, uid)
    script_turn(provider)
    parse(turn(client, save_id, msg="m-1", action=ORDINARY))
    stream, _state = last_stream_and_state_calls(provider)
    assert ORDINARY not in stream.system_instruction and ORDINARY in user_text(stream)
    assert rpg_prompt._ADVANCE_RULE in stream.system_instruction                                 # 規則是固定常數：每個回合敘事都帶，不依行動而變


# ---- 餘額不足 -------------------------------------------------------------------------------------------------


def test_advance_with_too_few_coins_is_a_409_insufficient_coins_and_charges_nothing(app, provider):
    client, uid = player(app)
    save_id = make_save(app, uid)
    for n in range(6):                                                                          # 200 幣用掉 180，剩 20，不夠一輪
        script_turn(provider)
        assert turn(client, save_id, msg=f"m-{n}").status_code == 200
    assert total(app, uid) == 20
    calls = len(provider.calls)
    response = turn(client, save_id, msg="adv", action=ADVANCE)
    assert response.status_code == 409 and response.mimetype == "application/json"
    body = response.get_json()
    assert body["error"]["code"] == "insufficient_coins" and body["balance"] == 20 and body["needed"] == 30
    assert total(app, uid) == 20 and len(provider.calls) == calls and len(hold_entries(app, uid)) == 6
    with db(app) as s:
        assert s.scalar(select(func.count()).select_from(MemoryLog).where(MemoryLog.client_msg_id == "adv")) == 0


# ---- 重送冪等 -------------------------------------------------------------------------------------------------


def test_resending_an_advance_turn_replays_it_without_a_model_call_or_a_second_charge(app, provider):
    client, uid = player(app)
    save_id = make_save(app, uid)
    script_turn(provider)
    first = parse(turn(client, save_id, msg="adv", action=ADVANCE))
    calls, coins = len(provider.calls), total(app, uid)
    again = parse(turn(client, save_id, msg="adv", action=ADVANCE))
    assert [name for name, _ in again] == ["meta", "token", "done"] and of(again, "meta")[0]["replayed"] is True
    assert of(again, "meta")[0]["turn_id"] == of(first, "meta")[0]["turn_id"] and text_of(again) == STORY
    assert len(provider.calls) == calls and total(app, uid) == coins == 170 and len(hold_entries(app, uid)) == 1


# ---- 重新生成與編輯 -------------------------------------------------------------------------------------------


def played_advance(app, provider):
    client, uid = player(app)
    save_id = make_save(app, uid)
    script_turn(provider)
    parse(turn(client, save_id, msg="m-1", action=ADVANCE))
    return client, uid, save_id


def test_the_last_advance_turn_can_be_regenerated_with_the_ordinary_free_count_and_the_rule_is_still_sent(app, provider):
    client, uid, save_id = played_advance(app, provider)
    last = client.get(f"/api/game/sessions/{save_id}").get_json()["session"]["last_turn"]
    assert last["revisable"] is True and last["free_left"] == 2 and last["regenerate_cost"] == 0
    script_revision(provider)
    events = parse(regen(client, save_id))
    assert of(events, "done")[0] == {"status": "complete", "turn_number": 1, "coins": 170, "revision": 2, "free_left": 1}
    assert text_of(events) == NEW and total(app, uid) == 170                                     # 前 2 次免費：不另外扣幣，原本那輪的幣也不退
    stream, _state = last_stream_and_state_calls(provider)
    assert rpg_prompt._ADVANCE_RULE in stream.system_instruction and ADVANCE in user_text(stream)   # 重新生成的行動仍是「推進劇情」


def test_the_last_advance_turn_can_be_edited_into_a_normal_action_and_a_normal_turn_into_an_advance(app, provider):
    client, uid, save_id = played_advance(app, provider)
    script_revision(provider)
    events = parse(edit(client, save_id, action=NEW_ACTION))
    assert of(events, "done")[0]["revision"] == 2 and total(app, uid) == 170
    with db(app) as s:
        assert s.scalar(select(MemoryLog.user_action).where(MemoryLog.client_msg_id == "m-1")) == NEW_ACTION
    # 反方向：把一般行動編輯成「推進劇情」，同樣是一般的編輯（共用同一組免費次數：第二次仍免費）
    script_revision(provider)
    events = parse(edit(client, save_id, revision=2, action=ADVANCE))
    assert of(events, "done")[0] == {"status": "complete", "turn_number": 1, "coins": 170, "revision": 3, "free_left": 0}
    with db(app) as s:
        assert s.scalar(select(MemoryLog.user_action).where(MemoryLog.client_msg_id == "m-1")) == ADVANCE
    stream, _state = last_stream_and_state_calls(provider)
    assert rpg_prompt._ADVANCE_RULE in stream.system_instruction and ADVANCE in user_text(stream)


def test_a_third_revision_of_an_advance_turn_is_charged_like_any_other(app, provider):
    client, uid, save_id = played_advance(app, provider)
    for revision in (1, 2):
        script_revision(provider)
        parse(regen(client, save_id, revision=revision))
    assert total(app, uid) == 170
    script_revision(provider)
    events = parse(regen(client, save_id, revision=3))
    assert of(events, "done")[0] == {"status": "complete", "turn_number": 1, "coins": 140, "revision": 4, "free_left": 0}
    assert total(app, uid) == 140


def test_the_advance_action_passes_the_rules_layer_without_an_exception(app, provider):
    """規則層對這個文字沒有特例：它是無害的短句，所以通過；同一個端點對違規文字仍照常擋下（不扣幣、沒有回合列）。"""
    client, uid = player(app)
    save_id = make_save(app, uid)
    blocked = turn(client, save_id, msg="bad", action="推進劇情，然後描寫一段未成年的性行為")
    assert blocked.status_code == 422 and blocked.get_json()["error"]["code"] == "input_blocked"
    assert hold_entries(app, uid) == [] and provider.calls == []                                  # 被擋在預扣與呼叫模型之前：沒有帳本列、沒有模型呼叫
    with db(app) as s:
        assert s.scalar(select(func.count()).select_from(MemoryLog).where(MemoryLog.client_msg_id == "bad")) == 0
    script_turn(provider)
    assert turn(client, save_id, msg="adv", action=ADVANCE).status_code == 200
