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

def test_there_are_twenty_one_unique_events():
    names = [value for key, value in vars(rpg_events).items() if key.startswith("EVENT_") and isinstance(value, str)]
    assert len(names) == len(set(names)) == len(rpg_events.EVENT_NAMES) == 21          # 12 個回合與第 0 幕事件 + 6 個記事簿事件 + 3 個修訂事件


def test_no_logging_function_takes_a_free_text_parameter():
    allowed = {
        "user_id", "session_id", "turn_number", "attempt", "start", "tier", "model", "chars", "latency_ms", "first_chunk_ms", "finish_reason",
        "dropped", "state_ok", "world_updated", "ended", "categories", "auto_retry", "final", "reason",
        # 記事簿：全是整數（或 None）；沒有任何文字參數
        "boundary", "entries", "longest", "added", "total", "version", "over_entries", "over_chars", "calls",
        # 修訂：種類（列舉）、是否免費（布林）、預扣的幣數（整數）；沒有理由、分類、行動與劇情的參數
        "kind", "is_free", "cost",
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
        rpg_events.log_notebook_captured(
            user_id=1, session_id=2, boundary=10, entries=3, longest=48, added=130, total=630, version=2, over_entries=0, over_chars=0, calls=1,
            latency_ms=4000, model="m", categories=("other_concern",),
        )
        rpg_events.log_notebook_skipped(user_id=1, session_id=2, boundary=10, reason="full", total=2100)
        rpg_events.log_notebook_abandoned(
            user_id=1, session_id=2, boundary=10, reason="blocked", entries=3, total=630, calls=1, latency_ms=3000, model="m", categories=("minor_sexual",),
        )
        rpg_events.log_notebook_failed(user_id=1, session_id=2, boundary=10, reason="transient", calls=2, latency_ms=9000, model="m")
        rpg_events.log_notebook_saved(user_id=1, session_id=2, version=3, total=1800)
        rpg_events.log_notebook_save_rejected(user_id=1, session_id=2, reason="blocked", total=1800, categories=("minor_sexual",))
        rpg_events.log_revision_started(user_id=1, session_id=2, kind="regenerate", attempt=2, is_free=False, cost=30)
        rpg_events.log_revision_completed(user_id=1, session_id=2, kind="edit", attempt=3, chars=480, latency_ms=4200, world_updated=True, ended=False)
        rpg_events.log_revision_failed(user_id=1, session_id=2, kind="regenerate", attempt=2, reason="blocked")
    records = [r for r in caplog.records if r.name == "app.rpg"]
    assert {r.event for r in records} == set(rpg_events.EVENT_NAMES)
    extra_keys = {"event", "user_id", "session_id", "turn_number", "attempt", "outcome", "reason", "model", "chars", "latency_ms",
                  "first_chunk_ms", "finish_reason", "dropped", "categories",
                  "boundary", "entries", "longest", "added", "total", "version", "over_entries", "over_chars",
                  "kind", "coins"}
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
    # 記事簿：列舉以外的原因、不是整數的欄位（含布林與字串）、不在列舉內的類別都會立刻出錯
    lambda: rpg_events.log_notebook_skipped(user_id=1, session_id=2, boundary=10, reason="其他原因"),
    lambda: rpg_events.log_notebook_skipped(user_id=1, session_id=2, boundary=10, reason="transient"),
    lambda: rpg_events.log_notebook_abandoned(user_id=1, session_id=2, boundary=10, reason="transient", entries=1, total=1, calls=1, latency_ms=1, model=None),
    lambda: rpg_events.log_notebook_failed(user_id=1, session_id=2, boundary=10, reason="blocked_by_provider", calls=1, latency_ms=1, model=None),
    lambda: rpg_events.log_notebook_failed(user_id=1, session_id=2, boundary=10, reason="這個條目寫了什麼", calls=1, latency_ms=1, model=None),
    lambda: rpg_events.log_notebook_save_rejected(user_id=1, session_id=2, reason="其他"),
    lambda: rpg_events.log_notebook_save_rejected(user_id=1, session_id=2, reason="blocked", categories=("她說了什麼話",)),
    lambda: rpg_events.log_notebook_save_rejected(user_id=1, session_id=2, reason="too_long", total="很多"),
    lambda: rpg_events.log_notebook_saved(user_id=1, session_id=2, version=True, total=1),
    lambda: rpg_events.log_notebook_saved(user_id=1, session_id=2, version=1, total="一千八百字"),
    lambda: rpg_events.log_notebook_captured(
        user_id=1, session_id=2, boundary=10, entries="三條", longest=1, added=1, total=1, version=1, over_entries=0, over_chars=0, calls=1,
        latency_ms=1, model=None,
    ),
    lambda: rpg_events.log_notebook_captured(
        user_id=1, session_id=2, boundary=10.5, entries=1, longest=1, added=1, total=1, version=1, over_entries=0, over_chars=0, calls=1,
        latency_ms=1, model=None,
    ),
    lambda: rpg_events.log_notebook_captured(
        user_id=1, session_id=2, boundary=10, entries=1, longest=1, added=1, total=1, version=1, over_entries=0, over_chars=0, calls=1,
        latency_ms=1, model=None, categories=("自由文字",),
    ),
    # 修訂：種類與原因必須是列舉；數字必須是整數；布林必須是布林（理由或行動文字放不進任何欄位）
    lambda: rpg_events.log_revision_started(user_id=1, session_id=2, kind="想換個結局", attempt=1, is_free=True, cost=0),
    lambda: rpg_events.log_revision_started(user_id=1, session_id=2, kind="regenerate", attempt="第二次", is_free=True, cost=0),
    lambda: rpg_events.log_revision_started(user_id=1, session_id=2, kind="regenerate", attempt=1, is_free="免費", cost=0),
    lambda: rpg_events.log_revision_started(user_id=1, session_id=2, kind="regenerate", attempt=1, is_free=True, cost="三十幣"),
    lambda: rpg_events.log_revision_started(user_id=1, session_id=2, kind="regenerate", attempt=1, is_free=True, cost=True),
    lambda: rpg_events.log_revision_completed(user_id=1, session_id=2, kind="edit", attempt=1, chars="很多", latency_ms=1, world_updated=True, ended=False),
    lambda: rpg_events.log_revision_completed(user_id=1, session_id=2, kind="edit", attempt=1, chars=1, latency_ms=1, world_updated="是", ended=False),
    lambda: rpg_events.log_revision_completed(user_id=1, session_id=2, kind="edit", attempt=1, chars=1, latency_ms=1, world_updated=True, ended="結局"),
    lambda: rpg_events.log_revision_completed(user_id=1, session_id=2, kind="rewrite", attempt=1, chars=1, latency_ms=1, world_updated=True, ended=False),
    lambda: rpg_events.log_revision_failed(user_id=1, session_id=2, kind="regenerate", attempt=1, reason="這一輪的劇情寫了什麼"),
    lambda: rpg_events.log_revision_failed(user_id=1, session_id=2, kind="regenerate", attempt=1, reason="blocked_by_provider"),
    lambda: rpg_events.log_revision_failed(user_id=1, session_id=2, kind="unknown", attempt=1, reason="blocked"),
])
def test_values_outside_the_enumerations_are_refused(call):
    with pytest.raises(ValueError):
        call()


