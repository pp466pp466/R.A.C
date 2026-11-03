"""使用者的資料存取（見 AGENTS.md 第 3、4、10 節）。

只用 ORM 表達式。會被並行請求同時修改的欄位（登入失敗計數、`token_version`）一律用單一條件式
UPDATE 完成，不做「讀出、在 Python 加一、寫回」，否則兩個 worker 並行時會互相覆蓋。

這個 Repository 的 `get_*` 方法只用於「使用者本人」的查詢（登入、辨識 session、註冊的重複判斷），
不屬於「只憑 ID 取別人資料」的情境；屬於使用者的其他資料（例如對話）必須另外定義帶 `user_id` 的方法。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.models.llm_call_log import LlmCallLog
from app.models.user import (
    DELETION_GRACE_PERIOD, ROLE_ADMIN, STATUS_ACTIVE, STATUS_DELETION_PENDING, User,
)
from app.repositories.base import BaseRepository
from app.repositories.errors import DuplicateUserError

# `replay_status` 的結果（備份還原後重新抹除）
REPLAY_MISSING = "missing"
REPLAY_ADMIN = "admin"
REPLAY_REISSUED = "reissued"
REPLAY_ELIGIBLE = "eligible"


class UserRepository(BaseRepository[User]):
    model = User

    # ---- 查詢（呼叫端須先正規化 email）-----------------------------------------

    def get_by_id(self, user_id: int) -> User | None:
        return self._session.get(User, user_id)

    def get_by_email(self, email: str) -> User | None:
        return self._session.scalar(select(User).where(User.email == email))

    def get_by_canonical_email(self, email_canonical: str) -> User | None:
        return self._session.scalar(select(User).where(User.email_canonical == email_canonical))

    # ---- 新增 -----------------------------------------------------------------

    def add_user(self, user: User) -> User:
        """新增使用者；email 或其正規形式重複時拋出 `DuplicateUserError`。

        在 SAVEPOINT 內寫入：重複只會回復這一筆，目前的交易（以及之前在同一個請求完成的讀寫）仍可繼續使用。
        """
        try:
            with self._session.begin_nested():
                self._session.add(user)
                self._session.flush()
        except IntegrityError as exc:
            raise DuplicateUserError from exc
        return user

    def save(self, user: User) -> None:
        """把目前對使用者物件做的修改寫入資料庫（不提交；由請求結束時統一提交）。"""
        self._session.flush()

    # ---- 原子操作 -------------------------------------------------------------

    def _run_update(self, user: User, statement) -> int:
        """執行條件式 UPDATE。先 flush 以保留尚未寫入的修改，之後讓物件在下次存取時重新讀取。"""
        self._session.flush()
        result = self._session.execute(statement.execution_options(synchronize_session=False))
        self._session.expire(user)
        return result.rowcount

    def increment_failed_login(self, user: User) -> int:
        """登入失敗計數加 1（單一 UPDATE），回傳加完之後的值。並行的失敗不會被低估。"""
        self._run_update(
            user,
            update(User).where(User.id == user.id).values(failed_login_count=User.failed_login_count + 1),
        )
        return self._session.execute(select(User.failed_login_count).where(User.id == user.id)).scalar_one()

    def lock_account(self, user: User, *, locked_until: datetime, now: datetime, min_failures: int) -> bool:
        """失敗次數達到門檻且目前沒有生效中的鎖定時，才設定鎖定；已鎖定的不會被延長。回傳是否真的鎖定。"""
        changed = self._run_update(
            user,
            update(User)
            .where(User.id == user.id)
            .where(User.failed_login_count >= min_failures)
            .where((User.locked_until.is_(None)) | (User.locked_until <= now))
            .values(locked_until=locked_until),
        )
        return changed == 1

    def release_expired_lock(self, user: User, *, now: datetime) -> bool:
        """鎖定已到期時，歸零計數並解除鎖定（開啟新的計數週期）。回傳是否真的解除。"""
        changed = self._run_update(
            user,
            update(User)
            .where(User.id == user.id)
            .where(User.locked_until.is_not(None))
            .where(User.locked_until <= now)
            .values(failed_login_count=0, locked_until=None),
        )
        return changed == 1

    def reset_login_failures(self, user: User) -> None:
        """登入成功：失敗計數歸零並解除鎖定。"""
        self._run_update(user, update(User).where(User.id == user.id).values(failed_login_count=0, locked_until=None))

    def bump_token_version(self, user: User) -> int:
        """`token_version` 加 1（使既有的登入狀態失效），回傳新的值。"""
        self._run_update(user, update(User).where(User.id == user.id).values(token_version=User.token_version + 1))
        return self._session.execute(select(User.token_version).where(User.id == user.id)).scalar_one()

    def complete_age_verification_if_unset(
        self,
        user: User,
        *,
        birth_date: date,
        age_range: str,
        is_adult: bool,
        confirmed_at: datetime,
        policy_version: str,
    ) -> bool:
        """尚未完成年齡確認時，一次寫入全部年齡欄位；已完成的帳號完全不改動。回傳是否真的寫入。

        條件式 UPDATE（`birth_date IS NULL`；「完成」只看生日是否存在，勾選版時代只有 `age_confirmed_at` 的帳號
        視為尚未完成）：並行的兩次補填只有一次會寫入，第一次寫入的內容不會被覆蓋，所以「已完成的帳號再次提交」
        不會改變原本的紀錄。五個欄位在同一個 UPDATE 寫入，不會出現只寫一半的狀態。
        """
        changed = self._run_update(
            user,
            update(User)
            .where(User.id == user.id)
            .where(User.birth_date.is_(None))
            .values(
                birth_date=birth_date,
                age_range=age_range,
                is_adult_at_confirmation=is_adult,
                age_confirmed_at=confirmed_at,
                age_policy_version=policy_version,
                age_deadline_at=None,                    # 完成了，不再有逾時清除的期限
            ),
        )
        return changed == 1

    def count_by_age_range(self) -> dict[str | None, int]:
        """各年齡區間的人數（`GROUP BY`，只回計數）；尚未完成年齡確認的帳號歸在 `None`。

        用於「定期察看用戶群」：回傳內容不含任何個別使用者的資料（沒有生日、email 或編號）。
        """
        rows = self._session.execute(select(User.age_range, func.count(User.id)).group_by(User.age_range)).all()
        return {code: count for code, count in rows}

    # ---- 閒置帳號（account-retention 規格）----------------------------------------

    @staticmethod
    def _last_seen():
        """最後一次成功登入的時間；從未登入過的帳號以建立時間為準。"""
        return func.coalesce(User.last_login_at, User.created_at)

    def count_inactive(self, cutoff: datetime) -> int:
        """最後一次登入早於 `cutoff` 的一般帳號數量（管理員不納入）。"""
        return self._session.execute(
            select(func.count(User.id)).where(User.role != ROLE_ADMIN).where(self._last_seen() < cutoff)
        ).scalar_one()

    def find_inactive_ids(self, cutoff: datetime, *, limit: int = 200) -> list[int]:
        """最後一次登入早於 `cutoff` 的一般帳號的內部編號（由小到大，最多 `limit` 個）。只回編號，不載入任何個資。"""
        statement = (
            select(User.id).where(User.role != ROLE_ADMIN).where(self._last_seen() < cutoff).order_by(User.id).limit(limit)
        )
        return list(self._session.scalars(statement))

    def delete_inactive(self, user_id: int, cutoff: datetime) -> bool:
        """條件式 DELETE：條件在刪除當下再確認一次（剛好登入的帳號不會被刪）。回傳是否真的刪除。

        `user_identities`、`auth_tokens` 靠資料庫的 `ON DELETE CASCADE` 一併移除（SQLite 由連線事件開啟外鍵）。
        不同步 Session 內已載入的物件：呼叫端應使用這個流程專用的 Session，不拿已刪除的使用者物件繼續做事。
        """
        result = self._session.execute(
            delete(User)
            .where(User.id == user_id)
            .where(User.role != ROLE_ADMIN)
            .where(self._last_seen() < cutoff)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    # ---- 備份還原後的重新抹除（deletion-replay 規格）-------------------------------------------

    def replay_status(self, user_id: int, deleted_at: datetime) -> str:
        """這個編號現在對應的帳號，相對於一筆刪除紀錄是什麼狀況（只讀角色與建立時間，不載入任何個資）。

        - `missing`：沒有這個帳號（已經不存在，重複執行時的正常結果）；
        - `admin`：管理員，絕不抹除；
        - `reissued`：帳號建立得比刪除時間晚——還原後新註冊的使用者沿用了先前已刪除帳號的編號，不是同一個人；
        - `eligible`：帳號在刪除時間之前就存在，應該被抹除。
        """
        row = self._session.execute(select(User.role, User.created_at).where(User.id == user_id)).first()
        if row is None:
            return REPLAY_MISSING
        role, created_at = row
        if role == ROLE_ADMIN:
            return REPLAY_ADMIN
        return REPLAY_REISSUED if created_at > deleted_at else REPLAY_ELIGIBLE

    def delete_if_created_not_after(self, user_id: int, deleted_at: datetime) -> bool:
        """條件式 DELETE：只刪「建立時間不晚於 `deleted_at`」且不是管理員的帳號，條件在刪除當下再確認一次。回傳是否真的刪除。

        串聯與不同步 Session 的注意事項同 `delete_inactive`。
        """
        result = self._session.execute(
            delete(User)
            .where(User.id == user_id, User.role != ROLE_ADMIN, User.created_at <= deleted_at)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    def count_locked(self, now: datetime) -> int:
        """目前被鎖定的帳號數；營運告警「帳號鎖定激增」的指標。同一個帳號只算一個。

        兩種鎖定合計：管理員密碼連續失敗的鎖定（`locked_until` 尚未到期，15 分鐘後自動解除），
        與玩家帳號的自動鎖定（`locked_at` 有值，只有管理員能解除）。
        """
        password_locked = and_(User.locked_until.is_not(None), User.locked_until > now)
        return self._session.execute(
            select(func.count(User.id)).where(or_(password_locked, User.locked_at.is_not(None)))
        ).scalar_one()

    # ---- 待確認年齡的帳號（post-login-age-confirmation 與 account-retention 規格）-------------------------

    @staticmethod
    def _overdue_unconfirmed(now: datetime):
        """逾時未補填：玩家、沒有生日、有期限記錄且期限已到。管理員與沒有期限記錄的舊帳號不納入。"""
        return and_(
            User.role != ROLE_ADMIN,
            User.birth_date.is_(None),
            User.age_deadline_at.is_not(None),
            User.age_deadline_at <= now,
        )

    def count_overdue_unconfirmed(self, now: datetime) -> int:
        return self._session.execute(select(func.count(User.id)).where(self._overdue_unconfirmed(now))).scalar_one()

    def find_overdue_unconfirmed_ids(self, now: datetime, *, limit: int = 200) -> list[int]:
        """逾時未補填的玩家帳號的內部編號（由小到大，最多 `limit` 個）。只回編號，不載入任何個資。"""
        statement = select(User.id).where(self._overdue_unconfirmed(now)).order_by(User.id).limit(limit)
        return list(self._session.scalars(statement))

    def delete_overdue_unconfirmed(self, user_id: int, now: datetime) -> bool:
        """條件式 DELETE：條件在刪除當下再確認一次（剛好完成年齡確認的帳號不會被刪）。回傳是否真的刪除。

        串聯與不同步 Session 的注意事項同 `delete_inactive`。
        """
        result = self._session.execute(
            delete(User).where(User.id == user_id).where(self._overdue_unconfirmed(now)).execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    def delete_unconfirmed_player(self, user_id: int) -> bool:
        """補填時發現未滿 13 歲：條件式刪除「還沒有生日的非管理員帳號」。管理員絕不抹除。回傳是否真的刪除。"""
        result = self._session.execute(
            delete(User)
            .where(User.id == user_id, User.role != ROLE_ADMIN, User.birth_date.is_(None))
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    # ---- 玩家帳號鎖定（account-lockout 規格）-------------------------------------------------------

    def lock_player(self, user: User, *, reason: str, now: datetime) -> bool:
        """條件式 UPDATE：只鎖定「角色為玩家且尚未鎖定」的帳號，並行達門檻時只有一個請求回傳真（只記錄一次鎖定）。"""
        changed = self._run_update(
            user,
            update(User)
            .where(User.id == user.id, User.role != ROLE_ADMIN, User.locked_at.is_(None))
            .values(locked_at=now, lock_reason=reason),
        )
        return changed == 1

    def unlock_player(self, user_id: int) -> bool:
        """條件式 UPDATE：解除玩家帳號的鎖定。回傳是否真的解除（本來就沒鎖定則為假）。"""
        self._session.flush()
        result = self._session.execute(
            update(User)
            .where(User.id == user_id, User.locked_at.is_not(None))
            .values(locked_at=None, lock_reason=None)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    # ---- 抹除（account-deletion 與 account-retention 規格；流程見 AccountErasureService）----------

    def clear_ledger_conversation_refs(self, user_id: int) -> int:
        """把這位使用者的用量帳本中的對話編號清為空值（單一 UPDATE），回傳受影響的筆數。

        帳本的 `conversation_id` 沒有外鍵（SQL Server 錯誤 1785），不先清掉的話，SQLite 重用整數編號時會錯指別人的新對話。
        必須在刪除使用者**之前**做：使用者一刪，帳本的 `user_id` 就變成空值，再也找不到要清的列。
        """
        self._session.flush()
        result = self._session.execute(
            update(LlmCallLog)
            .where(LlmCallLog.user_id == user_id, LlmCallLog.conversation_id.is_not(None))
            .values(conversation_id=None)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount

    @staticmethod
    def _pending_due(cutoff: datetime):
        """寬限期已滿的待刪除帳號。申請時間為空（理論上不會發生）一律視為未到期，保守地不自動刪除。"""
        return (
            User.status == STATUS_DELETION_PENDING,
            User.role != ROLE_ADMIN,
            User.deletion_requested_at.is_not(None),
            User.deletion_requested_at <= cutoff,
        )

    def count_pending_due(self, cutoff: datetime) -> int:
        """申請刪除的時間不晚於 `cutoff` 的待刪除帳號數量。"""
        return self._session.execute(select(func.count(User.id)).where(*self._pending_due(cutoff))).scalar_one()

    def find_pending_due_ids(self, cutoff: datetime, *, limit: int = 200) -> list[int]:
        """寬限期已滿的待刪除帳號的內部編號（由小到大，最多 `limit` 個）。只回編號，不載入任何個資。"""
        statement = select(User.id).where(*self._pending_due(cutoff)).order_by(User.id).limit(limit)
        return list(self._session.scalars(statement))

    def delete_if_pending_due(self, user_id: int, cutoff: datetime) -> bool:
        """條件式 DELETE：條件在刪除當下再確認一次（剛撤銷的帳號不會被刪）。回傳是否真的刪除。

        串聯與不同步 Session 的注意事項同 `delete_inactive`。
        """
        result = self._session.execute(
            delete(User).where(User.id == user_id, *self._pending_due(cutoff)).execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    # ---- 自助刪除的申請與撤銷（account-deletion 規格）-----------------------------------------

    def request_deletion(self, user: User, *, now: datetime) -> bool:
        """條件式 UPDATE：非管理員、狀態為 `active` 時，轉為待刪除。回傳是否真的轉換。

        同一個語句記下申請時間與申請前的狀態（SET 右邊讀的是更新前的值），並使 `token_version` 加 1，
        讓所有裝置（含目前這台）的登入狀態失效。兩個並行的申請只有一個會成立，不會加兩次、也不會把申請前的狀態
        覆蓋成 `deletion_pending`。
        """
        changed = self._run_update(
            user,
            update(User)
            .where(User.id == user.id)
            .where(User.role != ROLE_ADMIN)
            .where(User.status == STATUS_ACTIVE)
            .values(
                status_before_deletion=User.status,
                status=STATUS_DELETION_PENDING,
                deletion_requested_at=now,
                token_version=User.token_version + 1,
            ),
        )
        return changed == 1

    def cancel_deletion(self, user: User, *, now: datetime) -> bool:
        """條件式 UPDATE：待刪除且申請未滿寬限期時，還原為申請前的狀態並清除兩個欄位。回傳是否真的還原。

        一律還原為 `active`（申請前的狀態只可能是 `active`；`pending_verification` 已退役，遷移 `0011` 把殘留的改成 `active`）。
        不改變 `token_version`：持有受限登入狀態的人撤銷後，目前的登入狀態直接變成完整的。
        """
        changed = self._run_update(
            user,
            update(User)
            .where(User.id == user.id)
            .where(User.status == STATUS_DELETION_PENDING)
            .where(User.deletion_requested_at.is_not(None))
            .where(User.deletion_requested_at > now - DELETION_GRACE_PERIOD)
            .values(status=STATUS_ACTIVE, deletion_requested_at=None, status_before_deletion=None),
        )
        return changed == 1
