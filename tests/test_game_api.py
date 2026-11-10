"""`/api/game/*`（rpg-turn-stream 規格）：事件串流的順序與格式、標頭、開始前的錯誤一律是 JSON（404 一致、CSRF、餘額不足、衝突、
驗證、規則層）、已完成回合與第 0 幕的重送、增量同步、已完結仍可讀、被擋與中斷的回合不含文字，以及沒有 RPG 供應商時的 503。

用測試應用程式（SQLite）、直接寫入的登入狀態與假的串流供應商；回合與第 0 幕的各種出口對錢包與資料的細節由
`test_turn_runner_*`、`test_opening` 涵蓋，這裡驗證的是 HTTP 這一層：事件怎麼送出、錯誤怎麼回、誰能讀。
"""
import json
import re

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.llm.types import LLMTransientError
from app.models import MemoryLog, RpgSession
from app.models.rpg_session import TURN_BLOCKED
from app.repositories.session_repository import SessionRepository
from app.repositories.wallet_repository import WalletRepository
from tests.support.auth import FakeClock, add_user, sign_in_as
from tests.support.client import raw_client
from tests.support.game import BAD_SENTENCE, STORY, story_script
from tests.support.llm import Chunk, Fail, FakeLLMProvider
from tests.support.protagonist import protagonist
from tests.support.script_data import validated
from tests.test_llm_providers_by_purpose import BOTH, GEMINI_KEY, make_app

STATE_OK = json.dumps({"elapsed_months": 1, "current_location": "山門前"}, ensure_ascii=False)
GHOST = "00000000-0000-4000-8000-000000000000"
EVENT_NAMES = {"meta", "token", "ping", "transition", "state", "done", "error"}


@pytest.fixture()
def provider():
    return FakeLLMProvider()


@pytest.fixture()
def app(provider):
    return make_app({**BOTH}, rpg_llm_provider=provider)


def db(app) -> Session:
    return Session(app.extensions["db_engine"])


def player(app, n=1, **overrides):
    """已登入的玩家：回傳 (client, user_id)。"""
    with db(app) as s:
        user = add_user(s, n, password=None, **overrides)
        uid, tv = user.id, user.token_version
    client = app.test_client()
    sign_in_as(client, uid, tv)
    return client, uid


def make_save(app, uid, *, opening="ready", **changes) -> str:
    """為玩家開一個存檔；`opening="ready"` 時第 0 幕已完成，`"pending"` 時尚未生成。回傳對外識別碼。"""
    with db(app) as s:
        repo = SessionRepository(s)
        clock = FakeClock()
        save = repo.create(
            uid, source_version_id=None, snapshot=validated().content, snapshot_spec_version=1, protagonist=protagonist(**changes),
            content_tier="safe", now=clock(),
        )
        if opening == "ready":
            claimed = repo.claim_opening(uid, save.public_id, now=clock())
            assert claimed is not None and claimed[0] == "claimed"
            assert repo.finish_opening(uid, save.public_id, claimed_at=clock(), state="ready", text="開場的劇情。")
        s.commit()
        return save.public_id


def parse(response) -> list[tuple[str, dict]]:
    """把事件串流解析成 [(事件名稱, 資料)]，同時檢查格式：`event:` 一行、`data:` 一行單行 JSON、空行。"""
    assert response.status_code == 200, response.get_data(as_text=True)
    raw = response.get_data(as_text=True)
    assert raw.endswith("\n\n")
    events = []
    for block in raw[:-2].split("\n\n"):
        lines = block.split("\n")
        assert len(lines) == 2 and lines[0].startswith("event: ") and lines[1].startswith("data: "), block
        events.append((lines[0][len("event: "):], json.loads(lines[1][len("data: "):])))
    return events


def names(events):
    return [name for name, _ in events]


def of(events, name):
    return [data for event, data in events if event == name]


def text_of(events):
    return "".join(data["text"] for data in of(events, "token"))


def turn(client, save_id, *, msg="m-1", action="我走進山門。", **extra):
    return client.post(f"/api/game/sessions/{save_id}/turns/stream", json={"client_msg_id": msg, "action": action, **extra})


