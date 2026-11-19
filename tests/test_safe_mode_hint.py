"""safe-mode-upgrade-hint 規格：遊戲內「可以嘗試關閉安全模式」提示的後端（safe-mode-toggle 5.1）。

涵蓋：提示的前提（`AccessPolicy.upgrade_hint_eligible`：玩家設定為安全模式開、劇本未綁定、緊急煞車未啟用）與開始階段的兩個旗標、
輸入端與輸出端的觸發、每個存檔最多一次（條件式更新、換裝置不重複、不同存檔各一次）、被擋／中斷／斷線／被取代的回合不記錄、
修訂不重複、事件順序不變且提示只在 `state` 事件內（沒有文字）、不改變設定與預扣，以及日誌只含編號與原因列舉。
以真的資料庫（SQLite 與 SQL Server 各跑一次）與假的串流供應商驅動。
"""
import json
import logging
from datetime import timedelta

import pytest

from app.models.rpg_session import TURN_BLOCKED, TURN_COMPLETE
from app.services.content_tier import SAFE_RULES_VERSION
from tests.support.age import TODAY_UTC
from tests.support.game import BAD_SENTENCE, RunnerKit, story_script
from tests.support.llm import Step
from tests.support.script_data import validated

STATE_OK = json.dumps({"elapsed_months": 1, "current_location": "山門前"}, ensure_ascii=False)
SEXUAL_ACTION = "我抱著她，兩人發生了性行為。"
SEXUAL_SENTENCE = "他們發生了性行為。"
PLAIN_ACTION = "我走進山門。"
HINT = {"kind": "safe_mode_hint"}
CONSENT = {"adult_confirmed_at": TODAY_UTC - timedelta(days=3), "adult_rules_version": SAFE_RULES_VERSION, "adult_confirm_method": "self_declared"}
UNBOUND = validated(content_tier="adult").content          # 未綁定安全模式的劇本內容
BOUND = validated(content_tier="safe").content             # 綁定安全模式的劇本內容


@pytest.fixture()
def kit(member_session):
    return RunnerKit(member_session)


def names(events):
    return [event.name for event in events]


def of(events, name):
    return [event.data for event in events if event.name == name]


def hint_rows(caplog):
    return [r for r in caplog.records if r.name == "app.rpg" and getattr(r, "event", "") == "rpg.safe_mode_hint"]


def opened(kit, *, content=UNBOUND, **player):
    user = kit.player(**player)
    return user, kit.open_save(user, content=content)


def play(kit, user, save, *, msg="m-1", action=PLAIN_ACTION, sentences=None, blocked_first=False):
    """開始一個回合並跑完；`blocked_first` 為真時第一次嘗試含一句性描寫（安全尺度下會被審查層擋下），第二次正常。"""
    plan = kit.start(user, save, msg=msg, action=action)
    if blocked_first:
        kit.provider.script_stream(*story_script("山門前很安靜。", SEXUAL_SENTENCE, "後面的文字。"))
    kit.provider.script_stream(*story_script(*(sentences or ()))).script(Step(STATE_OK))
    return plan, kit.run(plan, user)


# ---- 提示的前提與開始階段的旗標 -----------------------------------------------------------------------------

@pytest.mark.parametrize(
    "case, content, player, forced, action, eligible, hint_input",
    [
        ("安全模式開、未綁定、性內容", UNBOUND, {}, False, SEXUAL_ACTION, True, True),
        ("安全模式開、未綁定、一般行動", UNBOUND, {}, False, PLAIN_ACTION, True, False),
        ("綁定的劇本", BOUND, {}, False, SEXUAL_ACTION, False, False),
        ("已關閉安全模式", UNBOUND, {"content_tier_preference": "adult", **CONSENT}, False, SEXUAL_ACTION, False, False),
        ("舊值 standard 加目前版本的同意＝已關閉", UNBOUND, {"content_tier_preference": "standard", **CONSENT}, False, SEXUAL_ACTION, False, False),
        ("偏好是關但同意已失效＝畫面顯示為開", UNBOUND, {"content_tier_preference": "adult", **{**CONSENT, "adult_rules_version": "2020-old"}}, False, SEXUAL_ACTION, True, True),
        ("緊急煞車", UNBOUND, {}, True, SEXUAL_ACTION, False, False),
        ("緊急煞車加已關閉", UNBOUND, {"content_tier_preference": "adult", **CONSENT}, True, SEXUAL_ACTION, False, False),
    ],
)
def test_the_start_phase_decides_eligibility_and_the_input_trigger(member_session, case, content, player, forced, action, eligible, hint_input):
    kit = RunnerKit(member_session, safe_mode_forced=forced)
    user, save = opened(kit, content=content, **player)
    plan = kit.start(user, save, action=action)
    assert (plan.hint_eligible, plan.hint_input) == (eligible, hint_input), case


