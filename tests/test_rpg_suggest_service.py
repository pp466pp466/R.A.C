"""rpg-suggestions 規格：`SuggestionService`（design D5 的十個步驟）與 `SessionRepository.turn_in_progress`。

以 `member_session` 同時在 SQLite 與真實 SQL Server（標記 sqlserver）上執行，模型是假的供應商（`RunnerKit`）：前提檢查的順序與錯誤、
重用不呼叫模型、預算、備援與重試、逐項驗證、失敗不保存、寫回前再比對錨點（產生期間劇情前進、並行寫入、存檔完結）、不碰錢包、帳本、
日誌只有白名單欄位、管理員行為相同。
"""
import json
import logging
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.llm.types import LLMBlockedError, LLMTransientError
from app.models import LlmCallLog, User
from app.models.rpg_session import OPENING_FALLBACK, OPENING_GENERATING, OPENING_PENDING
from app.observability import rpg_events
from app.services.content_tier import SAFE_RULES_VERSION
from app.services.current_user import CurrentUser
from app.services.errors import (
    ActionSuggestionsStale, ActionSuggestionsUnavailable, AgeConfirmationRequired, Conflict, NotFound, OpeningPending, ServiceUnavailable,
    SessionEnded, SystemBusy,
)
from app.services.game_service import hold_key
from app.services.rpg_suggest_prompt import SUGGEST_SCHEMA, build_suggest_system
from app.services.rpg_suggest_service import NO_PROVIDER_MESSAGE, SuggestionService
from tests.support.age import TODAY_UTC
from tests.support.game import FALLBACK_MODEL, MARKER, MODEL, RunnerKit, story_script
from tests.support.llm import Step
from tests.support.rpg_pure import with_card, world_with
from tests.support.script_data import validated

GHOST = "00000000-0000-4000-8000-000000000000"
ITEMS = ("向守衛打聽消息", "走進山門", "回頭看看來時的路")
STATE_OK = json.dumps({"elapsed_months": 1, "current_location": "山門前"}, ensure_ascii=False)
AGREED = {"adult_confirmed_at": TODAY_UTC, "adult_rules_version": SAFE_RULES_VERSION}
UNBOUND = validated(content_tier="adult").content


@pytest.fixture()
def kit(member_session):
    return RunnerKit(member_session)


def reply(*items) -> str:
    return json.dumps({"suggestions": list(items)}, ensure_ascii=False)


def service(kit, *, llm=True) -> SuggestionService:
    return SuggestionService(
        sessions=kit.sessions, policy=kit.policy, limits=kit.limits, llm=kit.llm_for(kit.session) if llm else None,
        budget_exhausted=lambda: kit.budget_exhausted, marker=MARKER, model=MODEL, clock=kit.clock, release=kit.session.commit,
    )


def setup(kit, **overrides):
    user = kit.player(**overrides)
    return user, kit.open_save(user)


def ask(kit, user, save, **kwargs):
    return service(kit, **kwargs).suggest(user, save.public_id)


def begin(kit, user, save, msg="m-1", action="我走進山門。"):
    return kit.game.begin_turn(user, save.public_id, client_msg_id=msg, action=action)


def finish(kit, user, save, msg="m-1", story="劇情。"):
    kit.sessions.complete_turn(user.id, save.public_id, msg, llm_raw_response=story, structured_result=None, now=kit.clock())
    kit.wallet.settle(user, hold_key(save.public_id, msg, 1))


def play(kit, user, save, msg="m-1"):
    begin(kit, user, save, msg=msg)
    finish(kit, user, save, msg=msg)


def suggest_rows(kit):
    return kit.ledger("rpg_suggest")


def stored(kit, save):
    kit.session.expire_all()
    return save.suggestions_json


def committed_ledger(kit):
    """另一個工作階段看得到的（已提交的）帳本：證明錯誤回應之後帳本仍保留。"""
    with Session(kit.session.get_bind()) as other:
        return list(other.scalars(select(LlmCallLog).order_by(LlmCallLog.id)))


# ---- 產生、保存與重用 ----------------------------------------------------------------------------------


def test_the_first_call_generates_once_saves_with_the_opening_anchor_and_changes_nothing_else(kit):
    user, save = setup(kit)
    version, wallet_before = kit.fresh(save).version, kit.total(user)
    kit.provider.script(reply(*ITEMS))
    result = ask(kit, user, save)
    assert result.items == ITEMS and result.reused is False and len(kit.provider.calls) == 1
    assert stored(kit, save) == {"seq": 0, "revision": 0, "items": list(ITEMS)}
    assert kit.fresh(save).version == version and kit.total(user) == wallet_before          # 世界版本與錢包都沒有動


