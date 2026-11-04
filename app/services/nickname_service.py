"""設定或更改自己的暱稱（見 player-nickname 規格）。

`PUT /api/account/nickname`：已登入、完成年齡確認的帳號（含管理員）設定或更改暱稱。先過帳號與年齡閘門（與其他會員功能同一個
入口，受限登入狀態在更早的登入檢查就被擋下）；格式由 `app/services/nickname.py` 的純函式驗證；第一次設定時產生短代號並保存，
之後更改暱稱不改變短代號。只讀寫呼叫者本人的資料列。事件只記事件名稱與使用者編號，不含暱稱與短代號。
"""
from __future__ import annotations

from app.observability import script_events
from app.repositories.user_repository import UserRepository
from app.services.access_policy import AccessPolicy
from app.services.current_user import CurrentUser
from app.services.errors import Unauthenticated, ValidationError
from app.services.nickname import check_nickname, new_code


class NicknameService:
    def __init__(self, *, users: UserRepository, policy: AccessPolicy) -> None:
        self._users = users
        self._policy = policy

    def set_nickname(self, current: CurrentUser, raw: object) -> CurrentUser:
        """設定或更改暱稱，回傳最新的帳號快照（含暱稱與短代號）。格式不合格時以暱稱欄位的錯誤回應。"""
        self._policy.require_chat_access(current)
        nickname, error = check_nickname(raw)
        if error is not None or nickname is None:
            raise ValidationError(fields={"nickname": error or ""})
        user = self._users.get_by_id(current.id)
        if user is None:
            raise Unauthenticated()
        self._users.set_nickname(user, nickname, new_code())
        script_events.log_nickname_set(user_id=user.id)
        return CurrentUser.from_model(user)