def opening(client, save_id):
    return client.post(f"/api/game/sessions/{save_id}/opening/stream", json={})


def total(app, uid) -> int:
    with db(app) as s:
        state = WalletRepository(s).state(uid)
        return 0 if state is None else state.free_balance + state.paid_balance


def turn_rows(app) -> int:
    with db(app) as s:
        return s.scalar(select(func.count()).select_from(MemoryLog))


def script_turn(provider, *items, state=STATE_OK):
    provider.script_stream(*(items or story_script())).script(state)


# ---- 閘門 ---------------------------------------------------------------------------------------

ENDPOINTS = [
    ("POST", "/api/game/sessions/{id}/turns/stream", {"client_msg_id": "m", "action": "a"}),
    ("POST", "/api/game/sessions/{id}/opening/stream", {}),
    ("GET", "/api/game/sessions/{id}", None),
    ("GET", "/api/game/sessions/{id}/turns", None),
]


def call(client, method, path, body):
    return client.open(path.format(id=GHOST), method=method, json=body) if body is not None else client.open(path.format(id=GHOST), method=method)


@pytest.mark.parametrize(("method", "path", "body"), ENDPOINTS)
def test_anonymous_requests_are_401_json(app, method, path, body):
    response = call(app.test_client(), method, path, body)
    assert response.status_code == 401 and response.get_json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize(("method", "path", "body"), ENDPOINTS)
def test_a_player_who_has_not_confirmed_their_age_is_a_403(app, method, path, body):
    client, _ = player(app, age=None)
    assert call(client, method, path, body).status_code == 403


@pytest.mark.parametrize(("method", "path", "body"), ENDPOINTS)
def test_a_locked_account_is_refused_on_every_endpoint(app, method, path, body):
    client, _ = player(app, locked_at=FakeClock()(), lock_reason="rate_limited")
    response = call(client, method, path, body)
    assert response.status_code == 403 and response.get_json()["error"]["code"] == "account_locked"


def test_posts_without_the_csrf_token_are_refused_and_hold_nothing(app):
    with db(app) as s:
        user = add_user(s, 1, password=None)
        uid, tv = user.id, user.token_version
    save_id = make_save(app, uid)
    client = raw_client(app)
    sign_in_as(client, uid, tv)
    for path in (f"/api/game/sessions/{save_id}/turns/stream", f"/api/game/sessions/{save_id}/opening/stream"):
        response = client.post(path, json={"client_msg_id": "m", "action": "a"}, headers={"Origin": "http://localhost"})
        assert response.status_code == 403, path
    assert turn_rows(app) == 0 and total(app, uid) == 0         # 沒有預扣（錢包甚至還沒建立）


# ---- 回合的事件串流 -------------------------------------------------------------------------------


def test_a_normal_turn_streams_events_in_order_with_the_required_headers(app, provider):
    client, uid = player(app)
    save_id = make_save(app, uid)
    script_turn(provider)
    response = turn(client, save_id)
    assert response.headers["Content-Type"] == "text/event-stream; charset=utf-8"
    assert response.headers["Cache-Control"] == "no-cache, no-transform"
    assert response.headers["X-Accel-Buffering"] == "no"
    events = parse(response)
    order = names(events)
    assert order[0] == "meta" and order[-2:] == ["state", "done"] and order[-3] == "ping"
    assert set(order) <= EVENT_NAMES and order.count("token") >= 1
    assert text_of(events) == STORY
    assert of(events, "meta")[0]["client_msg_id"] == "m-1" and of(events, "meta")[0]["replayed"] is False
    assert of(events, "done")[-1] == {"status": "complete", "turn_number": 1, "coins": 170}
    assert total(app, uid) == 170


def test_the_state_event_has_only_validated_fields_and_never_the_models_raw_json(app, provider):
    client, uid = player(app)
    save_id = make_save(app, uid)
    script_turn(provider)
    events = parse(turn(client, save_id))
    (state,) = of(events, "state")
    assert state["updated"] is True and state["location"] == "山門前"
    assert set(state) <= {"updated", "elapsed_months", "location", "summary", "goals", "notices"}
    assert "current_location" not in json.dumps(state)           # 模型輸出的欄位名稱不在事件裡


