"""客服對話的業務流程（見 cs-conversations、cs-conversation-memory 規格與 design D7、D8、D9、D11）。

重點：
- 每個方法都以呼叫者（`CurrentUser`）為擁有者條件；非擁有者與不存在一律 `NotFound`。
- 會寫入或呼叫模型的操作（建立、改名、送出訊息）先過 `AccessPolicy.require_chat_access`；讀取與刪除自己的對話只需登入與擁有者。
- 建立對話與送出新訊息再過 `UsageService` 的用量檢查（對話數上限；暫停、每分鐘、每日輪數、全站用量）：拒絕發生在寫入與取得
  處理標記之前，所以不留任何痕跡；已有回覆的重送在檢查之前就回傳，不受限制也不重複計入。
- 送出訊息的提交點（D8）：使用者訊息與處理標記先提交，**呼叫模型時沒有資料庫交易**（最長 30 秒），
  再把助理訊息、狀態、最後一筆帳本與清除標記在同一個交易提交。任何例外都會回復並清除標記後才往外傳。
- 重送是冪等的：以（對話、訊息識別碼）去重，已有回覆就直接回傳，不呼叫模型；使用者訊息已存在但沒有回覆（上次中斷）只補一次呼叫。
- 回覆只含文字與建議動作；意圖、實體、信心與摘要不離開伺服器。
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from app.models.chat import (
    MSG_STATUS_FALLBACK, MSG_STATUS_OK, ROLE_ASSISTANT, ROLE_USER, ChatConversation, ChatMessage,
)
from app.models.llm_call_log import KIND_CHAT
from app.observability.errors import describe_exception
from app.observability.llm_events import log_reply
from app.repositories.chat_repository import ChatRepository
from app.repositories.errors import DuplicateConversationError, DuplicateMessageError
from app.services.access_policy import AccessPolicy
from app.services.cs_context import HISTORY_FETCH_LIMIT, build_context, initial_state, next_state, normalize_state
from app.services.cs_intents import RESPONSE_SCHEMA, CsReply, semantic_rules, to_reply
from app.services.cs_prompt import build_system_instruction, build_user_contents, degraded_text
from app.services.current_user import CurrentUser
from app.services.errors import (
    ConversationBusy, MessageIdReused, NotFound, ServiceUnavailable, ValidationError, VersionConflict,
)
from app.services.input_cleaning import clean_message, clean_single_line
from app.services.llm_service import DEGRADED_BLOCKED, LlmService
from app.services.usage_service import UsageService

logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 1000
MAX_TITLE_CHARS = 100
DEFAULT_TITLE_CHARS = 30
PROCESSING_STALE_AFTER = timedelta(seconds=60)
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
MAX_MESSAGES_PAGE = 200

# 助理訊息的識別碼由使用者訊息的識別碼「決定性地」推得：唯一約束因此同時保證一則使用者訊息至多一則助理回覆
_ASSISTANT_NAMESPACE = uuid.UUID("5c7d2a54-1f0e-4c1a-9a53-0d3d6f6b7a11")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def assistant_message_id(user_client_msg_id: str) -> str:
    return str(uuid.uuid5(_ASSISTANT_NAMESPACE, user_client_msg_id))


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class ConversationView:
    """對外的對話資訊：不含內部編號、摘要與狀態。"""

    id: str
    title: str
    state_version: int
    updated_at: datetime
    created_at: datetime

    @classmethod
    def from_model(cls, conversation: ChatConversation) -> "ConversationView":
        return cls(
            id=conversation.public_id, title=conversation.title, state_version=conversation.state_version,
            updated_at=conversation.updated_at, created_at=conversation.created_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "state_version": self.state_version,
            "updated_at": _iso(self.updated_at), "created_at": _iso(self.created_at),
        }


@dataclass(frozen=True)
class MessageView:
    """對外的訊息：只有文字與（助理訊息的）建議動作；意圖、實體與信心不在其中。"""

    seq: int
    client_msg_id: str
    role: str
    content: str
    msg_status: str
    suggested_actions: tuple[dict[str, Any], ...]
    created_at: datetime

    @classmethod
    def from_model(cls, message: ChatMessage) -> "MessageView":
        actions = tuple(message.suggested_actions_json or ()) if message.role == ROLE_ASSISTANT else ()
        return cls(
            seq=message.seq, client_msg_id=message.client_msg_id, role=message.role, content=message.content,
            msg_status=message.msg_status, suggested_actions=actions, created_at=message.created_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq, "client_msg_id": self.client_msg_id, "role": self.role, "content": self.content,
            "msg_status": self.msg_status, "suggested_actions": [dict(a) for a in self.suggested_actions],
            "created_at": _iso(self.created_at),
        }


@dataclass(frozen=True)
class SendResult:
    user_message: MessageView
    assistant_message: MessageView
    conversation: ConversationView


def _uuid_field(value: object, field: str) -> str:
    text = value.strip().lower() if isinstance(value, str) else ""
    if not _UUID.fullmatch(text):
        raise ValidationError(fields={field: "識別碼格式不正確。"})
    return text


def _default_title(content: str) -> str:
    return " ".join(content.split())[:DEFAULT_TITLE_CHARS]


class ChatService:
    def __init__(
        self,
        *,
        chats: ChatRepository,
        llm: LlmService | None,
        policy: AccessPolicy,
        usage: UsageService,
        clock: Callable[[], datetime],
        commit: Callable[[], None],
        rollback: Callable[[], None],
        marker: str,
        after_reply: Callable[[int, str], None] | None = None,
    ) -> None:
        self._chats = chats
        self._llm = llm
        self._policy = policy
        self._usage = usage
        self._clock = clock
        self._commit = commit
        self._rollback = rollback
        self._marker = marker
        self._after_reply = after_reply

    # ---- 對話 -------------------------------------------------------------------------

    def _owned(self, user: CurrentUser, public_id: str) -> ChatConversation:
        conversation = self._chats.get_conversation(user.id, public_id) if isinstance(public_id, str) else None
        if conversation is None:
            raise NotFound()
        return conversation

    def create_conversation(self, user: CurrentUser, client_id: object) -> tuple[ConversationView, bool]:
        """建立對話；同一位使用者以同一識別碼重送回傳同一個。回傳（對話, 是否新建）。"""
        self._policy.require_chat_access(user)
        key = _uuid_field(client_id, "client_id")
        existing = self._chats.find_conversation_by_client_id(user.id, key)
        if existing is not None:
            return ConversationView.from_model(existing), False
        self._usage.check_create_conversation(user)           # 冪等重送在上面就回傳了，不受對話數上限影響
        try:
            conversation = self._chats.create_conversation(
                user.id, client_id=key, state=initial_state(user.member_tier), now=self._clock()
            )
        except DuplicateConversationError:                  # 並行的重送先建好了：回復後讀取它
            self._rollback()
            existing = self._chats.find_conversation_by_client_id(user.id, key)
            if existing is None:
                raise
            return ConversationView.from_model(existing), False
        return ConversationView.from_model(conversation), True

    def list_conversations(self, user: CurrentUser, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0) -> list[ConversationView]:
        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValidationError(fields={"limit": f"每頁筆數須為 1 至 {MAX_PAGE_SIZE}。"})
        if offset < 0:
            raise ValidationError(fields={"offset": "起始位置不可為負數。"})
        return [ConversationView.from_model(c) for c in self._chats.list_conversations(user.id, limit=limit, offset=offset)]

    def get_conversation(self, user: CurrentUser, public_id: str) -> ConversationView:
        return ConversationView.from_model(self._owned(user, public_id))

    def rename_conversation(self, user: CurrentUser, public_id: str, title: object, state_version: object) -> ConversationView:
        self._policy.require_chat_access(user)
        cleaned = clean_single_line(title) if isinstance(title, str) else None
        if not cleaned or len(cleaned) > MAX_TITLE_CHARS:
            raise ValidationError(fields={"title": f"標題須為 1 至 {MAX_TITLE_CHARS} 字。"})
        if not isinstance(state_version, int) or isinstance(state_version, bool) or state_version < 1:
            raise ValidationError(fields={"state_version": "版本號格式不正確。"})
        conversation = self._owned(user, public_id)
        if not self._chats.rename_conversation(
            user.id, conversation, title=cleaned, expected_version=state_version, now=self._clock()
        ):
            raise VersionConflict(ConversationView.from_model(conversation).to_dict())     # 物件已過期，會讀到最新的內容
        return ConversationView.from_model(conversation)

    def delete_conversation(self, user: CurrentUser, public_id: str) -> None:
        """永久刪除自己的對話（不需要閘門：停權前已有的歷史仍可刪除）。"""
        conversation = self._owned(user, public_id)
        if not self._chats.delete_conversation(user.id, conversation):
            raise NotFound()

    # ---- 讀取訊息 ---------------------------------------------------------------------

    def list_messages(
        self, user: CurrentUser, public_id: str, *, since_seq: int = 0, limit: int = MAX_MESSAGES_PAGE
    ) -> tuple[list[MessageView], bool]:
        if since_seq < 0:
            raise ValidationError(fields={"since_seq": "序號不可為負數。"})
        if not 1 <= limit <= MAX_MESSAGES_PAGE:
            raise ValidationError(fields={"limit": f"每次筆數須為 1 至 {MAX_MESSAGES_PAGE}。"})
        conversation = self._owned(user, public_id)
        messages, has_more = self._chats.messages_after(user.id, conversation, after_seq=since_seq, limit=limit)
        return [MessageView.from_model(m) for m in messages], has_more

    # ---- 送出訊息 ---------------------------------------------------------------------

    def _clean_content(self, content: object) -> str:
        cleaned = clean_message(content) if isinstance(content, str) else None
        if cleaned is None:
            raise ValidationError(fields={"content": "訊息內容含有無法處理的字元。"})
        if not cleaned:
            raise ValidationError(fields={"content": "請輸入訊息內容。"})
        if len(cleaned) > MAX_MESSAGE_CHARS:
            raise ValidationError(fields={"content": f"訊息最多 {MAX_MESSAGE_CHARS} 字。"})
        return cleaned

    def send_message(self, user: CurrentUser, public_id: str, client_msg_id: object, content: object) -> SendResult:
        self._policy.require_chat_access(user)
        if self._llm is None:
            raise ServiceUnavailable()                       # 沒有模型供應商：什麼都不寫
        message_id = _uuid_field(client_msg_id, "client_msg_id")
        text = self._clean_content(content)
        conversation = self._owned(user, public_id)

        existing = self._chats.find_message(user.id, conversation, message_id)
        if existing is not None:
            if existing.role != ROLE_USER or existing.content != text:
                raise MessageIdReused()
            reply = self._chats.find_message(user.id, conversation, assistant_message_id(message_id))
            if reply is not None:                            # 已處理過：回傳既有的回覆，不呼叫模型
                return self._result(existing, reply, conversation)

        # 用量限制：在取得處理標記之前檢查，拒絕時不寫入訊息、不設標記、不呼叫模型、不新增帳本。
        # 已儲存但沒有回覆的訊息（上次中斷）已被接受過一次，不再受每分鐘限制。
        self._usage.check_send(user, conversation_id=conversation.id, already_accepted=existing is not None)

        held_since = self._clock()
        if not self._chats.acquire_processing(
            user.id, conversation, now=held_since, stale_before=held_since - PROCESSING_STALE_AFTER
        ):
            raise ConversationBusy()
        try:
            return self._process(user, conversation, message_id, text, existing, held_since)
        except BaseException:
            self._abandon(user, conversation, held_since)
            raise

    def _abandon(self, user: CurrentUser, conversation: ChatConversation, held_since: datetime) -> None:
        """處理失敗：回復未提交的變更，再以新的交易清除自己的處理標記（使用者可以立刻重送）。"""
        try:
            self._rollback()
            self._chats.release_processing(user.id, conversation, held_since=held_since)
            self._commit()
        except Exception as exc:  # noqa: BLE001 - 清理失敗不得蓋掉原本的例外；標記 60 秒後自然過期
            logger.error("清除處理標記失敗", extra=describe_exception(exc))

    def _process(
        self,
        user: CurrentUser,
        conversation: ChatConversation,
        message_id: str,
        text: str,
        existing: ChatMessage | None,
        held_since: datetime,
    ) -> SendResult:
        now = held_since
        if existing is None:
            try:
                user_message = self._chats.append_message(
                    user.id, conversation, client_msg_id=message_id, role=ROLE_USER, content=text, now=now
                )
            except DuplicateMessageError:
                raise ConversationBusy() from None
            if user_message.seq == 1:
                self._chats.set_title_if_default(user.id, conversation, title=_default_title(text), now=now)
        else:
            user_message = existing
        self._commit()                                       # 使用者訊息與處理標記落地；之後到呼叫結束前不持有交易

        context = build_context(
            summary=conversation.summary, summary_upto_seq=conversation.summary_upto_seq,
            stored_state=conversation.state_json, member_tier=user.member_tier,
            messages=self._chats.latest_messages(user.id, conversation, limit=HISTORY_FETCH_LIMIT),
            current_client_msg_id=message_id,
        )
        contents = build_user_contents(
            summary=context.summary, state=context.state, history=context.history, message=text
        )
        outcome = self._llm.run_structured(
            kind=KIND_CHAT, user_id=user.id, conversation_id=conversation.id,
            system_instruction=build_system_instruction(self._marker), contents=contents, schema=RESPONSE_SCHEMA,
            rules=semantic_rules(self._marker),
        )

        reply = to_reply(outcome.value) if outcome.ok else None
        try:
            assistant_message = self._write_reply(user, conversation, message_id, outcome.degraded, reply)
        except DuplicateMessageError:
            # 另一個 worker 在標記過期後接手並先寫入了同一則回覆：以它為準（我們這次的帳本與寫入一併丟棄）
            self._rollback()
            assistant_message = self._chats.find_message(user.id, conversation, assistant_message_id(message_id))
            if assistant_message is None:
                raise ConversationBusy() from None
            self._chats.release_processing(user.id, conversation, held_since=held_since)
            self._commit()
            return self._result(user_message, assistant_message, conversation)
        except LookupError:                                  # 處理期間對話被刪除
            raise NotFound() from None

        if reply is not None:
            self._advance_state(user, conversation, reply)
        self._chats.release_processing(user.id, conversation, held_since=held_since)
        self._commit()
        log_reply(
            user_id=user.id, conversation_id=conversation.id, intent=reply.intent if reply else None,
            outcome=MSG_STATUS_OK if reply else MSG_STATUS_FALLBACK,
        )
        if outcome.degraded == DEGRADED_BLOCKED:             # 被安全過濾擋下的那一列帳本已隨上面的提交落地，才能算出是否達到暫停門檻
            self._usage.record_block_outcome(user)
        if self._after_reply is not None and reply is not None:
            try:
                self._after_reply(user.id, conversation.public_id)
            except Exception as exc:  # noqa: BLE001 - 登記摘要失敗不影響已提交的回覆；下一次請求會補做
                logger.error("登記摘要工作失敗", extra=describe_exception(exc))
        return self._result(user_message, assistant_message, conversation)

    def _write_reply(
        self, user: CurrentUser, conversation: ChatConversation, message_id: str, degraded: str | None, reply: CsReply | None
    ) -> ChatMessage:
        client_msg_id = assistant_message_id(message_id)
        now = self._clock()
        if reply is None:                                    # 降級：固定文案，標記為降級，不改狀態
            return self._chats.append_message(
                user.id, conversation, client_msg_id=client_msg_id, role=ROLE_ASSISTANT, content=degraded_text(degraded),
                msg_status=MSG_STATUS_FALLBACK, suggested_actions=[], now=now,
            )
        return self._chats.append_message(
            user.id, conversation, client_msg_id=client_msg_id, role=ROLE_ASSISTANT, content=reply.reply_text,
            intent=reply.intent, entities=dict(reply.entities), suggested_actions=[dict(a) for a in reply.suggested_actions],
            confidence=reply.confidence, msg_status=MSG_STATUS_OK, now=now,
        )

    def _advance_state(self, user: CurrentUser, conversation: ChatConversation, reply: CsReply) -> None:
        """寫入新狀態並使版本加 1（內容沒有改變就不寫）。版本被別的裝置的改名搶先時，以最新的版本重算一次。"""
        for _ in range(2):
            previous = normalize_state(conversation.state_json, user.member_tier)
            new_state = next_state(conversation.state_json, reply, user.member_tier)
            if new_state == previous and conversation.state_json == previous:
                return
            if self._chats.write_state(
                user.id, conversation, state=new_state, expected_version=conversation.state_version, now=self._clock()
            ):
                return

    def _result(self, user_message: ChatMessage, assistant_message: ChatMessage, conversation: ChatConversation) -> SendResult:
        return SendResult(
            MessageView.from_model(user_message), MessageView.from_model(assistant_message),
            ConversationView.from_model(conversation),
        )