def test_a_second_call_for_the_same_screen_reuses_the_saved_suggestions_without_the_model(kit):
    user, save = setup(kit)
    kit.provider.script(reply(*ITEMS))
    ask(kit, user, save)
    again = ask(kit, user, save)
    assert again.items == ITEMS and again.reused is True and len(kit.provider.calls) == 1 and len(suggest_rows(kit)) == 1


def test_reuse_needs_neither_a_provider_nor_the_global_budget(kit):
    user, save = setup(kit)
    kit.provider.script(reply(*ITEMS))
    ask(kit, user, save)
    kit.budget_exhausted = True
    assert ask(kit, user, save, llm=False).reused is True


def test_a_new_completed_turn_makes_the_old_suggestions_stale_and_the_next_call_generates_again(kit):
    user, save = setup(kit)
    kit.provider.script(reply(*ITEMS), reply("拿出行囊裡的地圖", "坐下來休息"))
    ask(kit, user, save)
    play(kit, user, save)
    assert kit.sessions.suggestion_state(user.id, save.public_id).items == ()
    second = ask(kit, user, save)
    assert second.items == ("拿出行囊裡的地圖", "坐下來休息") and second.reused is False and len(kit.provider.calls) == 2
    assert stored(kit, save)["seq"] == 1 and stored(kit, save)["revision"] == 1
    assert ask(kit, user, save).reused is True and len(kit.provider.calls) == 2


def test_a_successful_revision_makes_the_suggestions_stale(kit):
    user, save = setup(kit)
    kit.provider.script_stream(*story_script())
    kit.provider.script(STATE_OK)
    plan = kit.start(user, save)
    kit.run(plan, user)
    kit.provider.script(reply(*ITEMS))
    ask(kit, user, save)
    kit.provider.script_stream(*story_script("另一種寫法。"))
    kit.provider.script(STATE_OK)
    revision = kit.revise(user, save)
    kit.run_revision(revision, user)
    assert kit.sessions.suggestion_state(user.id, save.public_id).items == ()
    kit.provider.script(reply("走進山門", "回頭看看"))
    third = ask(kit, user, save)
    assert third.reused is False and stored(kit, save) == {"seq": 1, "revision": 2, "items": ["走進山門", "回頭看看"]}


def test_the_model_call_is_one_structured_rpg_suggest_call_with_the_frozen_model_and_the_ledger_row(kit):
    user, save = setup(kit)
    kit.provider.script(reply(*ITEMS))
    ask(kit, user, save)
    (request,) = kit.provider.calls
    assert request.model == MODEL and request.response_schema == SUGGEST_SCHEMA
    assert request.system_instruction == build_suggest_system("safe", "zh-Hant-TW", MARKER)
    (row,) = suggest_rows(kit)
    assert (row.kind, row.status, row.model, row.user_id, row.counted, row.attempt) == ("rpg_suggest", "ok", MODEL, user.id, False, 1)
    assert row.conversation_id is None and row.error_detail is None and row.prompt_tokens and row.completion_tokens


def test_the_prompt_has_only_what_the_player_can_see(kit):
    user, save = setup(kit)
    save.entities_json = world_with(
        with_card("林曉", stage="cracking", traits=[("獨特表層特質甲", "surface"), ("獨特隱藏特質乙", "hidden")], anchor_fact="獨特錨定事實丙", last_seen=1),
    ).entities
    save.notebook_text = "獨特記事簿內容丁"
    kit.session.flush()
    kit.provider.script(reply(*ITEMS))
    ask(kit, user, save)
    (request,) = kit.provider.calls
    body = "\n".join(part for content in request.contents for part in content.parts)
    assert "開場的劇情。" in body and "旅人" in body
    for hidden in ("獨特表層特質甲", "獨特隱藏特質乙", "獨特錨定事實丙", "獨特記事簿內容丁", "cracking"):
        assert hidden not in body and hidden not in request.system_instruction


