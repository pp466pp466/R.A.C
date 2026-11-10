"""service-observability 規格「RPG 回合與第 0 幕事件只含白名單欄位」，以及組裝層（registry）的接線。

日誌掃描：走過一個完整回合、一個被擋回合、一個中斷回合與第 0 幕，收集期間的全部日誌輸出，找不到行動、劇情、NPC 名稱、地點、
第 0 幕、被擋文字、冪等鍵、存檔識別碼與標記字串。
"""
import inspect
import json
import logging

import pytest

from app.observability import rpg_events
from app.observability.json_logging import ALLOWED_FIELDS
from app.services.registry import (
    build_opening_runner, build_runner_deps, build_turn_runner, get_game_service, has_rpg_provider,
)
from app.services.rpg_prompt import rpg_marker
from tests.support.game import BAD_SENTENCE, MARKER, RunnerKit, story_script
from tests.support.llm import FakeLLMProvider, Step
from tests.test_llm_providers_by_purpose import BOTH, RPG_MODEL, make_app, request_scope

BASE_FIELDS = {"time", "level", "logger", "message"}
ACTION = "我向林曉打聽祕密地圖的下落。"
STORY_A = ("獨特甲：山門前起了薄霧。", "獨特乙：林曉低聲說了一句話。", "獨特丙：你點了點頭。")
OPENING = ("獨特開場甲：海風拂過港口。", "獨特開場乙：遠處傳來鐘聲。", "獨特開場丙：旅程從這裡開始。")
STATE = json.dumps({"elapsed_months": 2, "current_location": "祕密山谷", "npc_updates": [{"name": "林曉", "anchor_fact": "獨特錨定事實"}]}, ensure_ascii=False)


# ---- 事件模組本身 -----------------------------------------------------------------------------

def test_there_are_twelve_unique_events():
    names = [value for key, value in vars(rpg_events).items() if key.startswith("EVENT_") and isinstance(value, str)]
    assert len(names) == len(set(names)) == len(rpg_events.EVENT_NAMES) == 12


def test_no_logging_function_takes_a_free_text_parameter():
    allowed = {
        "user_id", "session_id", "turn_number", "attempt", "start", "tier", "model", "chars", "latency_ms", "first_chunk_ms", "finish_reason",
        "dropped", "state_ok", "world_updated", "ended", "categories", "auto_retry", "final", "reason",
    }
    for name, function in inspect.getmembers(rpg_events, inspect.isfunction):
        if name.startswith("log_"):
            assert set(inspect.signature(function).parameters) <= allowed, name


def test_every_event_logs_only_whitelisted_fields(caplog):
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        rpg_events.log_turn_started(user_id=1, session_id=2, turn_number=3, attempt=1, start="new", tier="safe", model="m")
        rpg_events.log_turn_completed(
            user_id=1, session_id=2, turn_number=3, attempt=1, chars=500, latency_ms=4000, first_chunk_ms=900, model="m", finish_reason="stop",
            dropped=1, state_ok=True, world_updated=True, ended=False,
        )
        rpg_events.log_turn_blocked(user_id=1, session_id=2, attempt=1, categories=("minor_sexual", "marker_leak"), auto_retry=True, final=False)
        rpg_events.log_turn_interrupted(user_id=1, session_id=2, attempt=1, reason="disconnected", chars=10)
        rpg_events.log_turn_failed(user_id=1, session_id=2, attempt=1, reason="timeout", model=None)
        rpg_events.log_state_failed(user_id=1, session_id=2, turn_number=3, reason="validation")
        rpg_events.log_session_ended(user_id=1, session_id=2, turn_number=3, reason="age")
        rpg_events.log_opening_started(user_id=1, session_id=2, tier="standard", model="m")
        rpg_events.log_opening_ready(user_id=1, session_id=2, chars=300, latency_ms=3000, first_chunk_ms=None, model="m")
        rpg_events.log_opening_fallback(user_id=1, session_id=2, reason="blocked", categories=("minor_sexual",))
        rpg_events.log_opening_reset(user_id=1, session_id=2)
        rpg_events.log_opening_taken_over(user_id=1, session_id=2)
    records = [r for r in caplog.records if r.name == "app.rpg"]
    assert {r.event for r in records} == set(rpg_events.EVENT_NAMES)
    extra_keys = {"event", "user_id", "session_id", "turn_number", "attempt", "outcome", "reason", "model", "chars", "latency_ms",
                  "first_chunk_ms", "finish_reason", "dropped", "categories"}
    for record in records:
        emitted = {key for key in record.__dict__ if key in ALLOWED_FIELDS or key == "event"}
        assert emitted <= extra_keys | set(ALLOWED_FIELDS)
        assert all(not isinstance(getattr(record, key, None), (dict, list)) for key in extra_keys)


