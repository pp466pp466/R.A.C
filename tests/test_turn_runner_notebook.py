"""rpg-notebook-capture、rpg-turn-stream 規格：`TurnRunner` 的記事簿抓取旗標（`capture_target`）。

旗標只在「`done` 已被伺服器取走、回合 `complete`」之後成立：產生器停在 `done` 的 `yield` 時還沒有，客戶端在 `done` 寫入時斷線
（`GeneratorExit`）、被擋、中斷、結局、文字完整之後才斷線、從頭就是已完成回合的重送都不會成立。真正的抓取由 `NotebookCapturer` 負責，
這裡只驗證觸發條件與抓取對象（純資料）。以真的資料庫（SQLite 與 SQL Server）與假的供應商驗證。
"""
import json

import pytest

from app.llm.types import LLMTransientError
from app.services.rpg_notebook_capture import CaptureTarget
from tests.support.game import BAD_SENTENCE, RunnerKit, story_script
from tests.support.llm import Chunk, Fail, Step
from tests.support.script_data import validated

STATE_OK = json.dumps({"elapsed_months": 1, "current_location": "山門前"}, ensure_ascii=False)


@pytest.fixture()
def kit(member_session):
    return RunnerKit(member_session)


def setup(kit, content=None, **overrides):
    user = kit.player()
    save = kit.open_save(user, content=content, **overrides)
    plan = kit.start(user, save)
    return user, save, plan


def drain_until(generator, name):
    """取出事件直到（含）第一個叫 `name` 的事件；回傳取到的事件。產生器停在那個事件的 `yield`。"""
    seen = []
    for event in generator:
        seen.append(event)
        if event.name == name:
            return seen
    raise AssertionError(f"沒有出現 {name} 事件：{[event.name for event in seen]}")


# ---- 成立 ---------------------------------------------------------------------------------------

def test_the_flag_is_set_only_after_the_done_event_has_been_taken(kit):
    user, save, plan = setup(kit)
    kit.provider.script_stream(*story_script()).script(Step(STATE_OK))
    runner = kit.runner(plan, user)
    generator = runner.events()
    seen = drain_until(generator, "done")
    assert seen[-1].data["status"] == "complete" and runner.capture_target() is None      # 產生器還停在 done 的 yield
    assert list(generator) == []                                                          # 伺服器要下一個片段：產生器結束
    assert runner.capture_target() is not None


def test_the_target_is_plain_data_frozen_from_the_plan_and_the_current_member(kit):
    user, save, plan = setup(kit)
    kit.provider.script_stream(*story_script()).script(Step(STATE_OK))
    runner = kit.runner(plan, user)
    list(runner.events())
    target = runner.capture_target()
    assert target == CaptureTarget(
        user_id=user.id, session_id=save.id, session_public_id=save.public_id, member_tier=user.member_tier, content_tier=plan.tier,
        adult_age=plan.adult_age,
    )
    assert all(isinstance(value, (int, str)) for value in vars(target).values())               # 沒有 ORM 物件、工作階段或請求物件


def test_the_target_carries_the_members_tier_so_the_capacity_follows_the_membership(kit):
    user = kit.player(member_tier="vip")
    save = kit.open_save(user)
    plan = kit.start(user, save)
    kit.provider.script_stream(*story_script()).script(Step(STATE_OK))
    runner = kit.runner(plan, user)
    list(runner.events())
    assert runner.capture_target().member_tier == "vip"


def test_a_turn_whose_state_call_failed_still_triggers_the_capture(kit):
    user, save, plan = setup(kit)
    kit.provider.script_stream(*story_script()).script(Step("這不是 JSON"), Step("這也不是 JSON"))
    runner = kit.runner(plan, user)
    events = list(runner.events())
    assert events[-1].data["status"] == "complete"
    assert runner.capture_target() is not None


def test_the_events_are_unchanged_by_the_flag(kit):
    user, save, plan = setup(kit)
    kit.provider.script_stream(*story_script()).script(Step(STATE_OK))
    events = list(kit.runner(plan, user).events())
    names = [event.name for event in events]
    assert names[0] == "meta" and names[-2:] == ["state", "done"] and set(names) <= {"meta", "token", "ping", "state", "done"}


