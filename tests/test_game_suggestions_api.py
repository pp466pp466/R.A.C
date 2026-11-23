"""rpg-suggestions 規格「建議端點與前提檢查」「預設不產生建議」「不扣遊戲幣但計入用量帳本」：`POST /api/game/sessions/<id>/suggestions` 的 HTTP 這一層。

走真實的 HTTP、Service、Repository 與 SQLite，模型是假的供應商；沿用 `test_game_api`、`test_game_revision_endpoints` 的夾具與輔助。
閘門（未登入、年齡、鎖定）與 CSRF 的參數化在 `test_game_api` 的 `ENDPOINTS` 與缺 CSRF 的測試裡一併涵蓋這個端點；
`SuggestionService` 的每個步驟（重用、備援、逐項驗證、錨點、並行）由 `test_rpg_suggest_service` 以假供應商逐項驗證。
"""
import json
import logging

import pytest
from sqlalchemy import func, select

from app.models import LlmCallLog, RpgSession, WalletEntry
from app.repositories.session_repository import SessionRepository
from tests.support.auth import FakeClock
from tests.support.game import story_script
from tests.support.llm import FakeLLMProvider
from tests.test_game_api import GHOST, db, make_save, opening, parse, player, script_turn, total, turn
from tests.test_llm_providers_by_purpose import BOTH, GEMINI_KEY, make_app

PATH = "/api/game/sessions/{id}/suggestions"
ITEMS = ["向守衛打聽消息", "走進山門", "回頭看看來時的路"]


@pytest.fixture()
def provider():
    return FakeLLMProvider()


@pytest.fixture()
def app(provider):
    return make_app({**BOTH}, rpg_llm_provider=provider)


def reply(*items) -> str:
    return json.dumps({"suggestions": list(items)}, ensure_ascii=False)


def suggest(client, save_id):
    return client.post(PATH.format(id=save_id))


def rows(app, kind="rpg_suggest"):
    with db(app) as s:
        return list(s.scalars(select(LlmCallLog).where(LlmCallLog.kind == kind).order_by(LlmCallLog.id)))


def saved(app):
    with db(app) as s:
        return s.scalar(select(RpgSession.suggestions_json))


def wallet_entries(app) -> int:
    with db(app) as s:
        return s.scalar(select(func.count()).select_from(WalletEntry))


# ---- 成功 -------------------------------------------------------------------------------------------


def test_a_click_returns_json_with_the_plain_text_items_and_the_reused_flag(app, provider):
    client, uid = player(app)
    save_id = make_save(app, uid)
    provider.script(reply(*ITEMS))
    response = suggest(client, save_id)
    assert response.status_code == 200 and response.mimetype == "application/json"
    assert response.get_json() == {"suggestions": ITEMS, "reused": False}
    assert "no-store" in response.headers["Cache-Control"] and "event-stream" not in response.content_type
    assert len(provider.calls) == 1 and saved(app) == {"seq": 0, "revision": 0, "items": ITEMS}


def test_a_second_click_reuses_the_saved_items_and_the_snapshot_carries_them_until_the_screen_changes(app, provider):
    client, uid = player(app)
    save_id = make_save(app, uid)
    provider.script(reply(*ITEMS))
    suggest(client, save_id)
    again = suggest(client, save_id)
    assert again.get_json() == {"suggestions": ITEMS, "reused": True} and len(provider.calls) == 1
    assert client.get(f"/api/game/sessions/{save_id}").get_json()["session"]["suggestions"] == ITEMS
    script_turn(provider)
    parse(turn(client, save_id))
    assert client.get(f"/api/game/sessions/{save_id}").get_json()["session"]["suggestions"] == []              # 新回合完成：舊建議不再帶出
    provider.script(reply("拿出行囊裡的地圖"))
    assert suggest(client, save_id).get_json() == {"suggestions": ["拿出行囊裡的地圖"], "reused": False}


def test_suggestion_text_is_returned_verbatim_as_plain_text(app, provider):
    client, uid = player(app)
    save_id = make_save(app, uid)
    provider.script(reply("檢查<b>粗體</b>字樣", "走進山門"))
    assert suggest(client, save_id).get_json()["suggestions"] == ["檢查<b>粗體</b>字樣", "走進山門"]


