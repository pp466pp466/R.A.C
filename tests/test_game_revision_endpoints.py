"""rpg-turn-revision 規格「兩個修訂串流端點」：`POST /api/game/sessions/<id>/turns/<client_msg_id>/regenerate/stream` 與 `…/edit/stream`。

用測試應用程式（SQLite）、直接寫入的登入狀態與假的串流供應商；修訂的各種出口對錢包與資料的細節由 `test_rpg_revision_runner`、
`test_game_service_revision` 涵蓋，這裡驗證的是 HTTP 這一層：標頭與內容型別、CSRF 與 Origin、閘門、404 一致、開始前的失敗一律是 JSON
（不開啟事件串流）、沒有供應商時的 503（不認領、不預扣），以及頁面外殼的初始狀態。
"""
import json
import re

import pytest
from sqlalchemy import select

from app.models import MemoryLog, TurnRegeneration
from app.repositories.session_repository import SessionRepository
from app.services import rpg_world
from tests.support.auth import FakeClock, add_user, sign_in_as
from tests.support.client import raw_client
from tests.support.game import BAD_SENTENCE, STORY, story_script
from tests.support.llm import FakeLLMProvider
from tests.test_game_api import (
    EVENT_NAMES, GHOST, STATE_OK, db, make_save, names, of, parse, player, script_turn, text_of, total, turn,
)
from tests.test_llm_providers_by_purpose import BOTH, GEMINI_KEY, make_app

ACTION = "我走進山門。"
NEW_ACTION = "我繞到後山，沿著小徑往上走。"
REASON = "劇情和設定對不上，想換個寫法"
NEW_STORY = ("夜裡的雨敲著屋簷。", "老僧為你點亮一盞油燈。", "你在燈下翻開了舊書。")
NEW = "".join(NEW_STORY)
STATE_NEW = json.dumps({"elapsed_months": 1, "status_summary": "你在寺中避雨。"}, ensure_ascii=False)
REGEN_PATH = "/api/game/sessions/{id}/turns/{msg}/regenerate/stream"
EDIT_PATH = "/api/game/sessions/{id}/turns/{msg}/edit/stream"


@pytest.fixture()
def provider():
    return FakeLLMProvider()


@pytest.fixture()
def app(provider):
    return make_app({**BOTH}, rpg_llm_provider=provider)


# ---- 輔助 -------------------------------------------------------------------------------------

def regen(client, save_id, msg="m-1", *, revision=1, category="other", reason=REASON, **extra):
    body = {"expected_revision": revision, "category": category, "reason": reason, **extra}
    return client.post(REGEN_PATH.format(id=save_id, msg=msg), json=body)


def edit(client, save_id, msg="m-1", *, revision=1, action=NEW_ACTION, **extra):
    return client.post(EDIT_PATH.format(id=save_id, msg=msg), json={"expected_revision": revision, "action": action, **extra})


def played(app, provider, *, first=True):
    """一個玩家、一個存檔，已經完成第 1 輪（走 HTTP，所以有回合前快照）；回傳（client, uid, save_id）。"""
    client, uid = player(app)
    save_id = make_save(app, uid)
    if first:
        script_turn(provider)
        parse(turn(client, save_id))
    return client, uid, save_id


def script_revision(provider, *items, state=STATE_NEW):
    provider.script_stream(*(items or story_script(*NEW_STORY))).script(state)


def row(app, msg="m-1"):
    with db(app) as s:
        turn_row = s.scalar(select(MemoryLog).where(MemoryLog.client_msg_id == msg))
        return (turn_row.revision, turn_row.regen_count, turn_row.attempt, turn_row.revising_started_at, turn_row.llm_raw_response, turn_row.user_action)


def feedback_rows(app) -> int:
    with db(app) as s:
        return len(list(s.scalars(select(TurnRegeneration))))


def assert_untouched(app, uid, *, coins, feedback=0):
    """開始前就失敗的請求什麼都沒留下：沒有認領、嘗試序號不變、沒有回饋列、餘額不變。"""
    revision, regen_count, attempt, claimed, story, _action = row(app)
    assert (revision, regen_count, attempt, claimed, story) == (1, 0, 1, None, STORY)
    assert feedback_rows(app) == feedback and total(app, uid) == coins