@pytest.mark.parametrize("call", [
    lambda: rpg_events.log_turn_started(user_id=1, session_id=2, turn_number=3, attempt=1, start="free text", tier="safe", model="m"),
    lambda: rpg_events.log_turn_started(user_id=1, session_id=2, turn_number=3, attempt=1, start="new", tier="自由文字", model="m"),
    lambda: rpg_events.log_turn_blocked(user_id=1, session_id=2, attempt=1, categories=("她說了什麼話",), auto_retry=True, final=False),
    lambda: rpg_events.log_turn_interrupted(user_id=1, session_id=2, attempt=1, reason="使用者離開了", chars=1),
    lambda: rpg_events.log_turn_failed(user_id=1, session_id=2, attempt=1, reason="錯誤原文", model=None),
    lambda: rpg_events.log_state_failed(user_id=1, session_id=2, turn_number=1, reason="x"),
    lambda: rpg_events.log_session_ended(user_id=1, session_id=2, turn_number=1, reason="x"),
    lambda: rpg_events.log_turn_completed(
        user_id=1, session_id=2, turn_number=1, attempt=1, chars="很多", latency_ms=1, first_chunk_ms=None, model=None, finish_reason=None,
        dropped=0, state_ok=True, world_updated=True, ended=False,
    ),
    lambda: rpg_events.log_opening_fallback(user_id=1, session_id=2, reason="其他"),
])
def test_values_outside_the_enumerations_are_refused(call):
    with pytest.raises(ValueError):
        call()


# ---- 完整流程的日誌掃描 -------------------------------------------------------------------------

def test_a_full_flow_leaves_no_player_text_or_identifier_in_the_logs(member_session, log_lines):
    kit = RunnerKit(member_session)
    user = kit.player()
    save = kit.pending_save(user)
    secrets = [
        ACTION, *STORY_A, *OPENING, "獨特錨定事實", "祕密山谷", "林曉", BAD_SENTENCE, "少女", MARKER, save.public_id, "scan-1", "scan-2", "scan-3",
        "turn:" + save.public_id,
    ]

    opening = kit.begin_opening(user, save)
    kit.provider.script_stream(*story_script(*OPENING))
    kit.run_opening(opening.plan)

    plan = kit.start(user, save, msg="scan-1", action=ACTION)                              # 完整的回合
    kit.provider.script_stream(*story_script(*STORY_A)).script(Step(STATE))
    kit.run(plan, user)

    plan = kit.start(user, save, msg="scan-2", action=ACTION)                              # 被擋兩次的回合
    kit.provider.script_stream(*story_script(BAD_SENTENCE)).script_stream(*story_script("路上。", BAD_SENTENCE))
    kit.run(plan, user)

    plan = kit.start(user, save, msg="scan-3", action=ACTION)                              # 中斷的回合
    kit.provider.script_stream(*story_script(*STORY_A))
    generator = kit.runner(plan, user).events()
    seen = []
    while not seen or seen[-1].name != "token":
        seen.append(next(generator))
    generator.close()

    raw = log_lines.raw()
    for secret in secrets:
        assert secret not in raw, f"日誌洩漏了：{secret}"
    lines = log_lines()
    events = {line["event"] for line in lines if "event" in line}
    assert {
        rpg_events.EVENT_TURN_STARTED, rpg_events.EVENT_TURN_COMPLETED, rpg_events.EVENT_TURN_BLOCKED, rpg_events.EVENT_TURN_INTERRUPTED,
        rpg_events.EVENT_OPENING_STARTED, rpg_events.EVENT_OPENING_READY,
    } <= events
    for line in lines:
        assert set(line) <= BASE_FIELDS | set(ALLOWED_FIELDS), set(line) - BASE_FIELDS - set(ALLOWED_FIELDS)
    blocked = [line for line in lines if line.get("event") == rpg_events.EVENT_TURN_BLOCKED]
    assert blocked and all(line["categories"] == "minor_sexual" for line in blocked)