def test_an_event_stream_is_the_only_response_with_the_event_content_type(app, provider):
    client, uid = player(app)
    save_id = make_save(app, uid)
    script_turn(provider)
    assert turn(client, save_id).mimetype == "text/event-stream"
    assert client.get(f"/api/game/sessions/{save_id}").mimetype == "application/json"


def test_a_provider_failure_after_the_stream_starts_is_an_error_event_and_the_coins_come_back(app, provider):
    client, uid = player(app)
    save_id = make_save(app, uid)
    provider.script_stream(Chunk("清晨的海風吹過山門。"), Fail(LLMTransientError("rate_limited")))
    events = parse(turn(client, save_id))
    assert names(events)[0] == "meta" and names(events)[-1] == "error" and "done" not in names(events)
    error = of(events, "error")[0]
    assert error["code"] in {"generation_failed", "generation_timeout"} and re.search(r"[一-鿿]", error["message"])
    assert total(app, uid) == 200                                  # 預扣已退回


def test_two_blocked_outputs_end_with_transitions_and_a_blocked_done_without_the_blocked_text(app, provider):
    client, uid = player(app)
    save_id = make_save(app, uid)
    provider.script_stream(*story_script(BAD_SENTENCE)).script_stream(*story_script(BAD_SENTENCE))
    events = parse(turn(client, save_id))
    assert [data["will_retry"] for data in of(events, "transition")] == [True, False]
    assert of(events, "done")[-1] == {"status": "blocked", "turn_number": None, "coins": 200}
    assert BAD_SENTENCE not in json.dumps(events, ensure_ascii=False)
    assert total(app, uid) == 200


# ---- 開始前的錯誤一律是 JSON -----------------------------------------------------------------------


def test_a_non_owner_gets_the_same_404_json_as_a_missing_save(app):
    owner, owner_id = player(app, 1)
    save_id = make_save(app, owner_id)
    other, _ = player(app, 2)
    missing = turn(other, GHOST)
    foreign = turn(other, save_id)
    assert missing.status_code == foreign.status_code == 404
    assert missing.get_json() == foreign.get_json() and missing.mimetype == "application/json"
    for path in (f"/api/game/sessions/{save_id}", f"/api/game/sessions/{save_id}/turns", f"/api/game/sessions/{save_id}/opening/stream"):
        response = other.post(path, json={}) if path.endswith("stream") else other.get(path)
        assert response.status_code == 404 and response.mimetype == "application/json", path
    assert turn_rows(app) == 0


def test_insufficient_coins_is_a_json_409_and_leaves_no_turn_row(app, provider):
    client, uid = player(app)
    save_id = make_save(app, uid)
    script_turn(provider)
    for n in range(7):                                             # 200 幣只夠 6 回合（每回合 30）
        response = turn(client, save_id, msg=f"m-{n}")
        if response.status_code == 200:
            script_turn(provider)
            continue
        assert response.status_code == 409 and response.mimetype == "application/json"
        assert response.get_json()["error"]["code"] == "insufficient_coins"
        assert response.get_json()["balance"] < response.get_json()["needed"]
        break
    else:
        pytest.fail("餘額應該不足")
    with db(app) as s:
        assert s.scalar(select(func.count()).select_from(MemoryLog).where(MemoryLog.client_msg_id == "m-6")) == 0


@pytest.mark.parametrize(("body", "field"), [
    ({"action": "走"}, "client_msg_id"), ({"client_msg_id": "m", "action": "   "}, "action"), ({"client_msg_id": "m"}, "action"),
    ({"client_msg_id": "x" * 65, "action": "走"}, "client_msg_id"),
])
def test_validation_errors_are_a_400_naming_the_field_and_hold_nothing(app, body, field):
    client, uid = player(app)
    save_id = make_save(app, uid)
    response = client.post(f"/api/game/sessions/{save_id}/turns/stream", json=body)
    assert response.status_code == 400 and field in response.get_json()["error"]["fields"]
    assert turn_rows(app) == 0 and total(app, uid) in (0, 200)