def test_the_context_uses_the_last_three_completed_turns_when_there_are_some(kit):
    user, save = setup(kit)
    for n in range(1, 6):
        kit.provider.script_stream(*story_script(f"第{n}輪的劇情。"))
        kit.provider.script(STATE_OK)
        kit.run(kit.start(user, save, msg=f"m-{n}", action=f"第{n}輪的行動"), user)
    kit.provider.script(reply(*ITEMS))
    ask(kit, user, save)
    body = "\n".join(part for content in kit.provider.calls[-1].contents for part in content.parts)
    assert [f"第{n}輪的行動" in body for n in range(1, 6)] == [False, False, True, True, True]
    assert "開場的劇情。" not in body


def test_the_effective_mode_is_recomputed_for_each_call(kit):
    user = kit.player(1, content_tier_preference="safe")
    save = kit.open_save(user, content=UNBOUND)
    kit.provider.script(reply(*ITEMS), reply(*ITEMS))
    ask(kit, user, save)
    assert kit.provider.calls[-1].system_instruction == build_suggest_system("safe", "zh-Hant-TW", MARKER)
    play(kit, user, save)
    model = kit.session.get(User, user.id)                                      # 玩家在兩次建議之間關閉安全模式並同意
    model.content_tier_preference, model.adult_confirmed_at, model.adult_rules_version = "adult", TODAY_UTC, SAFE_RULES_VERSION
    kit.session.flush()
    service(kit).suggest(CurrentUser.from_model(model), save.public_id)
    assert kit.provider.calls[-1].system_instruction == build_suggest_system("adult", "zh-Hant-TW", MARKER)


# ---- 前提檢查 ----------------------------------------------------------------------------------------


def test_an_unconfirmed_account_is_refused_before_anything_else(kit):
    user = kit.player(age=None)
    other_save = kit.open_save(kit.player(2))
    with pytest.raises(AgeConfirmationRequired):
        ask(kit, user, other_save)
    assert kit.provider.calls == [] and kit.ledger() == []


def test_someone_elses_save_and_a_missing_save_are_the_same_not_found_and_never_call_the_model(kit):
    user, save = setup(kit)
    intruder = kit.player(2)
    for target in (save.public_id, GHOST):
        with pytest.raises(NotFound) as caught:
            service(kit).suggest(intruder, target)
        assert caught.value.code == "not_found"
    assert kit.provider.calls == [] and stored(kit, save) is None


def test_an_ended_session_is_session_ended_without_a_call(kit):
    user, save = setup(kit)
    kit.sessions.end_session(user.id, save.public_id, now=kit.clock())
    with pytest.raises(SessionEnded):
        ask(kit, user, save)
    assert kit.provider.calls == []


def test_an_unfinished_opening_is_opening_pending(kit):
    user = kit.player()
    save = kit.pending_save(user)
    assert save.opening_state == OPENING_PENDING
    with pytest.raises(OpeningPending):
        ask(kit, user, save)
    kit.sessions.claim_opening(user.id, save.public_id, now=kit.clock())
    kit.session.expire_all()
    assert save.opening_state == OPENING_GENERATING
    with pytest.raises(OpeningPending):
        ask(kit, user, save)
    assert kit.provider.calls == []


def test_a_fallback_opening_and_a_pre_migration_save_are_allowed(kit):
    user, save = setup(kit)
    save.opening_state = OPENING_FALLBACK
    kit.session.flush()
    kit.provider.script(reply(*ITEMS))
    assert ask(kit, user, save).items == ITEMS
    legacy_user = kit.player(2)
    legacy = kit.open_save(legacy_user)
    legacy.opening_state, legacy.opening_text = None, None
    kit.session.flush()
    kit.provider.script(reply(*ITEMS))
    assert ask(kit, legacy_user, legacy).items == ITEMS


def test_a_first_turn_that_is_still_streaming_is_turn_in_progress_even_with_no_completed_turn(kit):
    """任務 4.7：尚無完成回合時 `revision_state` 無從判斷，端點仍要知道有一個進行中的回合。"""
    user, save = setup(kit)
    begin(kit, user, save)
    assert kit.sessions.revision_state(user.id, save.public_id, now=kit.clock()).client_msg_id is None       # 對照：沒有完成的回合可判斷
    with pytest.raises(Conflict) as caught:
        ask(kit, user, save)
    assert caught.value.code == "turn_in_progress" and kit.provider.calls == []


def test_a_streaming_turn_after_a_completed_turn_is_turn_in_progress(kit):
    user, save = setup(kit)
    play(kit, user, save)
    begin(kit, user, save, msg="m-2")
    with pytest.raises(Conflict) as caught:
        ask(kit, user, save)
    assert caught.value.code == "turn_in_progress" and kit.provider.calls == []