def test_a_snapshot_with_a_legacy_or_missing_binding_follows_the_save_mode_reading(kit):
    user = kit.player()
    legacy = {**UNBOUND, "content_tier": "standard"}                     # 舊值 standard 讀作未綁定
    assert kit.policy.upgrade_hint_eligible(user, legacy) is True
    for broken in ({k: v for k, v in UNBOUND.items() if k != "content_tier"}, {**UNBOUND, "content_tier": "wild"}, None, "x"):
        assert kit.policy.upgrade_hint_eligible(user, broken) is False       # 缺少或無法判定視為綁定（與 save_mode 一致）


# ---- 觸發：輸入與輸出 ------------------------------------------------------------------------------------

def test_a_sexual_action_from_a_safe_mode_player_gets_the_hint_after_the_turn_completes(kit, caplog):
    user, save = opened(kit)
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        plan, events = play(kit, user, save, action=SEXUAL_ACTION)
    (state,) = of(events, "state")
    assert HINT in state["notices"] and state["updated"] is True
    assert kit.fresh(save).safe_hint_at is not None
    rows = hint_rows(caplog)
    assert len(rows) == 1 and (rows[0].reason, rows[0].outcome, rows[0].user_id, rows[0].session_id) == ("input", "shown", user.id, save.id)


def test_an_output_blocked_for_sexual_content_and_regenerated_gets_the_hint_with_the_output_reason(kit, caplog):
    user, save = opened(kit)
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        plan, events = play(kit, user, save, blocked_first=True)
    assert plan.hint_eligible and not plan.hint_input                    # 輸入沒有性內容；觸發來自輸出
    assert "transition" in names(events) and HINT in of(events, "state")[0]["notices"]
    assert [r.reason for r in hint_rows(caplog)] == ["output"]


def test_when_both_triggers_apply_the_reason_is_the_input(kit, caplog):
    user, save = opened(kit)
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        play(kit, user, save, action=SEXUAL_ACTION, blocked_first=True)
    assert [r.reason for r in hint_rows(caplog)] == ["input"]


def test_a_plain_turn_gets_no_hint_and_records_nothing(kit, caplog):
    user, save = opened(kit)
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        _, events = play(kit, user, save)
    assert of(events, "state")[0]["notices"] == [] and kit.fresh(save).safe_hint_at is None and hint_rows(caplog) == []


def test_an_output_blocked_for_another_reason_is_not_a_hint_trigger(kit):
    user, save = opened(kit)
    plan = kit.start(user, save)
    kit.provider.script_stream(*story_script("山門前很安靜。", BAD_SENTENCE, "後面的文字。")).script_stream(*story_script()).script(Step(STATE_OK))
    events = kit.run(plan, user)
    assert "transition" in names(events) and of(events, "state")[0]["notices"] == [] and kit.fresh(save).safe_hint_at is None


@pytest.mark.parametrize("case, content, player, forced", [
    ("綁定的劇本", BOUND, {}, False),
    ("已關閉安全模式", UNBOUND, {"content_tier_preference": "adult", **CONSENT}, False),
    ("緊急煞車", UNBOUND, {}, True),
])
def test_no_hint_for_a_bound_script_a_player_who_turned_it_off_or_the_emergency_brake(member_session, case, content, player, forced):
    kit = RunnerKit(member_session, safe_mode_forced=forced)
    user, save = opened(kit, content=content, **player)
    _, events = play(kit, user, save, action=SEXUAL_ACTION)
    assert of(events, "state")[0]["notices"] == [] and kit.fresh(save).safe_hint_at is None, case


# ---- 每個存檔最多一次 ------------------------------------------------------------------------------------

