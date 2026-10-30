"""管理員稽核紀錄的資料存取（見 admin-audit-log 規格）。

**只有新增與讀取**：這個類別沒有更新與刪除的方法，Service 與 API 也沒有（測試以反射檢查公開方法）。保存期限屆滿後的清除
由另外的排程指令負責（C-4c），不是這裡。
寫入前一律驗證，不信任呼叫端：
- 動作必須在固定的列舉內；
- 原因至多 200 字；
- 細節只接受「像列舉值的短字串」與整數：不接受巢狀結構與帶空白或標點的字串，所以訊息文字、email 與姓名放不進來。
Repository 不提交也不回復交易（由請求統一提交；`reauth.failed` 另由 Service 以 `keep_changes_on_error` 保存）。只用 ORM。

這個類別只被 `AdminService` 與 `registry` 使用（見 tests/architecture/test_admin_boundaries.py）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.admin_audit_log import AUDIT_ACTIONS, MAX_REASON_CHARS, AdminAuditLog
from app.models.user import User

_DETAIL_TOKEN = re.compile(r"[A-Za-z0-9_.-]{1,40}")
_DETAIL_KEY = re.compile(r"[a-z][a-z0-9_]{0,39}")


@dataclass(frozen=True)
class AuditEntry:
    """給後台檢視的一筆紀錄：管理員只有顯示名稱（已被刪除為 None），沒有 email 與內部編號。"""

    id: int
    created_at: datetime
    action: str
    admin_display_name: str | None
    target_user_public_id: str | None
    target_conversation_public_id: str | None
    reason: str | None
    detail: dict[str, Any]


def _check_detail(detail: Mapping[str, Any] | None) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in (detail or {}).items():
        if not isinstance(key, str) or not _DETAIL_KEY.fullmatch(key):
            raise ValueError(f"稽核細節的欄位名稱不合法：{key!r}")
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError(f"稽核細節 {key} 只接受字串或整數")
        if isinstance(value, str) and not _DETAIL_TOKEN.fullmatch(value):
            raise ValueError(f"稽核細節 {key} 必須是像列舉值的短字串（不含空白與標點）")
        clean[key] = value
    return clean


class AdminAuditRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self,
        *,
        admin_user_id: int | None,
        action: str,
        now: datetime,
        target_user_public_id: str | None = None,
        target_conversation_public_id: str | None = None,
        reason: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> AdminAuditLog:
        """新增一筆並 flush（讓寫入失敗立刻浮現）。動作不在列舉內、原因過長或細節不合法時拋出 `ValueError`，不寫入任何東西。"""
        if action not in AUDIT_ACTIONS:
            raise ValueError(f"未知的稽核動作：{action!r}")
        if reason is not None and len(reason) > MAX_REASON_CHARS:
            raise ValueError(f"原因最多 {MAX_REASON_CHARS} 字")
        row = AdminAuditLog(
            admin_user_id=admin_user_id, action=action, target_user_public_id=target_user_public_id,
            target_conversation_public_id=target_conversation_public_id, reason=reason, detail_json=_check_detail(detail),
            created_at=now,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def list(self, *, limit: int, offset: int) -> list[AuditEntry]:
        """由新到舊（同一時間以編號大者在前）。管理員以顯示名稱呈現；帳號已被刪除時為 None。"""
        rows = self._session.execute(
            select(AdminAuditLog, User.display_name)
            .outerjoin(User, User.id == AdminAuditLog.admin_user_id)
            .order_by(AdminAuditLog.created_at.desc(), AdminAuditLog.id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        return [
            AuditEntry(
                id=row.id, created_at=row.created_at, action=row.action, admin_display_name=name,
                target_user_public_id=row.target_user_public_id,
                target_conversation_public_id=row.target_conversation_public_id, reason=row.reason,
                detail=dict(row.detail_json or {}),
            )
            for row, name in rows
        ]
