"""後台專用的資料存取（見 admin-console 規格與 design D2、D3、D7 至 D12）。

這個類別**刻意**不遵守 `ChatRepository` 的「第一個參數必須是 `user_id`」規則：後台本來就是在讀寫他人的資料。
它的保護來自另一個方向：
- 只被 `AdminService` 與 `registry` 匯入（tests/architecture/test_admin_boundaries.py）；
- `AdminService` 的每個公開方法第一件事是 `AccessPolicy.require_admin`，並為每個讀取與改變寫入稽核；
- 一般使用者的 Repository（`ChatRepository`、`UserRepository`）完全沒有改動，它們的隔離保證不受影響。
會被並行請求同時修改的欄位（狀態、等級、`token_version`）一律用單一條件式 UPDATE，以 rowcount 判斷有沒有真的寫入，
兩位管理員同時操作也不會互相覆蓋。Repository 不提交也不回復交易；只用 ORM，不寫原生 SQL。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import and_, case, delete, func, or_, select, true, update
from sqlalchemy.orm import Session

from app.models.chat import ROLE_ASSISTANT, ChatConversation, ChatMessage
from app.models.llm_call_log import KIND_CHAT, STATUS_BLOCKED, STATUS_VALIDATION_FAILED, LlmCallLog
from app.models.lock_appeal import LockAppeal
from app.models.script_submission import SUBMISSION_LIFTED
from app.models.user import (
    MEMBER_TIERS, STATUS_ACTIVE, STATUS_SUSPENDED, User,
)
from app.repositories.lock_appeal_repository import LockAppealRepository
from app.repositories.lock_strike_repository import LockStrikeRepository
from app.repositories.script_submission_repository import ScriptSubmissionRepository
from app.repositories.user_repository import UserRepository


@dataclass(frozen=True)
class DayUsage:
    """一個台灣日的全站用量（來自帳本）。"""

    tokens: int          # 輸入、輸出與思考 token 的總和（空值以 0 計）
    calls: int           # 模型呼叫次數（含摘要、降級與失敗的嘗試）
    turns: int           # 計入額度的對話輪數
    blocked_calls: int   # 對話呼叫被內容安全過濾擋下的次數


@dataclass(frozen=True)
class FeedbackRow:
    """意見回饋清單的一列：只有識別碼、數量與時間，沒有任何訊息內容。"""

    conversation_public_id: str
    user_public_id: str
    count: int
    latest_at: datetime


@dataclass(frozen=True)
class FlaggedRow:
    """被標記清單的一列。`user_id`（內部編號）只供 Service 查詢該使用者今日的警告，**不得**送出給前端。"""

    conversation_public_id: str
    user_id: int
    user_public_id: str
    blocked_count: int
    latest_at: datetime


@dataclass(frozen=True)
class ReviewableConversation:
    """一個「可被管理員讀取」的對話：屬於意見回饋、被標記，或兩者。`user_id` 與 `id` 是內部編號，不外露。"""

    id: int
    public_id: str
    user_id: int
    user_public_id: str
    is_feedback: bool
    is_flagged: bool


@dataclass(frozen=True)
class MessageRow:
    seq: int
    role: str
    content: str
    msg_status: str
    created_at: datetime


@dataclass(frozen=True)
class SuspendedRow:
    """被暫停發佈帳號的一列：只有對外識別碼、顯示名稱與暫停時間，沒有 email、暱稱或內部編號。"""

    user_public_id: str
    display_name: str
    suspended_at: datetime


@dataclass(frozen=True)
class LockedRow:
    """被鎖定帳號的一列：只有對外識別碼、顯示名稱、鎖定時間與原因類別（固定列舉），沒有 email 或內部編號。"""

    user_public_id: str
    display_name: str
    locked_at: datetime
    lock_reason: str | None


@dataclass(frozen=True)
class AppealRow:
    """被鎖定或被暫停發佈的帳號送出的申訴。內容由使用者撰寫，只在管理員檢視時出現，不進日誌與稽核。

    `restriction`：`locked`（帳號目前被鎖定，優先）或 `suspended`（目前被暫停發佈），讓管理員知道該用哪一種解除。"""

    appeal_public_id: str
    user_public_id: str
    display_name: str
    content: str
    created_at: datetime
    handled_at: datetime | None
    restriction: str


@dataclass(frozen=True)
class FailureRow:
    """模型輸出驗證失敗的原文（僅管理員可讀）；沒有使用者與對話的任何識別。"""

    created_at: datetime
    kind: str
    model: str
    error_detail: str


def _aware(moment: datetime) -> datetime:
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


class AdminRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    # ---- 使用者：查詢 --------------------------------------------------------------------

    def find_user_by_canonical_email(self, email_canonical: str) -> User | None:
        """以「規範形式」的信箱精確比對（呼叫端先以註冊相同的正規化處理；沒有模糊搜尋，也沒有列出全部使用者的方法）。"""
        return self._session.scalar(select(User).where(User.email_canonical == email_canonical))

    def find_user_by_public_id(self, public_id: str) -> User | None:
        return self._session.scalar(select(User).where(User.public_id == public_id))

    # ---- 帳號鎖定：解除 -------------------------------------------------------------------

    def unlock_account(self, user: User, *, now: datetime) -> int | None:
        """解除玩家帳號的鎖定：條件式 UPDATE（兩位管理員同時解除只有一位成功），成功時一併清除該帳號的計次、
        把待處理的申訴標為已處理。回傳被標為已處理的申訴數；帳號已不是鎖定狀態時回傳 None（什麼也沒改）。"""
        if not UserRepository(self._session).unlock_player(user.id):
            return None
        LockStrikeRepository(self._session).clear_for_user(user.id)
        return LockAppealRepository(self._session).mark_pending_handled(user.id, now)

    # ---- 彙總（只有計數，沒有任何個別使用者的資料）------------------------------------------------

    def count_users_by_status(self) -> dict[str, int]:
        return {status: count for status, count in self._session.execute(select(User.status, func.count(User.id)).group_by(User.status))}

    def count_users_by_tier(self) -> dict[str, int]:
        return {tier: count for tier, count in self._session.execute(select(User.member_tier, func.count(User.id)).group_by(User.member_tier))}

    def usage_for_day(self, *, start: datetime, end: datetime) -> DayUsage:
        """某個台灣日（`start` 含、`end` 不含，帶時區的 UTC）的全站用量；一次查詢用條件式加總取得四個數字。"""
        tokens = (
            func.coalesce(LlmCallLog.prompt_tokens, 0)
            + func.coalesce(LlmCallLog.completion_tokens, 0)
            + func.coalesce(LlmCallLog.thinking_tokens, 0)
        )
        turns = case((and_(LlmCallLog.kind == KIND_CHAT, LlmCallLog.counted == true()), 1), else_=0)
        blocked = case((and_(LlmCallLog.kind == KIND_CHAT, LlmCallLog.status == STATUS_BLOCKED), 1), else_=0)
        total_tokens, calls, total_turns, total_blocked = self._session.execute(
            select(func.sum(tokens), func.count(LlmCallLog.id), func.sum(turns), func.sum(blocked))
            .where(LlmCallLog.created_at >= start, LlmCallLog.created_at < end)
        ).one()
        return DayUsage(tokens=int(total_tokens or 0), calls=int(calls or 0), turns=int(total_turns or 0), blocked_calls=int(total_blocked or 0))

    def blocked_by_user(self, *, start: datetime, end: datetime, user_ids: Sequence[int] | None = None) -> dict[int, tuple[int, datetime]]:
        """期間內對話呼叫被安全過濾擋下的使用者：{使用者的內部編號: (次數, 最近一次的時間)}。

        摘要呼叫被擋不算；帳號已被刪除（`user_id` 為空）的列不在其中。給了 `user_ids` 就只查這些人（被標記清單的一頁）。
        """
        statement = (
            select(LlmCallLog.user_id, func.count(LlmCallLog.id), func.max(LlmCallLog.created_at))
            .where(
                LlmCallLog.kind == KIND_CHAT, LlmCallLog.status == STATUS_BLOCKED, LlmCallLog.user_id.is_not(None),
                LlmCallLog.created_at >= start, LlmCallLog.created_at < end,
            )
            .group_by(LlmCallLog.user_id)
        )
        if user_ids is not None:
            if not user_ids:
                return {}
            statement = statement.where(LlmCallLog.user_id.in_(list(user_ids)))
        result: dict[int, tuple[int, datetime]] = {}
        for user_id, count, latest in self._session.execute(statement):
            result[user_id] = (count, latest.replace(tzinfo=timezone.utc) if latest.tzinfo is None else latest)
        return result

    # ---- 對話：兩類可被讀取的清單與資格 -----------------------------------------------------------

    def feedback_conversations(self, *, intents: Sequence[str], limit: int, offset: int) -> list[FeedbackRow]:
        """至少有一則助理回覆的意圖在 `intents` 內的對話，依最近一則的時間由新到舊。只有中繼資料。"""
        rows = self._session.execute(
            select(ChatConversation.public_id, User.public_id, func.count(ChatMessage.id), func.max(ChatMessage.created_at))
            .join(ChatConversation, ChatConversation.id == ChatMessage.conversation_id)
            .join(User, User.id == ChatConversation.user_id)
            .where(ChatMessage.role == ROLE_ASSISTANT, ChatMessage.intent.in_(list(intents)))
            .group_by(ChatConversation.id, ChatConversation.public_id, User.public_id)
            .order_by(func.max(ChatMessage.created_at).desc(), ChatConversation.id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        return [FeedbackRow(conv, user, count, _aware(latest)) for conv, user, count, latest in rows]

    def flagged_conversations(self, *, limit: int, offset: int) -> list[FlaggedRow]:
        """帳本中有「對話呼叫被安全過濾擋下」紀錄、且對話仍存在的對話，依最近一次被擋的時間由新到舊。只有中繼資料。

        帳本的 `conversation_id` 沒有外鍵：對話被刪除時由刪除流程清為空值，所以刪掉的對話自然離開清單。
        """
        rows = self._session.execute(
            select(
                ChatConversation.public_id, ChatConversation.user_id, User.public_id,
                func.count(LlmCallLog.id), func.max(LlmCallLog.created_at),
            )
            .join(ChatConversation, ChatConversation.id == LlmCallLog.conversation_id)
            .join(User, User.id == ChatConversation.user_id)
            .where(LlmCallLog.kind == KIND_CHAT, LlmCallLog.status == STATUS_BLOCKED, LlmCallLog.conversation_id.is_not(None))
            .group_by(ChatConversation.id, ChatConversation.public_id, ChatConversation.user_id, User.public_id)
            .order_by(func.max(LlmCallLog.created_at).desc(), ChatConversation.id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        return [FlaggedRow(conv, user_id, user, count, _aware(latest)) for conv, user_id, user, count, latest in rows]

    def find_reviewable(self, public_id: str, *, intents: Sequence[str]) -> ReviewableConversation | None:
        """對話屬於意見回饋或被標記時回傳它，否則（含不存在）回傳 None。**讀取與刪除的資格只有這一處定義**，
        與兩個清單的條件一致，所以「清單裡有的才讀得到」不會有第二套規則。"""
        found = self._session.execute(
            select(ChatConversation.id, ChatConversation.user_id, User.public_id)
            .join(User, User.id == ChatConversation.user_id)
            .where(ChatConversation.public_id == public_id)
        ).first()
        if found is None:
            return None
        conversation_id, user_id, user_public_id = found
        # 「有沒有至少一筆」用取一筆的查詢表達，不用 `SELECT EXISTS(...)`：SQL Server 不接受把 EXISTS 當值回傳
        is_feedback = self._session.scalar(
            select(ChatMessage.id)
            .where(ChatMessage.conversation_id == conversation_id, ChatMessage.role == ROLE_ASSISTANT, ChatMessage.intent.in_(list(intents)))
            .limit(1)
        ) is not None
        is_flagged = self._session.scalar(
            select(LlmCallLog.id)
            .where(LlmCallLog.conversation_id == conversation_id, LlmCallLog.kind == KIND_CHAT, LlmCallLog.status == STATUS_BLOCKED)
            .limit(1)
        ) is not None
        if not (is_feedback or is_flagged):
            return None
        return ReviewableConversation(conversation_id, public_id, user_id, user_public_id, is_feedback, is_flagged)

    def read_messages(self, conversation_id: int, *, limit: int) -> tuple[list[MessageRow], bool]:
        """依序號由小到大的訊息，最多 `limit` 則，並回報是否還有更多。呼叫端須先以 `find_reviewable` 確認資格。"""
        rows = self._session.scalars(
            select(ChatMessage).where(ChatMessage.conversation_id == conversation_id).order_by(ChatMessage.seq.asc()).limit(limit + 1)
        ).all()
        return (
            [MessageRow(m.seq, m.role, m.content, m.msg_status, _aware(m.created_at)) for m in rows[:limit]],
            len(rows) > limit,
        )

    def delete_conversation(self, conversation_id: int) -> bool:
        """永久刪除對話；訊息靠資料庫的 `ON DELETE CASCADE` 一併移除。

        帳本保留（用量與成本稽核不能因為刪內容而少），但同一交易把它對這個對話的參照清為空值：帳本的 `conversation_id`
        沒有外鍵，不清掉的話，SQLite 重用編號時會錯指別人的新對話，被刪的對話也會留在被標記清單裡。
        """
        self._session.flush()
        self._session.execute(
            update(LlmCallLog).where(LlmCallLog.conversation_id == conversation_id).values(conversation_id=None)
            .execution_options(synchronize_session=False)
        )
        result = self._session.execute(
            delete(ChatConversation).where(ChatConversation.id == conversation_id).execution_options(synchronize_session=False)
        )
        self._session.expunge_all()          # 被刪的對話與訊息物件不能再被使用
        return result.rowcount == 1

    # ---- 模型失敗的原文 --------------------------------------------------------------------

    def recent_failures(self, *, since: datetime, limit: int) -> list[FailureRow]:
        """自 `since` 起「輸出驗證失敗且有保存原文」的帳本列，由新到舊。期限由查詢本身限制，不依賴清除排程。"""
        rows = self._session.scalars(
            select(LlmCallLog)
            .where(
                LlmCallLog.status == STATUS_VALIDATION_FAILED, LlmCallLog.error_detail.is_not(None), LlmCallLog.created_at >= since
            )
            .order_by(LlmCallLog.created_at.desc(), LlmCallLog.id.desc())
            .limit(limit)
        ).all()
        return [FailureRow(_aware(r.created_at), r.kind, r.model, r.error_detail or "") for r in rows]

    # ---- 使用者：條件式改變 ----------------------------------------------------------------

    # ---- 帳號鎖定與申訴（account-lockout、lockout-appeals 規格）--------------------------------------

    def locked_accounts(self, *, limit: int, offset: int) -> list[LockedRow]:
        """目前被鎖定的玩家帳號（由最近鎖定的開始）。"""
        rows = self._session.execute(
            select(User.public_id, User.display_name, User.locked_at, User.lock_reason)
            .where(User.locked_at.is_not(None))
            .order_by(User.locked_at.desc(), User.id.desc())
            .limit(limit).offset(offset)
        )
        return [LockedRow(public_id, name, locked_at, reason) for public_id, name, locked_at, reason in rows]

    def publish_suspended_accounts(self, *, limit: int, offset: int) -> list[SuspendedRow]:
        """目前被暫停發佈的玩家帳號（由最近暫停的開始）。"""
        rows = self._session.execute(
            select(User.public_id, User.display_name, User.publish_suspended_at)
            .where(User.publish_suspended_at.is_not(None))
            .order_by(User.publish_suspended_at.desc(), User.id.desc())
            .limit(limit).offset(offset)
        )
        return [SuspendedRow(public_id, name, suspended_at) for public_id, name, suspended_at in rows]

    def lift_publish_suspension(self, user: User, *, now: datetime) -> int | None:
        """解除暫停發佈：條件式 UPDATE（兩位管理員同時解除只有一位成功），成功時補一筆 `lifted` 送審紀錄（使連續被拒中斷，所以
        解除後再被拒絕一次不會立刻又被暫停）、把待處理的申訴標為已處理。回傳被標為已處理的申訴數；帳號已不是暫停狀態時回傳 None。"""
        if not UserRepository(self._session).lift_publish_suspension(user):
            return None
        ScriptSubmissionRepository(self._session).record(user.id, SUBMISSION_LIFTED, (), now)
        return LockAppealRepository(self._session).mark_pending_handled(user.id, now)

    def appeals_of_restricted_accounts(self, *, limit: int, offset: int) -> list[AppealRow]:
        """只回傳「目前被鎖定」或「目前被暫停發佈」的帳號所送出的申訴；兩種狀態都解除之後它的申訴不再出現（資料仍隨帳號保存）。"""
        rows = self._session.execute(
            select(
                LockAppeal.public_id, User.public_id, User.display_name, LockAppeal.content, LockAppeal.created_at, LockAppeal.handled_at,
                User.locked_at,
            )
            .join(User, User.id == LockAppeal.user_id)
            .where(or_(User.locked_at.is_not(None), User.publish_suspended_at.is_not(None)))
            .order_by(LockAppeal.created_at.desc(), LockAppeal.id.desc())
            .limit(limit).offset(offset)
        )
        return [AppealRow(*row[:6], restriction="locked" if row[6] is not None else "suspended") for row in rows]

    def _update_user(self, user: User, statement) -> bool:
        """先 flush 以保留尚未寫入的修改，執行條件式 UPDATE，讓物件在下次存取時重新讀取；回傳是否真的寫入。"""
        self._session.flush()
        result = self._session.execute(statement.execution_options(synchronize_session=False))
        self._session.expire(user)
        return result.rowcount == 1

    def suspend_user(self, user: User, *, allowed_from: tuple[str, ...]) -> bool:
        """目前狀態在 `allowed_from` 內才改為 `suspended`，並使 `token_version` 加 1（既有的登入狀態立即失效）。"""
        return self._update_user(
            user,
            update(User)
            .where(User.id == user.id, User.status.in_(allowed_from))
            .values(status=STATUS_SUSPENDED, token_version=User.token_version + 1),
        )

    def unsuspend_user(self, user: User) -> bool:
        """只有 `suspended` 才能解除，結果一律是 `active`。"""
        return self._update_user(
            user, update(User).where(User.id == user.id, User.status == STATUS_SUSPENDED).values(status=STATUS_ACTIVE)
        )

    def change_tier(self, user: User, *, tier: str) -> bool:
        """等級與目前不同才寫入（相同回傳 False）。"""
        if tier not in MEMBER_TIERS:
            raise ValueError(f"會員等級不合法：{tier!r}")
        return self._update_user(
            user, update(User).where(User.id == user.id, User.member_tier != tier).values(member_tier=tier)
        )