def test_nothing_is_generated_while_loading_playing_or_opening_and_the_calls_per_turn_do_not_change(app, provider):
    client, uid = player(app)
    save_id = make_save(app, uid, opening="pending")
    provider.script_stream(*story_script("開場的劇情。"))
    parse(opening(client, save_id))
    script_turn(provider)
    parse(turn(client, save_id))
    client.get(f"/api/game/sessions/{save_id}")
    client.get(f"/api/game/sessions/{save_id}/turns")
    assert rows(app) == [] and saved(app) is None
    assert [row.kind for row in rows(app, "rpg_opening") + rows(app, "rpg_stream") + rows(app, "rpg_state")] == ["rpg_opening", "rpg_stream", "rpg_state"]
    assert len(provider.calls) == 3                                                          # 開場、劇情串流、狀態呼叫；沒有第四個


# ---- 不扣幣 -----------------------------------------------------------------------------------------


def test_it_never_touches_the_wallet(app, provider):
    client, uid = player(app)
    save_id = make_save(app, uid)
    provider.script(reply(*ITEMS))
    suggest(client, save_id)
    suggest(client, save_id)
    assert total(app, uid) == 0 and wallet_entries(app) == 0                                 # 錢包甚至還沒有建立


def test_a_player_with_an_empty_wallet_still_gets_suggestions_and_a_ledger_row_with_tokens_and_no_text(app, provider):
    client, uid = player(app)
    save_id = make_save(app, uid)
    for n in range(6):                                                                       # 200 幣用到剩 20，不夠一輪
        script_turn(provider)
        assert turn(client, save_id, msg=f"m-{n}").status_code == 200
    assert total(app, uid) == 20
    provider.script(reply(*ITEMS))
    assert suggest(client, save_id).status_code == 200 and total(app, uid) == 20
    (row,) = rows(app)
    assert (row.status, row.user_id, row.counted, row.model) == ("ok", uid, False, provider.calls[-1].model)
    assert row.prompt_tokens and row.completion_tokens and row.error_detail is None
    assert all(text not in str(row.__dict__) for text in ITEMS)


def test_an_admin_gets_the_same_behavior_with_an_empty_wallet(app, provider):
    client, uid = player(app, role="admin")
    save_id = make_save(app, uid)
    provider.script(reply(*ITEMS))
    assert suggest(client, save_id).get_json()["reused"] is False and suggest(client, save_id).get_json()["reused"] is True
    assert total(app, uid) == 0 and wallet_entries(app) == 0 and len(rows(app)) == 1


# ---- 開始前的失敗 ---------------------------------------------------------------------------------------


def test_a_non_owner_gets_the_same_404_json_as_a_missing_save_and_no_model_call(app, provider):
    owner, owner_id = player(app)
    save_id = make_save(app, owner_id)
    intruder, _ = player(app, n=2)
    missing = suggest(intruder, GHOST)
    refused = suggest(intruder, save_id)
    assert missing.status_code == refused.status_code == 404 and missing.get_json() == refused.get_json()
    assert provider.calls == [] and saved(app) is None


def test_an_ended_session_is_a_409_session_ended_without_a_model_call(app, provider):
    client, uid = player(app)
    save_id = make_save(app, uid)
    with db(app) as s:
        assert SessionRepository(s).end_session(uid, save_id, now=FakeClock()())
        s.commit()
    response = suggest(client, save_id)
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "session_ended" and provider.calls == []


def test_an_unfinished_opening_is_a_409_opening_pending_without_a_model_call(app, provider):
    client, uid = player(app)
    save_id = make_save(app, uid, opening="pending")
    response = suggest(client, save_id)
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "opening_pending" and provider.calls == []


def test_a_streaming_first_turn_is_a_409_turn_in_progress_even_with_no_completed_turn(app, provider):
    client, uid = player(app)
    save_id = make_save(app, uid)
    with db(app) as s:
        started = SessionRepository(s).start_turn(
            uid, save_id, client_msg_id="m-1", user_action="我走進山門。", now=FakeClock()(), stale_after_seconds=180, state_before=lambda: {},
        )
        assert started is not None
        s.commit()
    response = suggest(client, save_id)
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "turn_in_progress" and provider.calls == []


