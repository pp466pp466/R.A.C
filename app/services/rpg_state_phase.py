"""RPG 回合的「狀態階段」：文字完整且通過審查之後，由模型提議世界狀態的變化，Service 逐項驗證（見 rpg-turn-engine 規格與 design D6）。

`TurnRunner`（普通回合）與 `RevisionRunner`（重新生成與編輯最後一輪，rpg-turn-revision）共用這一段：兩者的收尾與失敗路徑不同
（普通回合以 `finish_turn` 指派新輪數，修訂以 `replace_turn` 保留輪數），但「呼叫狀態模型 → `apply_proposal` 驗證 → 產生 `state` 事件」完全相同，
所以放在這裡，不複製。

- `state_phase(plan, deps, marker, narration)`：狀態呼叫（結構化輸出，`kind=rpg_state`）並逐項驗證。任何失敗都回傳 `ok=False`；
  由呼叫端決定失敗的意義（普通回合：文字照常完成、世界狀態不變；修訂：整個修訂失敗，保留舊內容）。
- `state_event(state, updated)`：`state` 事件的白名單欄位。

`plan` 只要有 `StatePlan` 列出的唯讀屬性即可（`TurnPlan` 與 `RevisionPlan` 都符合）。串流與狀態呼叫期間沒有資料庫交易：
這裡自己開一個短的工作階段只為了讓 `LlmService` 寫帳本，不碰 `g`、`current_app`。行動、劇情與世界狀態都不進日誌。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from app.models.llm_call_log import KIND_RPG_STATE
from app.observability import rpg_events
from app.services import rpg_context, rpg_prompt, rpg_world
from app.services.rpg_runner_base import RunnerDeps

__all__ = ["StatePlan", "StateOutcome", "state_event", "state_phase"]


class StatePlan(Protocol):
    """狀態階段需要的、開始階段凍結的唯讀欄位。"""

    @property
    def user_id(self) -> int: ...
    @property
    def model(self) -> str: ...
    @property
    def script(self) -> Mapping[str, Any]: ...
    @property
    def protagonist(self) -> Mapping[str, Any]: ...
    @property
    def world(self) -> rpg_world.WorldState: ...
    @property
    def action(self) -> str: ...
    @property
    def turn_number(self) -> int: ...
    @property
    def ending_due(self) -> bool: ...
    @property
    def tier(self) -> str: ...
    @property
    def max_months(self) -> int: ...
    @property
    def adult_age(self) -> int: ...


@dataclass
class StateOutcome:
    ok: bool
    world: rpg_world.WorldState | None = None
    notices: tuple[rpg_world.Notice, ...] = ()
    dropped: int = 0
    ending: bool = False
    degraded: str | None = None


NOTICE_SAFE_MODE_HINT = "safe_mode_hint"        # `state` 事件通知的固定列舉項：只有這個鍵，沒有任何文字（文字由前端固定提供，不來自模型）


def with_safe_mode_hint(event: dict[str, Any]) -> dict[str, Any]:
    """在 `state` 事件的通知欄位加一個提示項目（遊戲內「可以嘗試關閉安全模式」，見 safe-mode-upgrade-hint）。不新增事件種類，
    也不論世界狀態有沒有更新都帶得出（`updated: false` 的事件一樣有 `notices`）；回傳新的字典，不改傳入的。"""
    return {**event, "notices": [*event.get("notices", []), {"kind": NOTICE_SAFE_MODE_HINT}]}


def state_event(state: StateOutcome, updated: bool) -> dict[str, Any]:
    """`state` 事件：驗證後的玩家可見欄位與通知。沒有寫入世界狀態時只有 `updated: false`。"""
    if not updated or state.world is None:
        return {"updated": False, "notices": []}
    world = state.world
    return {
        "updated": True, "elapsed_months": world.elapsed_months, "location": world.current_location, "summary": world.status_summary,
        "goals": [goal["text"] for goal in world.goals.get("active", []) if isinstance(goal.get("text"), str)],
        "notices": [notice.to_dict() for notice in state.notices],
    }


def state_phase(plan: StatePlan, deps: RunnerDeps, marker: str, narration: str) -> StateOutcome:
    """狀態呼叫（結構化輸出，`kind=rpg_state`）並逐項驗證。任何失敗都回傳 `ok=False`（呼叫端決定失敗的意義）。"""
    factory = deps.llm_factory
    failed = StateOutcome(False, ending=plan.ending_due, degraded="unavailable")       # 壽終年齡由 Service 判定，狀態呼叫失敗也照樣完結
    if factory is None:
        return failed
    system, contents = rpg_context.build_state_context(
        script=plan.script, world=plan.world, protagonist=plan.protagonist, action=plan.action, narration=narration,
        turn_number=plan.turn_number, ending_due=plan.ending_due, marker=marker,
    )
    try:
        with deps.session_factory() as session:
            llm = factory(session)
            result = llm.run_structured(
                kind=KIND_RPG_STATE, user_id=plan.user_id, conversation_id=None, system_instruction=system, contents=contents,
                schema=rpg_prompt.STATE_SCHEMA, rules=rpg_prompt.state_rules(marker), model=plan.model,
            )
            session.commit()
    except Exception:                                                    # noqa: BLE001 — 狀態呼叫不得拖垮已送達的文字
        return failed
    if not result.ok:
        failed.degraded = result.degraded if result.degraded in rpg_events.STATE_DEGRADED else "unavailable"
        return failed
    proposal = rpg_world.apply_proposal(
        plan.world, result.value, rpg_world.ProposalContext(
            script=plan.script, protagonist=plan.protagonist, turn_number=plan.turn_number, action=plan.action, narration=narration,
            tier=plan.tier, max_months=plan.max_months, ending_due=plan.ending_due, adult_age=plan.adult_age, markers=(marker,),
        ),
    )
    return StateOutcome(True, proposal.world, proposal.notices, proposal.dropped, proposal.ending)