def test_a_stale_streaming_turn_no_longer_blocks_the_call(kit):
    user, save = setup(kit)
    begin(kit, user, save)
    kit.clock.advance(seconds=kit.limits.coin_hold_timeout() + 1)
    kit.provider.script(reply(*ITEMS))
    assert ask(kit, user, save).items == ITEMS                                            # 逾時的生成不再算進行中（與開始回合的接手規則一致）


def test_a_revision_in_progress_is_turn_in_progress(kit):
    user, save = setup(kit)
    play(kit, user, save)
    kit.revise(user, save)                                                                 # 認領修訂但還沒執行
    with pytest.raises(Conflict) as caught:
        ask(kit, user, save)
    assert caught.value.code == "turn_in_progress" and kit.provider.calls == []


def test_the_repository_reports_progress_for_the_owner_only(kit):
    user, save = setup(kit)
    now, stale = kit.clock(), kit.limits.coin_hold_timeout()
    assert kit.sessions.turn_in_progress(user.id, save.public_id, now=now, stale_after_seconds=stale) is False
    begin(kit, user, save)
    assert kit.sessions.turn_in_progress(user.id, save.public_id, now=now, stale_after_seconds=stale) is True
    assert kit.sessions.turn_in_progress(user.id, save.public_id, now=now + timedelta(seconds=stale + 1), stale_after_seconds=stale) is False
    assert kit.sessions.turn_in_progress(kit.player(2).id, save.public_id, now=now, stale_after_seconds=stale) is None
    assert kit.sessions.turn_in_progress(user.id, GHOST, now=now, stale_after_seconds=stale) is None


def test_an_ended_session_is_reported_before_a_turn_in_progress(kit):
    user, save = setup(kit)
    begin(kit, user, save)
    kit.sessions.end_session(user.id, save.public_id, now=kit.clock())
    with pytest.raises(SessionEnded):
        ask(kit, user, save)


def test_a_turn_in_progress_is_reported_before_the_saved_suggestions_are_reused(kit):
    user, save = setup(kit)
    kit.provider.script(reply(*ITEMS))
    ask(kit, user, save)
    begin(kit, user, save)                                                                 # 錨點還沒變（回合尚未完成），但玩家已不能送出行動
    with pytest.raises(Conflict) as caught:
        ask(kit, user, save)
    assert caught.value.code == "turn_in_progress" and len(kit.provider.calls) == 1


def test_without_a_provider_the_call_is_a_503_and_nothing_is_saved(kit, caplog):
    user, save = setup(kit)
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        with pytest.raises(ServiceUnavailable) as caught:
            ask(kit, user, save, llm=False)
    assert caught.value.code == "service_unavailable" and str(caught.value) == NO_PROVIDER_MESSAGE
    assert stored(kit, save) is None and kit.ledger() == []
    (record,) = [r for r in caplog.records if getattr(r, "event", None) == rpg_events.EVENT_SUGGESTION_FAILED]
    assert record.reason == "no_provider" and record.attempt == 0


def test_a_spent_global_budget_is_system_busy_and_never_calls_the_model(kit):
    user, save = setup(kit)
    kit.budget_exhausted = True
    with pytest.raises(SystemBusy):
        ask(kit, user, save)
    assert kit.provider.calls == [] and stored(kit, save) is None and kit.ledger() == []


# ---- 不扣幣 ------------------------------------------------------------------------------------------


def test_the_call_never_touches_the_wallet_even_with_an_empty_balance(kit):
    user, save = setup(kit)
    assert kit.wallets.state(user.id) is None
    kit.provider.script(reply(*ITEMS))
    ask(kit, user, save)
    assert kit.wallets.state(user.id) is None and kit.total(user) == 0


def test_an_admin_gets_the_same_behavior(kit):
    admin = kit.player(90, role="admin")
    save = kit.open_save(admin)
    kit.provider.script(reply(*ITEMS))
    assert ask(kit, admin, save).items == ITEMS and ask(kit, admin, save).reused is True
    (row,) = suggest_rows(kit)
    assert row.user_id == admin.id and kit.wallets.state(admin.id) is None
    play(kit, admin, save)
    kit.budget_exhausted = True                                                            # 全站 token 上限對管理員一樣適用
    with pytest.raises(SystemBusy):
        ask(kit, admin, save)
    assert kit.wallets.state(admin.id) is not None and kit.total(admin) == 0               # 回合的預扣金額為 0；建議本身沒有任何錢包異動