def test_a_non_json_body_is_415_and_a_non_object_is_400(app):
    client, uid = player(app)
    save_id = make_save(app, uid)
    path = f"/api/game/sessions/{save_id}/turns/stream"
    assert client.post(path, data="x", content_type="text/plain").status_code == 415
    assert client.post(path, json=[1]).status_code == 400


def test_an_action_the_rules_layer_must_reject_is_a_422_with_categories_only(app):
    client, uid = player(app)
    save_id = make_save(app, uid)
    response = turn(client, save_id, action=BAD_SENTENCE)
    assert response.status_code == 422 and response.get_json()["error"]["code"] == "input_blocked"
    assert response.get_json()["categories"] and BAD_SENTENCE not in response.get_data(as_text=True)
    assert turn_rows(app) == 0


def test_a_turn_before_the_opening_is_done_is_a_409_opening_pending(app):
    client, uid = player(app)
    save_id = make_save(app, uid, opening="pending")
    response = turn(client, save_id)
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "opening_pending"


def test_an_ended_session_refuses_new_turns_but_still_reads(app, provider):
    client, uid = player(app)
    save_id = make_save(app, uid)
    script_turn(provider)
    parse(turn(client, save_id))
    with db(app) as s:
        assert SessionRepository(s).end_session(uid, save_id, now=FakeClock()())
        s.commit()
    response = turn(client, save_id, msg="m-2")
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "session_ended"
    snapshot = client.get(f"/api/game/sessions/{save_id}").get_json()["session"]
    assert snapshot["ended"] is True and snapshot["status"] == "ended" and snapshot["completed_turns"] == 1
    turns = client.get(f"/api/game/sessions/{save_id}/turns").get_json()["turns"]
    assert [item["status"] for item in turns] == ["complete"] and turns[0]["story"] == STORY


def test_without_an_rpg_provider_the_stream_endpoints_are_a_503_and_hold_nothing():
    app = make_app({"GEMINI_API_KEY": GEMINI_KEY})
    client, uid = player(app)
    save_id = make_save(app, uid, opening="pending")
    for path in (f"/api/game/sessions/{save_id}/turns/stream", f"/api/game/sessions/{save_id}/opening/stream"):
        response = client.post(path, json={"client_msg_id": "m", "action": "走"})
        assert response.status_code == 503 and response.mimetype == "application/json", path
        assert response.get_json()["error"]["code"] == "service_unavailable"
    assert turn_rows(app) == 0 and total(app, uid) == 0
    with db(app) as s:
        assert s.scalar(select(RpgSession.opening_state)) == "pending"        # 沒有搶佔第 0 幕
    assert client.get(f"/api/game/sessions/{save_id}").status_code == 200       # 讀取不需要供應商


# ---- 重送 -----------------------------------------------------------------------------------------


def test_resending_a_completed_turn_replays_the_story_without_a_model_call_or_a_charge(app, provider):
    client, uid = player(app)
    save_id = make_save(app, uid)
    script_turn(provider)
    first = parse(turn(client, save_id))
    calls = len(provider.calls)
    coins = total(app, uid)
    again = parse(turn(client, save_id))
    assert names(again) == ["meta", "token", "done"]
    assert of(again, "meta")[0]["replayed"] is True and of(again, "meta")[0]["turn_id"] == of(first, "meta")[0]["turn_id"]
    assert of(again, "token")[0]["text"] == STORY
    assert of(again, "done")[0] == {"status": "complete", "turn_number": 1, "coins": coins}
    assert len(provider.calls) == calls and total(app, uid) == coins and turn_rows(app) == 1


def test_resending_the_ending_turn_replays_it_as_ended(app, provider):
    client, uid = player(app)
    save_id = make_save(app, uid)
    script_turn(provider)
    parse(turn(client, save_id))
    with db(app) as s:
        SessionRepository(s).end_session(uid, save_id, now=FakeClock()())
        s.commit()
    again = parse(turn(client, save_id))
    assert of(again, "done")[0]["status"] == "ended"


