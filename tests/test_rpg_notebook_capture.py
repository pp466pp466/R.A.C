"""rpg-notebook-capture 規格：`NotebookCapturer`（每完成 10 輪抓取重點並追加到記事簿）。

真的 Repository、真的帳本與假的模型供應商（`FakeLLMProvider`）；一般測試以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行。
回合以 Repository 直接建立（`complete`），不經串流與錢包——抓取本來就不碰它們。競爭情境（重複觸發、抓取期間的玩家編輯）用供應商的
`hooks` 在「模型呼叫的瞬間」讓另一個工作階段先完成寫入，所以不需要真的執行緒，卻和真實的交錯順序一致。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import pytest
from sqlalchemy import func, select

from app.llm.types import LLMTransientError, TRANSIENT_RATE_LIMITED
from app.models.llm_call_log import KIND_RPG_NOTEBOOK, STATUS_OK
from app.models.wallet import WalletEntry
from app.observability import rpg_events
from app.repositories.session_repository import SessionRepository
from app.services import notebook_policy as policy
from app.services import rpg_notebook_prompt as prompts
from app.services import rpg_prompt
from app.services.rpg_notebook_capture import (
    OUTCOME_ABANDONED, OUTCOME_CAPTURED, OUTCOME_FAILED, OUTCOME_NOTHING, OUTCOME_SKIPPED, OUTCOME_SUPERSEDED, CaptureResult, CaptureTarget,
    NotebookCapturer,
)
from app.services.rpg_runner_base import RunnerDeps
from tests.support.game import BAD_SENTENCE, FALLBACK_MODEL, MARKER, MODEL, RunnerKit
from tests.support.llm import Step


def reply(*entries: str) -> str:
    return json.dumps({"entries": list(entries)}, ensure_ascii=False)


@dataclass
class World:
    kit: RunnerKit
    user: object
    save: object
    llm_sessions: list

    def target(self, *, member_tier: str = "free", content_tier: str = "safe", **overrides) -> CaptureTarget:
        values = dict(
            user_id=self.user.id, session_id=self.save.id, session_public_id=self.save.public_id, member_tier=member_tier,
            content_tier=content_tier, adult_age=18,
        )
        values.update(overrides)
        return CaptureTarget(**values)

    def capturer(self, **overrides) -> NotebookCapturer:
        kit = self.kit
        values = dict(
            session_factory=kit.factory, wallet_factory=kit.wallet_for, llm_factory=self._llm_for, provider=kit.provider, settings=kit.settings,
            clock=kit.clock, monotonic=kit.monotonic,
        )
        values.update(overrides)
        return NotebookCapturer(deps=RunnerDeps(**values), marker=MARKER, budget_exhausted=lambda _session: kit.budget_exhausted)

    def _llm_for(self, session):
        self.llm_sessions.append(session)
        return self.kit.llm_for(session)

    def run(self, **target_overrides) -> CaptureResult:
        return self.capturer().run(self.target(**target_overrides))

    # ---- 回合與記事簿 ----------------------------------------------------------------------------

    def complete(self, count: int, first: int | None = None) -> None:
        """依序完成 `count` 個回合（輪數接續目前已完成的）：行動「行動N」、劇情「劇情N」。"""
        repo = self.kit.sessions
        completed = self.kit.sessions.count_complete_turns(self.user.id, self.save.public_id) or 0
        for index in range(first or completed + 1, (first or completed + 1) + count):
            repo.start_turn(self.user.id, self.save.public_id, client_msg_id=f"t-{index}", user_action=f"行動{index}", now=self.kit.clock())
            repo.complete_turn(
                self.user.id, self.save.public_id, f"t-{index}", llm_raw_response=f"劇情{index}", structured_result=None, now=self.kit.clock(),
            )
        self.kit.session.commit()

    def set_notebook(self, text: str) -> None:
        """玩家（或測試）直接把記事簿設為 `text`（Repository 層不檢查容量）。"""
        repo = self.kit.sessions
        repo.save_notebook(self.user.id, self.save.public_id, text=text, expected_version=self.state()[1])
        self.kit.session.commit()

    def state(self) -> tuple[str, int, int]:
        """（記事簿文字, 版本, 已處理到的輪次），每次都讀最新的資料。"""
        self.kit.session.expire_all()
        values = (self.save.notebook_text, self.save.notebook_version, self.save.notebook_upto_turn)
        self.kit.session.commit()
        return values

    def edit_elsewhere(self, text: str) -> None:
        """另一個工作階段（另一個請求）把記事簿改成 `text` 並提交——玩家在抓取期間儲存。"""
        with self.kit.factory() as other:
            current = SessionRepository(other).get_owned(self.user.id, self.save.public_id).notebook_version
            SessionRepository(other).save_notebook(self.user.id, self.save.public_id, text=text, expected_version=current)
            other.commit()

    def prompt_text(self, call: int = 0) -> str:
        return "".join(part for content in self.kit.provider.calls[call].contents for part in content.parts)


@pytest.fixture()
def world(member_session) -> World:
    kit = RunnerKit(member_session)
    user = kit.player()
    save = kit.open_save(user)
    member_session.commit()
    return World(kit, user, save, [])


def events(caplog) -> dict[str, logging.LogRecord]:
    return {r.event: r for r in caplog.records if r.name == "app.rpg" and getattr(r, "event", "").startswith("rpg.notebook_")}


# ---- 觸發與視窗 ---------------------------------------------------------------------------------

def test_the_tenth_turn_does_not_trigger_and_the_eleventh_captures_turns_one_to_ten(world):
    world.complete(10)
    assert world.run() == CaptureResult(OUTCOME_NOTHING) and world.kit.provider.calls == []
    world.complete(1)
    world.kit.provider.script(Step(reply("艾拉是主角的青梅竹馬。")))
    assert world.run() == CaptureResult(OUTCOME_CAPTURED)
    text = world.prompt_text()
    assert all(f"第 {number} 輪" in text for number in range(1, 11)) and "第 11 輪" not in text
    assert world.state() == ("艾拉是主角的青梅竹馬。", 2, 10)


def test_there_is_nothing_to_do_before_any_window_exists_or_when_it_is_already_processed(world):
    assert world.run() == CaptureResult(OUTCOME_NOTHING)                               # 一輪都還沒有
    world.complete(11)
    world.kit.provider.script(Step(reply("設定甲。")))
    world.run()
    assert world.run() == CaptureResult(OUTCOME_NOTHING)                               # 標記已到 10，邊界 10 不大於標記
    assert len(world.kit.provider.calls) == 1


def test_interrupted_turns_do_not_count_toward_the_boundary(world):
    repo = world.kit.sessions
    world.complete(10)
    for index in range(3):                                                               # 3 個中斷的回合夾在中間
        repo.start_turn(world.user.id, world.save.public_id, client_msg_id=f"x-{index}", user_action="行動", now=world.kit.clock())
        repo.interrupt_turn(world.user.id, world.save.public_id, f"x-{index}", partial_response="", attempt=1)
    world.kit.session.commit()
    assert world.run() == CaptureResult(OUTCOME_NOTHING)                               # 完成的仍是 10 輪


def test_an_ended_save_and_someone_elses_save_are_left_alone(world):
    world.complete(11)
    world.kit.sessions.end_session(world.user.id, world.save.public_id, now=world.kit.clock())
    world.kit.session.commit()
    assert world.run() == CaptureResult(OUTCOME_NOTHING)
    other = world.kit.player(2)
    assert world.run(user_id=other.id) == CaptureResult(OUTCOME_NOTHING)
    assert world.kit.provider.calls == [] and world.state()[2] == 0


# ---- 只追加、容量與預備空間 -------------------------------------------------------------------------

def test_entries_are_appended_after_the_existing_content_which_is_never_rewritten(world):
    world.set_notebook("既有設定甲\n既有設定乙")
    world.complete(11)
    world.kit.provider.script(Step(reply("新設定一。", "新設定二。", "新設定三。")))
    assert world.run().outcome == OUTCOME_CAPTURED
    assert world.state() == ("既有設定甲\n既有設定乙\n新設定一。\n新設定二。\n新設定三。", 3, 10)


def test_entries_that_exceed_the_remaining_room_are_written_in_full_using_the_reserve(world):
    existing = "舊" * 1999
    entry = "新" * 399
    world.set_notebook(existing)
    world.complete(11)
    world.kit.provider.script(Step(reply(entry)))
    assert world.run().outcome == OUTCOME_CAPTURED
    text, version, upto = world.state()
    assert text == existing + "\n" + entry and policy.length_of(text) == 2399 and policy.is_full(len(text), "free") and (version, upto) == (3, 10)


def test_exactly_at_capacity_is_still_normal_and_the_reserve_can_be_used(world):
    world.set_notebook("舊" * 2000)
    world.complete(11)
    world.kit.provider.script(Step(reply("追加的設定。")))
    assert world.run().outcome == OUTCOME_CAPTURED
    assert world.state()[0] == "舊" * 2000 + "\n追加的設定。"


def test_over_the_limit_asks_the_model_to_condense_once_and_writes_the_condensed_entries(world):
    existing = "舊" * 1990
    world.set_notebook(existing)
    world.complete(11)
    long_entries = ("甲" * 200, "乙" * 200, "丙" * 200)                               # 1990 + 1 + 602 = 2593 > 2500
    world.kit.provider.script(Step(reply(*long_entries)), Step(reply("精" * 100)))
    assert world.run().outcome == OUTCOME_CAPTURED
    assert len(world.kit.provider.calls) == 2
    second = world.prompt_text(1)
    assert prompts.LABEL_PREVIOUS in second and prompts.LABEL_ROOM in second and "甲" * 200 in second
    assert f"{policy.room_for(existing, 'free')} 字" in second                          # 可追加的上限 = 2500 − 1990 − 1 = 509
    assert world.state() == (existing + "\n" + "精" * 100, 3, 10)


def test_still_over_the_limit_after_condensing_abandons_and_advances_the_marker(world, caplog):
    world.set_notebook("舊" * 1990)
    world.complete(11)
    world.kit.provider.script(Step(reply("甲" * 400, "乙" * 400)), Step(reply("丙" * 300, "丁" * 300)))
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        result = world.run()
    assert result == CaptureResult(OUTCOME_ABANDONED, "too_long") and len(world.kit.provider.calls) == 2
    assert world.state() == ("舊" * 1990, 2, 10)                                         # 記事簿不變（版本是玩家那次儲存的 2），標記推進
    record = events(caplog)[rpg_events.EVENT_NOTEBOOK_ABANDONED]
    assert record.reason == "too_long" and record.attempt == 2 and record.total > policy.ai_limit_for("free")


def test_a_larger_tier_has_a_larger_capacity_and_reserve(world):
    existing = "舊" * 4900                                                               # plus：容量 5000，上限 6250
    world.set_notebook(existing)
    world.complete(11)
    world.kit.provider.script(Step(reply("新" * 1000)))
    assert world.run(member_tier="plus").outcome == OUTCOME_CAPTURED
    assert policy.length_of(world.state()[0]) == 4900 + 1 + 1000


def test_a_full_notebook_skips_without_calling_the_model_and_does_not_backfill(world):
    world.set_notebook("舊" * 2100)
    world.complete(11)
    assert world.run() == CaptureResult(OUTCOME_SKIPPED, "full")
    assert world.kit.provider.calls == [] and world.state() == ("舊" * 2100, 2, 10)
    world.set_notebook("舊" * 2000)                                                      # 玩家刪到容量以內
    world.complete(10)                                                                   # 完成到第 21 輪
    world.kit.provider.script(Step(reply("恢復後的設定。")))
    assert world.run().outcome == OUTCOME_CAPTURED
    text = world.prompt_text()
    assert all(f"第 {number} 輪" in text for number in range(11, 21)) and "第 10 輪" not in text      # 只抓最新的 10 輪，被跳過的沒有補抓
    assert world.state()[2] == 20


def test_a_downgraded_member_over_the_new_capacity_counts_as_full(world):
    world.set_notebook("舊" * 2300)                                                      # 曾是 plus，降級為 free 後超過新容量
    world.complete(11)
    assert world.run(member_tier="free") == CaptureResult(OUTCOME_SKIPPED, "full") and world.kit.provider.calls == []


# ---- 沒有新內容與去重 ---------------------------------------------------------------------------------

def test_no_entries_is_not_a_failure_and_advances_the_marker(world, caplog):
    world.complete(11)
    world.kit.provider.script(Step(reply()))
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        assert world.run() == CaptureResult(OUTCOME_SKIPPED, "empty")
    assert world.state() == ("", 1, 10)
    assert events(caplog)[rpg_events.EVENT_NOTEBOOK_SKIPPED].reason == "empty" and rpg_events.EVENT_NOTEBOOK_FAILED not in events(caplog)


def test_entries_already_in_the_notebook_or_repeated_are_not_written_twice(world):
    world.set_notebook("艾拉是主角的青梅竹馬。")
    world.complete(11)
    world.kit.provider.script(Step(reply("艾拉是主角的青梅竹馬。", "  新設定。  ", "新設定。", "")))
    assert world.run().outcome == OUTCOME_CAPTURED
    assert world.state()[0] == "艾拉是主角的青梅竹馬。\n新設定。"


def test_when_every_entry_is_a_duplicate_it_counts_as_having_nothing_new(world):
    world.set_notebook("既有設定。")
    world.complete(11)
    world.kit.provider.script(Step(reply("既有設定。")))
    assert world.run() == CaptureResult(OUTCOME_SKIPPED, "empty") and world.state()[1:] == (2, 10)


def test_entries_beyond_what_the_prompt_asks_for_are_written_in_full_and_only_counted(world, caplog):
    world.complete(11)
    entries = [f"設定{index}：" + "字" * 10 for index in range(8)]
    entries[3] = "長" * 90
    world.kit.provider.script(Step(reply(*entries)))
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        assert world.run().outcome == OUTCOME_CAPTURED
    assert world.state()[0].split("\n") == entries                                       # 8 條都在，沒有丟棄與截斷
    record = events(caplog)[rpg_events.EVENT_NOTEBOOK_CAPTURED]
    assert (record.entries, record.longest, record.over_entries, record.over_chars) == (8, 90, 3, 40)


# ---- 規則層與洩漏 -------------------------------------------------------------------------------------

def test_one_entry_failing_the_rules_layer_abandons_the_whole_capture(world, caplog):
    world.complete(11)
    world.kit.provider.script(Step(reply("艾拉是主角的青梅竹馬。", BAD_SENTENCE)))
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        assert world.run() == CaptureResult(OUTCOME_ABANDONED, "blocked")
    assert world.state() == ("", 1, 10)                                                  # 好的那一條也沒有寫入；標記推進
    record = events(caplog)[rpg_events.EVENT_NOTEBOOK_ABANDONED]
    assert record.reason == "blocked" and record.categories == "minor_sexual"


def test_the_age_records_of_the_characters_in_the_save_reach_the_rules_layer(member_session):
    kit = RunnerKit(member_session)
    user = kit.player()
    save = kit.open_save(user, name="小雨", age=15)
    member_session.commit()
    world = World(kit, user, save, [])
    world.complete(11)
    kit.provider.script(Step(reply("小雨與阿明做愛。")))                                  # 沒有明確的未成年用語，只有主角的年齡紀錄 15 歲
    assert world.run(content_tier="adult") == CaptureResult(OUTCOME_ABANDONED, "blocked")


def test_the_content_tier_of_the_turn_decides_what_the_rules_layer_lets_through(world):
    world.complete(11)
    explicit = "兩個成年人做愛。"
    world.kit.provider.script(Step(reply(explicit)))
    assert world.run(content_tier="safe") == CaptureResult(OUTCOME_ABANDONED, "blocked")      # 安全模式：任何性內容都不寫進記事簿
    world.complete(10)
    world.kit.provider.script(Step(reply("成年的兩人在一起了。")))
    assert world.run(content_tier="safe").outcome == OUTCOME_CAPTURED                          # 乾淨的條目照常


def test_an_entry_leaking_an_internal_setting_name_is_abandoned(world, caplog):
    world.complete(11)
    world.kit.provider.script(Step(reply("艾拉目前處於 cracking 階段。")))
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        assert world.run() == CaptureResult(OUTCOME_ABANDONED, "blocked")
    assert events(caplog)[rpg_events.EVENT_NOTEBOOK_ABANDONED].categories == "setting_leak" and world.state() == ("", 1, 10)


def test_a_reply_containing_the_marker_is_a_validation_failure_that_writes_nothing_and_retries_later(world, caplog):
    world.complete(11)
    leaking = reply(f"內部識別碼是 {MARKER}。")
    world.kit.provider.script(Step(leaking), Step(leaking))                              # 驗證失敗以同一模型重試一次，仍失敗
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        assert world.run() == CaptureResult(OUTCOME_FAILED, "validation")
    assert len(world.kit.provider.calls) == 2 and world.state() == ("", 1, 0)            # 不寫入、不推進
    assert events(caplog)[rpg_events.EVENT_NOTEBOOK_FAILED].reason == "validation"
    world.kit.provider.script(Step(reply("補做成功的設定。")))
    assert world.run().outcome == OUTCOME_CAPTURED and world.state() == ("補做成功的設定。", 2, 10)


# ---- 失敗與補做 -------------------------------------------------------------------------------------

def test_a_transient_failure_does_not_advance_and_the_next_run_redoes_the_same_window(world, caplog):
    world.complete(11)
    limited = LLMTransientError(TRANSIENT_RATE_LIMITED)
    world.kit.provider.script(Step(limited), Step(LLMTransientError(TRANSIENT_RATE_LIMITED)))      # 主要模型失敗，切換備援也失敗
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        assert world.run() == CaptureResult(OUTCOME_FAILED, "transient")
    assert world.state() == ("", 1, 0) and events(caplog)[rpg_events.EVENT_NOTEBOOK_FAILED].attempt == 2
    world.kit.provider.script(Step(reply("補做的設定。")))
    assert world.run().outcome == OUTCOME_CAPTURED
    assert "第 1 輪" in world.prompt_text(-1) and "第 10 輪" in world.prompt_text(-1) and world.state() == ("補做的設定。", 2, 10)


def test_when_the_next_boundary_is_crossed_before_the_redo_only_the_latest_window_is_captured(world):
    world.complete(11)
    world.kit.provider.script(Step(LLMTransientError(TRANSIENT_RATE_LIMITED)), Step(LLMTransientError(TRANSIENT_RATE_LIMITED)))
    assert world.run().outcome == OUTCOME_FAILED
    world.complete(10)                                                                   # 一路玩到第 21 輪
    world.kit.provider.script(Step(reply("第二個視窗的設定。")))
    assert world.run().outcome == OUTCOME_CAPTURED
    text = world.prompt_text(-1)
    assert all(f"第 {number} 輪" in text for number in range(11, 21)) and "第 10 輪" not in text and "第 1 輪" not in text
    assert world.state() == ("第二個視窗的設定。", 2, 20)


def test_a_configuration_error_and_a_missing_provider_do_not_advance(world):
    from app.llm.types import LLMConfigError

    world.complete(11)
    world.kit.provider.script(Step(LLMConfigError(http_status=400)))
    assert world.run() == CaptureResult(OUTCOME_FAILED, "config") and world.state()[2] == 0
    no_provider = world.capturer(llm_factory=None)
    assert no_provider.run(world.target()) == CaptureResult(OUTCOME_FAILED, "unavailable") and world.state()[2] == 0


def test_a_provider_block_does_not_advance_either(world):
    from app.llm.types import BLOCKED_OUTPUT, LLMBlockedError

    world.complete(11)
    world.kit.provider.script(Step(LLMBlockedError(BLOCKED_OUTPUT)))
    assert world.run() == CaptureResult(OUTCOME_FAILED, "blocked") and world.state()[2] == 0


def test_when_the_global_budget_is_used_up_there_is_no_call_and_the_marker_stays(world, caplog):
    world.complete(11)
    world.kit.budget_exhausted = True
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        assert world.run() == CaptureResult(OUTCOME_SKIPPED, "budget")
    assert world.kit.provider.calls == [] and world.state()[2] == 0 and events(caplog)[rpg_events.EVENT_NOTEBOOK_SKIPPED].reason == "budget"
    world.kit.budget_exhausted = False
    world.kit.provider.script(Step(reply("預算恢復後的設定。")))
    assert world.run().outcome == OUTCOME_CAPTURED


def test_a_run_that_exhausts_the_time_budget_is_a_timeout_failure_that_does_not_advance(world, caplog):
    world.complete(11)
    world.kit.provider.hooks.append(lambda _request: world.kit.monotonic.advance(29))       # 第一次呼叫就吃掉單次預算（30 秒）的大半
    world.kit.provider.script(Step("這不是 JSON", seconds=1.0))                             # 需要重試，但預算已用盡
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        assert world.run() == CaptureResult(OUTCOME_FAILED, "timeout")
    assert len(world.kit.provider.calls) == 1 and world.state()[2] == 0
    assert events(caplog)[rpg_events.EVENT_NOTEBOOK_FAILED].reason == "timeout"


def test_the_second_call_budget_is_the_remaining_time_and_condensing_is_not_started_with_under_twenty_seconds_left(world):
    world.set_notebook("舊" * 1990)
    world.complete(11)

    def burn(_request):
        if len(world.kit.provider.calls) == 1:
            world.kit.monotonic.advance(45)                                              # 第一輪結束時已用掉約 45 秒

    world.kit.provider.hooks.append(burn)
    world.kit.provider.script(Step(reply("甲" * 400, "乙" * 400), seconds=1.0))
    result = world.run()
    assert result == CaptureResult(OUTCOME_ABANDONED, "too_long") and len(world.kit.provider.calls) == 1       # 剩餘 < 20 秒：不精簡
    assert world.state()[2] == 10


def test_the_condense_call_is_capped_by_the_remaining_total_budget(world):
    from dataclasses import replace

    world.kit.settings = replace(world.kit.settings, call_timeout_seconds=50.0)             # 單次逾時放寬，才看得出剩餘預算才是上限
    world.set_notebook("舊" * 1990)
    world.complete(11)
    world.kit.provider.hooks.append(lambda _request: world.kit.monotonic.advance(35) if len(world.kit.provider.calls) == 1 else None)
    world.kit.provider.script(Step(reply("甲" * 400, "乙" * 400), seconds=1.0), Step(reply("精" * 50), seconds=1.0))
    assert world.run().outcome == OUTCOME_CAPTURED
    first, second = world.kit.provider.calls
    assert first.timeout_seconds == pytest.approx(30.0)                                      # min(30, 60)
    assert second.timeout_seconds == pytest.approx(60.0 - 36.0)                              # 已用 36 秒，剩 24 秒（≥ 20 才啟動精簡）


def test_an_unexpected_error_never_escapes_and_leaves_the_marker_alone(world, log_lines):
    world.complete(11)

    def broken():
        raise RuntimeError("資料庫連線掛了：獨特的錯誤內文")

    capturer = world.capturer(session_factory=broken)
    assert capturer.run(world.target()) == CaptureResult(OUTCOME_FAILED, "unavailable")
    assert "獨特的錯誤內文" not in log_lines.raw() and world.state()[2] == 0


# ---- 並行與鎖 -----------------------------------------------------------------------------------------

def test_a_duplicate_trigger_during_the_call_appends_only_once(world):
    world.complete(11)
    state = {"nested": False}
    second = world.capturer()

    def trigger(_request):
        if not state["nested"]:
            state["nested"] = True
            assert second.run(world.target()).outcome == OUTCOME_CAPTURED                # 另一個回合的抓取在這個抓取的呼叫期間先完成

    world.kit.provider.hooks.append(trigger)
    world.kit.provider.script(Step(reply("內層先寫入的設定。")), Step(reply("外層晚到的設定。")))
    assert world.run() == CaptureResult(OUTCOME_SUPERSEDED)
    assert world.state() == ("內層先寫入的設定。", 2, 10)                                  # 只有一次追加生效，沒有重複條目


def test_the_append_lands_on_the_text_the_player_saved_during_the_call(world):
    world.complete(11)
    world.kit.provider.hooks.append(lambda _request: world.edit_elsewhere("玩家剛剛加的設定。"))
    world.kit.provider.script(Step(reply("抓取的設定。")))
    assert world.run().outcome == OUTCOME_CAPTURED
    assert world.state() == ("玩家剛剛加的設定。\n抓取的設定。", 3, 10)                      # 玩家的編輯還在，AI 追加在最新文字之後


def test_a_player_edit_that_filled_the_notebook_during_the_call_counts_as_full(world):
    world.complete(11)
    world.kit.provider.hooks.append(lambda _request: world.edit_elsewhere("滿" * 2100))
    world.kit.provider.script(Step(reply("抓取的設定。")))
    assert world.run() == CaptureResult(OUTCOME_SKIPPED, "full")
    assert world.state() == ("滿" * 2100, 2, 10)


def test_a_player_edit_that_leaves_no_room_for_the_new_entries_abandons_without_another_model_call(world, caplog):
    world.complete(11)
    world.kit.provider.hooks.append(lambda _request: world.edit_elsewhere("舊" * 1900))
    world.kit.provider.script(Step(reply("新" * 700)))                                   # 1900 + 1 + 700 = 2601 > 2500
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        assert world.run() == CaptureResult(OUTCOME_ABANDONED, "stale")
    assert len(world.kit.provider.calls) == 1 and world.state() == ("舊" * 1900, 2, 10)
    assert events(caplog)[rpg_events.EVENT_NOTEBOOK_ABANDONED].reason == "stale"


# ---- 帳本、錢包與資料區塊 -----------------------------------------------------------------------------

def test_the_ledger_has_a_rpg_notebook_record_with_the_model_and_no_text(world):
    world.complete(11)
    world.kit.provider.script(Step(reply("帳本測試的設定。")))
    world.run()
    (row,) = world.kit.ledger(KIND_RPG_NOTEBOOK)
    assert (row.kind, row.model, row.status, row.user_id) == (KIND_RPG_NOTEBOOK, MODEL, STATUS_OK, world.user.id) and row.conversation_id is None
    assert row.error_detail is None and (row.prompt_tokens, row.completion_tokens) == (100, 50)
    assert row.counted is False                                                          # 不是客服的對話輪數


def test_the_fallback_model_used_is_what_the_ledger_records(world):
    world.complete(11)
    world.kit.provider.script(Step(LLMTransientError(TRANSIENT_RATE_LIMITED)), Step(reply("備援寫的設定。")))
    assert world.run().outcome == OUTCOME_CAPTURED
    assert [row.model for row in world.kit.ledger(KIND_RPG_NOTEBOOK)] == [MODEL, FALLBACK_MODEL]


def test_the_wallet_is_never_touched(world, member_session):
    before = (world.kit.balances(world.user), member_session.scalar(select(func.count()).select_from(WalletEntry)))
    world.complete(11)
    world.kit.provider.script(Step(reply("不收費的設定。")))
    world.run()
    member_session.expire_all()
    assert (world.kit.balances(world.user), member_session.scalar(select(func.count()).select_from(WalletEntry))) == before


def test_player_text_stays_in_the_data_blocks_and_never_in_the_system_instruction(world):
    world.set_notebook("玩家寫的獨特記事簿內容")
    world.complete(11)
    world.kit.provider.script(Step(reply("設定甲。")))
    world.run(content_tier="standard")
    request = world.kit.provider.calls[0]
    for secret in ("玩家寫的獨特記事簿內容", "行動3", "劇情7"):
        assert secret not in request.system_instruction and secret in world.prompt_text()
    assert MARKER in request.system_instruction and rpg_prompt.TIER_BOUNDARIES["standard"] in request.system_instruction


def test_no_database_transaction_is_open_while_the_model_is_called(world):
    world.complete(11)
    seen = []
    world.kit.provider.hooks.append(lambda _request: seen.append([world.kit.session.in_transaction(), *[s.in_transaction() for s in world.llm_sessions]]))
    world.kit.provider.script(Step(reply("設定甲。")))
    target, capturer = world.target(), world.capturer()
    world.kit.session.commit()                                                           # 測試自己讀取屬性所開的交易不算
    capturer.run(target)
    assert seen == [[False, False]]


def test_the_logs_contain_no_notebook_entry_action_story_or_marker(world, log_lines):
    world.set_notebook("玩家寫的獨特記事簿內容")
    world.complete(11)
    world.kit.provider.script(Step(reply("獨特條目甲。", "獨特條目乙。")))
    world.run()
    world.complete(10)
    world.kit.provider.script(Step(reply(BAD_SENTENCE)))                                  # 一個被規則層放棄的
    world.run()
    world.complete(10)
    world.kit.provider.script(Step(LLMTransientError(TRANSIENT_RATE_LIMITED)), Step(LLMTransientError(TRANSIENT_RATE_LIMITED)))
    world.run()
    raw = log_lines.raw()
    for secret in ("獨特條目甲", "獨特條目乙", "玩家寫的獨特記事簿內容", "行動5", "劇情15", BAD_SENTENCE, MARKER, world.save.public_id):
        assert secret not in raw, f"日誌洩漏了：{secret}"
    names = {line["event"] for line in log_lines() if "event" in line}
    assert {rpg_events.EVENT_NOTEBOOK_CAPTURED, rpg_events.EVENT_NOTEBOOK_ABANDONED, rpg_events.EVENT_NOTEBOOK_FAILED} <= names


def test_the_capture_request_is_a_structured_non_streaming_call_for_the_rpg_model(world):
    world.complete(11)
    world.kit.provider.script(Step(reply("設定甲。")))
    world.run()
    (request,) = world.kit.provider.calls
    assert request.model == MODEL and request.response_schema == prompts.CAPTURE_SCHEMA and world.kit.provider.handles == []
    assert 0 < request.timeout_seconds <= 30.0
