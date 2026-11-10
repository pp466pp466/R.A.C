"""已完成的回合與第 0 幕的重送（見 rpg-turn-stream 規格「已完成回合的重送」）。

以已 `complete` 的 `client_msg_id` 重送回合，或第 0 幕已經 `ready`／`fallback` 時再次呼叫開場端點，都不呼叫模型、不碰錢包、不新增回合：
回傳 `meta`（標示 `replayed: true`）、一個含完整劇情原文的 `token`、`done`。事件的形狀與正常串流一致，前端用同一條接收路徑。

純函式：只做資料轉換，不碰資料庫與網路；劇情文字只出現在事件本文，不進日誌。
"""
from __future__ import annotations

from app.services.game_service import OpeningDone, TurnReplay
from app.services.rpg_runner_base import DONE_COMPLETE, DONE_ENDED, EVENT_DONE, EVENT_META, EVENT_TOKEN, SseEvent


def turn_replay_events(replay: TurnReplay, *, coins: int) -> list[SseEvent]:
    """已完成回合的重送事件。存檔已完結（結局回合）時 `done` 的狀態為 `ended`，與原本串流結束時一致。"""
    return [
        SseEvent(EVENT_META, {"turn_id": replay.seq, "client_msg_id": replay.client_msg_id, "attempt": replay.attempt, "replayed": True}),
        SseEvent(EVENT_TOKEN, {"text": replay.story}),
        SseEvent(EVENT_DONE, {
            "status": DONE_ENDED if replay.ended else DONE_COMPLETE, "turn_number": replay.turn_number, "coins": coins,
        }),
    ]


def opening_replay_events(done: OpeningDone, *, coins: int) -> list[SseEvent]:
    """已完成的第 0 幕的重送事件（沒有文字時，例如劇本沒有初始對話的 `fallback`，只有 `meta` 與 `done`）。"""
    events = [SseEvent(EVENT_META, {"kind": "opening", "replayed": True})]
    if done.text:
        events.append(SseEvent(EVENT_TOKEN, {"text": done.text}))
    events.append(SseEvent(EVENT_DONE, {"status": DONE_COMPLETE, "turn_number": None, "coins": coins, "opening_state": done.state}))
    return events