def finish_turn_directly(app, uid, save_id, *, msg="m-1"):
    """不經過串流，直接用儲存庫完成一個有快照的回合（沒有預扣）。"""
    with db(app) as s:
        repo = SessionRepository(s)
        world = rpg_world.WorldState.from_fields(
            entities=None, goals=None, milestone_progress=None, elapsed_months=0, current_location="", status_summary="",
        )
        started = repo.start_turn(uid, save_id, client_msg_id=msg, user_action=ACTION, now=FakeClock()(), state_before=world.to_fields())
        assert started is not None
        repo.complete_turn(uid, save_id, msg, llm_raw_response=STORY, structured_result=None, now=FakeClock()(), attempt=started.turn.attempt)
        s.commit()


# ---- 閘門 ---------------------------------------------------------------------------------------

ENDPOINTS = [
    ("POST", REGEN_PATH, {"expected_revision": 1, "category": "other", "reason": REASON}),
    ("POST", EDIT_PATH, {"expected_revision": 1, "action": NEW_ACTION}),
]


def call(client, path, body):
    return client.post(path.format(id=GHOST, msg="m-1"), json=body)


@pytest.mark.parametrize(("method", "path", "body"), ENDPOINTS)
def test_anonymous_requests_are_401_json(app, method, path, body):
    response = call(app.test_client(), path, body)
    assert response.status_code == 401 and response.get_json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize(("method", "path", "body"), ENDPOINTS)
def test_a_player_who_has_not_confirmed_their_age_is_a_403(app, method, path, body):
    client, _ = player(app, age=None)
    assert call(client, path, body).status_code == 403


@pytest.mark.parametrize(("method", "path", "body"), ENDPOINTS)
def test_a_locked_account_is_refused(app, method, path, body):
    client, _ = player(app, locked_at=FakeClock()(), lock_reason="rate_limited")
    response = call(client, path, body)
    assert response.status_code == 403 and response.get_json()["error"]["code"] == "account_locked"


def test_posts_without_the_csrf_token_are_refused_and_claim_and_hold_nothing(app, provider):
    client, uid, save_id = played(app, provider)
    with db(app) as s:
        from app.models import User

        token_version = s.get(User, uid).token_version
    bare = raw_client(app)
    sign_in_as(bare, uid, token_version)
    coins = total(app, uid)
    for path, body in ((REGEN_PATH, ENDPOINTS[0][2]), (EDIT_PATH, ENDPOINTS[1][2])):
        response = bare.post(path.format(id=save_id, msg="m-1"), json=body, headers={"Origin": "http://localhost"})
        assert response.status_code == 403, path
    assert_untouched(app, uid, coins=coins)


# ---- 成功的事件串流 --------------------------------------------------------------------------------

def test_a_regeneration_streams_events_in_order_with_the_required_headers_and_replaces_the_turn(app, provider):
    client, uid, save_id = played(app, provider)
    script_revision(provider)
    response = regen(client, save_id)
    assert response.headers["Content-Type"] == "text/event-stream; charset=utf-8"
    assert response.headers["Cache-Control"] == "no-cache, no-transform" and response.headers["X-Accel-Buffering"] == "no"
    events = parse(response)
    order = names(events)
    assert order[0] == "meta" and order[-3:] == ["ping", "state", "done"] and set(order) <= EVENT_NAMES and order.count("token") >= 1
    assert text_of(events) == NEW
    assert of(events, "meta")[0] == {"kind": "regenerate", "turn_id": 1, "client_msg_id": "m-1", "attempt": 2, "revision": 1, "replayed": False}
    assert of(events, "done")[0] == {"status": "complete", "turn_number": 1, "coins": 170, "revision": 2, "free_left": 1}
    assert row(app)[:3] == (2, 1, 2) and row(app)[4] == NEW and total(app, uid) == 170
    turns = client.get(f"/api/game/sessions/{save_id}/turns").get_json()["turns"]                     # 之後的讀取是新內容與新的 revision
    assert [(item["story"], item["revision"], item["turn_number"]) for item in turns] == [(NEW, 2, 1)]
    last = client.get(f"/api/game/sessions/{save_id}").get_json()["session"]["last_turn"]
    assert (last["client_msg_id"], last["revision"], last["revisable"], last["free_left"]) == ("m-1", 2, True, 1)


