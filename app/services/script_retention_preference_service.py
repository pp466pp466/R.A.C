"""玩家自己的「匿名保留我已發佈的劇本」偏好：查詢與更新（見 script-anonymous-retention 規格、AGENTS.md 第 11 節）。

- 偏好是布林 `keep`，預設關閉（既有帳號視為關閉）。它只在帳號被抹除的當下被讀取（見 `AccountErasureService`）；
  這裡只負責讓使用者在帳號頁讀寫，與 `StoryPreferenceService` 同型：只讀寫呼叫者本人的資料列，請求中沒有任何可指定他人的欄位。
- 輸入必須是**真正的布林**（不容忍 `"true"`、`1`、`null`），否則 400 並指出 `keep` 欄位，偏好不變。更新為冪等。
- 回應除了 `keep`，還有 `live_count`（目前上架的版本數）：帳號頁與刪除視窗據此決定要不要顯示「匿名保留」的說明與核取方塊。
  只有兩個整數與布林，不含任何劇本文字、標題或識別碼。
- 受限登入狀態（鎖定、等待刪除、尚未完成年齡確認）由端點的 `require_login()` 一律拒絕；管理員不能自助刪除帳號，
  所以這個偏好對管理員沒有意義，也一律拒絕（`Forbidden`），不論讀寫。
- 不記日誌事件：偏好的值不屬於可記錄的欄位，而它被採用的時刻（抹除）已有 `account.scripts_retained` 事件（只有編號與筆數）。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.models.user import ROLE_ADMIN
from app.repositories.script_repository import ScriptRepository
from app.repositories.user_repository import UserRepository
from app.services.current_user import CurrentUser
from app.services.errors import Forbidden, Unauthenticated, ValidationError

KEEP_INVALID_MESSAGE = "請以「開」或「關」指定是否匿名保留已發佈的劇本。"
ADMIN_FORBIDDEN_MESSAGE = "管理員帳號無法自行刪除，所以沒有這個設定。"


@dataclass(frozen=True)
class ScriptRetentionState:
    """帳號頁需要的兩個值：偏好與目前上架的版本數。"""

    keep: bool
    live_count: int

    def as_dict(self) -> dict:
        return {"keep": self.keep, "live_count": self.live_count}


class ScriptRetentionPreferenceService:
    def __init__(self, *, users: UserRepository, scripts: ScriptRepository) -> None:
        self._users = users
        self._scripts = scripts

    def get(self, current: CurrentUser) -> ScriptRetentionState:
        """目前的偏好與上架版本數。從資料庫讀取（不用登入快照），缺值視為關閉。"""
        user = self._require_player(current)
        return self._state(user.id, bool(user.keep_published_scripts))

    def set(self, current: CurrentUser, value: object) -> ScriptRetentionState:
        """把偏好設為 `value`，回傳更新後的狀態。不合法的值不改動任何東西。"""
        user = self._require_player(current)
        if not isinstance(value, bool):
            raise ValidationError(fields={"keep": KEEP_INVALID_MESSAGE})
        if not self._users.set_keep_published_scripts(user, value):
            raise Unauthenticated()                  # 帳號已不存在
        return self._state(user.id, value)

    # ---- 內部 --------------------------------------------------------------------

    def _state(self, user_id: int, keep: bool) -> ScriptRetentionState:
        return ScriptRetentionState(keep=keep, live_count=self._scripts.count_live_owned(user_id))

    def _require_player(self, current: CurrentUser):
        user = self._users.get_by_id(current.id)
        if user is None:
            raise Unauthenticated()
        if user.role == ROLE_ADMIN:
            raise Forbidden(ADMIN_FORBIDDEN_MESSAGE, code="admin_script_retention_forbidden")
        return user