def test_an_interrupted_turn_can_be_resent_with_the_same_id_and_is_charged_once(app, provider):
    client, uid = player(app)
    save_id = make_save(app, uid)
    provider.script_stream(Chunk("清晨的海風吹過山門。"), Fail(LLMTransientError("network")))
    assert "error" in names(parse(turn(client, save_id)))
    assert total(app, uid) == 200 and provider.remaining_streams == 0
    script_turn(provider)
    events = parse(turn(client, save_id))
    assert of(events, "meta")[0]["attempt"] == 2 and of(events, "done")[-1]["status"] == "complete"
    assert total(app, uid) == 170 and turn_rows(app) == 1


# ---- 第 0 幕 --------------------------------------------------------------------------------------


def test_the_opening_streams_once_and_a_second_call_returns_the_saved_text(app, provider):
    client, uid = player(app)
    save_id = make_save(app, uid, opening="pending")
    provider.script_stream(*story_script())
    response = opening(client, save_id)
    assert response.headers["Content-Type"] == "text/event-stream; charset=utf-8"
    events = parse(response)
    assert names(events)[0] == "meta" and of(events, "meta")[0]["kind"] == "opening" and of(events, "meta")[0]["replayed"] is False
    assert names(events)[-1] == "done" and of(events, "done")[0]["opening_state"] == "ready"
    assert text_of(events) == STORY
    assert total(app, uid) in (0, 200)                             # 第 0 幕不扣幣
    calls = len(provider.calls)
    again = parse(opening(client, save_id))
    assert names(again) == ["meta", "token", "done"] and of(again, "meta")[0]["replayed"] is True
    assert text_of(again) == STORY and len(provider.calls) == calls
    snapshot = client.get(f"/api/game/sessions/{save_id}").get_json()["session"]
    assert snapshot["opening"] == {"state": "ready", "text": STORY}


def test_a_turn_can_follow_the_opening(app, provider):
    client, uid = player(app)
    save_id = make_save(app, uid, opening="pending")
    provider.script_stream(*story_script())
    parse(opening(client, save_id))
    script_turn(provider)
    assert of(parse(turn(client, save_id)), "done")[-1]["status"] == "complete"


# ---- 唯讀端點 -------------------------------------------------------------------------------------


def test_the_session_snapshot_has_no_internal_fields_and_no_script_copy(app, provider):
    client, uid = player(app)
    save_id = make_save(app, uid)
    body = client.get(f"/api/game/sessions/{save_id}").get_json()["session"]
    assert body["id"] == save_id and body["status"] == "active" and body["completed_turns"] == 0
    assert body["opening"] == {"state": "ready", "text": "開場的劇情。"}
    assert set(body["world"]) == {"elapsed_months", "location", "summary", "goals", "places", "protagonist_age"}
    assert body["notebook"] == {"version": 1, "length": 0, "capacity": 2000, "full": False}      # 只有用量摘要（rpg-notebook）
    raw = json.dumps(body, ensure_ascii=False)
    for forbidden in ("script_snapshot", "entities", "marker", "source_version", "user_id", "notebook_text", "notebook_upto_turn"):
        assert forbidden not in raw
    with db(app) as s:
        internal = s.scalar(select(RpgSession.id))
    assert body["id"] != internal


def test_the_snapshot_shows_a_pending_opening_without_text(app):
    client, uid = player(app)
    save_id = make_save(app, uid, opening="pending")
    assert client.get(f"/api/game/sessions/{save_id}").get_json()["session"]["opening"] == {"state": "pending", "text": None}