def test_a_second_trigger_in_the_same_save_gives_no_second_hint_and_keeps_the_first_time(kit, caplog):
    user, save = opened(kit)
    play(kit, user, save, action=SEXUAL_ACTION)
    first_time = kit.fresh(save).safe_hint_at
    caplog.clear()                                                                           # 只看第二個回合的日誌（第一個回合的提示事件不算）
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        _, events = play(kit, user, save, msg="m-2", action=SEXUAL_ACTION)
    assert of(events, "state")[0]["notices"] == [] and kit.fresh(save).safe_hint_at == first_time
    assert hint_rows(caplog) == []


def test_each_save_gets_its_own_single_hint(kit):
    user, first = opened(kit)
    second = kit.open_save(user, content=UNBOUND)
    _, a = play(kit, user, first, action=SEXUAL_ACTION)
    _, b = play(kit, user, second, action=SEXUAL_ACTION)
    assert HINT in of(a, "state")[0]["notices"] and HINT in of(b, "state")[0]["notices"]
    assert kit.fresh(first).safe_hint_at is not None and kit.fresh(second).safe_hint_at is not None


def test_the_claim_is_a_conditional_update_that_only_one_caller_wins_and_only_the_owner_can_make(kit):
    user, save = opened(kit)
    other = kit.player(2)
    now = kit.clock()
    assert kit.sessions.claim_safe_hint(other.id, save.public_id, now=now) is False       # 非擁有者
    assert kit.sessions.claim_safe_hint(user.id, save.public_id, now=now) is True
    assert kit.sessions.claim_safe_hint(user.id, save.public_id, now=now + timedelta(minutes=5)) is False
    assert kit.fresh(save).safe_hint_at == now                                                # 第二次沒有蓋掉第一次


def test_a_new_device_reading_the_snapshot_never_sees_the_hint_again(kit):
    user, save = opened(kit)
    play(kit, user, save, action=SEXUAL_ACTION)
    replay = kit.game.begin_turn(user, save.public_id, client_msg_id="m-1", action=SEXUAL_ACTION)
    assert replay.replay is not None and replay.plan is None                                 # 已完成的重送走重播，不經過串流，沒有提示
    plan = kit.start(user, save, msg="m-9", action=SEXUAL_ACTION)
    assert plan.hint_eligible and plan.hint_input                                            # 前提仍成立，但紀錄擋下第二次（見上一個測試）


# ---- 沒有完成的回合不記錄 ---------------------------------------------------------------------------------

def test_a_turn_blocked_twice_gives_no_hint_and_records_nothing(kit, caplog):
    user, save = opened(kit)
    plan = kit.start(user, save, action=SEXUAL_ACTION)
    kit.provider.script_stream(*story_script(SEXUAL_SENTENCE)).script_stream(*story_script(SEXUAL_SENTENCE))
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        events = kit.run(plan, user)
    (turn,) = kit.turns(save)
    assert turn.turn_status == TURN_BLOCKED and "state" not in names(events)
    assert kit.fresh(save).safe_hint_at is None and hint_rows(caplog) == []


def test_an_interrupted_turn_records_nothing(kit):
    user, save = opened(kit)
    plan = kit.start(user, save, action=SEXUAL_ACTION)
    kit.provider.script_stream(*story_script("山門前很安靜。", finish=False))
    generator = kit.runner(plan, user).events()
    next(generator)
    generator.close()                                                                        # 文字還沒完成時斷線
    assert kit.fresh(save).safe_hint_at is None


def test_a_disconnect_after_the_text_completes_finishes_the_turn_but_leaves_the_hint_for_next_time(kit, caplog):
    user, save = opened(kit)
    plan = kit.start(user, save, action=SEXUAL_ACTION)
    kit.provider.script_stream(*story_script()).script(Step(STATE_OK))
    generator = kit.runner(plan, user).events()
    events = []
    while not events or events[-1].name != "ping":
        events.append(next(generator))
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        generator.close()
    (turn,) = kit.turns(save)
    assert turn.turn_status == TURN_COMPLETE                                                 # 回合照常完成並結算
    assert kit.fresh(save).safe_hint_at is None and hint_rows(caplog) == []                  # 玩家看不到，所以不記錄
    _, again = play(kit, user, save, msg="m-2", action=SEXUAL_ACTION)
    assert HINT in of(again, "state")[0]["notices"]                                          # 下一次還能提示


