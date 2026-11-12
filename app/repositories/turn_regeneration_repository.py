"""重新生成與編輯的回饋資料存取（見 rpg-turn-revision 的 design D7、AGENTS.md 第 7、11 節）。

只用 ORM 表達式，不提交交易（請求結束時統一提交）。

**擁有者隔離**：所有方法的第一個參數是 `user_id`，先以 `RpgSession.user_id == user_id` 找到存檔；別人的存檔與不存在的存檔得到相同的結果
（`None`，匯出則不產生任何東西）。沒有只憑識別碼取資料的方法，**也沒有任何管理員讀取路徑**（管理員的回饋檢視頁不在 rpg-turn-revision 範圍）。

**生命週期**：認領修訂的同一個 SAVEPOINT 內由 `add_pending` 插入一列（`pending`）；收尾時 `mark_result` 轉為 `applied`、`failed` 或
`blocked`——只有 `pending` 能轉出，所以結果一旦寫下就不會被晚到的執行者改寫；修訂逾時被新的請求接手時，`fail_pending_for_turn` 先把同一回合
仍是 `pending` 的舊列標為 `failed`（理由仍保留）。**不保存被取代的玩家行動與劇情**（資料最小化）。

**個資**：分類與理由是玩家文字，視為個資。這裡不記錄任何日誌；列隨存檔與帳號串聯刪除；匯出不含內部編號。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterator

from sqlalchemy import select, update

from app.models.rpg_session import (
    REGEN_APPLIED, REGEN_BLOCKED, REGEN_CATEGORIES, REGEN_FAILED, REGEN_KINDS, REGEN_PENDING, RpgSession, TurnRegeneration,
)
from app.repositories.base import BaseRepository

# 收尾時可以寫入的結果（`pending` 只能是插入時的初始值，不能被寫回）
FINAL_RESULTS = (REGEN_APPLIED, REGEN_FAILED, REGEN_BLOCKED)


class TurnRegenerationRepository(BaseRepository[TurnRegeneration]):
    model = TurnRegeneration

    def add_pending(
        self, user_id: int, public_id: str, *, turn_seq: int, attempt: int, kind: str, category: str | None, reason: str | None,
        is_free: bool,
    ) -> TurnRegeneration | None:
        """認領的同一個交易內插入一列回饋（結果為 `pending`）。不是自己的存檔回傳 `None`。

        `kind` 與 `category` 的值域在這裡再擋一次（`ValueError`）；長度與內容的檢查是 Service 的責任。`(turn_seq, attempt)` 在同一存檔內唯一：
        重複時資料庫的唯一約束擋下（`IntegrityError`，這一列的插入在自己的 SAVEPOINT 內復原，呼叫端的交易仍可使用）。"""
        if kind not in REGEN_KINDS:
            raise ValueError("kind 必須是 " + "、".join(REGEN_KINDS) + " 之一。")
        if category is not None and category not in REGEN_CATEGORIES:
            raise ValueError("category 必須是五個分類之一或空值。")
        session_id = self._owned_session_id(user_id, public_id)
        if session_id is None:
            return None
        row = TurnRegeneration(
            session_id=session_id, turn_seq=turn_seq, attempt=attempt, kind=kind, category=category, reason=reason,
            is_free=bool(is_free), result=REGEN_PENDING,
        )
        with self._session.begin_nested():
            self._session.add(row)
            self._session.flush()
        return row

    def mark_result(self, user_id: int, public_id: str, *, turn_seq: int, attempt: int, result: str, now: datetime | None = None) -> bool | None:
        """把這次嘗試的結果寫成 `applied`、`failed` 或 `blocked`。條件式 UPDATE：只有仍是 `pending` 的列會被改，所以已經有結果的列
        （例如被接手時標為 `failed` 之後，舊執行者才晚到的收尾）不會被改寫。成功回傳 `True`；沒有符合的列（不存在或已有結果）回傳 `False`；
        不是自己的存檔回傳 `None`。`result` 不是三個收尾結果之一拋出 `ValueError`。"""
        if result not in FINAL_RESULTS:
            raise ValueError("result 必須是 " + "、".join(FINAL_RESULTS) + " 之一。")
        session_id = self._owned_session_id(user_id, public_id)
        if session_id is None:
            return None
        values: dict[str, Any] = {"result": result}
        if now is not None:
            values["updated_at"] = now
        outcome = self._session.execute(
            update(TurnRegeneration).where(
                TurnRegeneration.session_id == session_id, TurnRegeneration.turn_seq == turn_seq, TurnRegeneration.attempt == attempt,
                TurnRegeneration.result == REGEN_PENDING,
            ).values(**values).execution_options(synchronize_session=False)
        )
        self._session.expire_all()                      # 已載入的列不留著舊的結果
        return outcome.rowcount == 1

    def fail_pending_for_turn(self, user_id: int, public_id: str, *, turn_seq: int, now: datetime | None = None) -> int | None:
        """接手逾時的認領時，先把同一回合仍是 `pending` 的舊列標為 `failed`（理由與分類保留）。回傳被標記的列數；不是自己的存檔回傳 `None`。
        要在插入新的 `pending` 之前呼叫，這樣新的一列不會被一起標記。"""
        session_id = self._owned_session_id(user_id, public_id)
        if session_id is None:
            return None
        values: dict[str, Any] = {"result": REGEN_FAILED}
        if now is not None:
            values["updated_at"] = now
        outcome = self._session.execute(
            update(TurnRegeneration).where(
                TurnRegeneration.session_id == session_id, TurnRegeneration.turn_seq == turn_seq, TurnRegeneration.result == REGEN_PENDING,
            ).values(**values).execution_options(synchronize_session=False)
        )
        self._session.expire_all()
        return outcome.rowcount

    def export_for_session(self, user_id: int, public_id: str, page_size: int = 500) -> Iterator[dict[str, Any]]:
        """一個存檔的全部回饋（含失敗與被擋的），依建立順序逐頁產生（以列的編號為游標）。不是自己的存檔不產生任何東西。

        含回合序號、嘗試序號、種類、分類、理由、是否免費、結果與時間。**不含**內部編號（列編號、存檔編號）。"""
        session_id = self._owned_session_id(user_id, public_id)
        if session_id is None:
            return
        after = 0
        while True:
            rows = list(self._session.scalars(
                select(TurnRegeneration).where(TurnRegeneration.session_id == session_id, TurnRegeneration.id > after)
                .order_by(TurnRegeneration.id).limit(page_size)
            ))
            if not rows:
                return
            for row in rows:
                yield {
                    "turn_seq": row.turn_seq, "attempt": row.attempt, "kind": row.kind, "category": row.category, "reason": row.reason,
                    "is_free": row.is_free, "result": row.result, "created_at": row.created_at, "updated_at": row.updated_at,
                }
            after = rows[-1].id

    # ---- 內部 ---------------------------------------------------------------------------

    def _owned_session_id(self, user_id: int, public_id: str) -> int | None:
        return self._session.scalar(select(RpgSession.id).where(RpgSession.user_id == user_id, RpgSession.public_id == public_id))