# ---- 記事簿事件 ---------------------------------------------------------------------------------

def _notebook_records(caplog):
    return {r.event: r for r in caplog.records if r.name == "app.rpg" and r.event.startswith("rpg.notebook_")}


def test_the_notebook_events_carry_only_ids_integers_and_enumerations(caplog):
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        rpg_events.log_notebook_captured(
            user_id=7, session_id=8, boundary=10, entries=8, longest=90, added=420, total=1419, version=4, over_entries=3, over_chars=40, calls=2,
            latency_ms=5200, model="rpg-primary-model",
        )
        rpg_events.log_notebook_skipped(user_id=7, session_id=8, boundary=20, reason="full", total=2100)
        rpg_events.log_notebook_abandoned(user_id=7, session_id=8, boundary=20, reason="too_long", entries=5, total=2600, calls=3, latency_ms=21000, model="m")
        rpg_events.log_notebook_failed(user_id=7, session_id=8, boundary=None, reason="unavailable", calls=0, latency_ms=3, model=None)
        rpg_events.log_notebook_saved(user_id=7, session_id=8, version=5, total=1800)
        rpg_events.log_notebook_save_rejected(user_id=7, session_id=8, reason="too_long", total=2240)
    records = _notebook_records(caplog)
    captured = records[rpg_events.EVENT_NOTEBOOK_CAPTURED]
    assert (captured.user_id, captured.session_id, captured.boundary, captured.entries, captured.longest) == (7, 8, 10, 8, 90)
    assert (captured.added, captured.total, captured.version, captured.over_entries, captured.over_chars) == (420, 1419, 4, 3, 40)
    assert (captured.attempt, captured.latency_ms, captured.model, captured.outcome) == (2, 5200, "rpg-primary-model", "captured")
    assert not hasattr(captured, "categories")                                       # 沒有類別時不帶這個欄位
    assert records[rpg_events.EVENT_NOTEBOOK_SKIPPED].reason == "full" and records[rpg_events.EVENT_NOTEBOOK_SKIPPED].total == 2100
    assert records[rpg_events.EVENT_NOTEBOOK_ABANDONED].reason == "too_long" and records[rpg_events.EVENT_NOTEBOOK_ABANDONED].attempt == 3
    assert records[rpg_events.EVENT_NOTEBOOK_FAILED].boundary is None and records[rpg_events.EVENT_NOTEBOOK_FAILED].reason == "unavailable"
    assert records[rpg_events.EVENT_NOTEBOOK_SAVED].version == 5 and records[rpg_events.EVENT_NOTEBOOK_SAVE_REJECTED].total == 2240
    for record in records.values():
        for key, value in record.__dict__.items():
            if key in {"event", "reason", "outcome", "model", "categories", "message", "name", "levelname", "pathname", "filename", "module",
                       "funcName", "msg", "processName", "threadName", "taskName", "asctime", "args", "exc_text", "stack_info"}:
                continue
            assert value is None or isinstance(value, (int, float, bool)), f"{record.event} 的 {key} 不是數字：{value!r}"


