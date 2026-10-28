"""第三方登入身分的資料存取。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.user import UserIdentity
from app.repositories.base import BaseRepository
from app.repositories.errors import DuplicateIdentityError


class UserIdentityRepository(BaseRepository[UserIdentity]):
    model = UserIdentity

    def get_by_subject(self, provider: str, provider_subject: str) -> UserIdentity | None:
        statement = select(UserIdentity).where(
            UserIdentity.provider == provider, UserIdentity.provider_subject == provider_subject
        )
        return self._session.scalar(statement)

    def get_for_user(self, user_id: int, provider: str) -> UserIdentity | None:
        statement = select(UserIdentity).where(UserIdentity.user_id == user_id, UserIdentity.provider == provider)
        return self._session.scalar(statement)

    def add_identity(self, identity: UserIdentity) -> UserIdentity:
        """綁定身分；同一個身分已被綁定、或該帳號已有同供應商的身分時拋出 `DuplicateIdentityError`。"""
        try:
            with self._session.begin_nested():
                self._session.add(identity)
                self._session.flush()
        except IntegrityError as exc:
            raise DuplicateIdentityError from exc
        return identity