# ---- 逐項驗證、重試與備援 ---------------------------------------------------------------------------------


def test_invalid_items_are_dropped_and_only_the_valid_ones_are_saved(kit):
    user, save = setup(kit)
    kit.provider.script(reply("傳訊息到 abc@example.com", "好", "走進山門"))
    result = ask(kit, user, save)
    assert result.items == ("走進山門",) and stored(kit, save)["items"] == ["走進山門"] and len(kit.provider.calls) == 1


def test_a_sexual_item_is_dropped_in_safe_mode_and_kept_when_the_safety_mode_is_off(kit):
    sexual = "兩人發生過性關係"
    user, save = setup(kit)
    kit.provider.script(reply(sexual, "走進山門"))
    assert ask(kit, user, save).items == ("走進山門",)
    adult = kit.player(2, content_tier_preference="adult", **AGREED)
    adult_save = kit.open_save(adult, content=UNBOUND)
    kit.provider.script(reply(sexual, "走進山門"))
    assert ask(kit, adult, adult_save).items == (sexual, "走進山門")
    bound = kit.open_save(adult, content=validated(content_tier="safe").content)                 # 綁定安全模式的劇本一律安全模式
    kit.provider.script(reply(sexual, "走進山門"))
    assert ask(kit, adult, bound).items == ("走進山門",)


def test_the_absolute_prohibition_is_dropped_in_every_mode(kit):
    adult = kit.player(2, content_tier_preference="adult", **AGREED)
    save = kit.open_save(adult, content=UNBOUND)
    kit.provider.script(reply("十歲的少女與他做愛", "走進山門"))
    assert ask(kit, adult, save).items == ("走進山門",)


def test_all_invalid_items_retry_once_then_fail_without_saving_and_the_player_can_try_again(kit, caplog):
    user, save = setup(kit)
    kit.provider.script(reply("好", "壞"), reply("abc@example.com 聯絡"))
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        with pytest.raises(ActionSuggestionsUnavailable) as caught:
            ask(kit, user, save)
    assert caught.value.code == "suggestions_unavailable" and "好" not in str(caught.value)
    assert len(kit.provider.calls) == 2 and stored(kit, save) is None
    assert [row.status for row in suggest_rows(kit)] == ["validation_failed", "validation_failed"]
    assert all(row.error_detail for row in suggest_rows(kit))                                  # 失敗的原文只存在 `error_detail`（14 天）
    (record,) = [r for r in caplog.records if getattr(r, "event", None) == rpg_events.EVENT_SUGGESTION_FAILED]
    assert (record.reason, record.attempt) == ("validation", 2)
    kit.provider.script(reply(*ITEMS))                                                        # 失敗不佔次數：再按一次就能產生
    assert ask(kit, user, save).items == ITEMS


def test_the_marker_in_the_output_is_a_validation_failure_and_the_retry_can_recover(kit):
    user, save = setup(kit)
    kit.provider.script(reply("走進山門", f"說出{MARKER}"), reply(*ITEMS))
    assert ask(kit, user, save).items == ITEMS and len(kit.provider.calls) == 2
    assert [row.status for row in suggest_rows(kit)] == ["validation_failed", "ok"]


def test_the_primary_model_failing_with_429_switches_to_the_fallback_model_and_never_to_gemini(kit):
    user, save = setup(kit)
    kit.provider.script(Step(LLMTransientError("rate_limited"), seconds=0.3), reply(*ITEMS))
    assert ask(kit, user, save).items == ITEMS
    assert [request.model for request in kit.provider.calls] == [MODEL, FALLBACK_MODEL]
    assert [(row.model, row.status) for row in suggest_rows(kit)] == [(MODEL, "rate_limited"), (FALLBACK_MODEL, "ok")]


def test_both_models_failing_is_unavailable_and_the_ledger_survives_the_error_response(kit, caplog):
    user, save = setup(kit)
    kit.provider.script(Step(LLMTransientError("server_error"), seconds=0.3), Step(LLMTransientError("server_error"), seconds=0.3))
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        with pytest.raises(ActionSuggestionsUnavailable):
            ask(kit, user, save)
    assert stored(kit, save) is None
    rows = [row for row in committed_ledger(kit) if row.kind == "rpg_suggest"]
    assert [row.status for row in rows] == ["server_error", "server_error"]                    # 已提交：之後的 4xx／5xx 不會把帳本回復
    (record,) = [r for r in caplog.records if getattr(r, "event", None) == rpg_events.EVENT_SUGGESTION_FAILED]
    assert record.reason == "transient"


