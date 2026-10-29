"""客服送給模型的上下文，與對話「當前狀態」的計算（見 cs-conversation-memory 規格與 design D9）。

兩者都是**純函式**，不碰資料庫、時鐘與模型，所以能以大量情境直接測試：
- `build_context`：摘要 + 最近 10 個「正常輪」+ 狀態。降級的輪次整輪排除（使用者訊息與降級回覆都不送給模型，
  但仍在使用者可見的歷史中）；沒有回覆的使用者訊息（處理中斷）與正在處理的這一則也不算一輪。
- `next_state` / `initial_state`：狀態只由「已通過驗證的列舉值」與使用者目前的會員等級推得，
  **沒有任何自由文字**（使用者訊息、模型的主題詞、email 都進不來），所以注入內容無法經由狀態留存到下一輪。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from app.models.chat import MSG_STATUS_OK, ROLE_ASSISTANT, ROLE_USER
from app.services.cs_intents import (
    ENTITY_ACCOUNT_ISSUE, ENTITY_ENUMS, ENTITY_FEEDBACK_TYPE, ENTITY_MEMBERSHIP_TIER, INTENT_ACCOUNT_HELP,
    INTENT_COMPLAINT_FEEDBACK, INTENT_GREETING, CsReply,
)

HISTORY_ROUNDS = 10
# 取最近多少則訊息來挑出 10 個正常輪：降級的輪次夾在中間時需要多取一些（取不到 10 輪就用取得的，不會出錯）
HISTORY_FETCH_LIMIT = 120

# 狀態的鍵與可進入 pending_entities 的實體（主題詞是模型寫的自由文字，不進狀態）
STATE_ENTITIES = (ENTITY_MEMBERSHIP_TIER, ENTITY_ACCOUNT_ISSUE, ENTITY_FEEDBACK_TYPE)
_OPEN_ISSUE_ENTITY = {INTENT_ACCOUNT_HELP: ENTITY_ACCOUNT_ISSUE, INTENT_COMPLAINT_FEEDBACK: ENTITY_FEEDBACK_TYPE}


@dataclass(frozen=True)
class ChatContext:
    """送給模型的資料：累積摘要、當前狀態與最近的正常輪（依序的 `(role, text)`）。"""

    summary: str | None
    state: dict[str, Any]
    history: tuple[tuple[str, str], ...]


def initial_state(member_tier: str) -> dict[str, Any]:
    return {"member_tier": member_tier, "last_intent": None, "pending_entities": {}, "open_issue": None}


def normalize_state(stored: dict[str, Any] | None, member_tier: str) -> dict[str, Any]:
    """讀出的狀態整理成固定形狀：只保留已知的鍵；實體只保留列舉內的值。`member_tier` 永遠是呼叫當下的等級。"""
    stored = stored or {}
    pending = stored.get("pending_entities") or {}
    return {
        "member_tier": member_tier,
        "last_intent": stored.get("last_intent"),
        "pending_entities": {
            name: pending[name] for name in STATE_ENTITIES if pending.get(name) in ENTITY_ENUMS[name]
        },
        "open_issue": stored.get("open_issue"),
    }


def next_state(previous: dict[str, Any] | None, reply: CsReply, member_tier: str) -> dict[str, Any]:
    """一輪成功的對話之後的新狀態（降級的輪次不呼叫這個函式，狀態與版本都不變）。

    - `last_intent`：本輪的意圖。
    - `pending_entities`：與前一份合併的非空列舉實體（同鍵覆蓋）；`greeting`（感謝、結束語）時清空。
    - `open_issue`：`account_help`／`complaint_feedback` 時為「<意圖>:<對應的實體或 none>」；`greeting` 時清空；
      其他意圖不改。
    """
    state = normalize_state(previous, member_tier)
    state["last_intent"] = reply.intent
    if reply.intent == INTENT_GREETING:
        state["pending_entities"] = {}
        state["open_issue"] = None
        return state
    for name in STATE_ENTITIES:
        if reply.entities.get(name) is not None:
            state["pending_entities"][name] = reply.entities[name]
    entity = _OPEN_ISSUE_ENTITY.get(reply.intent)
    if entity is not None:
        state["open_issue"] = f"{reply.intent}:{reply.entities.get(entity) or 'none'}"
    return state


def build_context(
    *,
    summary: str | None,
    summary_upto_seq: int,
    stored_state: dict[str, Any] | None,
    member_tier: str,
    messages: Iterable[Any],
    current_client_msg_id: str,
) -> ChatContext:
    """`messages` 是依序號排列的最近訊息（`ChatMessage` 或有相同屬性的物件）。

    一輪 = 序號相鄰的「使用者訊息 + 狀態為 ok 的助理回覆」。已被摘要涵蓋的輪次（助理訊息序號 ≤ 已摘要序號）不重複送出。
    """
    rounds: list[tuple[str, str]] = []
    pending_user = None
    for message in sorted(messages, key=lambda m: m.seq):
        if message.role == ROLE_USER:
            pending_user = message                    # 前一則若沒有回覆就被這一則取代：它不是完整的一輪
            continue
        if message.role == ROLE_ASSISTANT and pending_user is not None:
            counts = (
                message.seq == pending_user.seq + 1
                and message.msg_status == MSG_STATUS_OK
                and message.seq > summary_upto_seq
                and pending_user.client_msg_id != current_client_msg_id
            )
            if counts:
                rounds.append((pending_user.content, message.content))
        pending_user = None
    recent = rounds[-HISTORY_ROUNDS:]
    history = tuple(pair for user_text, reply_text in recent for pair in ((ROLE_USER, user_text), (ROLE_ASSISTANT, reply_text)))
    return ChatContext(summary=summary, state=normalize_state(stored_state, member_tier), history=history)