def test_blocked_categories_are_enumerations_never_the_blocked_text(caplog):
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        rpg_events.log_notebook_abandoned(
            user_id=1, session_id=2, boundary=10, reason="blocked", entries=2, total=300, calls=1, latency_ms=10, model=None,
            categories=("marker_leak", "minor_sexual"),
        )
        rpg_events.log_notebook_save_rejected(user_id=1, session_id=2, reason="blocked", categories=("real_person_suspected",))
    records = _notebook_records(caplog)
    assert records[rpg_events.EVENT_NOTEBOOK_ABANDONED].categories == "marker_leak,minor_sexual"            # 排序後以逗號相連
    assert records[rpg_events.EVENT_NOTEBOOK_SAVE_REJECTED].categories == "real_person_suspected"


def test_a_captured_event_can_note_a_passed_category(caplog):
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        rpg_events.log_notebook_captured(
            user_id=1, session_id=2, boundary=10, entries=1, longest=20, added=21, total=21, version=2, over_entries=0, over_chars=0, calls=1,
            latency_ms=5, model=None, categories=("other_concern",),
        )
    assert _notebook_records(caplog)[rpg_events.EVENT_NOTEBOOK_CAPTURED].categories == "other_concern"


def test_the_notebook_enumerations_match_the_design():
    assert rpg_events.NOTEBOOK_SKIP_REASONS == ("full", "empty", "budget")
    assert rpg_events.NOTEBOOK_ABANDON_REASONS == ("blocked", "too_long", "stale")
    assert set(rpg_events.NOTEBOOK_FAIL_REASONS) >= {"transient", "timeout", "config", "unavailable"}
    assert rpg_events.NOTEBOOK_REJECT_REASONS == ("too_long", "blocked", "conflict", "ended")


def test_the_notebook_logging_functions_take_no_text_parameter():
    """函式簽名本身就是守門：所有參數不是整數欄位、列舉、類別，就是模型名稱——沒有任何可以放進條目或記事簿文字的參數。"""
    integers = {"user_id", "session_id", "boundary", "entries", "longest", "added", "total", "version", "over_entries", "over_chars", "calls", "latency_ms"}
    enumerations = {"reason", "categories", "model"}
    for name, function in inspect.getmembers(rpg_events, inspect.isfunction):
        if name.startswith("log_notebook_"):
            assert set(inspect.signature(function).parameters) <= integers | enumerations, name


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


# ---- 修訂事件（rpg-turn-revision 的 design D10）-----------------------------------------------------