def test_the_completed_event_carries_only_integers_and_enumerations(member_session, log_lines):
    kit = RunnerKit(member_session)
    user = kit.player()
    save = kit.open_save(user)
    plan = kit.start(user, save)
    kit.provider.script_stream(*story_script(*STORY_A)).script(Step(STATE))
    kit.run(plan, user)
    (line,) = [item for item in log_lines() if item.get("event") == rpg_events.EVENT_TURN_COMPLETED]
    assert line["user_id"] == user.id and line["session_id"] == save.id and line["turn_number"] == 1 and line["attempt"] == 1
    assert line["outcome"] == "complete" and line["reason"] == "state_ok" and isinstance(line["chars"], int) and isinstance(line["latency_ms"], int)
    assert isinstance(line["dropped"], int) and line["model"] == "rpg-primary-model"


# ---- 組裝層（registry）----------------------------------------------------------------------------

def test_the_game_service_is_built_from_the_rpg_model_the_rpg_marker_and_the_shared_budget():
    app = make_app({**BOTH}, rpg_llm_provider=FakeLLMProvider())
    secret = app.extensions["app_config"].secret_key.reveal()
    with request_scope(app):
        service = get_game_service()
        assert service._model == RPG_MODEL and service._marker == rpg_marker(secret)
        assert service._budget_exhausted() is False
        assert has_rpg_provider() is True


def test_without_an_rpg_provider_the_service_builds_but_the_runner_dependencies_do_not():
    app = make_app({"GEMINI_API_KEY": BOTH["GEMINI_API_KEY"]})
    with request_scope(app):
        assert get_game_service() is not None and has_rpg_provider() is False
        with pytest.raises(RuntimeError):
            build_runner_deps()


def test_the_runner_dependencies_are_captured_before_the_response_and_use_the_rpg_provider():
    rpg = FakeLLMProvider()
    app = make_app({**BOTH}, rpg_llm_provider=rpg)
    with request_scope(app):
        deps = build_runner_deps()
    assert deps.provider is rpg and deps.settings.primary_model == RPG_MODEL
    with deps.session_factory() as session:                                          # 請求結束之後仍可開新的工作階段
        wallet = deps.wallet_factory(session)
        llm = deps.llm_factory(session)
        assert wallet is not None and llm._provider is rpg


def test_the_runner_builders_use_the_same_marker_as_the_game_service():
    app = make_app({**BOTH}, rpg_llm_provider=FakeLLMProvider())
    with request_scope(app):
        service = get_game_service()
        runner = build_turn_runner(_StubPlan(), current=None)
        opening = build_opening_runner(_StubOpeningPlan())
        assert runner._marker == service._marker == opening._marker


class _Characters(tuple):
    pass


class _StubPlan:
    user_id = 1
    tier = "safe"
    characters = ()
    adult_age = 18


class _StubOpeningPlan:
    user_id = 1
    tier = "safe"
    characters = ()
    adult_age = 18
