"""客服對話與訊息的資料存取（見 cs-conversations、cs-conversation-memory 與 access-control 規格）。

**每個對外方法的第一個參數都是 `user_id`，查詢一律帶 `user_id` 條件**；不提供只憑識別碼取資料的方法給 API 路徑使用
（AGENTS.md 第 10 節）。因此這個類別刻意**不繼承** `BaseRepository`：基底的 `get`、`get_by_public_id`、`list`、`delete`
都不帶擁有者，繼承了就等於留一條能繞過隔離的路。`tests/architecture` 以 AST 檢查所有公開方法都有 `user_id`。

需要原子性的寫入都是「條件式 UPDATE」，以回傳的列數判斷有沒有真的寫入：
取得與清除處理標記、改名、寫入狀態、寫入摘要。條件不符回傳 False，不覆蓋別人的結果。
Repository 不提交也不回復交易（由 Service 決定提交點）；唯一約束被違反時只拋出可辨識的錯誤，
呼叫端須先回復交易再重新讀取。只用 ORM，不寫原生 SQL。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.chat import (
    DEFAULT_CONVERSATION_TITLE, MSG_STATUS_OK, ROLE_USER, ChatConversation, ChatMessage,
)
from app.models.llm_call_log import LlmCallLog
from app.repositories.errors import DuplicateConversationError, DuplicateMessageError


class ChatRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    # ---- 對話 -------------------------------------------------------------------

    def find_conversation_by_client_id(self, user_id: int, client_id: str) -> ChatConversation | None:
        return self._session.scalar(
            select(ChatConversation).where(ChatConversation.user_id == user_id, ChatConversation.client_id == client_id)
        )

    def create_conversation(self, user_id: int, *, client_id: str, state: dict[str, Any], now: datetime) -> ChatConversation:
        """建立對話。同一位使用者的相同 `client_id` 已存在（並行重送）時拋出 `DuplicateConversationError`。"""
        conversation = ChatConversation(
            user_id=user_id, client_id=client_id, title=DEFAULT_CONVERSATION_TITLE, summary_upto_seq=0,
            state_json=dict(state), state_version=1, created_at=now, updated_at=now,
        )
        self._session.add(conversation)
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise DuplicateConversationError from exc
        return conversation

    def get_conversation(self, user_id: int, public_id: str) -> ChatConversation | None:
        """依對外識別碼取得「自己的」對話；別人的與不存在的一樣回傳 None。"""
        return self._session.scalar(
            select(ChatConversation).where(ChatConversation.user_id == user_id, ChatConversation.public_id == public_id)
        )

    def export_conversations(self, user_id: int) -> list[ChatConversation]:
        """匯出個人資料用：這位使用者的**全部**對話（由舊到新）。訊息另以 `messages_after` 分頁讀取，不在這裡一次載入。"""
        return list(
            self._session.scalars(
                select(ChatConversation)
                .where(ChatConversation.user_id == user_id)
                .order_by(ChatConversation.created_at.asc(), ChatConversation.id.asc())
            )
        )

    def list_conversations(self, user_id: int, *, limit: int, offset: int) -> Sequence[ChatConversation]:
        return self._session.scalars(
            select(ChatConversation)
            .where(ChatConversation.user_id == user_id)
            .order_by(ChatConversation.updated_at.desc(), ChatConversation.id.desc())
            .limit(limit)
            .offset(offset)
        ).all()

    def _update_conversation(self, conversation: ChatConversation, statement) -> int:
        """執行條件式 UPDATE；先 flush 以保留尚未寫入的修改，之後讓物件在下次存取時重新讀取。"""
        self._session.flush()
        result = self._session.execute(statement.execution_options(synchronize_session=False))
        self._session.expire(conversation)
        return result.rowcount

    def rename_conversation(self, user_id: int, conversation: ChatConversation, *, title: str, expected_version: int, now: datetime) -> bool:
        """版本相符才改名並使版本號加 1。回傳是否真的寫入（False = 版本已被別的裝置改變）。"""
        return self._update_conversation(
            conversation,
            update(ChatConversation)
            .where(
                ChatConversation.id == conversation.id,
                ChatConversation.user_id == user_id,
                ChatConversation.state_version == expected_version,
            )
            .values(title=title, state_version=ChatConversation.state_version + 1, updated_at=now),
        ) == 1

    def set_title_if_default(self, user_id: int, conversation: ChatConversation, *, title: str, now: datetime) -> bool:
        """標題還是預設的「新對話」才寫入（第一則訊息附加時用），使用者改名後不會被覆寫。"""
        return self._update_conversation(
            conversation,
            update(ChatConversation)
            .where(
                ChatConversation.id == conversation.id,
                ChatConversation.user_id == user_id,
                ChatConversation.title == DEFAULT_CONVERSATION_TITLE,
            )
            .values(title=title, state_version=ChatConversation.state_version + 1, updated_at=now),
        ) == 1

    def delete_conversation(self, user_id: int, conversation: ChatConversation) -> bool:
        """永久刪除對話；訊息靠資料庫的 `ON DELETE CASCADE` 一併移除。

        用量帳本保留（它不含對話內容），但同一交易把它對這個對話的參照清為空值：帳本的 `conversation_id` 沒有外鍵，
        不清掉的話，SQLite 重用編號時會錯指別人的新對話。
        """
        self._session.flush()
        self._session.execute(
            update(LlmCallLog)
            .where(LlmCallLog.user_id == user_id, LlmCallLog.conversation_id == conversation.id)
            .values(conversation_id=None)
            .execution_options(synchronize_session=False)
        )
        result = self._session.execute(
            delete(ChatConversation)
            .where(ChatConversation.id == conversation.id, ChatConversation.user_id == user_id)
            .execution_options(synchronize_session=False)
        )
        self._session.expunge_all()          # 被刪的對話與訊息物件不能再被使用
        return result.rowcount == 1

    # ---- 用量限制的計數（只讀；見 cs-usage-limits 規格）-------------------------------------

    def count_conversations(self, user_id: int) -> int:
        return self._session.scalar(select(func.count(ChatConversation.id)).where(ChatConversation.user_id == user_id)) or 0

    def count_processing(self, user_id: int, *, fresh_since: datetime, exclude_conversation_id: int | None = None) -> int:
        """這位使用者「處理標記仍有效」（不早於 `fresh_since`）的對話數；可排除指定的對話（正要送出的那一個）。

        正在處理的訊息還沒有計入帳本，把它們當成已預留的名額，並行送出就不會一起通過每日額度的檢查。
        """
        statement = select(func.count(ChatConversation.id)).where(
            ChatConversation.user_id == user_id,
            ChatConversation.processing_since.is_not(None),
            ChatConversation.processing_since >= fresh_since,
        )
        if exclude_conversation_id is not None:
            statement = statement.where(ChatConversation.id != exclude_conversation_id)
        return self._session.scalar(statement) or 0

    def recent_user_message_times(self, user_id: int, *, since: datetime, limit: int) -> list[datetime]:
        """這位使用者自 `since` 起「已被接受」的使用者訊息時間（由新到舊，最多 `limit` 筆）。

        使用者訊息在呼叫模型之前就已提交，所以並行連發的請求彼此看得到；被拒絕的請求沒有寫入訊息，不會被算進來。
        第 `limit` 筆加上視窗長度就是最早可再送出的時間。
        """
        return list(self._session.scalars(
            select(ChatMessage.created_at)
            .join(ChatConversation, ChatConversation.id == ChatMessage.conversation_id)
            .where(
                ChatConversation.user_id == user_id,
                ChatMessage.role == ROLE_USER,
                ChatMessage.created_at >= since,
            )
            .order_by(ChatMessage.created_at.desc())
            .limit(limit)
        ).all())

    # ---- 處理標記（同一對話同時只處理一則）-------------------------------------------

    def acquire_processing(self, user_id: int, conversation: ChatConversation, *, now: datetime, stale_before: datetime) -> bool:
        """沒有人在處理，或標記已過期（早於 `stale_before`）時，設定標記並回傳 True。條件式 UPDATE：並發只有一個成功。"""
        return self._update_conversation(
            conversation,
            update(ChatConversation)
            .where(
                ChatConversation.id == conversation.id,
                ChatConversation.user_id == user_id,
                or_(ChatConversation.processing_since.is_(None), ChatConversation.processing_since < stale_before),
            )
            .values(processing_since=now),
        ) == 1

    def release_processing(self, user_id: int, conversation: ChatConversation, *, held_since: datetime) -> bool:
        """只清除「我設定的那一個」標記；標記已被接手者改過就不動它。"""
        return self._update_conversation(
            conversation,
            update(ChatConversation)
            .where(
                ChatConversation.id == conversation.id,
                ChatConversation.user_id == user_id,
                ChatConversation.processing_since == held_since,
            )
            .values(processing_since=None),
        ) == 1

    # ---- 狀態與摘要（版本與序號為條件）------------------------------------------------

    def write_state(self, user_id: int, conversation: ChatConversation, *, state: dict[str, Any], expected_version: int, now: datetime) -> bool:
        return self._update_conversation(
            conversation,
            update(ChatConversation)
            .where(
                ChatConversation.id == conversation.id,
                ChatConversation.user_id == user_id,
                ChatConversation.state_version == expected_version,
            )
            .values(state_json=dict(state), state_version=ChatConversation.state_version + 1, updated_at=now),
        ) == 1

    def write_summary(self, user_id: int, conversation: ChatConversation, *, summary: str, expected_upto_seq: int, new_upto_seq: int) -> bool:
        """已摘要序號仍是 `expected_upto_seq` 才寫入並推進；條件不符（別的請求先完成了）就丟棄，序號不會倒退。"""
        if new_upto_seq <= expected_upto_seq:
            return False
        return self._update_conversation(
            conversation,
            update(ChatConversation)
            .where(
                ChatConversation.id == conversation.id,
                ChatConversation.user_id == user_id,
                ChatConversation.summary_upto_seq == expected_upto_seq,
            )
            .values(summary=summary, summary_upto_seq=new_upto_seq),
        ) == 1

    # ---- 訊息 -------------------------------------------------------------------

    def find_message(self, user_id: int, conversation: ChatConversation, client_msg_id: str) -> ChatMessage | None:
        return self._session.scalar(
            select(ChatMessage)
            .join(ChatConversation, ChatConversation.id == ChatMessage.conversation_id)
            .where(
                ChatConversation.id == conversation.id,
                ChatConversation.user_id == user_id,
                ChatMessage.client_msg_id == client_msg_id,
            )
        )

    def append_message(
        self,
        user_id: int,
        conversation: ChatConversation,
        *,
        client_msg_id: str,
        role: str,
        content: str,
        now: datetime,
        intent: str | None = None,
        entities: dict[str, Any] | None = None,
        suggested_actions: list[dict[str, Any]] | None = None,
        confidence: float | None = None,
        msg_status: str = MSG_STATUS_OK,
    ) -> ChatMessage:
        """附加一則訊息，序號為目前最大值加 1，並把對話的最後活動時間更新為 `now`。

        序號或訊息識別碼撞到唯一約束時拋出 `DuplicateMessageError`；此時 Session 需要回復，且不會留下半套資料。
        """
        owned = self._session.scalar(
            select(ChatConversation.id).where(ChatConversation.id == conversation.id, ChatConversation.user_id == user_id)
        )
        if owned is None:
            raise LookupError("對話不屬於這位使用者")
        last = self._session.scalar(
            select(func.max(ChatMessage.seq)).where(ChatMessage.conversation_id == conversation.id)
        )
        message = ChatMessage(
            conversation_id=conversation.id, client_msg_id=client_msg_id, seq=(last or 0) + 1, role=role, content=content,
            intent=intent, entities_json=entities, suggested_actions_json=suggested_actions, confidence=confidence,
            msg_status=msg_status, created_at=now,
        )
        self._session.add(message)
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise DuplicateMessageError from exc
        self._update_conversation(
            conversation,
            update(ChatConversation).where(ChatConversation.id == conversation.id).values(updated_at=now),
        )
        return message

    def messages_after(self, user_id: int, conversation: ChatConversation, *, after_seq: int, limit: int) -> tuple[list[ChatMessage], bool]:
        """序號大於 `after_seq` 的訊息（由小到大），最多 `limit` 則，並回報是否還有更多。"""
        rows = self._session.scalars(
            select(ChatMessage)
            .join(ChatConversation, ChatConversation.id == ChatMessage.conversation_id)
            .where(
                ChatConversation.id == conversation.id,
                ChatConversation.user_id == user_id,
                ChatMessage.seq > after_seq,
            )
            .order_by(ChatMessage.seq.asc())
            .limit(limit + 1)
        ).all()
        return list(rows[:limit]), len(rows) > limit

    def latest_messages(self, user_id: int, conversation: ChatConversation, *, limit: int) -> list[ChatMessage]:
        """最近的 `limit` 則訊息，依序號由小到大（組上下文用）。"""
        rows = self._session.scalars(
            select(ChatMessage)
            .join(ChatConversation, ChatConversation.id == ChatMessage.conversation_id)
            .where(ChatConversation.id == conversation.id, ChatConversation.user_id == user_id)
            .order_by(ChatMessage.seq.desc())
            .limit(limit)
        ).all()
        return list(reversed(rows))
