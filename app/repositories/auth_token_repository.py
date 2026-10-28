"""信箱驗證與重設密碼權杖的資料存取。

只保存權杖的 SHA-256 雜湊。「是否可以使用」的判定與「標記為已使用」是同一個條件式 UPDATE，
因此並行的兩次使用最多只有一次成功，不需要鎖。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update

from app.models.user import AuthToken
from app.repositories.base import BaseRepository


class AuthTokenRepository(BaseRepository[AuthToken]):
    model = AuthToken

    def add_token(
        self, *, user_id: int, purpose: str, token_hash: str, created_at: datetime, expires_at: datetime
    ) -> AuthToken:
        return self.add(
            AuthToken(
                user_id=user_id, purpose=purpose, token_hash=token_hash, created_at=created_at, expires_at=expires_at
            )
        )

    def invalidate_unused(self, *, user_id: int, purpose: str, now: datetime) -> int:
        """把這位使用者這個用途所有尚未使用的權杖標記為已使用（發放新權杖時取代舊的）。回傳筆數。"""
        self._session.flush()
        result = self._session.execute(
            update(AuthToken)
            .where(AuthToken.user_id == user_id, AuthToken.purpose == purpose, AuthToken.used_at.is_(None))
            .values(used_at=now)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount

    def consume(self, *, token_hash: str, purpose: str, now: datetime) -> int | None:
        """原子地使用權杖：未使用、用途相符且尚未過期才成功，成功時回傳該權杖所屬的 user_id，否則回傳 None。

        恰好在到期時間的那一刻視為已過期。
        """
        self._session.flush()
        result = self._session.execute(
            update(AuthToken)
            .where(
                AuthToken.token_hash == token_hash,
                AuthToken.purpose == purpose,
                AuthToken.used_at.is_(None),
                AuthToken.expires_at > now,
            )
            .values(used_at=now)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return None
        return self._session.execute(select(AuthToken.user_id).where(AuthToken.token_hash == token_hash)).scalar_one()

    def latest_created_at(self, *, user_id: int, purpose: str) -> datetime | None:
        """這位使用者這個用途最近一次發放的時間（寄信頻率限制用）。"""
        statement = select(func.max(AuthToken.created_at)).where(
            AuthToken.user_id == user_id, AuthToken.purpose == purpose
        )
        return self._session.execute(statement).scalar_one()

    def count_created_since(self, *, user_id: int, purpose: str, since: datetime) -> int:
        """自某個時間點起，這位使用者這個用途發放了幾筆（含已被取代的）。"""
        statement = select(func.count(AuthToken.id)).where(
            AuthToken.user_id == user_id, AuthToken.purpose == purpose, AuthToken.created_at > since
        )
        return self._session.execute(statement).scalar_one()