def test_a_turn_taken_over_by_a_newer_attempt_records_nothing(kit, monkeypatch):
    from app.repositories.session_repository import SessionRepository

    user, save = opened(kit)
    plan = kit.start(user, save, action=SEXUAL_ACTION)
    kit.provider.script_stream(*story_script()).script(Step(STATE_OK))
    original = SessionRepository.finish_turn

    def superseded(self, *args, **kwargs):
        from app.repositories.errors import InvalidTurnTransitionError

        raise InvalidTurnTransitionError("被新的嘗試取代")

    monkeypatch.setattr(SessionRepository, "finish_turn", superseded)
    events = kit.run(plan, user)
    monkeypatch.setattr(SessionRepository, "finish_turn", original)
    assert events[-1].name == "error" and kit.fresh(save).safe_hint_at is None


# ---- 事件協定與不改變任何東西 ------------------------------------------------------------------------------

def test_the_event_order_is_unchanged_and_the_hint_lives_inside_the_state_event_only(kit):
    user, save = opened(kit)
    _, events = play(kit, user, save, action=SEXUAL_ACTION)
    order = names(events)
    assert order[0] == "meta" and order[-1] == "done" and order.index("state") == len(order) - 2
    assert set(order) <= {"meta", "token", "ping", "state", "done"}                         # 沒有新的事件種類
    assert order.count("state") == 1 and "safe_mode_hint" not in order
    notice = of(events, "state")[0]["notices"][-1]
    assert notice == HINT                                                                    # 只有一個固定的列舉鍵，沒有任何文字
    assert "safe_mode_hint" not in "".join(event.encode() for event in events if event.name != "state")


def test_the_hint_also_arrives_when_the_state_call_is_unavailable(kit):
    kit.state_available = False
    user, save = opened(kit)
    plan = kit.start(user, save, action=SEXUAL_ACTION)
    kit.provider.script_stream(*story_script())
    events = kit.run(plan, user)
    assert of(events, "state") == [{"updated": False, "notices": [HINT]}]


def test_the_hint_changes_nothing_about_the_turn_the_player_setting_or_the_coins(kit):
    user, save = opened(kit)
    plan, events = play(kit, user, save, action=SEXUAL_ACTION)
    assert events[-1].data["status"] == "complete" and events[-1].data["coins"] == 170 and kit.total(user) == 170     # 與沒有提示的回合同樣結算
    from app.models import User

    kit.session.expire_all()
    assert kit.session.get(User, user.id).content_tier_preference == "safe"                 # 不自動調高
    next_plan = kit.start(user, save, msg="m-2", action=PLAIN_ACTION)
    assert next_plan.tier == "safe"                                                          # 下一回合仍以安全尺度生成
    assert "safe_mode_hint" not in next_plan.system_instruction and not any("safe_mode_hint" in str(part) for part in next_plan.contents)   # 不進模型的上下文


def test_a_regeneration_never_adds_the_hint_again(kit):
    user, save = opened(kit)
    play(kit, user, save, action=SEXUAL_ACTION)
    plan = kit.revise(user, save, kind="regenerate")
    kit.provider.script_stream(*story_script()).script(Step(STATE_OK))
    events = kit.run_revision(plan, user)
    assert of(events, "state")[0]["notices"] == []


def test_an_edit_never_records_a_hint_either(kit):
    user, save = opened(kit)
    play(kit, user, save, action=PLAIN_ACTION)
    plan = kit.revise(user, save, kind="edit", action=SEXUAL_ACTION)
    kit.provider.script_stream(*story_script()).script(Step(STATE_OK))
    events = kit.run_revision(plan, user)
    assert of(events, "state")[0]["notices"] == [] and kit.fresh(save).safe_hint_at is None


# ---- 日誌只含編號與列舉 ------------------------------------------------------------------------------------

def test_the_log_event_has_only_ids_and_the_reason_enumeration_and_never_any_text(kit, caplog):
    user, save = opened(kit)
    with caplog.at_level(logging.DEBUG, logger="app.rpg"):
        play(kit, user, save, action=SEXUAL_ACTION)
    (row,) = hint_rows(caplog)
    emitted = {key: value for key, value in row.__dict__.items() if key in {"event", "user_id", "session_id", "reason", "outcome"}}
    assert emitted == {"event": "rpg.safe_mode_hint", "user_id": user.id, "session_id": save.id, "reason": "input", "outcome": "shown"}
    everything = " ".join(str(value) for record in caplog.records for value in (record.getMessage(), *record.__dict__.values()) if isinstance(value, (str, int)))
    for secret in (SEXUAL_ACTION, SEXUAL_SENTENCE, "性行為", "發生"):
        assert secret not in everything, secret