def test_an_edit_streams_events_replaces_the_action_charges_and_resets_the_free_count(app, provider):
    client, uid, save_id = played(app, provider)
    script_revision(provider)
    events = parse(edit(client, save_id))
    assert of(events, "meta")[0]["kind"] == "edit"
    assert of(events, "done")[0] == {"status": "complete", "turn_number": 1, "coins": 140, "revision": 2, "free_left": 2}
    revision, regen_count, _attempt, claimed, story, action = row(app)
    assert (revision, regen_count, claimed, story, action) == (2, 0, None, NEW, NEW_ACTION) and total(app, uid) == 140


def test_the_state_event_of_a_revision_has_only_validated_fields(app, provider):
    client, uid, save_id = played(app, provider)
    script_revision(provider)
    events = parse(regen(client, save_id))
    (state,) = of(events, "state")
    assert state["updated"] is True and set(state) <= {"updated", "elapsed_months", "location", "summary", "goals", "notices"}
    assert "status_summary" not in json.dumps(state)


def test_a_failure_after_the_stream_starts_is_an_error_event_and_the_old_content_stays(app, provider):
    client, uid, save_id = played(app, provider)
    script_revision(provider, state="不是 JSON")
    provider.script("還是不是 JSON")
    events = parse(regen(client, save_id))
    assert names(events)[0] == "meta" and names(events)[-1] == "error" and "done" not in names(events)
    error = of(events, "error")[0]
    assert error["code"] == "generation_failed" and re.search(r"[一-鿿]", error["message"])
    assert row(app)[:4] == (1, 0, 2, None) and row(app)[4] == STORY and total(app, uid) == 170


def test_two_blocked_outputs_end_with_a_blocked_done_and_never_show_the_blocked_text(app, provider):
    client, uid, save_id = played(app, provider)
    provider.script_stream(*story_script(BAD_SENTENCE)).script_stream(*story_script(BAD_SENTENCE))
    events = parse(regen(client, save_id))
    assert [data["will_retry"] for data in of(events, "transition")] == [True, False]
    assert of(events, "done")[-1] == {"status": "blocked", "turn_number": 1, "coins": 170, "revision": 1, "free_left": 2}
    assert BAD_SENTENCE not in json.dumps(events, ensure_ascii=False)
    assert row(app)[4] == STORY


# ---- 開始前的錯誤一律是 JSON -----------------------------------------------------------------------

def test_a_non_owner_gets_the_same_404_json_as_a_missing_save_or_turn_and_nothing_is_claimed(app, provider):
    owner, owner_id, save_id = played(app, provider)
    other, other_id = player(app, 2)
    coins = total(app, owner_id)
    responses = [
        regen(other, GHOST), regen(other, save_id), regen(owner, save_id, "no-such-turn"),
        edit(other, GHOST), edit(other, save_id), edit(owner, save_id, "no-such-turn"),
    ]
    assert {response.status_code for response in responses} == {404} and all(response.mimetype == "application/json" for response in responses)
    assert len({json.dumps(response.get_json(), sort_keys=True) for response in responses}) == 1
    assert_untouched(app, owner_id, coins=coins)
    assert total(app, other_id) == 0


@pytest.mark.parametrize(("make_request", "field"), [
    (lambda c, s: c.post(REGEN_PATH.format(id=s, msg="m-1"), json={"category": "other", "reason": REASON}), "expected_revision"),
    (lambda c, s: c.post(REGEN_PATH.format(id=s, msg="m-1"), json={"expected_revision": "1", "category": "other", "reason": REASON}), "expected_revision"),
    (lambda c, s: c.post(EDIT_PATH.format(id=s, msg="m-1"), json={"expected_revision": 1, "action": "   "}), "action"),
    (lambda c, s: c.post(EDIT_PATH.format(id=s, msg="m-1"), json={"expected_revision": 1}), "action"),
    (lambda c, s: c.post(REGEN_PATH.format(id=s, msg="x" * 65), json={"expected_revision": 1, "category": "other", "reason": REASON}), "client_msg_id"),
])
def test_a_malformed_body_is_a_400_json_naming_the_field_and_holds_nothing(app, provider, make_request, field):
    client, uid, save_id = played(app, provider)
    coins = total(app, uid)
    response = make_request(client, save_id)
    assert response.status_code == 400 and response.mimetype == "application/json" and field in response.get_json()["error"]["fields"]
    assert_untouched(app, uid, coins=coins)