@pytest.mark.parametrize(("step", "reason"), [
    (Step(LLMBlockedError("output")), "blocked"),
    (Step(LLMBlockedError("input")), "blocked"),
])
def test_a_provider_block_is_unavailable_without_a_retry(kit, step, reason, caplog):
    user, save = setup(kit)
    kit.provider.script(step)
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        with pytest.raises(ActionSuggestionsUnavailable):
            ask(kit, user, save)
    assert len(kit.provider.calls) == 1 and stored(kit, save) is None
    assert [row.status for row in committed_ledger(kit)] == ["blocked"]
    (record,) = [r for r in caplog.records if getattr(r, "event", None) == rpg_events.EVENT_SUGGESTION_FAILED]
    assert record.reason == reason


def test_simplified_characters_in_the_model_output_are_converted_to_traditional(kit):
    user, save = setup(kit)
    kit.provider.script(reply("走进山门", "向守卫行礼"))
    assert ask(kit, user, save).items == ("走進山門", "向守衛行禮")


# ---- 寫回前再比對錨點 -------------------------------------------------------------------------------------


def during_the_call(kit, action):
    """模型呼叫的期間（供應商收到請求之後、回覆之前）執行一次 `action`：模擬另一個裝置或並行的請求。"""
    done: list[bool] = []

    def hook(_request):
        if not done:
            done.append(True)
            action()

    kit.provider.hooks.append(hook)


def test_a_turn_that_completes_during_the_call_makes_the_result_stale_and_nothing_is_saved(kit, caplog):
    user, save = setup(kit)
    during_the_call(kit, lambda: play(kit, user, save))
    kit.provider.script(reply(*ITEMS))
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        with pytest.raises(ActionSuggestionsStale) as caught:
            ask(kit, user, save)
    assert caught.value.code == "suggestions_stale" and stored(kit, save) is None
    assert kit.sessions.suggestion_state(user.id, save.public_id).items == ()
    assert [row.status for row in suggest_rows(kit)] == ["ok"]                                  # token 已經用掉：帳本照記
    (record,) = [r for r in caplog.records if getattr(r, "event", None) == rpg_events.EVENT_SUGGESTION_FAILED]
    assert record.reason == "stale"


def test_a_concurrent_request_that_saved_first_wins_and_the_result_is_marked_reused(kit, caplog):
    user, save = setup(kit)
    during_the_call(kit, lambda: kit.sessions.save_suggestions(user.id, save.public_id, seq=0, revision=0, items=("別人先寫的建議",)))
    kit.provider.script(reply(*ITEMS))
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        result = ask(kit, user, save)
    assert result.items == ("別人先寫的建議",) and result.reused is True
    assert stored(kit, save)["items"] == ["別人先寫的建議"]                                       # 沒有覆蓋
    (record,) = [r for r in caplog.records if getattr(r, "event", None) == rpg_events.EVENT_SUGGESTION_SERVED]
    assert record.outcome == "reused" and record.count == 1


def test_a_session_that_ends_during_the_call_is_session_ended_and_nothing_is_saved(kit):
    user, save = setup(kit)
    during_the_call(kit, lambda: kit.sessions.end_session(user.id, save.public_id, now=kit.clock()))
    kit.provider.script(reply(*ITEMS))
    with pytest.raises(SessionEnded):
        ask(kit, user, save)
    assert stored(kit, save) is None


# ---- 日誌 --------------------------------------------------------------------------------------------


def test_the_served_events_carry_only_ids_integers_and_enumerations(kit, caplog):
    user, save = setup(kit)
    kit.provider.script(reply(*ITEMS))
    with caplog.at_level(logging.INFO, logger="app.rpg"):
        ask(kit, user, save)
        ask(kit, user, save)
    first, second = [r for r in caplog.records if getattr(r, "event", None) == rpg_events.EVENT_SUGGESTION_SERVED]
    assert (first.user_id, first.session_id, first.count, first.outcome, first.model) == (user.id, save.id, 3, "generated", MODEL)
    assert (second.user_id, second.session_id, second.count, second.outcome, second.model) == (user.id, save.id, 3, "reused", None)
    text = " ".join(str(record.__dict__) for record in caplog.records)
    for secret in (*ITEMS, "開場的劇情。", save.public_id, MARKER):
        assert secret not in text, secret