def test_a_revision_in_progress_is_a_409_turn_in_progress(app, provider):
    client, uid = player(app)
    save_id = make_save(app, uid)
    script_turn(provider)
    parse(turn(client, save_id))
    with db(app) as s:
        assert SessionRepository(s).claim_revision(uid, save_id, "m-1", expected_revision=1, now=FakeClock()(), stale_after_seconds=180) is not None
        s.commit()
    calls = len(provider.calls)
    response = suggest(client, save_id)
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "turn_in_progress" and len(provider.calls) == calls


def test_a_spent_site_token_limit_is_a_503_system_busy_with_retry_after_and_no_model_call(provider):
    app = make_app({**BOTH, "USAGE_DAILY_TOKEN_LIMIT": "1000"}, rpg_llm_provider=provider)
    client, uid = player(app)
    save_id = make_save(app, uid)
    with db(app) as s:
        s.add(LlmCallLog(
            user_id=None, conversation_id=None, kind="chat", provider="fake", model="m", attempt=1, status="ok", counted=False,
            prompt_tokens=1000, latency_ms=10, created_at=FakeClock()(),
        ))
        s.commit()
    response = suggest(client, save_id)
    assert response.status_code == 503 and response.get_json()["error"]["code"] == "system_busy" and response.headers.get("Retry-After")
    assert provider.calls == [] and saved(app) is None


def test_without_an_rpg_provider_the_call_is_a_503_but_saved_suggestions_are_still_served():
    app = make_app({"GEMINI_API_KEY": GEMINI_KEY})
    client, uid = player(app)
    save_id = make_save(app, uid)
    response = suggest(client, save_id)
    assert response.status_code == 503 and response.mimetype == "application/json"
    assert response.get_json()["error"]["code"] == "service_unavailable" and saved(app) is None and rows(app) == []
    with db(app) as s:
        assert SessionRepository(s).save_suggestions(uid, save_id, seq=0, revision=0, items=ITEMS).outcome == "saved"
        s.commit()
    assert suggest(client, save_id).get_json() == {"suggestions": ITEMS, "reused": True}                    # 讀取已保存的建議不需要供應商


# ---- 失敗不保存 -------------------------------------------------------------------------------------------


def test_unusable_output_is_a_generic_503_that_saves_nothing_keeps_the_ledger_and_can_be_retried(app, provider):
    client, uid = player(app)
    save_id = make_save(app, uid)
    provider.script(reply("好"), reply("傳訊息到 abc@example.com"))
    response = suggest(client, save_id)
    assert response.status_code == 503 and response.mimetype == "application/json"
    error = response.get_json()["error"]
    assert error["code"] == "suggestions_unavailable" and error["message"] == "建議暫時無法產生，可以自己輸入行動。"
    assert "abc@example.com" not in response.get_data(as_text=True) and saved(app) is None
    assert [row.status for row in rows(app)] == ["validation_failed", "validation_failed"]                  # 錯誤回應之後帳本仍保留（呼叫過模型就是用過 token）
    provider.script(reply(*ITEMS))
    assert suggest(client, save_id).get_json() == {"suggestions": ITEMS, "reused": False}                   # 失敗不佔次數


# ---- 日誌 -------------------------------------------------------------------------------------------------


def test_the_logs_of_a_whole_suggestion_flow_carry_no_player_or_model_text(app, provider, caplog):
    client, uid = player(app)
    save_id = make_save(app, uid)
    with caplog.at_level(logging.DEBUG):
        provider.script(reply("好"), reply("傳訊息到 abc@example.com"))
        suggest(client, save_id)
        provider.script(reply(*ITEMS))
        suggest(client, save_id)
        suggest(client, save_id)
    text = " ".join(str(record.__dict__) + record.getMessage() for record in caplog.records)
    for secret in (*ITEMS, "abc@example.com", "開場的劇情。", save_id):
        assert secret not in text, secret
    events = [getattr(record, "event", None) for record in caplog.records]
    assert events.count("rpg.suggestion_failed") == 1 and events.count("rpg.suggestion_served") == 2


def test_the_log_events_are_the_only_rpg_events_a_click_emits(app, provider, caplog):
    client, uid = player(app)
    save_id = make_save(app, uid)
    provider.script(reply(*ITEMS))
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        suggest(client, save_id)
    assert [getattr(record, "event", None) for record in caplog.records if record.name == "app.rpg"] == ["rpg.suggestion_served"]