@pytest.mark.parametrize(("body", "field"), [
    ({"expected_revision": 1, "category": "other", "reason": "太短"}, "reason"),
    ({"expected_revision": 1, "category": "other"}, "reason"),
    ({"expected_revision": 1, "category": "no-such-category", "reason": REASON}, "category"),
    ({"expected_revision": 1, "reason": REASON}, "category"),
    ({"expected_revision": 1, "category": "other", "reason": "字" * 51}, "reason"),
])
def test_a_missing_or_invalid_reason_or_category_is_a_422_naming_the_field_and_holds_nothing(app, provider, body, field):
    client, uid, save_id = played(app, provider)
    coins = total(app, uid)
    response = client.post(REGEN_PATH.format(id=save_id, msg="m-1"), json=body)
    assert response.status_code == 422 and response.mimetype == "application/json" and field in response.get_json()["error"]["fields"]
    assert "categories" not in response.get_json()
    assert_untouched(app, uid, coins=coins)


def test_an_edit_with_the_same_action_is_a_422_and_an_edit_the_rules_layer_must_reject_is_a_422_with_categories_only(app, provider):
    client, uid, save_id = played(app, provider)
    coins = total(app, uid)
    same = edit(client, save_id, action=ACTION)
    assert same.status_code == 422 and "action" in same.get_json()["error"]["fields"]
    blocked = edit(client, save_id, action=BAD_SENTENCE)
    assert blocked.status_code == 422 and blocked.get_json()["error"]["code"] == "input_blocked"
    assert blocked.get_json()["categories"] and BAD_SENTENCE not in blocked.get_data(as_text=True)
    assert_untouched(app, uid, coins=coins)


def test_a_stale_expected_revision_is_a_409_revision_conflict_and_changes_nothing(app, provider):
    client, uid, save_id = played(app, provider)
    script_revision(provider)
    parse(regen(client, save_id))                                                                      # revision 變成 2
    coins = total(app, uid)
    response = regen(client, save_id, revision=1)
    assert response.status_code == 409 and response.mimetype == "application/json"
    assert response.get_json()["error"]["code"] == "revision_conflict"
    assert row(app)[:4] == (2, 1, 2, None) and total(app, uid) == coins and feedback_rows(app) == 1


def test_a_turn_that_is_not_the_last_one_is_a_409_not_revisable_with_the_reason(app, provider):
    client, uid, save_id = played(app, provider)
    script_turn(provider)
    parse(turn(client, save_id, msg="m-2", action="我再往前走。"))
    coins = total(app, uid)
    for response in (regen(client, save_id, "m-1"), edit(client, save_id, "m-1")):
        assert response.status_code == 409 and response.mimetype == "application/json"
        assert response.get_json()["error"]["code"] == "not_revisable" and response.get_json()["reason"] == "not_last"
    assert total(app, uid) == coins and feedback_rows(app) == 0


def test_an_ended_session_is_a_409_not_revisable_ended(app, provider):
    client, uid, save_id = played(app, provider)
    with db(app) as s:
        assert SessionRepository(s).end_session(uid, save_id, now=FakeClock()())
        s.commit()
    response = regen(client, save_id)
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "not_revisable" and response.get_json()["reason"] == "ended"