# ---- 不成立 -------------------------------------------------------------------------------------

def test_a_disconnect_while_writing_done_does_not_set_the_flag(kit):
    user, save, plan = setup(kit)
    kit.provider.script_stream(*story_script()).script(Step(STATE_OK))
    runner = kit.runner(plan, user)
    generator = runner.events()
    drain_until(generator, "done")
    generator.close()                                                     # done 寫入失敗：WSGI 伺服器關閉回應，產生器收到 GeneratorExit
    assert runner.capture_target() is None
    (turn,) = kit.turns(save)
    assert turn.turn_status == "complete"                                 # 回合本身已完成並結算，只是這次不抓取，由下個回合補做


def test_a_disconnect_after_the_narration_completed_does_not_set_the_flag(kit):
    user, save, plan = setup(kit)
    kit.provider.script_stream(*story_script()).script(Step(STATE_OK))
    runner = kit.runner(plan, user)
    generator = runner.events()
    drain_until(generator, "ping")                                        # 文字已完整、正要開始狀態呼叫
    generator.close()
    assert runner.capture_target() is None
    (turn,) = kit.turns(save)
    assert turn.turn_status == "complete"                                 # 收尾照常完成（斷線後仍結算）


def test_a_disconnect_during_the_stream_does_not_set_the_flag(kit):
    user, save, plan = setup(kit)
    kit.provider.script_stream(*story_script())
    runner = kit.runner(plan, user)
    generator = runner.events()
    assert next(generator).name == "meta"
    generator.close()
    assert runner.capture_target() is None


def test_a_blocked_turn_does_not_set_the_flag(kit):
    user, save, plan = setup(kit)
    kit.provider.script_stream(*story_script(BAD_SENTENCE)).script_stream(*story_script(BAD_SENTENCE))
    runner = kit.runner(plan, user)
    events = list(runner.events())
    assert events[-1].name == "done" and events[-1].data["status"] == "blocked"
    assert runner.capture_target() is None


def test_a_blocked_first_attempt_that_regenerates_normally_sets_the_flag(kit):
    user, save, plan = setup(kit)
    kit.provider.script_stream(*story_script(BAD_SENTENCE)).script_stream(*story_script()).script(Step(STATE_OK))
    runner = kit.runner(plan, user)
    events = list(runner.events())
    assert events[-1].data["status"] == "complete"
    assert runner.capture_target() is not None


def test_a_model_failure_does_not_set_the_flag(kit):
    user, save, plan = setup(kit)
    kit.provider.script_stream(Chunk("清晨的海風吹過山門。"), Fail(LLMTransientError("network")))
    runner = kit.runner(plan, user)
    events = list(runner.events())
    assert events[-1].name == "error"
    assert runner.capture_target() is None


def test_an_ending_turn_does_not_set_the_flag(kit):
    content = validated(ending_mode="custom", ending_text="主角在山中安然離世。").content
    user, save, plan = setup(kit, content=content)
    kit.provider.script_stream(*story_script()).script(Step(json.dumps({"ending_proposed": True})))
    runner = kit.runner(plan, user)
    events = list(runner.events())
    assert events[-1].data["status"] == "ended"
    assert runner.capture_target() is None


def test_an_unexpected_error_does_not_set_the_flag(kit, monkeypatch):
    user, save, plan = setup(kit)
    kit.provider.script_stream(*story_script()).script(Step(STATE_OK))
    runner = kit.runner(plan, user)

    def broken():
        raise RuntimeError("非預期的錯誤")

    monkeypatch.setattr(runner, "_finalize", broken)
    events = list(runner.events())
    assert events[-1].name == "error"
    assert runner.capture_target() is None


def test_a_runner_that_has_not_started_has_no_target(kit):
    user, save, plan = setup(kit)
    assert kit.runner(plan, user).capture_target() is None
