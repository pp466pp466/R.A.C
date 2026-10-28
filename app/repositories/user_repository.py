"""使用者的資料存取（見 AGENTS.md 第 3、4、10 節）。

只用 ORM 表達式。會被並行請求同時修改的欄位（登入失敗計數、`token_version`）一律用單一條件式
UPDATE 完成，不做「讀出、在 Python 加一、寫回」，否則兩個 worker 並行時會互相覆蓋。

這個 Repository 的 `get_*` 方法只用於「使用者本人」的查詢（登入、辨識 session、註冊的重複判斷），
不屬於「只憑 ID 取別人資料」的情境；屬於使用者的其他資料（例如對話）必須另外定義帶 `user_id` 的方法。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.models.user import User
from app.repositories.base import BaseRepository
from app.repositories.errors import DuplicateUserError


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
