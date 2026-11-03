"""資料模型與共用欄位慣例。

在這裡匯入全部模型，讓 `Base.metadata` 一定包含所有資料表（Alembic 與測試都依賴它）。
"""
from app.models.admin_audit_log import AdminAuditLog
from app.models.alert_state import AlertState
from app.models.base import Base
from app.models.chat import ChatConversation, ChatMessage
from app.models.deletion_tombstone import DeletionTombstone
from app.models.llm_call_log import LlmCallLog
from app.models.lock_appeal import LockAppeal
from app.models.lock_strike import LockStrike
from app.models.rpg_session import MemoryLog, RpgSession
from app.models.script import Script, ScriptVersion, ScriptVersionTag
from app.models.user import AuthToken, User, UserIdentity

__all__ = [
    "Base", "User", "UserIdentity", "AuthToken", "DeletionTombstone", "ChatConversation", "ChatMessage", "LlmCallLog",
    "AdminAuditLog", "AlertState", "LockStrike", "LockAppeal", "Script", "ScriptVersion", "ScriptVersionTag",
    "RpgSession", "MemoryLog",
]