def test_insufficient_coins_is_a_json_409_and_leaves_no_claim_and_no_feedback_row(app, provider):
    client, uid, save_id = played(app, provider)
    with db(app) as s:
        from app.repositories.runtime_limit_repository import RuntimeLimitRepository

        RuntimeLimitRepository(s).set_value("rpg.free_regenerations", 0, FakeClock()())              # 讓重新生成一開始就收費
        s.commit()
    for n in range(2, 7):                                                                              # 200 幣：6 輪各 30 之後剩 20，不夠一次修訂
        script_turn(provider)
        parse(turn(client, save_id, msg=f"m-{n}", action=f"我繼續走，第 {n} 步。"))
    assert total(app, uid) == 20
    response = regen(client, save_id, "m-6")
    assert response.status_code == 409 and response.mimetype == "application/json"
    assert response.get_json()["error"]["code"] == "insufficient_coins" and response.get_json()["balance"] < response.get_json()["needed"]
    response = edit(client, save_id, "m-6")
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "insufficient_coins"
    revision, regen_count, attempt, claimed, _story, _action = row(app, "m-6")
    assert (revision, regen_count, attempt, claimed) == (1, 0, 1, None) and feedback_rows(app) == 0 and total(app, uid) == 20


def test_a_revision_in_progress_blocks_a_new_turn_and_a_second_revision_with_turn_in_progress(app, provider):
    client, uid, save_id = played(app, provider)
    with db(app) as s:                                                                                 # 另一個裝置正在重新生成（認領未逾時）
        assert SessionRepository(s).claim_revision(uid, save_id, "m-1", expected_revision=1, now=FakeClock()(), stale_after_seconds=180) is not None
        s.commit()
    coins = total(app, uid)
    new_turn = turn(client, save_id, msg="m-2", action="我再往前走。")
    assert new_turn.status_code == 409 and new_turn.mimetype == "application/json" and new_turn.get_json()["error"]["code"] == "turn_in_progress"
    again = regen(client, save_id)
    assert again.status_code == 409 and again.get_json()["error"]["code"] == "turn_in_progress"
    with db(app) as s:
        assert [turn_row.client_msg_id for turn_row in s.scalars(select(MemoryLog))] == ["m-1"]        # 沒有新的回合列
    assert total(app, uid) == coins and feedback_rows(app) == 0


def test_a_turn_being_generated_blocks_a_revision_with_not_revisable_in_progress(app, provider):
    client, uid, save_id = played(app, provider)
    with db(app) as s:                                                                                 # 下一輪正在生成（未逾時）
        assert SessionRepository(s).start_turn(uid, save_id, client_msg_id="m-2", user_action="我再往前走。", now=FakeClock()()) is not None
        s.commit()
    response = regen(client, save_id)
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "not_revisable" and response.get_json()["reason"] == "in_progress"
    assert feedback_rows(app) == 0


def test_a_turn_without_a_snapshot_is_a_409_not_revisable_no_snapshot(app, provider):
    client, uid, save_id = played(app, provider, first=False)
    with db(app) as s:
        repo = SessionRepository(s)
        started = repo.start_turn(uid, save_id, client_msg_id="m-1", user_action=ACTION, now=FakeClock()())          # 沒有快照（遷移之前的回合）
        repo.complete_turn(uid, save_id, "m-1", llm_raw_response=STORY, structured_result=None, now=FakeClock()(), attempt=started.turn.attempt)
        s.commit()
    response = regen(client, save_id)
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "not_revisable" and response.get_json()["reason"] == "no_snapshot"


def test_without_an_rpg_provider_the_revision_endpoints_are_a_503_and_claim_and_hold_nothing():
    app = make_app({"GEMINI_API_KEY": GEMINI_KEY})
    client, uid = player(app)
    save_id = make_save(app, uid)
    finish_turn_directly(app, uid, save_id)
    for response in (regen(client, save_id), edit(client, save_id)):
        assert response.status_code == 503 and response.mimetype == "application/json"
        assert response.get_json()["error"]["code"] == "service_unavailable"
    revision, regen_count, attempt, claimed, story, _action = row(app)
    assert (revision, regen_count, attempt, claimed, story) == (1, 0, 1, None, STORY)                  # 不認領
    assert feedback_rows(app) == 0 and total(app, uid) == 0                                            # 不預扣（錢包甚至還沒建立）
    assert client.get(f"/api/game/sessions/{save_id}").status_code == 200                              # 讀取不需要供應商
