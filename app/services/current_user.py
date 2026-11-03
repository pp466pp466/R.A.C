"""已登入使用者的快照：Service 回傳給端點與安全輔助的資料（不是 ORM 物件）。

端點與 `app/security` 不得匯入資料模型，所以需要使用者資料時一律拿到這個物件。
`id` 與 `token_version` 只給伺服器內部使用（寫入登入 cookie、權限判斷），**絕不**出現在對外的回應中。
`birth_date` 同樣只給伺服器內部使用（權限政策與年齡群組由它於當下計算），**絕不**出現在 `public_dict()`。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from app.models.user import (
    DELETION_GRACE_PERIOD, ROLE_ADMIN, STATUS_ACTIVE, STATUS_DELETION_PENDING, User,
)


def iso_utc(moment: datetime) -> str:
    """對外回應的時間格式：UTC、秒、結尾 Z（例如 2026-11-06T08:30:15Z）。"""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# 受限登入狀態（見 access-control 規格「受限登入狀態預設拒絕」）。同時符合多種時，被鎖定優先於待確認年齡，待確認年齡優先於等待刪除。
RESTRICTION_LOCKED = "locked"
RESTRICTION_AGE_PENDING = "age_pending"
RESTRICTION_DELETION_PENDING = "deletion_pending"


@dataclass(frozen=True)
class CurrentUser:
    id: int
    public_id: str
    email: str
    display_name: str
    member_tier: str
    role: str
    status: str
    email_verified: bool
    has_password: bool
    birth_date: date | None        # 內部欄位；None = 尚未完成年齡確認
    token_version: int
    deletion_requested_at: datetime | None = None    # 只有待刪除的帳號有值；用來算出預定刪除的時間
    locked_at: datetime | None = None                # 只有被鎖定的玩家帳號有值（管理員不會被這種鎖定）

    @property
    def age_confirmed(self) -> bool:
        """是否已完成年齡確認（只看生日是否存在；是否成年另由政策於當下計算）。"""
        return self.birth_date is not None

    @classmethod
    def from_model(cls, user: User) -> "CurrentUser":
        # 「完成」要求生日與年齡區間都在；兩者本來就由同一個 UPDATE 寫入，這裡再保守一次
        complete = user.birth_date is not None and user.age_range is not None
        return cls(
            id=user.id,
            public_id=user.public_id,
            email=user.email,
            display_name=user.display_name,
            member_tier=user.member_tier,
            role=user.role,
            status=user.status,
            email_verified=user.email_verified_at is not None,
            has_password=bool(user.password_hash),
            birth_date=user.birth_date if complete else None,
            token_version=user.token_version,
            deletion_requested_at=user.deletion_requested_at,
            locked_at=user.locked_at,
        )

    @property
    def restriction(self) -> str | None:
        """目前的受限登入狀態，沒有受限時為 None。管理員不會被鎖定，也不會是待確認年齡（只是未確認前不能用 AI 功能）。"""
        if self.role != ROLE_ADMIN:
            if self.locked_at is not None:
                return RESTRICTION_LOCKED
            if self.birth_date is None:
                return RESTRICTION_AGE_PENDING
        if self.awaiting_deletion:
            return RESTRICTION_DELETION_PENDING
        return None

    @property
    def needs_password_step(self) -> bool:
        """管理員通過 Google 之後還必須再通過密碼驗證，兩者都通過才建立登入 session（玩家沒有密碼）。"""
        return self.role == ROLE_ADMIN

    @property
    def may_delete_account(self) -> bool:
        """帳號頁是否提供「刪除帳號」：一般會員（管理員不可自助刪除），且帳號狀態允許申請（使用中）。"""
        return self.role != ROLE_ADMIN and self.status == STATUS_ACTIVE

    @property
    def awaiting_deletion(self) -> bool:
        """帳號正在等待刪除（寬限期內）：只持有受限的登入狀態，除了撤銷刪除與登出，其餘功能都不可用。"""
        return self.status == STATUS_DELETION_PENDING

    @property
    def deletion_scheduled_at(self) -> datetime | None:
        """預定刪除的時間（申請時間加寬限期）；只有待刪除的帳號有值。"""
        if not self.awaiting_deletion or self.deletion_requested_at is None:
            return None
        return self.deletion_requested_at + DELETION_GRACE_PERIOD

    def public_dict(self, *, age_group: str | None, google_linked: bool | None = None) -> dict[str, Any]:
        """可以回傳給使用者本人的資料。不含內部編號、密碼雜湊、`token_version`、生日與年齡區間。

        年齡只回「是否已完成」與年齡群組（`minor`、`adult`，尚未完成為 None）；群組必須由呼叫端以時鐘與
        年齡規則算出後傳入（沒有預設值，避免忘了傳而把成年人顯示成未知）。
        """
        data: dict[str, Any] = {
            "public_id": self.public_id,
            "email": self.email,
            "display_name": self.display_name,
            "member_tier": self.member_tier,
            "role": self.role,
            "status": self.status,
            "email_verified": self.email_verified,
            "has_password": self.has_password,
            "age_confirmed": self.age_confirmed,
            "age_group": age_group,
            "restriction": self.restriction,
        }
        if google_linked is not None:
            data["google_linked"] = google_linked
        scheduled = self.deletion_scheduled_at
        if scheduled is not None:
            data["deletion_scheduled_at"] = iso_utc(scheduled)
        return data