def test_the_revision_events_carry_only_ids_integers_and_enumerations(caplog):
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        rpg_events.log_revision_started(user_id=1, session_id=2, kind="regenerate", attempt=3, is_free=False, cost=30)
        rpg_events.log_revision_started(user_id=1, session_id=2, kind="regenerate", attempt=2, is_free=True, cost=0)
        rpg_events.log_revision_completed(user_id=1, session_id=2, kind="edit", attempt=4, chars=480, latency_ms=4200, world_updated=True, ended=True)
        rpg_events.log_revision_completed(user_id=1, session_id=2, kind="edit", attempt=5, chars=90, latency_ms=100, world_updated=False, ended=False)
        rpg_events.log_revision_failed(user_id=1, session_id=2, kind="regenerate", attempt=3, reason="state_failed")
    records = [r for r in caplog.records if r.name == "app.rpg"]
    assert [r.event for r in records] == [
        "rpg.revision_started", "rpg.revision_started", "rpg.revision_completed", "rpg.revision_completed", "rpg.revision_failed",
    ]
    paid, free, ended, plain, failed = records
    assert (paid.kind, paid.attempt, paid.coins, paid.outcome) == ("regenerate", 3, 30, "paid")
    assert (free.coins, free.outcome) == (0, "free")
    assert (ended.kind, ended.attempt, ended.chars, ended.latency_ms, ended.outcome, ended.reason) == ("edit", 4, 480, 4200, "ended", "world_updated")
    assert (plain.outcome, plain.reason) == ("complete", "world_unchanged")
    assert (failed.kind, failed.reason, failed.outcome, failed.levelno) == ("regenerate", "state_failed", "failed", logging.WARNING)
    for record in records:
        assert record.session_id == 2 and record.user_id == 1
        for name in ("kind", "attempt", "coins", "chars", "latency_ms", "outcome", "reason", "session_id", "user_id"):
            value = getattr(record, name, None)
            assert value is None or isinstance(value, (int, str)), (record.event, name)      # 沒有 dict、list 或任何自由文字結構


def test_the_revision_logging_functions_take_no_player_text_parameter():
    functions = {name: function for name, function in inspect.getmembers(rpg_events, inspect.isfunction) if name.startswith("log_revision_")}
    assert set(functions) == {"log_revision_started", "log_revision_completed", "log_revision_failed"}
    forbidden = {"reason_text", "category", "action", "text", "narration", "snapshot", "world", "message", "feedback", "content"}
    for name, function in functions.items():
        parameters = set(inspect.signature(function).parameters)
        assert not parameters & forbidden, name
        assert parameters <= {"user_id", "session_id", "kind", "attempt", "is_free", "cost", "chars", "latency_ms", "world_updated", "ended", "reason"}, name


def test_the_revision_enumerations_match_the_design():
    assert rpg_events.REVISION_FAIL_REASONS == ("blocked", "timeout", "model_failed", "state_failed", "superseded", "disconnected", "internal")
    assert {rpg_events.EVENT_REVISION_STARTED, rpg_events.EVENT_REVISION_COMPLETED, rpg_events.EVENT_REVISION_FAILED} == {
        "rpg.revision_started", "rpg.revision_completed", "rpg.revision_failed",
    }
    for reason in rpg_events.REVISION_FAIL_REASONS:                                             # 每個原因都能記錄（兩種種類）
        for kind in ("regenerate", "edit"):
            rpg_events.log_revision_failed(user_id=1, session_id=2, kind=kind, attempt=1, reason=reason)


def test_a_revision_event_never_carries_free_text(caplog):
    marker = "獨特理由字樣甲乙丙"
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        rpg_events.log_revision_started(user_id=1, session_id=2, kind="regenerate", attempt=2, is_free=False, cost=30)
        rpg_events.log_revision_completed(user_id=1, session_id=2, kind="regenerate", attempt=2, chars=300, latency_ms=900, world_updated=True, ended=False)
        rpg_events.log_revision_failed(user_id=1, session_id=2, kind="regenerate", attempt=2, reason="timeout")
        with pytest.raises(ValueError):
            rpg_events.log_revision_failed(user_id=1, session_id=2, kind="regenerate", attempt=2, reason=marker)       # 自由文字進不來
    lines = [json.dumps({k: str(v) for k, v in r.__dict__.items() if k in ALLOWED_FIELDS or k in ("event", "message")}, ensure_ascii=False) for r in caplog.records]
    assert lines and not any(marker in line for line in lines)