def test_turns_can_be_synced_incrementally_with_since_seq(app, provider):
    client, uid = player(app)
    save_id = make_save(app, uid)
    for n in (1, 2, 3):
        script_turn(provider)
        parse(turn(client, save_id, msg=f"m-{n}", action=f"第 {n} 個行動。"))
    all_turns = client.get(f"/api/game/sessions/{save_id}/turns").get_json()
    assert [t["turn_number"] for t in all_turns["turns"]] == [1, 2, 3] and all_turns["has_more"] is False
    assert all_turns["turns"][0]["action"] == "第 1 個行動。" and all_turns["turns"][0]["story"] == STORY
    seqs = [t["seq"] for t in all_turns["turns"]]
    assert seqs == sorted(seqs)
    later = client.get(f"/api/game/sessions/{save_id}/turns?since_seq={seqs[0]}").get_json()["turns"]
    assert [t["seq"] for t in later] == seqs[1:]
    page = client.get(f"/api/game/sessions/{save_id}/turns?limit=2").get_json()
    assert len(page["turns"]) == 2 and page["has_more"] is True


@pytest.mark.parametrize("query", ["since_seq=-1", "since_seq=abc", "limit=0", "limit=201", "limit=x"])
def test_bad_query_parameters_are_a_400(app, query):
    client, uid = player(app)
    save_id = make_save(app, uid)
    assert client.get(f"/api/game/sessions/{save_id}/turns?{query}").status_code == 400


def test_blocked_and_interrupted_turns_are_marked_and_carry_no_text(app, provider):
    client, uid = player(app)
    save_id = make_save(app, uid)
    provider.script_stream(*story_script(BAD_SENTENCE)).script_stream(*story_script(BAD_SENTENCE))
    parse(turn(client, save_id, msg="blocked-1"))
    with db(app) as s:                                              # 一個卡在生成中、帶著部分文字的回合
        repo = SessionRepository(s)
        started = repo.start_turn(uid, save_id, client_msg_id="partial-1", user_action="另一個行動。", now=FakeClock()())
        repo.interrupt_turn(uid, save_id, "partial-1", partial_response="尚未完成的部分文字。", attempt=started.turn.attempt)
        s.commit()
    body = client.get(f"/api/game/sessions/{save_id}/turns").get_json()
    by_id = {t["client_msg_id"]: t for t in body["turns"]}
    assert by_id["blocked-1"]["status"] == TURN_BLOCKED and by_id["blocked-1"]["story"] is None and by_id["blocked-1"]["turn_number"] is None
    assert by_id["partial-1"]["status"] == "interrupted" and by_id["partial-1"]["story"] is None
    raw = json.dumps(body, ensure_ascii=False)
    assert BAD_SENTENCE not in raw and "尚未完成的部分文字" not in raw


def test_json_responses_under_api_game_are_not_cacheable(app):
    client, uid = player(app)
    save_id = make_save(app, uid)
    for response in (
        client.get(f"/api/game/sessions/{save_id}"), client.get(f"/api/game/sessions/{save_id}/turns"),
        client.get(f"/api/game/sessions/{GHOST}"), turn(client, GHOST),
    ):
        assert response.headers["Cache-Control"] == "no-store", response.request.path


def test_a_player_reads_only_their_own_turns(app, provider):
    owner, owner_id = player(app, 1)
    save_id = make_save(app, owner_id)
    script_turn(provider)
    parse(turn(owner, save_id))
    other, _ = player(app, 2)
    assert other.get(f"/api/game/sessions/{save_id}/turns").status_code == 404
    assert other.get(f"/api/game/sessions/{save_id}").status_code == 404


# ---- 斷線 -----------------------------------------------------------------------------------------


def test_closing_the_response_closes_the_runners_event_generator(app):
    """客戶端斷線時 WSGI 伺服器會關閉回應：這個關閉必須傳到事件產生器，讓 `TurnRunner`／`OpeningRunner` 的收尾（退款、退回 pending）執行。"""
    from app.controllers.game_api_controller import _event_stream
    from app.services.rpg_runner_base import SseEvent

    closed = []

    def events():
        try:
            yield SseEvent("meta", {"x": 1})
            yield SseEvent("token", {"text": "a"})
        except GeneratorExit:
            closed.append(True)
            raise

    with app.test_request_context():
        response = _event_stream(events())
        stream = iter(response.response)
        assert next(stream).startswith("event: meta")
        response.close()
    assert closed == [True]
